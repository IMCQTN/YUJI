"""Create, evidence-validate and revise a language profile. No LLM/API dependency."""
from __future__ import annotations
import argparse
from pathlib import Path
from common import *

DIMENSIONS = {"rhythm", "lexicon", "punctuation", "pragmatics", "relationship", "boundary", "topic", "register"}

def create(corpus, out):
    meta, rows = corpus_info(corpus)
    require(not Path(out).exists(), "档案文件已存在，请换输出名或执行 feedback")
    profile = {
        "schema_version": VERSION, "persona_id": meta["persona_id"], "target": meta["target"],
        "version": 1, "train_hash": meta["train_hash"], "source_files": meta["sources"],
        "representation": {"kind": "style_simulation", "display_label": meta["target"] + "（模拟）",
                           "identity_claims_allowed": False, "external_send_allowed": False},
        "voice_summary": "", "observed_stats": stats(rows),
        "rules": [], "claims": [], "relationships": [], "user_constraints": [],
        "feedback_log": [], "limitations": list(meta["warnings"]),
        "defaults": {"strength": "balanced", "relationship": "ordinary_acquaintance", "mode": "chat"},
        "status": "draft",
    }
    write_json(out, profile)
    return {"profile": str(Path(out).resolve()), "persona_id": meta["persona_id"], "status": "draft",
            "next": "用训练语料补充 voice_summary/rules/evidence，再执行 validate；init 不会自动推断人格。"}

def validate(profile, corpus):
    meta, rows = corpus_info(corpus)
    lookup = {r["id"]: r for r in rows}
    errors, warnings = [], []
    def check(condition, msg):
        if not condition:
            errors.append(msg)
    check(profile.get("schema_version") == VERSION, "schema_version 不支持")
    check(profile.get("persona_id") == meta["persona_id"], "persona_id 与语料不一致")
    check(profile.get("target") == meta["target"], "target 与语料不一致")
    check(profile.get("train_hash") == meta["train_hash"], "train_hash 不一致：请重建证据")
    representation = profile.get("representation", {})
    check(isinstance(representation, dict), "representation 需要对象")
    if isinstance(representation, dict):
        check(representation.get("kind") == "style_simulation", "仅允许明确标记的表达风格模拟")
        check(representation.get("display_label") == meta["target"] + "（模拟）", "必须保留人物模拟标签")
        check(representation.get("identity_claims_allowed") is False, "不能允许真人身份声明")
        check(representation.get("external_send_allowed") is False, "不能允许对外冒名发送")
    check(type(profile.get("version")) is int and profile.get("version", 0) >= 1, "version 需要正整数")
    check(isinstance(profile.get("voice_summary"), str) and bool(profile.get("voice_summary", "").strip()), "缺少 voice_summary")
    seen = set()
    for section in ("rules", "claims", "relationships", "user_constraints", "feedback_log"):
        check(isinstance(profile.get(section), list), f"{section} 必须是数组")
    for section in ("rules", "claims", "relationships"):
        values = profile.get(section)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                errors.append(f"{section} 条目需要对象")
                continue
            rid = item.get("id")
            check(isinstance(rid, str) and bool(rid) and rid not in seen, f"{section}: ID 缺失或重复 {rid}")
            seen.add(str(rid))
            check(bool(item.get("instruction") if section == "rules" else item.get("statement")), f"{rid}: 缺少文字说明")
            if section == "rules":
                check(item.get("dimension") in DIMENSIONS, f"{rid}: dimension 无效")
            provenance = item.get("provenance", "corpus")
            check(provenance in ("corpus", "user", "fictional"), f"{rid}: provenance 无效")
            confidence = item.get("confidence", "tentative")
            check(confidence in ("tentative", "supported", "user"), f"{rid}: confidence 无效")
            if provenance == "corpus":
                evidence = item.get("evidence")
                check(isinstance(evidence, list) and bool(evidence), f"{rid}: 语料结论需要 evidence")
                valid_ids = set()
                for ev in evidence if isinstance(evidence, list) else []:
                    if not isinstance(ev, dict):
                        errors.append(f"{rid}: evidence 应为对象")
                        continue
                    eid, quote = ev.get("id"), ev.get("quote")
                    check(eid in lookup, f"{rid}: 引用非训练消息 {eid}")
                    check(isinstance(quote, str) and bool(quote.strip()), f"{rid}: 缺少原文短摘录")
                    if eid in lookup and isinstance(quote, str) and quote.strip():
                        check(quote in lookup[eid]["text"], f"{rid}: 摘录与原文不符 {eid}")
                        if quote in lookup[eid]["text"]:
                            valid_ids.add(eid)
                if section == "rules" and confidence == "supported":
                    check(len(valid_ids) >= 2, f"{rid}: supported 规律至少需要两条不同消息证据")
                check(confidence != "user", f"{rid}: 语料证据不能标记 confidence=user")
            else:
                check(bool(item.get("source_note")), f"{rid}: 用户/虚构设定需要 source_note")
                check(confidence != "supported", f"{rid}: 用户/虚构设定不能标为语料支持")
            if section == "claims":
                check(item.get("truth_status") in ("corpus_statement", "user_defined", "fictional"), f"{rid}: 需要 truth_status，不能宣称已核实真人事实")
                expected_truth = {"corpus": "corpus_statement", "user": "user_defined", "fictional": "fictional"}.get(provenance)
                check(item.get("truth_status") == expected_truth, f"{rid}: truth_status 必须与 provenance 一致")
                check(bool(item.get("subject")), f"{rid}: claim 需要 subject")
            scope = item.get("scope", {})
            check(isinstance(scope, dict), f"{rid}: scope 必须是对象")
    constraints = profile.get("user_constraints", [])
    if isinstance(constraints, list):
        for item in constraints:
            check(isinstance(item, dict) and bool(item.get("instruction")), "user_constraints 每项需要 instruction")
    if not profile.get("rules"):
        warnings.append("尚无有证据的语言规则，只有基础统计")
    if len(rows) < 30:
        warnings.append("训练语料较少，场景泛化需要更多样本")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "rules": len(profile.get("rules", [])) if isinstance(profile.get("rules"), list) else 0,
            "persona_id": meta["persona_id"], "note": "引用校验不等于已证明语义推断正确；仍需人工检查规则是否被摘录支持。"}

