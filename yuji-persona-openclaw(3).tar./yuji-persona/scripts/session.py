"""Versioned persona-isolated sessions, provenance memory, branches and restore bundles."""
from __future__ import annotations
import argparse
import copy
from pathlib import Path
from common import *

EVENTS = ("neutral", "conflict", "repair_attempt", "repair_accepted", "boundary", "friendly")
PROVENANCE = ("user_statement", "character_statement", "user_setting", "fictional")

def paths(store, persona, session):
    folder = Path(store) / identifier(persona)
    sid = identifier(session)
    return folder / (sid + ".json"), folder / "snapshots" / sid, folder / (sid + ".lock")

def checked_profile(path):
    p = read_json(path)
    require(p.get("schema_version") == VERSION, "不支持的档案版本")
    identifier(p["persona_id"])
    require(isinstance(p.get("target"), str) and bool(p["target"]), "档案缺少 target")
    validate_representation(p)
    return p

def validate_representation(profile):
    r = profile.get("representation", {})
    require(isinstance(r, dict) and r.get("kind") == "style_simulation", "只允许明确标记的风格模拟档案")
    require(r.get("display_label") == profile["target"] + "（模拟）", "人物档案必须保留模拟标签")
    require(r.get("identity_claims_allowed") is False and r.get("external_send_allowed") is False,
            "人物档案不能允许真人身份声明或对外冒名发送")

def validate_state(state, profile, sid=None, allow_drift=False):
    validate_representation(profile)
    require(state.get("schema_version") == VERSION, "不支持的存档版本")
    require(state.get("persona_id") == profile["persona_id"], "存档人物不匹配，禁止跨人物加载")
    require(state.get("simulation_label") == profile["representation"]["display_label"], "存档缺少或修改了模拟标签")
    identifier(state["session_id"])
    if sid:
        require(state["session_id"] == sid, "session_id 不匹配")
    require(type(state.get("revision")) is int and state["revision"] >= 0, "revision 无效")
    require(isinstance(state.get("dialogue"), list) and isinstance(state.get("memories"), list), "存档数组无效")
    require(isinstance(state.get("turn_hashes"), dict), "缺少轮次索引")
    require(allow_drift or state.get("profile_hash") == digest(profile), "档案已更新：请明确执行 rebind 后继续，禁止静默换人物设定")
    seen = set()
    for turn in state["dialogue"]:
        identifier(turn["id"])
        require(turn["id"] not in seen, "存档出现重复 turn ID")
        seen.add(turn["id"])
        require(isinstance(turn.get("user"), str) and isinstance(turn.get("assistant"), str), "轮次文本无效")
    for memory in state["memories"]:
        identifier(memory["id"])
        require(memory.get("provenance") in PROVENANCE, "记忆来源类型无效")
        require(isinstance(memory.get("sources"), list), "记忆缺少来源")

def load(store, profile, sid, allow_drift=False):
    current, _, _ = paths(store, profile["persona_id"], sid)
    state = read_json(current)
    validate_state(state, profile, sid, allow_drift)
    return state

def save(store, profile, state):
    current, snapshots, _ = paths(store, profile["persona_id"], state["session_id"])
    state["updated_at"] = now()
    write_json(snapshots / f"r{state['revision']:06d}.json", state)
    write_json(current, state)

def summary(state):
    return {"persona_id": state["persona_id"], "session_id": state["session_id"],
            "revision": state["revision"], "recent_turns": len(state["dialogue"]),
            "memories": len(state["memories"]), "simulation_state": state["simulation_state"],
            "note": "模拟场景状态，不是真人心理测量；本地文件需导出/持久保存后才能承诺跨会话恢复。"}

