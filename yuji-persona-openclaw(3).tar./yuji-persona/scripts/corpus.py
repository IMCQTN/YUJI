"""Normalize attributed corpora, split conversations, and retrieve evidence offline."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from common import *

ALIASES = {"speaker": ("speaker", "author", "sender", "name", "role"), "text": ("text", "content", "message")}

def field(row, key):
    for name in ALIASES.get(key, (key,)):
        if name in row:
            return row[name]
    return None

def parse_file(path, target, single_author=False):
    p = Path(path)
    require(p.is_file(), f"找不到语料：{p}")
    require(p.stat().st_size <= MAX_BYTES, "单个语料超过 40 MiB，请拆分")
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        rows = read_jsonl(p)
    elif suffix == ".json":
        data = read_json(p)
        rows = data if isinstance(data, list) else data.get("messages", data.get("records"))
        require(isinstance(rows, list), "JSON 需要消息数组，或 messages/records 数组")
    elif suffix in (".csv", ".tsv"):
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t" if suffix == ".tsv" else ","))
    elif suffix in (".txt", ".md"):
        raw = p.read_text(encoding="utf-8-sig")
        if single_author:
            rows = [{"speaker": target, "text": t.strip()}
                    for i, t in enumerate(re.split(r"\n\s*\n", raw)) if t.strip()]
        else:
            rows, conv = [], None
            # Explicit speaker labels only. Timestamps may precede a label in square brackets.
            pattern = re.compile(r"^(?:\[([^\]]+)\]\s*)?([^:：\n]{1,64})\s*[:：]\s*(.*)$")
            for line in raw.splitlines():
                if line.startswith("=== ") and line.endswith(" ==="):
                    conv = line[4:-4].strip()
                    continue
                match = pattern.match(line)
                if match:
                    stamp, speaker, text = match.groups()
                    rows.append({"speaker": speaker, "text": text, "timestamp": stamp, "conversation_id": conv})
                elif line.strip():
                    require(bool(rows), "无作者标签：单一作者文本请明确使用 --single-author；多人记录请添加姓名标签")
                    rows[-1]["text"] += "\n" + line
    else:
        raise ValueError("仅直接接收 txt/md/json/jsonl/csv/tsv；其他格式请先抽取文本")
    normalized = []
    for i, row in enumerate(rows, 1):
        require(isinstance(row, dict), f"第 {i} 条不是对象")
        speaker, text = field(row, "speaker"), field(row, "text")
        if speaker is None and single_author:
            speaker = target
        require(isinstance(speaker, str) and speaker.strip(), f"第 {i} 条缺少说话人")
        require(isinstance(text, str), f"第 {i} 条缺少字符串 text/content")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        require(len(text) <= 100000, f"第 {i} 条超过 100000 字符，请按自然段拆分")
        raw_conv = row.get("conversation_id") or row.get("session_id")
        conv = str(raw_conv or "default")
        record = {"speaker": speaker.strip(), "text": text,
                  "conversation_id": conv, "timestamp": row.get("timestamp") or None,
                  "scene": str(row.get("scene") or "general"),
                  "relationship": str(row.get("relationship") or "unknown"),
                  "source": {"file": p.name, "row": i, "source_id": str(row.get("id") or i)}}
        # Include the source filename and original row to preserve distinct, repeated utterances.
        if raw_conv is None or raw_conv == "":
            record["implicit_conversation"] = True
        record["id"] = "m-" + digest(record)[:18]
        normalized.append(record)
    return normalized

def make_corpus(inputs, target, out, seed="yuji-v1", holdout=.2, single_author=False, persona_id=None):
    require(isinstance(target, str) and target.strip(), "需要明确目标说话人")
    require(0 <= holdout <= .5, "holdout 范围为 0–0.5")
    out = Path(out)
    require(not (out / "manifest.json").exists(), "输出目录已有 corpus；增量重建请使用新目录")
    resolved = [Path(p).resolve() for p in inputs]
    require(len(resolved) == len(set(resolved)), "同一输入文件重复传入")
    rows, source_digests, warnings = [], [], []
    require(len({p.name for p in resolved}) == len(resolved), "输入文件同名会使来源不清，请先重命名")
    for p in resolved:
        batch = parse_file(p, target, single_author)
        rows.extend(batch)
        source_digests.append({"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    require(bool(rows), "没有可用消息")
    # Drop only identical source records; preserve natural repeated short answers and their contexts.
    unique, seen = [], set()
    for row in rows:
        if row["id"] not in seen:
            unique.append(row)
            seen.add(row["id"])
    groups = defaultdict(list)
    for row in unique:
        # Explicit conversation_id identifies the same conversation even across input files.
        key = ("implicit", row["source"]["file"]) if row.get("implicit_conversation") else ("explicit", row["conversation_id"])
        groups[key].append(row)
    examples, group_ranks = [], {}
    for group_key, messages in groups.items():
        group_id = "g-" + digest(group_key)[:24]
        # Keep prior seed ordering for explicit groups; namespaces themselves cannot collide.
        group_ranks[group_id] = group_key[1] if group_key[0] == "explicit" else group_key[1] + ":default"
        for idx, row in enumerate(messages):
            if row["speaker"] != target:
                continue
            example = dict(row)
            example["group_id"] = group_id
            example["context"] = [{k: x[k] for k in ("id", "speaker", "text", "timestamp")}
                                  for x in messages[max(0, idx - 4):idx]]
            examples.append(example)
    require(bool(examples), f"没有找到目标 {target!r}；可用说话人：{sorted({r['speaker'] for r in unique})}")
    active_groups = sorted({r["group_id"] for r in examples}, key=lambda k: (digest([seed, group_ranks[k]]), k))
    n_holdout = max(1, round(len(active_groups) * holdout)) if holdout and len(active_groups) >= 3 else 0
    hold_groups = set(active_groups[:n_holdout])
    train = [r for r in examples if r["group_id"] not in hold_groups]
    heldout = [r for r in examples if r["group_id"] in hold_groups]
    # Remove overlapping answers and target context from evaluation; do not leak adjacent targets.
    train_texts = {normalize(r["text"]) for r in train}
    train_target_ids = {r["id"] for r in train}
    hold_clean = []
    excluded = 0
    for row in heldout:
        duplicate = normalize(row["text"]) in train_texts
        # Conservative near-duplicate screening for long messages, not short common replies.
        a = set(tokens(row["text"]))
        if len(row["text"]) >= 24 and a:
            duplicate = duplicate or any(len(r["text"]) >= 24 and
                len(a & set(tokens(r["text"]))) / max(1, len(a | set(tokens(r["text"])))) >= .9 for r in train)
        if duplicate or any(x["id"] in train_target_ids for x in row["context"]):
            excluded += 1
        else:
            hold_clean.append(row)
    heldout = hold_clean
    if not heldout:
        warnings.append("无可用独立留出样本；不能报告盲测效果。至少提供三个独立会话且避免重复回复。")
    if len(train) < 30:
        warnings.append("训练消息少于 30 条：可以轻量模仿，稳定习惯与场景差异证据不足。")
    if len({r["scene"] for r in train}) < 3:
        warnings.append("标注场景少于三类：不要把当前场景语气外推到全部关系/情境。")
    if any(re.search(r"忽略.{0,10}指令|ignore.{0,20}instructions|system\s*prompt", r["text"], re.I) for r in examples):
        warnings.append("语料含指令样文字，仅作为数据分析；不会执行。")
    # Same-name people are separated by their first source's content. To extend a real
    # existing person, explicitly reuse its persona_id; never identify a person by name alone.
    pid = identifier(persona_id) if persona_id else "p-" + digest([target, source_digests[0]["sha256"]])[:16]
    meta = {"schema_version": VERSION, "target": target,
            "persona_id": pid, "seed": seed, "holdout_ratio": holdout,
            "sources": source_digests, "speakers": dict(Counter(r["speaker"] for r in unique)),
            "target_messages": len(examples), "train_messages": len(train), "heldout_messages": len(heldout),
            "heldout_groups": len(hold_groups), "excluded_overlap": excluded,
            "train_hash": digest(train), "heldout_hash": digest(heldout),
            "warnings": warnings, "split": "conversation-group; seeded hash order; overlap excluded"}
    # A corpus is committed by writing its manifest last.
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "train.jsonl", train)
    write_jsonl(out / "heldout.jsonl", heldout)
    write_json(out / "stats.json", stats(train))
    write_json(out / "manifest.json", meta)
    return meta

def retrieve(directory, query, scene=None, relationship=None, limit=5):
    meta, rows = corpus_info(directory)
    require(1 <= limit <= 20, "limit 应为 1–20")
    docs = [tokens(" ".join(x["text"] for x in r["context"]) + " " + r["text"]) for r in rows]
    df = Counter(t for doc in docs for t in set(doc))
    avg = sum(map(len, docs)) / max(1, len(docs))
    qterms = set(tokens(query))
    scored = []
    for row, doc in zip(rows, docs):
        tf, score = Counter(doc), 0.0
        for term in qterms:
            idf = math.log(1 + (len(rows) - df[term] + .5) / (df[term] + .5))
            f = tf[term]
            score += idf * f * 2.2 / (f + 1.2 * (.25 + .75 * len(doc) / max(1, avg)))
        if scene and row["scene"] == scene:
            score += 1.8
        if relationship and row["relationship"] == relationship:
            score += .8
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    selected, phrases = [], set()
    for score, row in scored:
        # Avoid spending retrieval context on duplicate reply text.
        key = normalize(row["text"])
        if key in phrases:
            continue
        selected.append({"score": round(score, 4), **row})
        phrases.add(key)
        if len(selected) == limit:
            break
    return {"target": meta["target"], "method": "BM25 + scene/relationship bonuses; not semantic probability",
            "query": query, "examples": selected}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Normalize and group-split source files")
    b.add_argument("--input", action="append", required=True)
    b.add_argument("--target", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--seed", default="yuji-v1")
    b.add_argument("--holdout", type=float, default=.2)
    b.add_argument("--single-author", action="store_true")
    b.add_argument("--persona-id", help="Explicitly reuse an existing person's namespace during an update")
    r = sub.add_parser("retrieve", help="Retrieve training examples only")
    r.add_argument("--corpus", required=True)
    r.add_argument("--query", required=True)
    r.add_argument("--scene")
    r.add_argument("--relationship")
    r.add_argument("--limit", type=int, default=5)
    a = p.parse_args()
    if a.command == "build":
        print_result(make_corpus(a.input, a.target, a.out, a.seed, a.holdout, a.single_author, a.persona_id))
    else:
        print_result(retrieve(a.corpus, a.query, a.scene, a.relationship, a.limit))

if __name__ == "__main__":
    run_cli(main)
