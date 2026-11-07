"""Observable style diagnostics, withheld-answer cases, and honest human blind packs."""
from __future__ import annotations
import argparse
import math
import random
import statistics
from collections import Counter
from pathlib import Path
from common import *

def heldout_rows(directory):
    meta, train = corpus_info(directory)
    heldout = read_jsonl(Path(directory) / "heldout.jsonl")
    require(digest(heldout) == meta["heldout_hash"], "留出语料校验失败")
    require(not ({r["group_id"] for r in train} & {r["group_id"] for r in heldout}), "训练与留出会话重叠")
    return meta, train, heldout

def make_cases(directory, out):
    meta, _, heldout = heldout_rows(directory)
    require(not Path(out).exists(), "测试题文件已存在，请换输出名")
    cases = []
    for row in heldout:
        # Do not leak any held-out target reply through another case's history.
        context = [x for x in row["context"] if x["speaker"] != meta["target"]]
        if not context:
            continue
        cases.append({"case_id": "case-" + row["id"], "scene": row["scene"], "relationship": row["relationship"],
                      "context": [{"speaker": x["speaker"], "text": x["text"]} for x in context],
                      "instruction": "独立回复最后一位说话人；只使用训练集和档案，不读取留出参考答案。"})
    write_jsonl(out, cases)
    return {"cases": len(cases), "out": str(Path(out).resolve()),
            "status": "ready" if cases else "no_independent_test",
            "note": "不含目标人物的留出回复；纯文章没有对话上下文时不自动制造聊天题。"}

def candidate_map(path):
    rows, found = read_jsonl(path), {}
    for row in rows:
        cid = row.get("case_id")
        require(isinstance(cid, str) and bool(cid), "候选缺少 case_id")
        require(cid not in found, f"重复 case_id：{cid}")
        text = row.get("text", row.get("reply"))
        require(isinstance(text, str) and text.strip(), f"{cid}: 缺少非空 text/reply")
        require(len(text) <= 10000, f"{cid}: 候选过长")
        found[cid] = text
    return found

def copy_flags(text, train):
    long_windows = {text[i:i + 32] for i in range(max(0, len(text) - 31))}
    matches = []
    for row in train:
        other = row["text"]
        if long_windows and any(other[i:i + 32] in long_windows for i in range(max(0, len(other) - 31))):
            matches.append(row["id"])
        elif len(text) >= 8 and normalize(text) == normalize(other):
            matches.append(row["id"])
    return matches[:10]

def check_text(text, profile, train=None, recent=None):
    require(isinstance(text, str) and len(text) <= 10000, "文本超过单次检查上限 10000 字符")
    f = features(text)
    rules = profile.get("observed_stats", {})
    length = rules.get("length", {})
    flags = []
    if len(text) > max(60, length.get("p90", 40) * 2):
        flags.append("明显长于训练语料常见长度；检查是否确实由复杂问题需要")
    means = rules.get("mean_features", {})
    if f["has_list"] and means.get("has_list", 0) < .1:
        flags.append("语料很少使用列表；检查是否过于报告化")
    if f["emoji_per_100_chars"] > max(8, means.get("emoji_per_100_chars", 0) * 3):
        flags.append("表情密度偏高；检查是否过度表演")
    if re.search(r"首先.{0,60}其次|综上所述|希望这些建议对你有帮助", text, re.S):
        flags.append("出现通用助手套语；对照人物证据判断是否合适")
    if recent and any(normalize(text) == normalize(x) for x in recent[-5:]):
        flags.append("与最近回复完全重复")
    copied = copy_flags(text, train or [])
    if copied:
        flags.append("存在整句复用或至少连续 32 字符重合；检查是否只在复制语料")
    return {"features": f, "review_flags": flags, "source_overlap_ids": copied,
            "note": "自动检查仅提供复核线索；不验证事实、人格或真人心理。"}

def distribution(texts, reference):
    rows = [{"text": t} for t in texts]
    ref = [{"text": t} for t in reference]
    a, b = stats(rows), stats(ref)
    if not texts or not reference:
        return {"status": "insufficient_data", "candidate_n": len(texts), "reference_n": len(reference)}
    keys = features("").keys()
    delta = {k: round(a["mean_features"][k] - b["mean_features"][k], 4) for k in keys}
    return {"candidate_n": len(texts), "reference_n": len(reference),
            "candidate_length": a["length"], "reference_length": b["length"],
            "mean_feature_delta_candidate_minus_reference": delta,
            "unique_reply_ratio": round(len({normalize(t) for t in texts}) / len(texts), 4),
            "note": "差值越小仅说明该表面指标越接近；短、少标点或少表情本身不代表更真实。"}

def assess(directory, candidates_path, out, baseline_path=None):
    meta, train, heldout = heldout_rows(directory)
    refs = {"case-" + r["id"]: r for r in heldout if any(x["speaker"] != meta["target"] for x in r["context"])}
    require(bool(refs), "没有独立对话测试题，不能生成效果报告")
    candidate = candidate_map(candidates_path)
    require(set(candidate) == set(refs), f"case ID 不完整：missing={sorted(set(refs)-set(candidate))}, extra={sorted(set(candidate)-set(refs))}")
    ordered = sorted(refs)
    reference = [refs[c]["text"] for c in ordered]
    def system_report(data):
        require(set(data) == set(refs), "基线 case ID 必须与测试题完全一致")
        return {"distribution": distribution([data[c] for c in ordered], reference),
                "copy_review": [{"case_id": c, "source_ids": copy_flags(data[c], train)} for c in ordered if copy_flags(data[c], train)],
                "exact_reference_matches": [c for c in ordered if normalize(data[c]) == normalize(refs[c]["text"])]}
    result = {"schema_version": VERSION, "target": meta["target"], "cases": len(refs),
              "train_hash": meta["train_hash"], "candidate_sha256": digest(candidate),
              "skill": system_report(candidate),
              "status": "small_sample_descriptive" if len(refs) < 20 else "descriptive_metrics",
              "human_blind_test": "not_performed",
              "limitations": ["不输出综合真实度或身份概率", "不能从自动指标证明语义、情境和事实边界合格", "生成者须未见参考回复；工具不能证明执行者没有提前看过"]}
    if baseline_path:
        result["baseline"] = system_report(candidate_map(baseline_path))
    write_json(out, result)
    return result