def new(store, profile, sid, relationship="ordinary_acquaintance"):
    validate_representation(profile)
    current, _, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        require(not current.exists(), "会话已存在，不能覆盖；换 session 名称")
        state = {"schema_version": VERSION, "persona_id": profile["persona_id"], "session_id": sid,
                 "simulation_label": profile["representation"]["display_label"],
                 "revision": 0, "profile_hash": digest(profile), "profile_version": profile["version"],
                 "created_at": now(), "updated_at": now(), "parent": None,
                 "relationship_setting": relationship, "dialogue": [], "memories": [],
                 "turn_hashes": {}, "forgotten_turn_ids": [], "events": [],
                 "simulation_state": {"tension": "neutral", "last_event": "neutral", "scene": "general"}}
        save(store, profile, state)
    return summary(state)

def check_revision(state, expected):
    require(state["revision"] == expected, "会话版本冲突：重新读取 revision 后再提交，禁止覆盖")

def turn(store, profile, sid, expected, tid, user, assistant, scene="general", event="neutral"):
    identifier(tid)
    require(isinstance(user, str) and user.strip(), "用户消息不能为空")
    require(isinstance(assistant, str) and assistant.strip(), "回复不能为空")
    require(len(user) <= 100000 and len(assistant) <= 100000, "单轮文本太长")
    require(event in EVENTS, "未知事件")
    _, _, lockfile = paths(store, profile["persona_id"], sid)
    payload = {"id": tid, "user": user, "assistant": assistant, "scene": scene, "event": event}
    with lock(lockfile):
        state = load(store, profile, sid)
        if tid in state["turn_hashes"]:
            require(state["turn_hashes"][tid] == digest(payload), "turn ID 已用且内容不同")
            return {**summary(state), "idempotent": True, "display_reply": profile["representation"]["display_label"] + "\n" + assistant}
        require(tid not in state["forgotten_turn_ids"], "此 turn ID 已遗忘，禁止重新恢复；新对话请用新 ID")
        check_revision(state, expected)
        state["turn_hashes"][tid] = digest(payload)
        state["dialogue"].append({**payload, "created_at": now()})
        # No silent truncation: ask for provenance summaries once the working window grows.
        mood = state["simulation_state"]
        mood.update({"last_event": event, "scene": scene})
        if event == "conflict":
            mood["tension"] = "raised"
        elif event == "repair_attempt" and mood["tension"] != "neutral":
            mood["tension"] = "unresolved"
        elif event == "repair_accepted":
            mood["tension"] = "easing"
        state["events"].append({"turn_id": tid, "event": event, "scene": scene})
        state["revision"] += 1
        save(store, profile, state)
    result = summary(state)
    result["display_reply"] = profile["representation"]["display_label"] + "\n" + assistant
    if len(state["dialogue"]) > 24:
        result["action"] = "生成带来源的记忆摘要，再执行 compact；尚未自动删减原文。"
    return result

def add_memory(store, profile, sid, expected, mid, text, subject, provenance, source_ids=None, quote=None, source_note=None):
    identifier(mid)
    require(text.strip() and subject.strip(), "记忆内容和归属不能为空")
    require(provenance in PROVENANCE, "记忆来源类型无效")
    _, _, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        state = load(store, profile, sid)
        check_revision(state, expected)
        require(not any(m["id"] == mid for m in state["memories"]), "记忆 ID 已存在")
        turns = {t["id"]: t for t in state["dialogue"]}
        sources = source_ids or []
        require(all(x in state["turn_hashes"] for x in sources), "记忆引用了不存在/已遗忘轮次")
        if provenance in ("user_statement", "character_statement"):
            require(bool(sources) and bool(quote), "陈述记忆需要来源轮次及原文 quote")
            key = "user" if provenance == "user_statement" else "assistant"
            require(any(tid in turns and quote in turns[tid][key] for tid in sources), "quote 不在所归属说话人的近期原文中；请在压缩前建立记忆")
        else:
            require(bool(source_note), "用户/虚构设定需要 source_note")
        memory = {"id": mid, "text": text, "subject": subject, "provenance": provenance,
                  "sources": sources, "quote": quote, "source_note": source_note, "created_at": now()}
        state["memories"].append(memory)
        state["revision"] += 1
        save(store, profile, state)
    return {**summary(state), "memory_id": mid}