def feedback(path, expected, instruction, scene="general", supersedes=None):
    require(instruction.strip(), "纠正内容不能为空")
    p = Path(path)
    with lock(str(p) + ".lock"):
        profile = read_json(p)
        require(profile["version"] == expected, "档案版本冲突，请重新读取后再修改")
        if supersedes:
            require(any(r["id"] == supersedes for r in profile["rules"]), "找不到被覆盖的 rule ID")
        history = p.parent / (p.name + ".history") / f"v{expected:04d}.json"
        write_json(history, profile)
        entry = {"id": "f-" + digest([expected, instruction, scene])[:12], "instruction": instruction,
                 "scene": scene, "source": "user_feedback", "supersedes": supersedes, "created_at": now()}
        profile["user_constraints"].append(entry)
        profile["feedback_log"].append(entry)
        profile["version"] += 1
        write_json(p, profile)
    return {"version": profile["version"], "feedback": entry, "profile_hash": digest(profile)}

def card(profile, scene=None):
    constraints = [r for r in profile["user_constraints"] if r.get("scene", "general") in ("general", scene)]
    disabled = {r.get("supersedes") for r in constraints}
    rules = [r for r in profile["rules"] if r["id"] not in disabled and
             r.get("scope", {}).get("scene", "general") in ("general", scene)]
    return {"persona_id": profile["persona_id"], "target": profile["target"],
            "voice_summary": profile["voice_summary"], "scene": scene or "general",
            "representation": profile["representation"],
            "user_constraints_first": constraints, "rules": rules,
            "claims": profile["claims"], "relationships": profile["relationships"],
            "limits": profile["limitations"], "profile_hash": digest(profile)}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("init")
    n.add_argument("--corpus", required=True)
    n.add_argument("--out", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--profile", required=True)
    v.add_argument("--corpus", required=True)
    f = sub.add_parser("feedback")
    f.add_argument("--profile", required=True)
    f.add_argument("--expected-version", type=int, required=True)
    f.add_argument("--instruction", required=True)
    f.add_argument("--scene", default="general")
    f.add_argument("--supersedes")
    c = sub.add_parser("card")
    c.add_argument("--profile", required=True)
    c.add_argument("--scene")
    a = p.parse_args()
    if a.command == "init":
        print_result(create(a.corpus, a.out))
    elif a.command == "validate":
        report = validate(read_json(a.profile), a.corpus)
        print_result(report)
        if not report["valid"]:
            raise SystemExit(2)
    elif a.command == "feedback":
        print_result(feedback(a.profile, a.expected_version, a.instruction, a.scene, a.supersedes))
    else:
        print_result(card(read_json(a.profile), a.scene))

if __name__ == "__main__":
    run_cli(main)