def blind_pack(cases_path, candidates_path, baseline_path, out_dir, seed="yuji-blind-v1"):
    cases = read_jsonl(cases_path)
    lookup = {r["case_id"]: r for r in cases}
    require(len(lookup) == len(cases) and cases, "测试题为空或 case ID 重复")
    skill, base = candidate_map(candidates_path), candidate_map(baseline_path)
    require(set(skill) == set(base) == set(lookup), "盲评三份文件 case ID 不一致")
    out = Path(out_dir)
    require(not (out / "questions.md").exists(), "盲评文件已存在，请用新目录")
    rng = random.Random(seed)
    keys, parts = {}, ["# 匿名风格评测\n\n请先独立阅读训练语料的人物风格卡（不含候选生成者身份）。逐题选择更符合该人物且语义合适的 A/B，难分时选 tie。分别注意：语言节奏、场景合适、原意保持、事实边界。不要把更礼貌或更长直接等同更像。\n"]
    order = list(lookup)
    rng.shuffle(order)
    for index, cid in enumerate(order, 1):
        a_is_skill = bool(rng.getrandbits(1))
        a, b = (skill[cid], base[cid]) if a_is_skill else (base[cid], skill[cid])
        qid = f"q{index:03d}"
        keys[qid] = {"case_id": cid, "A": "skill" if a_is_skill else "baseline", "B": "baseline" if a_is_skill else "skill"}
        context = "\n".join(f"> {r['speaker']}：{r['text']}" for r in lookup[cid]["context"])
        parts.append(f"\n## {qid}\n\n{context}\n\n**A**\n\n{a}\n\n**B**\n\n{b}\n\n选择：____　理由：____\n")
    atomic_text(out / "questions.md", "\n".join(parts))
    write_json(out / "answer-key.json", {"schema_version": VERSION, "questions": keys, "seed": seed})
    return {"questions": len(keys), "questions_file": str((out / "questions.md").resolve()),
            "key_file": str((out / "answer-key.json").resolve()), "note": "只把 questions.md 给评审；答案键单独保管。尚未进行人工盲评。"}

def blind_score(ratings_path, key_path, out):
    key = read_json(key_path)["questions"]
    ratings = read_jsonl(ratings_path)
    require(bool(ratings), "没有人工评分记录，不能报告盲评结果")
    seen, counts, evaluators = set(), Counter(), set()
    for row in ratings:
        who, qid, choice = row.get("evaluator"), row.get("question_id"), row.get("choice")
        require(isinstance(who, str) and who.strip(), "需要实际评审标识 evaluator")
        require(qid in key and choice in ("A", "B", "tie"), "题号/选择无效")
        require((who, qid) not in seen, "同一评审重复评分同一题")
        seen.add((who, qid))
        evaluators.add(who)
        counts["tie" if choice == "tie" else key[qid][choice]] += 1
    result = {"ratings": len(ratings), "evaluators": len(evaluators), "preferences": dict(counts),
              "missing_ratings": {who: sorted(set(key) - {q for w, q in seen if w == who}) for who in sorted(evaluators)},
              "note": "仅统计提交的人工记录，工具不能核实评审身份；小样本偏好不等于真人还原率。"}
    write_json(out, result)
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("cases")
    c.add_argument("--corpus", required=True)
    c.add_argument("--out", required=True)
    a = sub.add_parser("assess")
    a.add_argument("--corpus", required=True)
    a.add_argument("--candidates", required=True)
    a.add_argument("--baseline")
    a.add_argument("--out", required=True)
    t = sub.add_parser("check")
    t.add_argument("--profile", required=True)
    t.add_argument("--text-file", required=True)
    t.add_argument("--corpus")
    t.add_argument("--recent", help="JSONL with text fields for recent replies")
    b = sub.add_parser("blind-pack")
    b.add_argument("--cases", required=True)
    b.add_argument("--candidates", required=True)
    b.add_argument("--baseline", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--seed", default="yuji-blind-v1")
    s = sub.add_parser("blind-score")
    s.add_argument("--ratings", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--out", required=True)
    x = p.parse_args()
    if x.command == "cases": result = make_cases(x.corpus, x.out)
    elif x.command == "assess": result = assess(x.corpus, x.candidates, x.out, x.baseline)
    elif x.command == "blind-pack": result = blind_pack(x.cases, x.candidates, x.baseline, x.out_dir, x.seed)
    elif x.command == "blind-score": result = blind_score(x.ratings, x.key, x.out)
    else:
        profile = read_json(x.profile)
        train = corpus_info(x.corpus)[1] if x.corpus else []
        if x.corpus:
            require(corpus_info(x.corpus)[0]["persona_id"] == profile["persona_id"], "人物不匹配")
        recent = [r["text"] for r in read_jsonl(x.recent)] if x.recent else []
        text = Path(x.text_file).read_text(encoding="utf-8")
        result = check_text(text, profile, train, recent)
    print_result(result)

if __name__ == "__main__":
    run_cli(main)