def compact(store, profile, sid, expected, keep=12):
    require(1 <= keep <= 24, "keep 范围为 1–24")
    _, _, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        state = load(store, profile, sid)
        check_revision(state, expected)
        discard = state["dialogue"][:-keep]
        summarized = {tid for m in state["memories"] for tid in m["sources"]}
        require(all(t["id"] in summarized for t in discard), "有旧轮次尚无来源记忆，请先摘要再压缩；不会静默丢失")
        state["dialogue"] = state["dialogue"][-keep:]
        state["revision"] += 1
        save(store, profile, state)
    return {**summary(state), "compacted_turns": len(discard), "note": "旧原文仍在历史快照；彻底遗忘请使用 forget。"}

def branch(store, profile, sid, new_sid, revision=None):
    identifier(new_sid)
    require(new_sid.casefold() != sid.casefold(), "分支名称不能与主线相同或仅大小写不同")
    # Lock both names in stable order to avoid deadlock under concurrent callers.
    src, snapshots, src_lock = paths(store, profile["persona_id"], sid)
    dst, _, dst_lock = paths(store, profile["persona_id"], new_sid)
    first, second = sorted([src_lock, dst_lock])
    with lock(first), lock(second):
        # The current session may have been rebound to a newer profile. The selected
        # snapshot, not the live head, must match the profile used for this branch.
        current = load(store, profile, sid, allow_drift=True)
        require(not dst.exists(), "分支已存在")
        rev = current["revision"] if revision is None else revision
        require(type(rev) is int and 0 <= rev <= current["revision"], "分支 revision 无效")
        origin = read_json(snapshots / f"r{rev:06d}.json")
        validate_state(origin, profile, sid)
        state = copy.deepcopy(origin)
        state.update({"session_id": new_sid, "revision": 0, "created_at": now(),
                      "parent": {"session_id": sid, "revision": rev}})
        save(store, profile, state)
    return summary(state)

def forget(store, profile, sid, expected, memory_id=None, turn_id=None):
    require(bool(memory_id) != bool(turn_id), "只指定 memory-id 或 turn-id 之一")
    _, snapshots, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        state = load(store, profile, sid)
        check_revision(state, expected)
        if memory_id:
            selected = [m for m in state["memories"] if m["id"] == memory_id]
            require(bool(selected), "找不到该记忆")
            erase_ids = set(selected[0]["sources"])
        else:
            require(turn_id in state["turn_hashes"], "找不到该轮次")
            erase_ids = {turn_id}
        state["memories"] = [m for m in state["memories"] if m["id"] != memory_id and not (set(m["sources"]) & erase_ids)]
        state["dialogue"] = [t for t in state["dialogue"] if t["id"] not in erase_ids]
        state["events"] = [e for e in state["events"] if e["turn_id"] not in erase_ids]
        for tid in erase_ids:
            state["turn_hashes"].pop(tid, None)
        state["forgotten_turn_ids"] = sorted(set(state["forgotten_turn_ids"]) | erase_ids)
        # Derived scene state must not keep the effect of a forgotten event.
        state["simulation_state"] = {"tension": "neutral", "last_event": "neutral", "scene": "general"}
        state["revision"] += 1
        # All local snapshots of THIS session could reconstruct deleted content: remove them.
        for old in snapshots.glob("r*.json"):
            old.unlink()
        save(store, profile, state)
    return {**summary(state), "erased_source_turns": sorted(erase_ids),
            "scope": "已清理此会话的记忆、对应原文和全部旧快照；不撤销其他分支、语料源或已导出的副本。"}

def rebind(store, profile, sid, expected):
    _, _, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        state = load(store, profile, sid, allow_drift=True)
        check_revision(state, expected)
        state["profile_hash"] = digest(profile)
        state["profile_version"] = profile["version"]
        state["revision"] += 1
        save(store, profile, state)
    return summary(state)

