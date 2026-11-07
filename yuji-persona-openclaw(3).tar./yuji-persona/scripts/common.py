"""Shared, offline primitives. Corpus text is always data, never executable code."""
from __future__ import annotations
import contextlib
import errno
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
MAX_BYTES = 40 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
WINDOWS_RESERVED_IDS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
SCENES = ("general", "daily", "comfort", "conflict", "repair", "invitation", "refusal", "formal", "humor")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def require(condition, message):
    if not condition:
        raise ValueError(message)

def identifier(value):
    require(isinstance(value, str) and bool(ID_RE.fullmatch(value)), "标识只能包含字母、数字、下划线和连字符，长度 1–96")
    require(value.upper() not in WINDOWS_RESERVED_IDS, "标识不能使用 Windows 保留设备名，如 CON、NUL、COM1")
    return value

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()

def read_json(path):
    p = Path(path)
    require(p.stat().st_size <= MAX_BYTES, "文件超过 40 MiB，请先拆分")
    return json.loads(p.read_text(encoding="utf-8-sig"))

def read_jsonl(path):
    p = Path(path)
    require(p.stat().st_size <= MAX_BYTES, "文件超过 40 MiB，请先拆分")
    result = []
    for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{p.name}:{i} JSON 格式错误") from e
        require(isinstance(row, dict), f"{p.name}:{i} 应为 JSON 对象")
        result.append(row)
    return result

def atomic_text(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".yuji-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

def write_jsonl(path, rows):
    atomic_text(path, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))

@contextlib.contextmanager
def _windows_lock(f, api, timeout=30.0, poll_interval=.05):
    """Lock byte zero with the Windows CRT; the API is injectable for backend tests."""
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write(b"\0")
        f.flush()
    deadline = time.monotonic() + timeout
    retry_errnos = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    while True:
        f.seek(0)
        try:
            api.locking(f.fileno(), api.LK_NBLCK, 1)
            break
        except OSError as exc:
            if exc.errno not in retry_errnos and getattr(exc, "winerror", None) not in (33, 36):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("等待会话文件锁超时；稍后重试，勿覆盖其他进程的写入") from exc
            time.sleep(poll_interval)
    try:
        yield
    finally:
        f.seek(0)
        api.locking(f.fileno(), api.LK_UNLCK, 1)

@contextlib.contextmanager
def lock(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            with _windows_lock(f, msvcrt):
                yield
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

def tokens(text):
    text = unicodedata.normalize("NFKC", text).lower()
    words = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words

def emoji_count(text):
    return sum(1 for c in text if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF)

def quantile(values, q):
    if not values:
        return 0
    a = sorted(values)
    pos = (len(a) - 1) * q
    lo = int(pos)
    return round(a[lo] + (a[min(lo + 1, len(a) - 1)] - a[lo]) * (pos - lo), 3)

def features(text):
    length = max(1, len(text))
    return {
        "chars": len(text),
        "question_per_100_chars": 100 * (text.count("?") + text.count("？")) / length,
        "exclamation_per_100_chars": 100 * (text.count("!") + text.count("！")) / length,
        "emoji_per_100_chars": 100 * emoji_count(text) / length,
        "linebreaks": text.count("\n"),
        "ends_with_period": float(text.rstrip().endswith(("。", "."))),
        "has_list": float(bool(re.search(r"(?m)^\s*(?:[-*]|\d+[.)、])\s*\S", text))),
    }

def stats(rows):
    require(all(isinstance(x.get("text"), str) for x in rows), "统计输入缺少 text")
    feats = [features(r["text"]) for r in rows]
    lengths = [x["chars"] for x in feats]
    joined = "".join(r["text"] for r in rows)
    ngrams = Counter(t for r in rows for t in set(tokens(r["text"])))
    return {
        "messages": len(rows),
        "characters": sum(lengths),
        "length": {"p10": quantile(lengths, .1), "median": quantile(lengths, .5), "p90": quantile(lengths, .9)},
        "mean_features": {k: round(statistics.mean(x[k] for x in feats), 4) for k in features("")} if feats else {},
        "scene_counts": dict(Counter(r.get("scene", "general") for r in rows)),
        "relationship_counts": dict(Counter(r.get("relationship", "unknown") for r in rows)),
        "punctuation": dict(Counter(c for c in joined if c in "，。！？、；：…~～!?.,;:")),
        "candidate_ngrams": [{"text": k, "message_count": v} for k, v in ngrams.most_common(25) if v >= 2],
        "note": "字符二元组只用于检索/候选线索，不等于词语、性格或稳定口头禅；未测量真人心理。",
    }

def corpus_info(directory):
    p = Path(directory)
    meta = read_json(p / "manifest.json")
    require(meta.get("schema_version") == VERSION, "不支持的 corpus 版本")
    rows = read_jsonl(p / "train.jsonl")
    require(digest(rows) == meta["train_hash"], "训练语料校验失败：请重建 corpus，勿手动改写")
    require(all(r.get("speaker") == meta["target"] for r in rows), "训练语料混入其他作者")
    return meta, rows

def print_result(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))

def run_cli(fn):
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        fn()
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