def export_bundle(store, profile, sid, out):
    _, _, lockfile = paths(store, profile["persona_id"], sid)
    # Serialize by destination as well as source: two different sessions exporting
    # concurrently must not both pass the existence check and replace each other.
    with lock(str(out) + ".export.lock"), lock(lockfile):
        require(not Path(out).exists(), "导出文件已存在，请换文件名")
        state = load(store, profile, sid)
        payload = {"schema_version": VERSION, "kind": "yuji-session-export", "profile": profile, "state": state}
        bundle = {**payload, "checksum": digest(payload)}
        write_json(out, bundle)
    return {"export": str(Path(out).resolve()), "includes": "人物档案、当前会话、记忆；不包含完整语料和历史快照", "checksum": bundle["checksum"]}

def import_bundle(store, profile, sid, bundle_path):
    data = read_json(bundle_path)
    payload = {k: v for k, v in data.items() if k != "checksum"}
    require(data.get("kind") == "yuji-session-export" and digest(payload) == data.get("checksum"), "恢复包格式或校验失败")
    require(digest(data["profile"]) == digest(profile), "请使用恢复包中相同版本的人物档案")
    state = data["state"]
    validate_state(state, profile)
    current, _, lockfile = paths(store, profile["persona_id"], sid)
    with lock(lockfile):
        require(not current.exists(), "导入目标会话已存在，禁止覆盖")
        state = copy.deepcopy(state)
        state.update({"session_id": sid, "revision": 0,
                      "parent": {"imported_session": data["state"]["session_id"], "revision": data["state"]["revision"]}})
        save(store, profile, state)
    return summary(state)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    names = ("new", "show", "turn", "memory-add", "compact", "branch", "forget", "rebind", "export", "import")
    parsers = {}
    for name in names:
        x = sub.add_parser(name)
        x.add_argument("--profile", required=True)
        x.add_argument("--store", required=True)
        x.add_argument("--session", required=True)
        parsers[name] = x
    parsers["new"].add_argument("--relationship", default="ordinary_acquaintance")
    for name in ("turn", "memory-add", "compact", "forget", "rebind"):
        parsers[name].add_argument("--expected-revision", type=int, required=True)
    t = parsers["turn"]
    t.add_argument("--turn-id", required=True)
    t.add_argument("--user", required=True)
    t.add_argument("--assistant", required=True)
    t.add_argument("--scene", default="general")
    t.add_argument("--event", choices=EVENTS, default="neutral")
    m = parsers["memory-add"]
    m.add_argument("--memory-id", required=True)
    m.add_argument("--text", required=True)
    m.add_argument("--subject", required=True)
    m.add_argument("--provenance", choices=PROVENANCE, required=True)
    m.add_argument("--source-turn", action="append")
    m.add_argument("--quote")
    m.add_argument("--source-note")
    parsers["compact"].add_argument("--keep", type=int, default=12)
    parsers["branch"].add_argument("--new-session", required=True)
    parsers["branch"].add_argument("--from-revision", type=int)
    group = parsers["forget"].add_mutually_exclusive_group(required=True)
    group.add_argument("--memory-id")
    group.add_argument("--turn-id")
    parsers["export"].add_argument("--out", required=True)
    parsers["import"].add_argument("--bundle", required=True)
    a = p.parse_args()
    profile = checked_profile(a.profile)
    base = (a.store, profile, a.session)
    if a.command == "new": result = new(*base, a.relationship)
    elif a.command == "show": result = load(*base)
    elif a.command == "turn": result = turn(*base, a.expected_revision, a.turn_id, a.user, a.assistant, a.scene, a.event)
    elif a.command == "memory-add": result = add_memory(*base, a.expected_revision, a.memory_id, a.text, a.subject, a.provenance, a.source_turn, a.quote, a.source_note)
    elif a.command == "compact": result = compact(*base, a.expected_revision, a.keep)
    elif a.command == "branch": result = branch(*base, a.new_session, a.from_revision)
    elif a.command == "forget": result = forget(*base, a.expected_revision, a.memory_id, a.turn_id)
    elif a.command == "rebind": result = rebind(*base, a.expected_revision)
    elif a.command == "export": result = export_bundle(*base, a.out)
    else: result = import_bundle(*base, a.bundle)
    print_result(result)

if __name__ == "__main__":
    run_cli(main)
