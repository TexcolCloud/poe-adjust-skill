"""TeleAgent-facing POE build workflow. Run --help; stdout is always JSON."""
from __future__ import annotations

import argparse
import copy
import csv
from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET

import buildxml as bx
import engine
import facts
import routes
import resources

VERSION = "2.4.0"
SCHEMA_VERSION = 2
METRICS = {
    "CombinedDPS": "主技能综合 DPS", "MinionCombinedDPS": "当前主技能召唤物单体综合 DPS", "FullDPS": "已选技能总 DPS", "HitChance": "角色技能命中率 %", "MinionHitChance": "当前主技能召唤物命中率 %",
    "Life": "生命", "EnergyShield": "能量护盾", "ManaUnreserved": "可用魔力",
    "LifeRegenRecovery": "生命秒回", "ManaRegenRecovery": "魔力秒回",
    "EnergyShieldRegenRecovery": "能量护盾秒回", "EnergyShieldRecharge": "能量护盾充能速率（需启动充能）",
    "EnergyShieldLeechGainRate": "能量护盾吸取速率（需持续命中）", "ESCost": "主技能直接能盾消耗", "ESPerSecondCost": "主技能每秒能盾消耗（含 EB）",
    "NetLifeRegen": "扣除自身持续伤害后生命净秒回", "NetManaRegen": "扣除自身持续伤害后魔力净秒回", "NetEnergyShieldRegen": "扣除自身持续伤害后能盾净秒回",
    "LifeLeechGainRate": "生命吸取速率（需持续命中）", "LifeCost": "主技能单次生命消耗",
    "ManaLeechGainRate": "魔力吸取速率（需持续命中）",
    "LifePerSecondCost": "主技能每秒生命消耗", "ManaCost": "主技能单次魔力消耗", "ManaPerSecondCost": "主技能每秒魔力消耗",
    "FireResist": "火焰抗性 %", "ColdResist": "冰霜抗性 %", "LightningResist": "闪电抗性 %", "ChaosResist": "混沌抗性 %",
    "Armour": "护甲", "Evasion": "闪避值", "BlockChance": "攻击格挡 %", "SpellBlockChance": "法术格挡 %", "SpellSuppressionChance": "法术压制 %",
    "PhysicalMaximumHitTaken": "物理最大承伤", "FireMaximumHitTaken": "火焰最大承伤", "ColdMaximumHitTaken": "冰霜最大承伤", "LightningMaximumHitTaken": "闪电最大承伤", "ChaosMaximumHitTaken": "混沌最大承伤",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    path = Path(path)
    bx.require(path.is_file() and path.stat().st_size <= bx.MAX_XML, f"JSON 文件不存在或过大：{path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    bx.require(isinstance(value, dict), "JSON 顶层应为对象。")
    return value


def write_text(path, text):
    path = Path(path)
    engine.check_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def view_json(args):
    """Read ordinary task JSON by the existing pointer contract; never recalculate."""
    path=Path(args.file).resolve()
    bx.require(path.suffix.lower()==".json","view 读取任务工作区已有 JSON，游戏原文使用 data。")
    bx.require(1<=args.limit<=100 and args.offset>=0,"limit 为1–100，offset为非负整数。",location="/pagination")
    selected=facts.pointer(read_json(path),args.pointer)
    fields=args.fields.split(",") if args.fields else []
    bx.require(all(field.strip()==field and field for field in fields) and len(fields)==len(set(fields)),"fields 使用不同的直接字段名，逗号分隔。",location="/fields")
    missing=[]
    def project(value,index=None):
        if not fields: return value
        bx.require(isinstance(value,dict),"fields 只能选择对象字段；先用 pointer 定位对象或对象数组。",location=args.pointer)
        absent=[field for field in fields if field not in value]
        if absent: missing.append({"index":index,"fields":absent})
        return {key:value[key] for key in fields if key in value}
    if isinstance(selected,list):
        total=len(selected);end=min(total,args.offset+args.limit)
        value=[project(row,args.offset+index) for index,row in enumerate(selected[args.offset:end])]
        following=end if end<total else None
    else:
        bx.require(args.offset==0,"offset 仅用于数组；对象用 fields，嵌套值用 pointer。",location="/offset")
        value=project(selected);total=len(selected) if isinstance(selected,dict) else 1;following=None
    result={"file":str(path),"pointer":args.pointer,"fields":fields,"offset":args.offset,"total":total,"next_offset":following,
            "value":value,"missing_fields":missing,"read_only":True,
            "scope":"当前已保存JSON的所选字段；没有重新计算或刷新来源。缺字段保持缺失，不按零/false补值。"}
    bx.require(len(json.dumps(result,ensure_ascii=False,allow_nan=False))<=12000,"所选内容仍过大；缩小 limit、选择 fields 或用 pointer 进入具体对象，不输出整份文件。",location=args.pointer)
    return result


def diagnosis_summary(result):
    """Keep the first player-facing diagnosis on the native calculation basis."""
    stats = result["stats"]
    minion = any(skill.get("is_main") and skill.get("minion") for skill in result.get("active_skills", []))
    damage, hit = ("MinionCombinedDPS", "MinionHitChance") if minion else ("CombinedDPS", "HitChance")
    rows = [f"当前主技能：{result.get('main_skill_name', result.get('main_skill', '未识别'))}。"]
    rows.append(f"本机统计已分配：普通天赋 {result['points']} 点，升华 {result['ascendancy_points']} 点（包含连接小点）。这不是剩余点数；可用上限仍须结合本次确认的等级与任务进度。")
    if result.get("secondary_ascendancy_points"):
        rows.append(f"另有次级升华已分配 {result['secondary_ascendancy_points']} 点，不能混入普通天赋或主升华点数。")
    rows += [f"{METRICS[key]}：{stats[key]:g}" for key in (damage, hit, "Life", "EnergyShield", "ChaosResist", "ManaUnreserved") if key in stats]
    rows.append("DPS 已包含引擎的命中计算，不再乘命中率。其他技能是否计入总输出以 damage_sources 的实际分项为准，不能根据装备着召唤宝石推断。")
    if stats.get("ManaUnreserved", 0) < 0:
        rows.append("原文件的保留配置超出可用魔力，需要调整同时开启的保留技能。各技能可能支付魔力、生命或能盾，不能据此断言所有技能都不能使用。")
    rows.append("这是原文件当前配置的诊断，未证明续航可持续，也不能直接用它承诺改装后的实战提升；后续候选会核对常态条件与资源。")
    return "\n\n".join(rows)


def read_run(path):
    run = Path(path).resolve()
    state = read_json(run / "run.json")
    bx.require(state.get("schema_version") == SCHEMA_VERSION, "旧会话为历史记录；请在新目录 start，用有来源的用户意图生成 schema_version=2 并重新计算，不能直接导出旧候选。")
    bx.require(state.get("invoked") is True, "请先由用户明确调用 POE 配装助手。")
    original = run / "original.xml"
    bx.require(original.is_file() and bx.digest(original.read_bytes()) == state["original_sha256"], "原始快照发生变化。请用用户提供的文件建立新会话。")
    if state.get("intent"):
        bx.require(bx.digest(json.dumps(state["intent"],ensure_ascii=False,sort_keys=True,allow_nan=False))==state.get("intent_hash"),"已确认意图记录被直接修改；请恢复记录并用 intent 命令更新，不能沿用旧候选。")
    return run, state


def begin(args):
    bx.require(args.invoked, "仅在用户明确调用后传入 --invoked。")
    source = Path(args.input).resolve()
    document = bx.read_document(source)
    root = engine.resolve_root(args.pob)
    run = Path(args.run).resolve()
    engine.check_output_path(run / "run.json")
    if (run / "run.json").exists():
        _, state = read_run(run)
        bx.require(state["input_sha256"] == bx.digest(source.read_bytes()), "此会话已有其他角色，请使用新目录。")
        return {**workflow_status(argparse.Namespace(run=str(run),pob=str(root))),"already_exists":True}
    bx.require(not run.exists() or not any(run.iterdir()), "请选择空的会话目录，以免覆盖已有文件。")
    info=engine.health(root)
    run.mkdir(parents=True, exist_ok=True)
    original = bx.serialise(document)
    write_text(run / "original.xml", original)
    state = {"schema_version": SCHEMA_VERSION, "skill_version": VERSION, "invoked": True, "created_at": now(), "phase": "clarifying",
             "source_path": str(source), "input_sha256": bx.digest(source.read_bytes()), "original_sha256": bx.digest(original), **info}
    write_json(run / "run.json", state)
    inspection = bx.summary(document)
    write_json(run / "inspection.json", inspection)
    overview = {"class": inspection["build"].get("className"), "ascendancy": inspection["build"].get("ascendClassName"),
                "level": inspection["build"].get("level"), "tree_version": inspection["tree"].get("treeVersion"),
                "data_version": info["data_version"], "version_source_hint": info["version_source_hint"], "skill_groups": [
                    {"index": group["index"], "slot": group.get("slot"), "enabled": group.get("enabled"),
                     "gems": [{key: gem.get(key) for key in ("index", "skillId", "nameSpec", "level", "quality")} for gem in group["gems"]]}
                    for group in inspection["skill_groups"]]}
    return {"run": str(run), "phase": "clarifying", "inspection": str(run / "inspection.json"), "overview": overview,
            "next": "先结合以上角色信息逐轮确认目标、赛季版本、预算、改装范围和进度；其余装备原文在 inspection，宝石字段是 skillId/nameSpec。"}


def validate_intent(value, original, native=None, folder=None):
    allowed = {"schema_version", "confirmed", "goal", "objectives", "league", "game_version", "version_source", "version_checked_at", "mode", "budget", "target_level", "allow_class_change", "allow_ascendancy_change", "allow_main_skill_change", "keep_slots", "keep_skill_ids", "scenario", "baseline_main_group", "minimum_stats", "quest_passive_points", "ascendancy_points", "secondary_ascendancy_points", "normal_points_available", "max_changed_equipment", "constraint_sources", "evidence", "notes"}
    bx.require(value.get("schema_version") == SCHEMA_VERSION, "intent 使用 schema_version=2；旧默认策略字段需按 references/build.md 与 evidence.md 迁移。")
    bx.require(not set(value) - allowed, "意图包含未知/弃用字段：" + ", ".join(sorted(set(value) - allowed)))
    bx.require(value.get("confirmed") is True, "先归纳已表达的目标；confirmed 只记录流程，不代表程序独立验证用户表达。")
    for key in ("goal", "league"):
        bx.require(isinstance(value.get(key), str) and value[key].strip(), "需要明确的 " + key)
    if value.get("game_version"):
        engine.validate_game_version(value["game_version"], value["league"])
    if value.get("version_checked_at"): engine.validate_checked_at(value["version_checked_at"])
    defaults = {"allow_class_change": None, "allow_ascendancy_change": None, "allow_main_skill_change": None,
                "keep_slots": [], "keep_skill_ids": [], "scenario": {}, "minimum_stats": {}, "notes": "", "budget": None,
                "mode": None, "target_level": None, "quest_passive_points": None, "normal_points_available": None,
                "ascendancy_points": None, "secondary_ascendancy_points": None}
    result = {**defaults, **value}
    bx.require(result["mode"] in (None, "refine", "rebuild", "both"), "mode 为 refine/rebuild/both 或未知 null。")
    for key in ("allow_class_change", "allow_ascendancy_change", "allow_main_skill_change"):
        bx.require(result[key] is None or type(result[key]) is bool, key + " 为 true/false/null；未知不等于禁止。")
    budget = result["budget"]
    if budget is not None:
        bx.require(isinstance(budget, dict) and set(budget) == {"amount", "currency"}, "budget 需要 amount/currency；未确定时为 null。")
        bx.require(type(budget["amount"]) in (int, float) and math.isfinite(budget["amount"]) and budget["amount"] >= 0, "预算金额应为有限非负数。")
        bx.require(isinstance(budget["currency"], str) and budget["currency"], "预算需要通货单位。")
    for key in ("target_level", "quest_passive_points", "normal_points_available", "ascendancy_points", "secondary_ascendancy_points"):
        bx.require(result[key] is None or type(result[key]) is int and result[key] >= (1 if key == "target_level" else 0), key + " 为进度整数或 null，不套用固定赛季上限。")
    bx.require(isinstance(result["keep_slots"], list), "keep_slots 应为数组。")
    known_slots = {row["name"] for row in (native or {}).get("slots", [])}
    known_slots.update(row.get("name") for row in bx.active_set(original, "Items", "ItemSet", "activeItemSet").findall("Slot"))
    for entry in result["keep_slots"]:
        bx.require(isinstance(entry, dict) and set(entry) == {"slot", "preserve"} and entry["preserve"] in {"item_identity", "exact_spec"}, "keep_slots 每项为 {slot,preserve}；preserve=item_identity 或 exact_spec。")
        bx.require(entry["slot"] in known_slots, "当前已读数据未识别保留槽位：" + entry["slot"])
    bx.require(isinstance(result["keep_skill_ids"], list) and all(isinstance(v, str) and v for v in result["keep_skill_ids"]), "keep_skill_ids 应为 ID 数组。")
    bx.require(isinstance(result["minimum_stats"], dict), "minimum_stats 应为对象。")
    for key, val in result["minimum_stats"].items():
        bx.require(isinstance(key, str) and key and type(val) in (int, float) and math.isfinite(val), "约定最低指标必须是本机指标名和有限数值。")
    if result.get("max_changed_equipment") is not None:
        rule = result["max_changed_equipment"]
        bx.require(isinstance(rule, dict) and set(rule) == {"count", "basis"} and type(rule["count"]) is int and rule["count"] >= 0 and rule["basis"] in {"replaced_items", "changed_slots"}, "max_changed_equipment 需要 count 与 basis=replaced_items/changed_slots。")
    if result.get("baseline_main_group") is not None:
        bx.index_one(bx.groups(original), result["baseline_main_group"], "原角色主技能组")
    bx.set_config(copy.deepcopy(original), result["scenario"])
    records = facts.load_evidence(result.get("evidence", []), folder or Path.cwd(), {"league": result["league"]})
    facts.objective_coverage(result.get("objectives",[]),records,[])
    sources = result.get("constraint_sources", {})
    bx.require(isinstance(sources, dict), "constraint_sources 是约束字段到用户声明 evidence_id 的映射。")
    required = ["goal"] + [key for key in ("mode", "budget", "target_level", "quest_passive_points", "normal_points_available", "ascendancy_points", "secondary_ascendancy_points", "max_changed_equipment", "allow_class_change", "allow_ascendancy_change", "allow_main_skill_change", "baseline_main_group") if result.get(key) is not None]
    required += [key for key in ("keep_slots", "keep_skill_ids", "scenario", "minimum_stats") if result[key]]
    for key in required:
        ref = sources.get("/" + key)
        bx.require(ref in records and records[ref]["kind"] == "user_statement" and records[ref]["status"] == "user_declared", "明确限制 /" + key + " 需要 constraint_sources 中对应的用户表达；模型假设请放 plan。")
    result["constraint_sources"] = sources
    return result

def set_intent(args):
    run, state = read_run(args.run)
    root = engine.resolve_root(state["pob_path"])
    native = engine.calculate(root, catalog="rules", query="slots")
    intent = validate_intent(read_json(args.file), bx.read_document(run / "original.xml"), native, Path(args.file).resolve().parent)
    if intent.get("game_version"):
        engine.validate_game_version(intent["game_version"], intent["league"], native.get("data_version"))
    state["intent"] = intent
    state["intent_evidence_folder"] = str(Path(args.file).resolve().parent)
    state["intent_hash"] = bx.digest(json.dumps(intent, ensure_ascii=False, sort_keys=True, allow_nan=False))
    state["target_version_evidence"] = facts.version_fact(intent.get("version_source"), intent.get("game_version") or "", intent["league"], run / ".poe-evidence", native["engine_fingerprint"])
    state["phase"] = "designing"
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"run": str(run), "phase": state["phase"], "intent": intent, "intent_hash": state["intent_hash"], "target_version_evidence": state["target_version_evidence"], "next": "用本机事实与来源设计；未知进度只影响依赖它的检查，不能当零或满进度。"}

def prepared(original, intent, *, baseline=False, scenario=None):
    root = copy.deepcopy(original)
    common = dict(scenario or {})
    for key, value in intent.get("scenario", {}).items():
        bx.require(key not in common or common[key] == value, "比较场景覆盖了用户限定条件：" + key)
        common[key] = value
    bx.set_config(root, common)
    root.find("Build").set("characterLevelAutoMode", "false")
    if baseline and intent.get("baseline_main_group") is not None:
        root.find("Build").set("mainSocketGroup", str(intent["baseline_main_group"]))
    return root

def active_slot(name, result):
    slot = next((row for row in result.get("slots", []) if row["name"] == name), None)
    if slot is not None: return slot.get("active", True)
    # No native slot record means activity is unknown, not an inferred weapon layout.
    return True

def active_gems(result):
    return [gem for gem in result.get("gems", []) if gem.get("enabled") and gem.get("group_enabled") and active_slot(gem.get("slot", ""), result)]


def socket_layout_issues(document, result, unsupported=None):
    """Check physical links using the engine's sequential socket assignment."""
    errors = []
    equipped = {item["slot"]: item for item in result["equipped"]}
    texts = {int(item.get("id")): bx.text_item(item) for item in document.findall("Items/Item")}
    offsets, used_links = {}, {}
    for index, group in enumerate(bx.groups(document), 1):
        slot = group.get("slot", "")
        gems = [gem for gem in result["gems"] if gem["group"] == index]
        if group.get("source") or not slot or not gems:
            continue
        offset = offsets.get(slot, 0)
        offsets[slot] = offset + len(gems)
        item = equipped.get(slot)
        if not item:
            errors.append(f"第 {index} 组需要关联实际装备插槽；当前 {slot or '未指定'} 没有装备。")
            continue
        match = re.search(r"^Sockets:\s*(.+)$", texts.get(item["id"], ""), re.M)
        links = [re.findall(r"[RGBWAD]", text) for text in match[1].split()] if match else []
        physical = [(link_index, colour) for link_index, colours in enumerate(links) for colour in colours]
        assigned = physical[offset:offset + len(gems)]
        if len(assigned) != len(gems):
            errors.append(f"{slot} 插槽数量不足：第 {index} 组按原生顺序占用第 {offset+1}–{offset+len(gems)} 孔，装备只有 {len(physical)} 孔。移除不用的组或调整装备/技能位置。")
            continue
        if any(colour in "AD" for _, colour in assigned):
            if unsupported is not None:
                unsupported.append(f"capability:special_socket:{slot}:{index}")
            else:
                errors.append(f"{slot} 第 {index} 组使用特殊插槽，当前普通孔接口未覆盖，需进一步核对。")
        if len({link for link, _ in assigned}) > 1:
            errors.append(f"{slot} 连线不匹配：第 {index} 组跨越未相连的插槽；按装备孔位顺序拆分或重新连接，不能仅在 PoB 中写作一组。")
        has_support = any(gem.get("support") and gem.get("enabled") for gem in gems)
        for link in {link for link, _ in assigned}:
            previous = used_links.get((slot, link))
            if previous and (previous[1] or has_support):
                errors.append(f"{slot} 第 {previous[0]} 与第 {index} 组共享物理连线且含辅助；请合并为同一技能组，确保辅助、保留及消耗按真实连接计算。")
            used_links[(slot, link)] = (index, has_support)
    return errors


def mechanical_issues(document, result, intent):
    errors, warnings, gaps = [], list(result.get("warnings", [])), []
    stats = result["stats"]
    for node in result.get("granted_passives", []):
        if node["allocated_on_tree"]:
            warnings.append(f"节点 {node['id']} {node.get('display_name',node['name'])} 已在树上分配并被额外授予，不能算两份收益；是否调整由模型判断。")
    if intent.get("game_version") and result.get("data_version"):
        if intent["game_version"] != result["data_version"]: errors.append("目标版本与本机机制数据不一致。")
    elif not result.get("data_version"): gaps.append("version:data")
    if result.get("data_version") and result["tree_version"].split("_")[:2] != result["data_version"].split("."):
        gaps.append("version:tree")
    caps = result.get("capabilities", {})
    if not caps.get("native_gem_requirements") and result.get("gems"): gaps.append("capability:native_gem_requirements")
    if not caps.get("matching_socket_quality") and any(gem.get("slot") for gem in result.get("gems", [])): gaps.append("capability:matching_socket_quality")
    if not caps.get("excluded_guard_validation") and any(gem.get("enableGlobal1") == "false" for group in bx.groups(document) for gem in group.findall("Gem")):
        gaps.append("capability:excluded_guard_validation")
    for entry in bx.active_config(document).findall("Input"):
        name = entry.get("name", "")
        if name in {"customMods", "customModsExtra"}:
            gaps.append("config:" + name)
        if name not in result["config_keys"]:
            gaps.append("config:" + name)
            warnings.append("本机接口未识别配置：" + name)
        elif name in result["config_lists"]:
            allowed = result["config_lists"][name]
            if not allowed:
                gaps.append("config:" + name)
                continue
            value = None
            kinds = [key for key in ("string", "number", "boolean") if key in entry.attrib]
            if len(kinds) == 1:
                kind = kinds[0]
                if kind == "string": value = entry.get(kind)
                elif kind == "boolean" and entry.get(kind) in ("true", "false"): value = entry.get(kind) == "true"
                elif kind == "number":
                    try: value = float(entry.get(kind))
                    except ValueError: pass
            if not any((type(value) == type(option) or type(value) in (int,float) and type(option) in (int,float)) and value == option for option in allowed):
                errors.append(f"无效配置值：{name}={value!r}；请使用 catalog config 的 val：{allowed[:12]}")
    spec = bx.active_spec(document)
    for field, actual in (("classId","class_id"),("ascendClassId","ascendancy_id"),("secondaryAscendClassId","secondary_ascendancy_id")):
        raw = spec.get(field)
        if raw is None or raw == "nil" and field == "secondaryAscendClassId": continue  # Native unselected secondary sentinel, not an inferred ID.
        bx.require(raw.isdigit(),"XML 职业/升华编号无效："+field+"="+raw)
        expected = int(raw)
        if actual not in result:
            if expected: gaps.append("tree:" + field)
        elif result[actual] != expected: errors.append(f"引擎未保留天赋 {field}={expected}，实际为 {result[actual]}。")
    if result["level"] != int(document.find("Build").get("level")):
        errors.append("引擎调整了输入角色等级；当前原生接口不接受该等级。")
    requested_nodes = {int(n) for n in spec.get("nodes", "").split(",") if n}
    missing = requested_nodes - set(result["nodes"])
    if missing: errors.append("引擎未保留部分天赋节点：" + ",".join(map(str, sorted(missing))))
    for group in bx.groups(document):
        if not group.get("source"): continue
        for gem in group.findall("Gem"):
            retained = [entry for entry in result["gems"] if entry.get("source") == group.get("source") and entry.get("id") == gem.get("skillId")]
            if retained and not any(entry.get("slot", "") == group.get("slot", "") and entry["group_enabled"] == (group.get("enabled", "true") != "false") for entry in retained):
                errors.append("引擎改变了授予技能状态：" + group.get("source"))
    for skill in result.get("active_skills", []):
        if skill.get("trigger_target_required_level") and skill.get("trigger_level_limit") is not None and skill["trigger_target_required_level"] > skill["trigger_level_limit"]:
            errors.append(f"第 {skill['group']} 组 {skill.get('name')} 的需求等级超过触发辅助原生上限 {skill['trigger_level_limit']}。")
    def requirements(required, available, subject):
        if type(required) in (int,float) and required > 0:
            if type(available) not in (int,float): gaps.append(subject)
            elif required > available: errors.append(f"{subject} 需求 {required:g} 超过 {available:g}。")
    for gem in result["gems"]:
        if not gem.get("slot") and not gem.get("source"): continue
        label = f"gem:{gem['group']}:{gem['index']}"
        if not gem.get("recognised"): errors.append("未知宝石：" + label)
        if gem.get("support") and gem.get("enabled") and gem.get("group_enabled") and not (gem.get("support_applied") or gem.get("supports_excluded_guards")):
            warnings.append(label + " 未辅助启用技能；若是练级/备用用途需说明，不能声称它提供了辅助收益。")
        if gem.get("native_level_present") is False and not gem.get("source"): gaps.append(label + ":level")
        for req, available in (("req_level",result["level"]),("req_str",stats.get("Str")),("req_dex",stats.get("Dex")),("req_int",stats.get("Int"))):
            requirements(gem.get(req), available, label + ":" + req)
    equipped = {item["slot"]: item for item in result["equipped"]}
    expected = [(slot.get("name", ""), int(slot.get("itemId", "0"))) for slot in bx.active_set(document,"Items","ItemSet","activeItemSet").findall("Slot")]
    expected += [("Jewel " + slot.get("nodeId"), int(slot.get("itemId", "0"))) for slot in spec.findall("Sockets/Socket") if int(slot.get("nodeId","0")) in requested_nodes]
    for name, item_id in expected:
        if item_id and equipped.get(name, {}).get("id") != item_id:
            errors.append(f"{name} 的装备 {item_id} 未被引擎装入；不能把静默移除的装备当作生效。")
    for name, item in equipped.items():
        if not item.get("base_recognised"): errors.append(name + " 装备基底未被识别。")
        if item.get("valid_for_slot") is False: errors.append(name + " 的物品不满足原生槽位要求。")
        if active_slot(name, result):
            for req, available in (("level",result["level"]),("str",stats.get("Str")),("dex",stats.get("Dex")),("int",stats.get("Int"))):
                requirements(item.get("requirements",{}).get(req),available,"item:" + name + ":" + req)
        if item.get("unparsed"):
            warnings.append(name + " 存在未解析词缀：" + "；".join(item["unparsed"]))
            if not (item.get("slot", "").startswith("Flask ") and item.get("active") is False): gaps.append("item:" + str(item["id"]) + ":unparsed")
    errors.extend(socket_layout_issues(document,result,gaps))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)), list(dict.fromkeys(gaps))


def material_requirements(original, document, before, after):
    """Derive purchases/upgrades from actual XML, not a prose claim of ownership."""
    def gem_key(gem):
        return gem.get("id"), gem.get("level", 1), gem.get("quality", 0)
    stock = Counter(gem_key(gem) for gem in before["gems"] if not gem.get("source") and gem.get("slot"))
    needed = Counter()
    names = {}
    for gem in after["gems"]:
        if not gem.get("id") or gem.get("source") or not gem.get("slot"):
            continue
        key = gem_key(gem)
        needed[key] += 1
        names[key] = gem.get("name") or key[0]
    requirements = []
    for key, count in sorted((needed - stock).items()):
        skill, level, quality = key
        prior = sorted({(g[1], g[2]) for g in stock if g[0] == skill})
        requirements.append({"kind": "gem", "skill_id": skill, "level": level, "quality": quality, "quantity": count,
                             "description": f"{names[key]} {level} 级 / {quality} 品质 × {count}" + (f"（原 XML 记录等级/品质：{prior}，需练级、调品质或采购）" if prior else "（原 XML 未记录足量同规格宝石）")})
    old_texts = bx.item_texts(original)
    old_items = Counter(old_texts[item["id"]] for item in before["equipped"] if item["id"] in old_texts)
    item_texts = bx.item_texts(document)
    for item in after["equipped"]:
        text = item_texts.get(item["id"], "")
        if old_items[text]:
            old_items[text] -= 1
        else:
            requirements.append({"kind": "item", "item_id": item["id"], "quantity": 1,
                                 "spec_sha256": bx.digest(text),
                                 "description": f"{item['slot']}：{item['name']}（新物品或原物品改造，含孔色/连线变化）"})
    removed = set(before.get("nodes", [])) - set(after.get("nodes", []))
    if removed:
        requirements.append({"kind":"service","service_id":"passive_respec","quantity":len(removed),
                             "description":"移除已分配节点，需核对实际洗点资源；节点数不等于通货价格。"})
    grouped = {}
    for row in requirements:
        key = facts.material_ref(row)
        if key in grouped: grouped[key]["quantity"] += row["quantity"]
        else: grouped[key] = {**row, "requirement_id": key}
    return list(grouped.values())


def equipment_changes(original, document, before, after):
    """One native/XML slot comparison for constraints and generated explanations."""
    before_slots={row["slot"]:row for row in before["equipped"]}
    after_slots={row["slot"]:row for row in after["equipped"]}
    old_texts=bx.item_texts(original);new_texts=bx.item_texts(document)
    changed,replaced,rows=[],[],[]
    for slot in sorted(before_slots.keys()|after_slots.keys()):
        old,new=before_slots.get(slot),after_slots.get(slot)
        different=not old or not new or old_texts.get(old["id"])!=new_texts.get(new["id"])
        if different:
            changed.append(slot)
            rows.append({"slot":slot,"before":old,"after":new})
        if not old or not new or old["id"]!=new["id"] or old.get("base")!=new.get("base"): replaced.append(slot)
    return {"changed_slots":changed,"replaced_items":replaced,"changes":rows}


def check_constraints(original, document, before, after, intent, plan, equipment=None):
    errors, pending = [], []
    if intent.get("mode") not in (None,"both",plan["mode"]): errors.append("候选超出用户确认的改装模式。")
    if intent.get("target_level") is not None and after["level"] != intent["target_level"]: errors.append("候选等级与用户确认的目标等级不一致。")
    if after["tree_version"] != before["tree_version"]: errors.append("候选天赋版本发生变化，需原生迁移及新的版本依据，不能直接改 treeVersion。")
    for flag,field,label in (("allow_class_change","class","职业"),("allow_ascendancy_change","ascendancy","升华"),("allow_main_skill_change","main_skill","主技能")):
        if before[field] != after[field]:
            if intent.get(flag) is False: errors.append("用户明确要求保留"+label+"。")
            elif intent.get(flag) is None: pending.append("permission:"+flag)
    if before.get("secondary_ascendancy_id") != after.get("secondary_ascendancy_id"):
        if intent.get("allow_ascendancy_change") is False: errors.append("用户明确要求保留升华，副升华发生变化。")
        elif intent.get("allow_ascendancy_change") is None: pending.append("permission:allow_ascendancy_change")
    equipment=equipment if equipment is not None else equipment_changes(original,document,before,after)
    changed,replaced=equipment["changed_slots"],equipment["replaced_items"]
    for entry in intent.get("keep_slots",[]):
        slot=entry["slot"]
        if entry["preserve"]=="exact_spec" and slot in changed: errors.append("必须保留完整规格的装备被改动："+slot)
        elif entry["preserve"]=="item_identity":
            if slot in replaced: errors.append("必须保留实物的槽位更换了物品："+slot)
            elif slot in changed: pending.append("transformation:"+slot)
    rule=intent.get("max_changed_equipment")
    if rule:
        counted=replaced if rule["basis"]=="replaced_items" else changed
        if len(counted)>rule["count"]: errors.append(f"装备改动 {len(counted)} 处超过约定 {rule['count']} 处，口径 {rule['basis']}："+"、".join(sorted(counted)))
    enabled_ids={gem.get("id") for gem in active_gems(after)}
    for skill in intent.get("keep_skill_ids",[]):
        if skill not in enabled_ids: errors.append("必须保留的技能未启用："+skill)
    for key,threshold in intent.get("minimum_stats",{}).items():
        value=after["stats"].get(key)
        if type(value) not in (float,int): pending.append("metric:"+key)
        elif value<threshold: errors.append(f"{METRICS.get(key,key)} 为 {value:g}，低于约定 {threshold:g}。")
    for values in (plan.get("scenario",{}),plan.get("operations",{}).get("config",{})):
        for key,value in values.items():
            if key in intent.get("scenario",{}) and value != intent["scenario"][key]: errors.append("候选修改了用户限定场景："+key)
    if plan["mode"]=="rebuild" and not plan.get("migration_steps"): pending.append("migration")
    if plan.get("template_xml") and not plan.get("sources"): pending.append("template_source")
    return errors,pending

def budget_summary(plan, intent, requirements=None, evidence=None, claims=None):
    evidence,claims=evidence or {},claims or []
    entries,inventory=plan.get("shopping",[]),plan.get("inventory",[])
    bx.require(isinstance(entries,list) and isinstance(inventory,list),"shopping/inventory 应为数组。")
    target=intent.get("budget") or {}
    currency=target.get("currency")
    limit=target.get("amount")
    stocks,orders={},{}
    warnings=[]
    for group,rows in (("inventory",inventory),("shopping",entries)):
        for entry in rows:
            bx.require(isinstance(entry,dict) and "status" not in entry,"物资状态由程序生成；owned 使用 inventory 与用户声明，价格通过 evidence_refs 关联来源。")
            ref=facts.material_ref(entry)
            qty=entry.get("quantity")
            bx.require(type(qty) is int and qty>0,"物资 quantity 应为正整数。")
            refs=entry.get("evidence_refs",[])
            bx.require(isinstance(refs,list) and all(isinstance(v,str) for v in refs) and len(refs)==len(set(refs)),"物资 evidence_refs 应为不重复的引用数组。")
            records=[evidence[v] for v in refs if v in evidence and evidence[v].get("subject")==ref and evidence[v].get("status")!="unresolved"]
            if group=="inventory":
                bx.require(ref not in stocks,"相同规格库存请合并数量，不能重复抵消需求："+ref)
                declared=[r for r in records if r["kind"]=="user_statement" and type(r.get("context",{}).get("quantity")) is int and r["context"]["quantity"]>0 and (entry["kind"]!="item" or entry.get("spec_sha256") and r.get("context",{}).get("spec_sha256")==entry["spec_sha256"])]
                stocks[ref]={**entry,"status":"user_declared" if sum(r["context"]["quantity"] for r in declared)>=qty else "unknown"}
                continue
            bx.require(ref not in orders,"相同规格购物记录请合并数量："+ref)
            row={**entry,"status":"unknown"}
            lo,hi=entry.get("low"),entry.get("high")
            if lo is not None or hi is not None:
                bx.require(type(lo) in (int,float) and type(hi) in (int,float) and math.isfinite(lo) and math.isfinite(hi) and 0<=lo<=hi,"价格 low/high 是该条全部 quantity 总价，需为有效有限区间。")
                row["status"]="estimate"
                # The source is observed; matching its meaning to this price remains an explicit model assessment.
                matching=[r for r in records if r.get("status") in {"source_observed","artifact_observed"} and r.get("checked_at") and r.get("context",{}).get("realm")=="cn" and r.get("context",{}).get("league")==intent.get("league") and r.get("context",{}).get("currency")==entry.get("currency") and r.get("context",{}).get("quantity")==qty and r.get("context",{}).get("low")==lo and r.get("context",{}).get("high")==hi and (entry["kind"]!="item" or entry.get("spec_sha256") and r.get("context",{}).get("spec_sha256")==entry["spec_sha256"])]
                if matching and any(c["status"]=="model_assessed" and "quote:"+ref in c["subjects"] and set(c["evidence_refs"]) & {r["evidence_id"] for r in matching} for c in claims): row["status"]="quote_assessed"
                if currency is None: currency=entry.get("currency")
                if not currency or entry.get("currency")!=currency:
                    row["status"]="unknown"
                    warnings.append(ref+" 通货未与预算统一，未做无依据换算。")
            orders[ref]=row
    materials=[]
    unresolved=[]
    for need in requirements or []:
        ref=facts.material_ref(need)
        stock=stocks.get(ref,{})
        if need["kind"]=="item" and stock.get("spec_sha256")!=need.get("spec_sha256"): stock={}
        owned=min(need["quantity"],stock.get("quantity",0)) if stock.get("status")=="user_declared" else 0
        remaining=need["quantity"]-owned
        order=orders.get(ref,{})
        if need["kind"]=="item" and order.get("spec_sha256")!=need.get("spec_sha256"): order={}
        satisfied=remaining==0 or order.get("quantity",0)>=remaining and order.get("status")=="quote_assessed"
        if not satisfied: unresolved.append(ref)
        materials.append({**need,"requirement_id":ref,"declared_owned":owned,"remaining":remaining,"status":"user_declared" if remaining==0 else order.get("status","unknown"),"evidence_refs":stock.get("evidence_refs",[]) + order.get("evidence_refs",[])})
    low=high=known_low=0
    any_unknown=False
    for ref,row in orders.items():
        if row["status"]=="unknown": any_unknown=True; continue
        low+=row["low"]; high+=row["high"]
        applicable=next((n for n in requirements or [] if facts.material_ref(n)==ref and (n["kind"]!="item" or n.get("spec_sha256")==row.get("spec_sha256"))),None)
        if row["status"]=="quote_assessed" and applicable: known_low+=row["low"]
    estimated=any(row["status"]=="estimate" for row in orders.values())
    over=limit is not None and known_low>limit
    crossing=limit is not None and low<=limit<high
    if over: status="over_budget"
    elif unresolved or any_unknown: status="unknown"
    elif estimated or crossing: status="uncertain_range"
    elif orders: status="quote_assessed"
    elif materials: status="user_declared"
    else: status="no_additional_cost"
    if status in {"unknown","uncertain_range"}: warnings.append("成本或物资尚未充分对应；不能承诺预算内买齐。")
    if status=="quote_assessed": warnings.append("价格根据实际来源片段及模型匹配判断，非成交保证，购买前复核。")
    return {"status":status,"low":low,"high":high,"known_lower_bound":known_low,"totals_complete":not unresolved and not any_unknown,
            "currency":currency,"limit":limit,"over_budget":over,"range_crosses_limit":crossing,"unresolved":unresolved,"warnings":warnings,"materials":materials,"shopping":list(orders.values()),"inventory":list(stocks.values())}

def compare(before, after, comparable=True):
    result={}
    keys=before["stats"].keys() & after["stats"].keys()
    for key in sorted(keys):
        a,b=before["stats"][key],after["stats"][key]
        if type(a) in (float,int) and type(b) in (float,int) and math.isfinite(a) and math.isfinite(b):
            result[key]={"before":a,"after":b,"delta":b-a,"percent":(b-a)/abs(a)*100 if a and comparable else None,"scope":"native_snapshot"}
    return result

def semantics(result):
    return {"class": result["class"], "ascendancy": result["ascendancy"], "secondary_ascendancy_id": result.get("secondary_ascendancy_id"), "level": result["level"], "tree_version": result["tree_version"],
            "config_values": result.get("config_saved_values", result["config_values"]),
            "nodes": result["nodes"], "main_skill": result["main_skill"], "main_group": result["main_group"],
            "gems": [{key: gem.get(key) for key in ("group", "index", "id", "source", "level", "quality", "enabled", "group_enabled", "slot", "matches_socket", "socket_quality_bonus", "count", "group_count", "skill_part", "skill_stage_count", "skill_minion", "main_active_skill", "include_in_full_dps")} for gem in result["gems"]],
            "equipped": [{key: item.get(key) for key in ("slot", "id", "name", "base", "active")} for item in result["equipped"]]}


def roundtrip(root, result, document):
    saved = bx.preserve_extensions(document, bx.parse(result["xml"]))
    xml = bx.serialise(saved)
    reloaded = engine.calculate(root, xml, cache=False)
    bx.require(semantics(result) == semantics(reloaded), "XML 回读后配置、装备、天赋或技能发生变化，未通过导出检查。")
    for key in result["stats"].keys() | reloaded["stats"].keys():
        a, b = result["stats"].get(key), reloaded["stats"].get(key)
        if type(a) not in (int,float) and type(b) not in (int,float):
            bx.require(a == b, "XML 回读后的状态不一致：" + key)
            continue
        if a is None and b is None: continue
        bx.require(isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-5), "XML 回读后的计算结果不一致：" + key)
    # Subsequent evidence describes the saved file's native representation.
    for key in ("config_values", "config_defaults", "config_saved_values", "config_labels"):
        if key in reloaded: result[key] = reloaded[key]
    return xml


def materialize_items(plan, root):
    """Translate optional ordinary rare recipes using the installed modifier pool."""
    recipes = []
    for item in plan.get("operations", {}).get("items", []):
        if "craft" not in item:
            continue
        bx.require(set(item) == {"id", "craft"} and type(item["id"]) is int and item["id"] > 0, "配方物品只接受 id 与 craft，不能混入 text。")
        spec = item["craft"]
        bx.require(isinstance(spec, dict) and set(spec) <= {"base", "ilvl", "quality", "sockets", "affixes"}, "craft 仅接受 base/ilvl/quality/sockets/affixes。")
        bx.require(isinstance(spec.get("base"), str) and spec["base"].strip(), "craft.base 使用 catalog bases 返回的完整基底名。")
        bx.require(type(spec.get("ilvl")) is int and spec["ilvl"] > 0, "craft.ilvl 应为正整数；原生词缀与获取依据分别核对。")
        bx.require(type(spec.get("quality", 0)) is int and spec.get("quality", 0) >= 0, "craft.quality 应为非负整数，取得方式须有依据。")
        sockets = spec.get("sockets", "")
        bx.require(isinstance(sockets, str) and (not sockets or re.fullmatch(r"[RGBW](?:[- ][RGBW])*", sockets)), "craft.sockets 只接受普通孔，例如 W-W-W-W 或 R-R G-G。")
        affixes = spec.get("affixes")
        bx.require(isinstance(affixes, list) and bool(affixes), "craft.affixes 应为非空数组；数量由本机物品实例校验。")
        for affix in affixes:
            bx.require(isinstance(affix, dict) and set(affix) <= {"id", "roll"} and isinstance(affix.get("id"), str), "词缀配方需要 catalog affixes 返回的 id，可选 roll。")
            roll = affix.get("roll", 0.5)
            bx.require(type(roll) in (float, int) and math.isfinite(roll) and 0 <= roll <= 1, "roll 是词缀范围内位置 0–1，默认 0.5，不是实际数值。")
        recipes.append({"id": item["id"], **spec})
    if not recipes:
        return plan, []
    bx.require(len({spec["id"] for spec in recipes}) == len(recipes), "不能有重复配方物品 ID。")
    audit = []
    for offset in range(0,len(recipes),10):
        audit.extend(engine.calculate(root, crafts=recipes[offset:offset+10])["crafted_items"])
    generated = {item["id"]: item["text"] for item in audit}
    resolved = copy.deepcopy(plan)
    resolved["operations"]["items"] = [{"id": item["id"], "text": generated[item["id"]]} if "craft" in item else item for item in resolved["operations"]["items"]]
    return resolved, audit


def calculation_facts(result, claims=()):
    referenced = set()
    for claim in claims:
        for ref in claim.get("evidence_refs", []):
            if "#/stats/" in ref: referenced.add(ref.split("#/stats/", 1)[1])
    return {"xml_sha256": result.get("xml_sha256"), "engine_fingerprint": result.get("engine_fingerprint"),
            "level": result.get("level"), "class": result.get("class"), "ascendancy": result.get("ascendancy"),
            "points":result.get("points"),"ascendancy_points":result.get("ascendancy_points"),"secondary_ascendancy_points":result.get("secondary_ascendancy_points"),
            "main_skill": result.get("main_skill"), "main_skill_name": result.get("main_skill_name"),
            "config_values": result.get("config_values", {}), "config_labels": result.get("config_labels", {}),
            "stats": {k: v for k, v in result.get("stats", {}).items() if k in set(METRICS) | referenced},
            "active_skills": result.get("active_skills", []), "damage_sources": result.get("damage_sources", []),
            "equipped":result.get("equipped",[]),"resource_rules":result.get("resource_rules",{}),
            "scope": "当前配置与所列主体的原生快照；参考技能必须用其 skill_ref，主技能面板不能代替副技能或阶段。"}


def assess_candidate(original, document, baseline, result, intent, plan, evidence_folder, intent_evidence_folder=None, *, root=None):
    """One assessment path for evaluation and final export; scopes stay explicit."""
    context = {"engine_fingerprint": result["engine_fingerprint"], "league": intent["league"], "game_version": intent.get("game_version")}
    evidence = facts.load_evidence(intent.get("evidence", []), intent_evidence_folder or evidence_folder, context, root=root)
    plan_evidence = facts.load_evidence(plan.get("evidence", []), evidence_folder, context, root=root)
    bx.require(not (evidence.keys() & plan_evidence.keys()),"intent 和 plan 的 evidence_id 不能重复。")
    evidence.update(plan_evidence)
    claims = facts.assess_claims(plan.get("claims", []), evidence, {"baseline": baseline, "candidate": result})
    coverage,coverage_pending=facts.objective_coverage(intent.get("objectives",[]),evidence,claims)
    errors, warnings, native_gaps = mechanical_issues(document, result, intent)
    mechanical_errors = list(errors)
    equipment=equipment_changes(original,document,baseline,result)
    constraint_errors, constraint_pending = check_constraints(original, document, baseline, result, intent, plan,equipment)
    # Permission and absent native measurements cannot be supplied by a model conclusion.
    constraint_pending = [p for p in constraint_pending if p.startswith(("permission:", "metric:")) or not facts.has_claim(claims,p)]
    errors += constraint_errors
    audit = resources.assess(result, intent, plan, claims)
    errors += audit["errors"]
    warnings += audit["warnings"]
    requirements = material_requirements(original, document, baseline, result)
    budget = budget_summary(plan, intent, requirements, evidence, claims)
    if budget["over_budget"]: errors.append("已知采购成本下界超过用户预算。")
    warnings += budget["warnings"]
    pending = native_gaps + constraint_pending + audit["pending"] + coverage_pending
    pending += ["material:"+value for value in budget["unresolved"]]
    if budget["status"] in {"unknown", "uncertain_range"}: pending.append("budget")
    if intent.get("budget") and budget["high"] > intent["budget"]["amount"]: pending.append("budget:upper_bound")
    for item in plan.get("operations", {}).get("items", []):
        if not facts.has_claim(claims, "legality:item:"+str(item["id"])): pending.append("legality:item:"+str(item["id"]))
    for key in plan.get("scenario", {}):
        if key not in intent.get("scenario", {}) and not facts.has_claim(claims,"config:"+key): pending.append("config:"+key)
    assessment = plan.get("assessment", {})
    bx.require(isinstance(assessment,dict) and set(assessment) <= {"goal_claim_ids","scope","unresolved","review"}, "assessment 接受 goal_claim_ids/scope/unresolved/review。")
    for key in ("goal_claim_ids", "unresolved"):
        bx.require(isinstance(assessment.get(key,[]),list) and all(isinstance(v,str) and v for v in assessment.get(key,[])), "assessment."+key+" 应为字符串数组。")
    goal_ids = assessment.get("goal_claim_ids", [])
    goal_claims = {c["claim_id"]:c for c in claims if "goal" in c["subjects"] and c["status"]=="model_assessed"}
    if not goal_ids or any(key not in goal_claims for key in goal_ids) or not assessment.get("scope"): pending.append("goal_assessment")
    pending += ["assessment:"+v for v in assessment.get("unresolved", [])]
    version = facts.version_fact(intent.get("version_source"), intent.get("game_version"), intent["league"], evidence_folder / ".poe-evidence", result["engine_fingerprint"])
    if version["status"] != "source_observed": pending.append("version:target")
    comparison_notes = []
    for key,label in (("level","角色等级"),("class","职业"),("main_skill","主技能口径"),("config_values","战斗配置")):
        if baseline.get(key) != result.get(key): comparison_notes.append(label+"不同。")
    for key in ("GuardSkillActive",):
        if baseline["stats"].get(key) != result["stats"].get(key): comparison_notes.append(key+"开启条件不同。")
    def flasks(r): return sorted(i["slot"] for i in r.get("equipped",[]) if i["slot"].startswith("Flask ") and i.get("active"))
    if flasks(baseline) != flasks(result): comparison_notes.append("药剂开启条件不同。")
    if baseline.get("warnings"): warnings.append("原方案存在原生警告，需按具体内容审查受影响结论；警告本身不等于所有指标不可比。")
    if any(r["projection"] != "native_full_speed" for r in audit["resources"]): comparison_notes.append("使用节奏调整未重算伤害，数值只代表原生快照。")
    if comparison_notes: warnings.append("前后只展示数值差，不声明同条件提升百分比。")
    review, review_pending = facts.final_review({"intent": intent, "plan": plan}, evidence, claims,
        {"baseline": calculation_facts(baseline, claims), "candidate": calculation_facts(result, claims),
         "resources": audit, "materials": requirements, "budget": budget, "objective_coverage":coverage, "equipment_changes":equipment,"comparison_notes": comparison_notes,
         "pending": list(dict.fromkeys(pending)), "errors": errors}, assessment)
    pending += review_pending
    errors, pending = list(dict.fromkeys(errors)), list(dict.fromkeys(pending))
    diagnostics=list(audit.get("diagnostics",[]))
    for claim in claims:
        diagnostics.extend({**row,"claim_id":claim["claim_id"],"category":"input","status":"unknown"} for row in claim["diagnostics"])
    for objective in coverage:
        diagnostics.extend({**row,"subject":objective["required_subject"],"category":"input","status":"unknown"} for row in objective["diagnostics"])
    for category,messages in (("mechanical",mechanical_errors),("user_constraint",constraint_errors),("usage",audit["errors"]),("budget",["已知采购成本下界超过用户预算。"] if budget["over_budget"] else [])):
        for message in messages:
            diagnostics.append({"code":category+"_violation","category":category,"subject":"candidate","evidence_refs":[],"affects":["delivery"],"message":message,"status":"failed"})
    for subject in pending:
        diagnostics.append({"code":"unresolved_dependency","category":"capability" if subject.startswith(("capability:","native:")) else "evidence","subject":subject,
                            "evidence_refs":[ref for claim in claims if subject in claim["subjects"] for ref in claim["evidence_refs"]],"affects":["delivery",subject],"message":"需要补足对应事实、条件或用户意图："+subject,"status":"unknown"})
    status="blocked" if errors else "draft" if pending else "scoped_result"
    return {"status":status,"delivery_brief":facts.delivery_brief(status,errors,pending), "errors":errors,"pending":pending,"warnings":list(dict.fromkeys(warnings)),
            "validation":{"mechanical":"failed" if mechanical_errors else "unknown" if native_gaps else "passed",
                          "user_constraints":"failed" if constraint_errors else "unknown" if constraint_pending else "passed",
                          "usage":"failed" if audit["errors"] else "unknown" if audit["pending"] else "model_assessed",
                          "target_version":version["status"], "goal":"model_assessed" if "goal_assessment" not in pending else "unresolved",
                          "scope":"程序核对结构、可用原生事实和证据引用；目标取舍、来源语义、效果覆盖仍是模型判断，不是实战保证。"},
            "configuration_audit":{"original_inputs":[dict(n.attrib) for n in bx.active_config(original).findall("Input")],"common_changes":plan.get("scenario",{}),"user_required":intent.get("scenario",{}),"candidate_changes":plan.get("operations",{}).get("config",{}),"baseline_actual":baseline.get("config_values",{}),"candidate_actual":result.get("config_values",{})},
            "diagnostics":diagnostics,"budget":budget,"readiness":audit,"objective_coverage":coverage,"claims":claims,"evidence":evidence,"target_version_evidence":version,
            "review_packet":review,
            "equipment_changes":equipment,"actual_changes":actual_changes(baseline,result,equipment),"comparison_notes":comparison_notes,"comparison":compare(baseline,result,not comparison_notes)}


def evaluate(args):
    run, state = read_run(args.run)
    bx.require(state.get("intent"), "先通过多轮对话确认意图，并执行 intent。")
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称请使用 1–48 位小写英文、数字或连字符。")
    intent, plan_path = state["intent"], Path(args.plan).resolve()
    source_plan_sha256=bx.digest(plan_path.read_bytes())
    plan = read_json(plan_path)
    bx.require(plan.get("schema_version")==SCHEMA_VERSION,"plan 使用 schema_version=2，旧策略字段请按 references/build.md 与 evidence.md 迁移。")
    original = bx.read_document(run / "original.xml")
    root = engine.resolve_root(args.pob or state["pob_path"])
    info = engine.check(root)
    engine.validate_game_version(intent.get("game_version"), intent["league"], info.get("data_version"))
    native = engine.calculate(root, catalog="rules")
    scenario = plan.get("scenario", {})
    context_hash = bx.digest(json.dumps({"intent":state["intent_hash"], "scenario":scenario,"engine":info["engine_fingerprint"],"skill":VERSION},sort_keys=True,ensure_ascii=False,allow_nan=False))
    base_path = run / "baseline.json"
    cached = read_json(base_path) if base_path.exists() else {}
    if (cached.get("context_hash")==context_hash and cached.get("skill_version")==VERSION and cached.get("engine_fingerprint")==info["engine_fingerprint"]
            and bx.digest(base_path.read_text(encoding="utf-8"))==state.get("baseline_sha256")):
        baseline=cached
    else:
        baseline=engine.calculate(root,bx.serialise(prepared(original,intent,baseline=True,scenario=scenario)))
        baseline.update(context_hash=context_hash,skill_version=VERSION,intent_hash=state["intent_hash"])
        write_json(base_path,baseline)
    state["baseline_sha256"]=bx.digest(base_path.read_text(encoding="utf-8"))
    source=original
    if plan.get("template_xml"):
        path=Path(plan["template_xml"])
        source=bx.read_document(path if path.is_absolute() else plan_path.parent/path)
    resolved, crafted = materialize_items(plan,root)
    document=bx.apply_plan(prepared(source,intent,scenario=scenario),resolved,native=native)
    result=engine.calculate(root,bx.serialise(document))
    bx.require(result["engine_fingerprint"]==baseline["engine_fingerprint"]==native["engine_fingerprint"]==info["engine_fingerprint"],"计算期间引擎内容发生变化，请更新完成后重算。")
    missing_nodes=sorted({int(v) for v in bx.active_spec(document).get("nodes","").split(",") if v}-set(result["nodes"]))
    tree_diagnostics=[]
    if missing_nodes:
        queried=native_queries(root,[{"kind":"nodes","query":"id:"+str(v),"limit":1} for v in missing_nodes],result["xml"])
        bx.require(queried["engine_fingerprint"]==result["engine_fingerprint"],"天赋诊断期间引擎变化，请重算。")
        for node_id,row in zip(missing_nodes,queried["catalog_batch"]):
            node=next((e for e in row["entries"] if e["id"]==node_id),None)
            tree_diagnostics.append({"id":node_id,"found":node is not None,**({k:node[k] for k in ("name","ascendancy_name","reachable","points_to_allocate","path") if k in node} if node else {})})
    xml=roundtrip(root,result,document)
    result["xml_sha256"]=bx.digest(xml)
    assessment=assess_candidate(original,document,baseline,result,intent,plan,plan_path.parent,Path(state.get("intent_evidence_folder",str(plan_path.parent))),root=root)
    bx.require(bx.digest(plan_path.read_bytes())==source_plan_sha256,"计算期间源计划变化，请重新 evaluate。",location="/source_plan",input_file=str(plan_path))
    destination=run/"candidates"/args.name
    requested=bx.serialise(document)
    write_text(destination/"requested.xml",requested)
    write_text(destination/"candidate.xml",xml)
    write_json(destination/"plan.json",plan)
    outcome={"schema_version":SCHEMA_VERSION,"skill_version":VERSION,"name":plan.get("name",args.name),"evaluated_at":now(),
             "engine_version":result["engine_version"],"engine_fingerprint":result["engine_fingerprint"],"intent_hash":state["intent_hash"],
             "original_sha256":state["original_sha256"],"xml_sha256":bx.digest(xml),"requested_sha256":bx.digest(requested),"roundtrip_verified":True,
             "source_plan":str(plan_path),"source_plan_sha256":source_plan_sha256,
             "evidence_folder":str(plan_path.parent),"baseline":baseline,"result":{k:v for k,v in result.items() if k!="xml"},
             "plan":plan,"crafted_items":crafted,"tree_diagnostics":tree_diagnostics,**assessment}
    write_json(destination/"evaluation.json",outcome)
    write_json(destination/"review.json",outcome["review_packet"])
    state.setdefault("candidates",{})[args.name]={"evaluation_sha256":bx.digest((destination/"evaluation.json").read_text(encoding="utf-8"))}
    state.update(phase="evaluated",updated_at=now(),engine_fingerprint=result["engine_fingerprint"])
    write_json(run/"run.json",state)
    return {"candidate":str(destination),"evaluation_file":str(destination/"evaluation.json"),"status":outcome["status"],"errors":outcome["errors"],"pending":outcome["pending"],"warnings":outcome["warnings"],
            "budget":outcome["budget"],"readiness":outcome["readiness"],"roundtrip_verified":True,"delivery_brief":outcome["delivery_brief"],
            "comparison":{k:v for k,v in outcome["comparison"].items() if k in METRICS},"baseline_warnings":baseline.get("warnings",[]),
            "tree_diagnostics":tree_diagnostics,"review_file":str(destination/"review.json"),"review_token":outcome["review_packet"]["token"],
            "next":"按 evaluation.json 修订；review.json 集中列出最终对象、数值、规格和引用。审查后在源 plan.assessment.review 填 token/findings/unresolved，再 evaluate；方案改变会作废旧复核。不要自动复制 token 代替审查。"}

def actual_changes(before, after, equipment=None):
    """Use recalculated objects for the delivery summary, never stale plan prose."""
    rows = []
    for key, label in (("class", "职业"), ("ascendancy", "升华"), ("main_skill_name", "主技能"), ("level", "角色等级")):
        old = before.get(key + "_cn", before.get(key))
        new = after.get(key + "_cn", after.get(key))
        if old != new: rows.append(f"{label}：{old} → {new}。")
    def gem_set(result):
        return Counter((gem.get("display_name", gem.get("name", gem.get("id", "未识别宝石"))), gem.get("slot", ""), gem.get("level", 1), gem.get("quality", 0)) for gem in active_gems(result) if not gem.get("source"))
    old_gems, new_gems = gem_set(before), gem_set(after)
    for label, difference in (("新增或调整后启用", new_gems - old_gems), ("移出原位置或停用", old_gems - new_gems)):
        for (name, slot, level, quality), count in difference.items():
            rows.append(f"{label}：{name} · {slot} · {level} 级/{quality} 品质 ×{count}。")
    added, removed = set(after["nodes"]) - set(before["nodes"]), set(before["nodes"]) - set(after["nodes"])
    if added or removed:
        rows.append(f"天赋实际增加 {len(added)} 个节点、移除 {len(removed)} 个节点；含升华，具体分配见 XML。")
    for key,label in (("points","普通天赋"),("ascendancy_points","升华"),("secondary_ascendancy_points","次级升华")):
        old,new=before.get(key),after.get(key)
        if type(old) is int and type(new) is int and old!=new: rows.append(f"{label}实际已分配：{old} → {new} 点（变化 {new-old:+d}）；各类点数分别计数。")
    for change in (equipment or {}).get("changes",[]):
        old,new=change["before"],change["after"]
        rows.append(f"装备/规格：{change['slot']}：{old['name'] if old else '空槽'} → {new['name'] if new else '空槽'}。")
    return rows or ["核心职业、主技能、在用宝石与天赋没有变化；装备和配置差异见后文。"]


def scenario_summary(result):
    """Describe the native calculation input, including engine-provided defaults."""
    values = result["config_values"]
    boss = values.get("enemyIsBoss")
    label = result["config_labels"].get("enemyIsBoss")
    enemy = f"{label}（{boss}）" if label else str(boss) if boss is not None else "未提供（未推断内部默认规则）"
    return f"敌人类型：{enemy}；敌人等级参数：{values.get('enemyLevel', '未提供（按引擎内部规则）')}。"


def report_html(state, outcome):
    esc=lambda value:html.escape(str(value))
    show=lambda value:"未知" if value is None else f"{value:,.2f}" if type(value) in (int,float) else esc(value)
    plan,result,budget=outcome["plan"],outcome["result"],outcome["budget"]
    brief=outcome.get("delivery_brief") or facts.delivery_brief(outcome["status"],outcome["errors"],outcome["pending"])
    status=brief["label"]
    def bullets(values): return "<ul>"+"".join("<li>"+esc(v)+"</li>" for v in values)+"</ul>"
    displayed=set(METRICS)|set(state["intent"].get("minimum_stats",{}))
    rows="".join(f"<tr><td>{esc(METRICS.get(key,key))}</td><td>{show(row['before'])}</td><td>{show(row['after'])}</td><td>{show(row['delta'])}</td></tr>" for key,row in outcome["comparison"].items() if key in displayed)
    resources="".join(f"<tr><td>{esc(row['resource'])}</td><td>{show(row['pool'])}</td><td>{show(row['single_cost'])}</td><td>{show(row['cost_per_second'])}</td><td>{show(row['regeneration'])}</td><td>{show(row['leech_on_hit'])}</td><td>{show(row['net_cost'])}</td></tr>" for row in outcome["readiness"]["resources"])
    materials="".join(f"<tr><td>{esc(row['requirement_id'])}</td><td>{row['quantity']}</td><td>{row['declared_owned']}</td><td>{esc(row['status'])}</td></tr>" for row in budget["materials"])
    claims="".join(f"<li><b>{esc(c['claim_id'])}</b> [{esc(c['status'])}] {esc(c['rendered_text'])}{bullets(c['conditions']+c['uncertainties'])}<small>依据：{esc(', '.join(c['evidence_refs']))}</small></li>" for c in outcome["claims"])
    objectives=bullets([row['text']+'：'+row['status']+'；判断 '+', '.join(row['claim_ids']) for row in outcome['objective_coverage']])
    evidence="".join(f"<li>{esc(key)}：{esc(row['status'])}；{esc(row.get('origin',''))}；{esc(row.get('excerpt',row.get('text',row.get('value',''))))} {('<a href='+chr(34)+esc(row['url'])+chr(34)+'>来源</a>') if isinstance(row.get('url'),str) and row['url'].startswith(('https://','http://')) else ''}</li>" for key,row in outcome["evidence"].items())
    groups={}
    for gem in result["gems"]: groups.setdefault(gem["group"],[]).append(gem)
    damage=esc(json.dumps(result.get("damage_sources",[]),ensure_ascii=False,indent=2))
    links=bullets([f"{gems[0].get('slot') or '未装备'} / {'参与计算' if gems[0].get('group_enabled') else '不参与计算但非空槽仍占孔'}："+" — ".join(str(g.get('display_name',g.get('name',g.get('id'))))+f" {g.get('level')}/{g.get('quality')}" for g in gems) for gems in groups.values()])
    detail=esc(json.dumps({"usage":plan.get("usage",[]),"resources":outcome["readiness"]["resources"],"config":result.get("config_values",{}),"assessment":plan.get("assessment",{}),"budget":budget,"crafted_items":outcome.get("crafted_items",[]),"xml_sha256":outcome["xml_sha256"],"engine_fingerprint":outcome["engine_fingerprint"]},ensure_ascii=False,indent=2))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(outcome['name'])}</title>
<style>body{{font:16px/1.7 system-ui;background:#f5f6f8;color:#202a38;max-width:1050px;margin:auto;padding:28px}}section{{background:white;padding:24px;margin:18px 0;border-radius:12px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}small{{color:#526174}}h1{{margin-bottom:6px}}</style>
<h1>{esc(outcome['name'])}</h1><p>{esc(status)} · PoECharm {esc(outcome['engine_version'])}</p>
<section><h2>目标与结论</h2><p>{esc(state['intent']['goal'])}</p><p>{esc(brief['message'])}</p>{objectives}<p>模型方案摘要（以当前校验和所列条件为限）：{esc(plan['summary'])}</p><p>{esc(outcome['validation']['scope'])}</p>{bullets(outcome['errors']+outcome['pending']+outcome['warnings'])}</section>
<section><h2>实际改动与准备</h2>{bullets(outcome['actual_changes'])}{bullets(plan.get('migration_steps',[]))}<p>{esc(scenario_summary(result))}</p>{bullets(outcome['comparison_notes'])}</section>
<section><h2>原生计算数值</h2><p>下表为当前配置快照；使用频率或覆盖时间改变后，未重算的 DPS 不能作为持续输出。</p><table><tr><th>指标</th><th>原方案</th><th>候选</th><th>数值差</th></tr>{rows}</table></section>
<section><h2>技能与资源</h2>{links}<table><tr><th>资源</th><th>可用池</th><th>单次费用</th><th>每秒费用</th><th>净秒回</th><th>命中吸取</th><th>净消耗</th></tr>{resources}</table><p>吸取需要命中；充能、时序与额外操作的条件见依据及明细。未知项不按零计算。</p></section>
<section><h2>原生 Full DPS 分项</h2><p>只包括本机当前实际计入的技能与数量；数量与覆盖条件须与上方模型判断对应。</p><pre>{damage}</pre></section>
<section><h2>物资与预算</h2><p>状态：{esc(budget['status'])}；{'完整所列报价' if budget['totals_complete'] else '部分已列金额，缺项未计入'} {show(budget['low'])}–{show(budget['high'])} {esc(budget['currency'] or '通货待定')}；用户预算 {show(budget['limit'])}。持有声明不等于已独立盘点；报价不保证成交。</p><table><tr><th>新增需求</th><th>数量</th><th>声明持有</th><th>状态</th></tr>{materials}</table></section>
<section><h2>判断与证据</h2><p>最终模型复核：{esc(outcome.get('review_packet',{}).get('status','unresolved'))}。引用存在不代表文字与因果已被程序证明；复核未完成时，以下说明仍待审查。</p><p>{esc(outcome.get('review_packet',{}).get('findings') or '尚无对应本次方案的模型复核记录。')}</p><ul>{claims}</ul><details><summary>证据索引</summary><ul>{evidence}</ul></details><details><summary>配置、操作与成本明细</summary><pre>{detail}</pre></details></section>
<section><h2>使用</h2><p>在 PoECharm 中打开同目录 XML。草案需先处理上方缺口；按准备条件逐项落实后再在游戏内切换。文件不会修改服务器角色。</p></section></html>'''

def read_candidate(args):
    run,state=read_run(args.run)
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}",args.name),"候选名称无效。")
    folder=run/"candidates"/args.name
    raw=(folder/"evaluation.json").read_text(encoding="utf-8")
    bx.require(bx.digest(raw)==state.get("candidates",{}).get(args.name,{}).get("evaluation_sha256"),"候选记录被修改或未完成计算，请重新 evaluate。")
    outcome=json.loads(raw)
    bx.require(outcome.get("schema_version")==SCHEMA_VERSION and outcome.get("skill_version")==VERSION,"协议或技能已变化，请重新 evaluate。")
    bx.require(outcome["intent_hash"]==state.get("intent_hash") and outcome["original_sha256"]==state["original_sha256"],"意图或原始角色变化，请重算。")
    source_plan=Path(outcome["source_plan"])
    bx.require(source_plan.is_file() and bx.digest(source_plan.read_bytes())==outcome["source_plan_sha256"],"源计划被修改或移动，请使用源计划重新 evaluate。",location="/source_plan",input_file=str(source_plan))
    xml=(folder/"candidate.xml").read_text(encoding="utf-8")
    bx.require(bx.digest(xml)==outcome["xml_sha256"],"候选 XML 被修改，请重新 evaluate。")
    bx.require(bx.digest((folder/"requested.xml").read_text(encoding="utf-8"))==outcome["requested_sha256"],"请求 XML 被修改，请重新 evaluate。")
    root=engine.resolve_root(args.pob or state["pob_path"])
    bx.require(engine.check(root)["engine_fingerprint"]==outcome["engine_fingerprint"],"引擎、数据或适配器变化，请重新 evaluate。")
    return run,state,outcome,xml,root

def probe(args):
    run,state,outcome,xml,root=read_candidate(args)
    source=bx.parse(xml)
    baseline=engine.calculate(root,xml)
    if getattr(args,"node_ids",None):
        ids=[int(v) for v in args.node_ids.split(",")]
        bx.require(ids and len(set(ids))==len(ids) and all(v>0 for v in ids),"--node-ids 使用不同的正整数节点 ID。")
        screened=engine.calculate(root,xml,node_probes=[[v] for v in ids])
        bx.require(screened["engine_fingerprint"]==outcome["engine_fingerprint"],"筛选期间引擎变化。")
        return {"status":"screened","engine_fingerprint":screened["engine_fingerprint"],"xml_sha256":bx.digest(xml),"alternatives":screened["node_screening"],
                "next":"仅为原生节点词缀筛选，不含连接路径、点数或组合认证；选定完整路径后用 --variants 实算，再 evaluate。"}
    if getattr(args,"variants",None):
        request=read_json(args.variants)
        bx.require(set(request)=={"variants"},"比较文件只接受 variants。")
        variants=request["variants"]
    else:
        bx.require(getattr(args,"ids",None) and getattr(args,"group",None) and getattr(args,"index",None),"使用 --variants，或同时提供 --ids/--group/--index。")
        ids=[v.strip() for v in args.ids.split(",")]
        bx.require(ids and len(set(ids))==len(ids) and all(re.fullmatch(r"[A-Za-z0-9_]+",v) for v in ids),"--ids 需要不同的本机 skill_id。")
        group=bx.index_one(bx.groups(source),args.group,"技能组")
        bx.require(not group.get("source"),"来源技能请修改实际装备/天赋来源。")
        bx.index_one(group.findall("Gem"),args.index,"宝石")
        variants=[]
        for skill_id in ids:
            change={"group":args.group,"index":args.index,"skill_id":skill_id}
            change.update({k:getattr(args,k) for k in ("level","quality") if getattr(args,k,None) is not None})
            variants.append({"id":skill_id,"operations":{"gems":[change]}})
    bx.require(isinstance(variants,list) and variants,"variants 需要非空数组。")
    seen=set()
    for variant in variants:
        bx.require(isinstance(variant,dict) and set(variant)=={"id","operations"} and isinstance(variant["id"],str) and variant["id"] and variant["id"] not in seen,"每个变体使用唯一 id 和完整 operations。")
        seen.add(variant["id"])
    metrics=getattr(args,"metrics",None)
    metrics=metrics.split(",") if metrics else list(METRICS)
    bx.require(all(k and isinstance(k,str) for k in metrics),"metrics 使用本次数值输出中的指标名。")
    dps_metric="MinionCombinedDPS" if any(skill.get("is_main") and skill.get("minion") for skill in baseline.get("active_skills",[])) else "CombinedDPS"
    original=bx.read_document(run/"original.xml")
    rows=[]
    for offset in range(0,len(variants),12):
        prepared_variants=[]
        for variant in variants[offset:offset+12]:
            try:
                plan={"mode":outcome["plan"]["mode"],"summary":"候选局部或组合比较","operations":variant["operations"]}
                resolved,crafts=materialize_items(plan,root)
                document=bx.apply_plan(source,resolved,native=baseline)
                prepared_variants.append((variant,document,plan,crafts))
            except (ValueError,KeyError,TypeError,IndexError) as exc:
                rows.append({"id":variant["id"],"status":"blocked","errors":[str(exc)]})
        if not prepared_variants: continue
        inputs=[bx.serialise(v[1]) for v in prepared_variants]
        try:
            batch=engine.calculate(root,xml_batch=inputs)
            calculations=batch["calculations"]
        except engine.EngineError:
            calculations=[]
            for text in inputs:
                try: calculations.append(engine.calculate(root,text))
                except engine.EngineError as exc: calculations.append({"failure":str(exc)})
        for (variant,document,plan,crafts),result in zip(prepared_variants,calculations):
            if "failure" in result:
                rows.append({"id":variant["id"],"status":"blocked","errors":[result["failure"]]}); continue
            bx.require(result["engine_fingerprint"]==outcome["engine_fingerprint"],"比较期间引擎变化，请重算。")
            errors,warnings,pending=mechanical_issues(document,result,state["intent"])
            ce,cp=check_constraints(original,document,outcome["baseline"],result,state["intent"],plan)
            audit=resources.assess(result,state["intent"],plan,[])
            context_changes=[k for k in ("config_values","level","class","main_skill") if baseline.get(k)!=result.get(k)]
            comparison=compare(baseline,result,not context_changes)
            rows.append({"id":variant["id"],**({"skill_id":variant["id"]} if not getattr(args,"variants",None) else {}),"status":"blocked" if errors+ce+audit["errors"] else "screened",
                         "engine_fingerprint":result["engine_fingerprint"],"xml_sha256":result["input_xml_sha256"],"operations":variant["operations"],"crafted_items":crafts,
                         "stats":{k:result["stats"].get(k) for k in metrics},"comparison":{k:v for k,v in comparison.items() if k in metrics},"context_changes":context_changes,
                         "dps_metric":dps_metric,"dps_delta":comparison.get(dps_metric,{}).get("delta"),"allocated_points":{k:result[k] for k in ("points","ascendancy_points","secondary_ascendancy_points")},
                         "errors":errors+ce+audit["errors"],"pending":pending+cp+audit["pending"],"warnings":warnings+audit["warnings"],"readiness":audit})
    return {"status":"screened","alternatives":rows,"next":"各变体均为独立完整原生实算；组合必须单列变体，不能相加单项收益。未继承旧操作/证据判断，不改候选，不证明预算、路径可得或全局最优；选定后更新完整 plan 再 evaluate。"}


def export(args):
    run,state,outcome,xml,root=read_candidate(args)
    folder=run/"candidates"/args.name
    output=run/"output"/args.name
    state.update(phase="exporting",selected_candidate=args.name,updated_at=now())
    write_json(run/"run.json",state)
    write_json(output/"delivery.json",{"file_exported":False,"status":"preparing","candidate":args.name})
    verification=engine.calculate(root,xml,cache=False)
    verification["xml_sha256"]=bx.digest(xml)
    bx.require(verification["engine_fingerprint"]==outcome["engine_fingerprint"],"最终复算期间引擎变化。")
    bx.require(semantics(verification)==semantics(outcome["result"]),"最终复算角色状态与候选记录不同，请重算。")
    for key in verification["stats"].keys()|outcome["result"]["stats"].keys():
        a,b=verification["stats"].get(key),outcome["result"]["stats"].get(key)
        equal=math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-5) if type(a) in (int,float) and type(b) in (int,float) else a==b
        bx.require(equal,"最终复算数值变化："+key)
    requested=bx.read_document(folder/"requested.xml")
    assessment=assess_candidate(bx.read_document(run/"original.xml"),requested,outcome["baseline"],verification,state["intent"],outcome["plan"],Path(outcome["evidence_folder"]),Path(state.get("intent_evidence_folder",outcome["evidence_folder"])),root=root)
    outcome.update(assessment)
    outcome["result"]={k:v for k,v in verification.items() if k!="xml"}
    write_json(output/"final-assessment.json",outcome)
    write_json(output/"review.json",outcome["review_packet"])
    write_text(output/"配装说明.html",report_html(state,outcome))
    if outcome["status"]=="blocked":
        for old_name in ("配装草案.xml","改进配装.xml"):
            (output/old_name).unlink(missing_ok=True)
        write_json(output/"delivery.json",{"file_exported":False,"status":"blocked","candidate":args.name})
        state.update(phase="evaluated",updated_at=now()); write_json(run/"run.json",state)
        return {"status":"blocked","file_exported":False,"delivery_brief":outcome["delivery_brief"],"files":[str(output/"配装说明.html")],"errors":outcome["errors"],"pending":outcome["pending"],"next":"已生成诊断说明；先解决已知矛盾并重新 evaluate。"}
    filename="配装草案.xml" if outcome["status"]=="draft" else "改进配装.xml"
    write_text(output/filename,xml)
    (output/("改进配装.xml" if filename=="配装草案.xml" else "配装草案.xml")).unlink(missing_ok=True)
    write_json(output/"delivery.json",{"file_exported":True,"status":outcome["status"],"candidate":args.name,
               "files":{name:bx.digest((output/name).read_bytes()) for name in (filename,"配装说明.html")},"review_token":outcome["review_packet"]["token"]})
    state.update(phase="exported",selected_candidate=args.name,updated_at=now())
    write_json(run/"run.json",state)
    stats=verification["stats"]
    selected=("MinionCombinedDPS",) if any(s.get("is_main") and s.get("minion") for s in verification.get("active_skills",[])) else ("CombinedDPS",)
    values="；".join(f"{METRICS[k]} {stats[k]:,.1f}" for k in selected+("Life","EnergyShield","ChaosResist") if type(stats.get(k)) in (int,float))
    summary=outcome["delivery_brief"]["message"]+"已导出 XML，通过本机计算、保存回读与最终复算。\n\n"+scenario_summary(verification)+"\n"+values+"。以上为原生配置快照。\n\n预算状态："+outcome["budget"]["status"]+"。"
    if outcome["pending"]:
        summary+="未解决项："+"、".join(outcome["pending"][:12])+"。"
        if len(outcome["pending"])>12: summary+=f"另有 {len(outcome['pending'])-12} 项见说明及 final-assessment.json。"
    if outcome["comparison_notes"]: summary+="前后计算有口径差异，不提供同条件提升百分比。"
    if outcome["budget"]["status"] in {"unknown","uncertain_range"}: summary+="成本或物资缺口未补齐，不能承诺预算内买齐。"
    if any(s.get("is_main") and s.get("generals_cry") for s in verification.get("active_skills",[])): summary+="幻影吸取未计入玩家回复，供尸、触发与操作条件见说明。"
    return {"status":outcome["status"],"file_exported":True,"delivery_brief":outcome["delivery_brief"],"files":[str(output/filename),str(output/"配装说明.html")],"facts_summary":summary,
            "facts":outcome["review_packet"]["facts"],"objective_coverage":outcome["objective_coverage"],
            "pending":outcome["pending"],"budget_status":outcome["budget"]["status"],"warnings":outcome["warnings"],
            "reply_policy":"直接交付返回的文件，保留草案文件名；先沿用 delivery_brief/facts_summary 的真实状态和全部缺口类别，再解释 claims[].rendered_text 中有依据的取舍。最终聊天与HTML保持同一结论；不把复核token或回读当机制、预算或目标达成证明。"}

def native_queries(root,queries,xml="",class_id=None):
    bx.require(isinstance(queries,list) and queries,"queries 应为非空数组。")
    result=None
    rows=[]
    for offset in range(0,len(queries),30):
        chunk=engine.calculate(root,xml,queries=queries[offset:offset+30],class_id=class_id)
        if result is not None: bx.require(chunk["engine_fingerprint"]==result["engine_fingerprint"],"分批查询期间引擎变化，请重试。")
        else: result=chunk
        rows.extend(chunk["catalog_batch"])
    result["catalog_batch"]=rows
    return result


def catalog(args):
    root=engine.resolve_root(args.pob)
    class_id=getattr(args,"class_id",None)
    bx.require(class_id is None or not args.input,"--class-id 不能与 --input 混用。")
    kinds={"gems","skills","nodes","uniques","config","links","classes","bases","affixes","rules","metrics"}
    document=bx.serialise(bx.read_document(Path(args.input))) if args.input else ""
    if getattr(args,"batch",None):
        queries=read_json(args.batch).get("queries")
        bx.require(isinstance(queries,list) and queries,"queries 应为非空数组。")
        for query in queries:
            bx.require(isinstance(query,dict) and set(query)<={"kind","query","limit","offset"} and query.get("kind") in kinds,"查询使用 kind/query/limit/offset；translations 单独查询。")
            bx.require(isinstance(query.get("query",""),str) and type(query.get("limit",20)) is int and 1<=query.get("limit",20)<=100 and type(query.get("offset",0)) is int and query.get("offset",0)>=0,"limit=1–100，offset 为非负整数。")
        result=native_queries(root,queries,document,class_id)
        return {k:result.get(k) for k in ("engine_version","tree_version","data_version","engine_fingerprint","tree_context","socket_limits","rules")} | {"queries":result["catalog_batch"]}
    bx.require(args.kind,"catalog 需要 --kind 或 --batch。")
    offset=getattr(args,"offset",0)
    bx.require(type(offset) is int and offset>=0,"offset 应为非负整数。")
    if args.kind=="translations":
        entries=[]
        folder=root.parent/"Data"/"Translate"/"zh-rCN"
        bx.require(folder.is_dir(),"此安装没有 PoECharm 简体中文翻译数据。")
        for path in sorted(folder.glob("*.csv"),key=lambda p:(p.name!="Items_Gems.txt.csv",p.name)):
            with path.open(encoding="utf-8-sig",errors="replace",newline="") as stream:
                for row in csv.reader(stream):
                    if len(row)>=2 and any(args.query.casefold() in cell.casefold() for cell in row[:2]): entries.append({"english":row[0],"chinese":row[1],"source":path.name})
        selected=entries[offset:offset+args.limit]
        return {"entries":selected,"total":len(entries),"offset":offset,"next_offset":offset+len(selected) if offset+len(selected)<len(entries) else None}
    result=engine.calculate(root,document,catalog=args.kind,query=args.query,limit=args.limit,offset=offset,class_id=class_id)
    return {k:result.get(k) for k in ("engine_version","tree_version","data_version","engine_fingerprint","tree_context","socket_limits","rules")} | {"total":result.get("catalog_total"),"entries":result.get("catalog",[]),"offset":result.get("catalog_offset"),"next_offset":result.get("catalog_next_offset")}

def research(args):
    output = Path(args.out).resolve()
    bx.require(output.suffix.lower() == ".json", "research --out 请使用工作区 JSON 文件。")
    root = engine.resolve_root(args.pob)
    if args.trials:
        bx.require(not args.league and not args.source, "research --trials 不使用 --league / --source；先完成版本核对。")
        info = engine.check(root)
        evidence = facts.trial_facts(output.parent / ".poe-evidence", info["engine_fingerprint"], engine.require_data_version(info))
        record = {"trial_evidence": evidence, "engine_fingerprint": info["engine_fingerprint"]}
        write_json(output, record)
        return {"file": str(output), **record, "next": "按实际剧情试炼进度安排升华；area_level 是区域等级，不是最低进入等级。仍须核对第四次迷宫、祭品和安全通关条件。"}
    if args.version:
        bx.require(args.league and args.league.strip(), "research --version 需要 --league，使用本次用户确认的赛季或永久区。")
        info = engine.check(root)
        data_version = engine.require_data_version(info)
        source = args.source or (info.get("version_source_hint") or {}).get("url", "")
        engine.validate_game_version(data_version, args.league, data_version, source)
        evidence = facts.version_fact(source, data_version, args.league, output.parent / ".poe-evidence", info["engine_fingerprint"])
        record = {"league": args.league, "game_version": data_version, "engine_fingerprint": info["engine_fingerprint"], "version_evidence": evidence}
        write_json(output, record)
        return {"file": str(output), **record, "next": "阅读正文摘录核对目标赛季；unverified 时联网寻找官方公告，再加 --source URL 重试。不要读取工具内部输出目录或把缺失依据改成 verified。"}
    bx.require(not args.league and not args.source, "--league / --source 仅用于 research --version。")
    ids = args.gems.split(",")
    bx.require(ids and len(set(ids)) == len(ids) and all(re.fullmatch(r"[A-Za-z0-9_]+", value) for value in ids), "--gems 需要不同的本机 skill_id；程序自动分批。")
    calculation = native_queries(root, [{"kind": "gems", "query": "id:" + value, "limit": 2} for value in ids])
    gems = {entry["skill_id"]: entry for query in calculation["catalog_batch"] for entry in query["entries"] if entry["skill_id"] in ids}
    bx.require(set(gems) == set(ids), "部分宝石 ID 未识别，请先 catalog gems 查询。")
    evidence = facts.gem_facts(gems, output.parent / ".poe-evidence", calculation["engine_fingerprint"], calculation.get("data_version"), calculation["classes"])
    write_json(output, {"gems": evidence, "engine_fingerprint": calculation["engine_fingerprint"], "data_version": calculation["data_version"]})
    return {"file": str(output), "source_checked": [key for key, value in evidence.items() if value["status"] == "source_checked"],
            "unverified": [key for key, value in evidence.items() if value["status"] != "source_checked"],
            "next": "读取 gems[skill_id].native 的技能原文、使用门槛和 level_requirements[].base_resources 基础资源，以及 choices 的网页章节/任务/NPC/职业。基础数据不等于完整连线实算；两类依据不能互相替代。已返回字段无需重复 catalog。未核实条目需另查，不能自填 verified。"}


def route_snapshots(route,native,root,folder,output):
    """Materialize declared key stages through the same XML and native interfaces."""
    records={}
    for stage_index,stage in enumerate(route["stages"]):
        if "snapshot" not in stage: continue
        sid=stage["stage_id"]; snap=stage["snapshot"]
        record={"status":"unsupported","assumptions":snap.get("assumptions",[]),"scope":"仅代表所声明装备、天赋、宝石与配置的条件快照，不是保证按时取得或安全通关。"}
        records[sid]=record
        try:
            if stage.get("level") is None:
                record["reason"]="未确认角色等级，不能生成精确实算。"; continue
            if snap.get("template_xml"):
                path=Path(snap["template_xml"])
                source=bx.read_document(path if path.is_absolute() else folder/path)
            else:
                source=bx.parse(native["xml"])
            operations=copy.deepcopy(snap.get("operations",{}))
            op_path=f"/stages/{stage_index}/snapshot/operations"
            bx.require_keys(operations,{"items","equip","tree","config","main_group","weapon_set","gems","groups"},op_path,"阶段等级和基础技能列表来自 stage；snapshot.operations 不能另设 level/replace_skills。")
            for index,change in enumerate(operations.get("gems",[])):
                bx.require_keys(change,set(bx.GEM_FIELDS)-{"skill_id","level","quality","enabled"}|{"group","index"},op_path+f"/gems/{index}","阶段宝石身份/等级/品质/启用来自 stage；快照 gems 仅补计数、部位等状态。")
            for index,change in enumerate(operations.get("groups",[])):
                bx.require_keys(change,{"index","main_active_skill","include_in_full_dps","group_count"},op_path+f"/groups/{index}","阶段组位置/启用来自 stage；快照 groups 仅补主主动、计数和总伤害选择。")
            tree=operations.setdefault("tree",{})
            bx.require("class_id" not in tree or tree["class_id"]==route["class_id"],"快照职业与路线不一致；该值由路线填入。",location=op_path+"/tree/class_id",expected=route["class_id"],actual=tree.get("class_id"))
            tree["class_id"]=route["class_id"]
            selected=next(c for c in native["classes"] if c["id"]==route["class_id"])
            asc=next((a for a in selected["ascendancies"] if a["name"]==route.get("ascendancy")),None)
            asc_id=asc["id"] if stage["ascendancy_allocated_nodes"] and asc else 0
            bx.require("ascendancy_id" not in tree or tree["ascendancy_id"]==asc_id,"快照升华与本阶段路线不一致；该值由当前阶段已分配节点推导。",location=op_path+"/tree/ascendancy_id",expected=asc_id,actual=tree.get("ascendancy_id"))
            tree["ascendancy_id"]=asc_id
            if "nodes" in tree: tree["nodes"]=sorted(set(tree["nodes"])|set(stage["ascendancy_allocated_nodes"]))
            else: tree["nodes_add"]=sorted(set(tree.get("nodes_add",[]))|set(stage["ascendancy_allocated_nodes"]))
            operations["level"]=stage["level"]
            operations["replace_skills"]=[{"label":g["name"],"slot":g["slot"],"enabled":g.get("enabled",True),"gems":[{k:v[k] for k in ("skill_id","level","quality") if k in v} for v in g["gems"]]} for g in stage["skill_groups"]]
            plan={"mode":"rebuild","summary":"路线阶段条件快照","operations":operations}
            resolved,crafts=materialize_items(plan,root)
            document=bx.apply_plan(source,resolved,native=native)
            directory=output.parent/(output.stem+".calculations")/bx.digest(sid)[:12]
            requested=bx.serialise(document)
            cache_path=directory/"snapshot.json"
            calculated=None
            if cache_path.is_file() and (directory/"snapshot.xml").is_file():
                try:
                    cached=read_json(cache_path)
                    saved=(directory/"snapshot.xml").read_text(encoding="utf-8")
                    candidate=cached["result"]
                    if (cached["request_sha256"]==bx.digest(requested) and candidate["engine_fingerprint"]==native["engine_fingerprint"]
                            and candidate["xml_sha256"]==bx.digest(saved) and cached["result_sha256"]==bx.digest(json.dumps(candidate,sort_keys=True,ensure_ascii=False,allow_nan=False))):
                        engine.validate_contract(candidate)
                        calculated=candidate
                except (OSError,ValueError,KeyError,TypeError): pass
            if calculated is None:
                calculated=engine.calculate(root,requested)
                bx.require(calculated["engine_fingerprint"]==native["engine_fingerprint"],"路线阶段计算期间引擎变化。")
                saved=roundtrip(root,calculated,document)
                calculated["xml_sha256"]=bx.digest(saved)
                write_text(directory/"snapshot.xml",saved)
                write_json(cache_path,{"request_sha256":bx.digest(requested),"result":calculated,
                                      "result_sha256":bx.digest(json.dumps(calculated,sort_keys=True,ensure_ascii=False,allow_nan=False))})
            record.update(status="calculated",xml_path=str(directory/"snapshot.xml"),xml_sha256=bx.digest(saved),engine_fingerprint=calculated["engine_fingerprint"],
                          result=calculated,document=document,directory=directory,crafted_items=crafts)
        except (ValueError,KeyError,TypeError,IndexError,OSError) as exc:
            record.update(status="unsupported",reason=str(exc))
            if isinstance(exc,bx.ContractError): record["diagnostic"]=dict(exc.diagnostic)
        finally:
            progress=read_json(output.with_suffix(".state.json"))
            write_json(output.with_suffix(".state.json"),{**progress,"phase":"calculating","file_exported":False,"output":str(output),
                       "engine_fingerprint":native["engine_fingerprint"],"stages":{sid:{k:v for k,v in row.items() if k in {"status","reason","xml_path","xml_sha256"}} for sid,row in records.items()},
                       "next":"处理具体错误后重跑同一 route 命令；已保存阶段按输入和摘要复用。"})
    return records


def route_command(args):
    bx.require(args.invoked,"仅在用户明确调用技能后规划路线。")
    path=Path(args.file).resolve(); output=Path(args.out).resolve()
    bx.require(output.suffix.lower()==".html" and output!=path,"--out 需要独立 HTML 文件。")
    input_sha256=bx.digest(path.read_bytes())
    route=read_json(path)
    root=engine.resolve_root(args.pob)
    write_json(output.with_suffix(".state.json"),{"phase":"preparing","file_exported":False,"input":str(path),"input_sha256":input_sha256,"output":str(output),"engine_fingerprint":engine.fingerprint(root)})
    ids=routes.gem_ids(route)
    queries=[{"kind":"gems","query":"id:"+v,"limit":2} for v in ids]
    queries += [{"kind":"links","query":v,"limit":1} for v in routes.link_queries(route)]
    queries += [{"kind":"bases","query":"id:"+v,"limit":2} for v in routes.base_names(route)]
    calculation=native_queries(root,queries,class_id=route.get("class_id"))
    catalogs=calculation["catalog_batch"]
    gems={e["skill_id"]:e for q in catalogs if q["kind"]=="gems" for e in q["entries"]}
    links={q["query"]:q["entries"][0] for q in catalogs if q["kind"]=="links"}
    bases={e["name"]:e for q in catalogs if q["kind"]=="bases" for e in q["entries"]}
    nodes=[]; offset=0
    if route.get("ascendancy"):
        while offset is not None:
            page=engine.calculate(root,catalog="nodes",query=route["ascendancy"],limit=100,offset=offset,class_id=route.get("class_id"))
            bx.require(page["engine_fingerprint"]==calculation["engine_fingerprint"],"路线查询期间引擎发生变化。")
            nodes+=page["catalog"]; offset=page.get("catalog_next_offset")
    context={"engine_fingerprint":calculation["engine_fingerprint"],"league":route.get("league"),"game_version":route.get("game_version")}
    registry=facts.load_evidence(route.get("evidence",[]),path.parent,context,root=root)
    claims=facts.assess_claims(route.get("claims",[]),registry,{"catalog":calculation})
    checked,warnings=routes.validate(route,gems,calculation.get("data_version"),calculation["classes"],links,nodes,bases=bases,slots=calculation["slots"],evidence=registry,claims=claims)
    snapshots=route_snapshots(checked,calculation,root,path.parent,output)
    if snapshots:
        native={"catalog":calculation,**{"stage:"+sid:r["result"] for sid,r in snapshots.items() if "result" in r}}
        claims=facts.assess_claims(route.get("claims",[]),registry,native)
        checked,warnings=routes.validate(route,gems,calculation.get("data_version"),calculation["classes"],links,nodes,bases=bases,slots=calculation["slots"],evidence=registry,claims=claims)
    route_errors=[]
    stage_materials={}
    for stage in checked["stages"]:
        sid=stage["stage_id"]
        for item in stage.get("required_items",[]): stage_materials[facts.material_ref(item)]=item
        if sid not in snapshots: continue
        record=snapshots[sid]
        if "result" not in record:
            stage["calculation"]=record
            checked["pending"].append("native:snapshot:"+sid)
            if record.get("diagnostic",{}).get("action")=="edit_input": route_errors.append(sid+"："+record["reason"])
            continue
        value=record.pop("result"); document=record.pop("document"); directory=record.pop("directory")
        errors,notes,gaps=mechanical_issues(document,value,checked)
        if value["ascendancy_points"]!=stage["ascendancy_used"]: errors.append("快照实际升华点数与阶段路径不一致。")
        actual_bases={r["slot"]:r["base"] for r in value["equipped"]}
        for slot,base in stage.get("bases",{}).items():
            if actual_bases.get(slot)!=base: errors.append("快照装备与阶段声明的基底不一致："+slot)
        relevant=[c for c in claims if {"stage:"+sid,"snapshot:"+sid}&set(c["subjects"])]
        audit=resources.assess(value,checked,{"usage":stage["snapshot"].get("usage",[])},relevant)
        errors+=audit["errors"]; notes+=audit["warnings"]; gaps+=audit["pending"]
        texts=bx.item_texts(document)
        material_specs=[]
        for item in value["equipped"]:
            need=stage_materials.get("item:"+str(item["id"]),{})
            spec=bx.digest(texts.get(item["id"],""))
            material_specs.append({"kind":"item","item_id":item["id"],"slot":item["slot"],"name":item["name"],"spec_sha256":spec,"quantity":1})
            if need.get("spec_sha256")!=spec: gaps.append("material:item:"+str(item["id"]))
        condition_checks,condition_gaps=facts.entry_checks(stage.get("entry_checks",[]),value,relevant,"stage:"+sid)
        gaps+=condition_gaps
        metrics=stage["snapshot"].get("metrics",list(METRICS))
        bx.require(isinstance(metrics,list) and all(isinstance(k,str) and k for k in metrics),"snapshot.metrics 应为指标名数组。")
        missing=[k for k in metrics if k not in value["stats"]]
        gaps += ["metric:"+k for k in missing]
        record.update(status="blocked" if errors else "draft" if gaps else "calculated",native_calculated=True,stats={k:value["stats"].get(k) for k in metrics},errors=errors,pending=gaps,
                      config_values=value["config_values"],readiness=audit,main_skill=value["main_skill"],active_skills=value["active_skills"],roundtrip_verified=True,
                      material_specs=material_specs,entry_checks=condition_checks,facts=calculation_facts(value,relevant))
        write_json(directory/"evaluation.json",{**record,"native":{k:v for k,v in value.items() if k!="xml"}})
        record["evidence_path"]=str(directory/"evaluation.json")
        stage["calculation"]=record
        route_errors += [sid+"："+error for error in errors]
        checked["pending"] += ["stage:"+sid+":"+gap for gap in gaps]
        warnings+=notes
    fact_folder=path.parent/".poe-evidence"
    gem_evidence=facts.gem_facts(gems,fact_folder,calculation["engine_fingerprint"],calculation.get("data_version"),calculation["classes"])
    acquisition_warnings,unresolved=facts.reconcile_acquisitions(checked,gem_evidence)
    warnings+=acquisition_warnings; checked["pending"]+=unresolved
    trials=facts.trial_facts(fact_folder,calculation["engine_fingerprint"],calculation.get("data_version")) if any(s["ascendancy_used"] for s in checked["stages"]) else {"status":"not_required"}
    tw,tm=facts.reconcile_trial_progress(checked,trials); warnings+=tw
    if tm: checked["pending"].append("trial_progress")
    version=facts.version_fact(route.get("version_source"),route.get("game_version"),route["league"],fact_folder,calculation["engine_fingerprint"])
    if version["status"]!="source_observed": checked["pending"].append("version:target")
    checked["pending"]=list(dict.fromkeys(checked["pending"]))
    checked["fact_evidence"]={"gems":gem_evidence,"trials":trials,"version":version}
    requirements={}
    for stage in checked["stages"]:
        for item in stage.get("required_items",[]):
            ref=facts.material_ref(item)
            if ref in requirements: requirements[ref]["quantity"]+=item["quantity"]
            else: requirements[ref]=copy.deepcopy(item)
    # A route's explicit held-instance records are already inventory declarations.
    # Reuse their evidence instead of making the model duplicate the same input.
    owned={}
    for stage in checked["stages"]:
        for item in stage.get("required_items",[]):
            if item.get("evidence_status")!="user_declared": continue
            ref=facts.material_ref(item)
            if ref not in owned: owned[ref]={k:v for k,v in item.items() if k in {"kind","skill_id","level","quality","item_id","service_id","spec_sha256","quantity","evidence_refs"}}
            else:
                owned[ref]["quantity"]+=item["quantity"]
                owned[ref]["evidence_refs"]=sorted(set(owned[ref].get("evidence_refs",[])+item.get("evidence_refs",[])))
    declared={facts.material_ref(item):item for item in route.get("inventory",[])}
    budget=budget_summary({**route,"inventory":list({**owned,**declared}.values())},route,list(requirements.values()),registry,claims)
    checked["budget_assessment"]=budget
    if route.get("budget") is not None:
        if budget["status"] in {"unknown","uncertain_range"} or budget["high"]>route["budget"]["amount"]: checked["pending"].append("budget")
    warnings+=budget["warnings"]
    checked["pending"]=list(dict.fromkeys(checked["pending"]+["material:"+ref for ref in budget["unresolved"]]))
    if budget["over_budget"]: route_errors.append("已知采购成本下界超过用户预算。")
    review,review_pending=facts.final_review(route,registry,claims,
        {"engine_fingerprint":calculation["engine_fingerprint"],"budget":budget,"pending":checked["pending"],"errors":route_errors,"objective_coverage":checked["objective_coverage"],
         "stages":[{"stage_id":s["stage_id"],"level":s.get("level"),"act":s.get("act"),"completed_act":s.get("completed_act"),
                    "entry_conditions":s.get("entry_conditions",[]),"calculation":s.get("calculation",{"status":"not_calculated"})} for s in checked["stages"]]},route.get("assessment",{}))
    checked["pending"]+=review_pending
    checked["final_review"]={k:review[k] for k in ("token","status","findings","scope")}
    checked["errors"]=list(dict.fromkeys(route_errors))
    checked["delivery_status"]="blocked" if route_errors else "draft" if checked["pending"] else "scoped_result"
    checked["delivery_brief"]=facts.delivery_brief(checked["delivery_status"],checked["errors"],checked["pending"])
    stage_summary=[{"stage_id":s["stage_id"],"name":s["name"],"status":s.get("calculation",{}).get("status","not_calculated"),
                    "native_calculated":s.get("calculation",{}).get("native_calculated",False),
                    "errors_total":len(s.get("calculation",{}).get("errors",[])),"pending_total":len(s.get("calculation",{}).get("pending",[])),
                    **{k:v for k,v in s.get("calculation",{}).items() if k in {"xml_path","diagnostic","reason"}}} for s in checked["stages"]]
    bx.require(engine.fingerprint(root)==calculation["engine_fingerprint"],"路线核对期间引擎变化，旧阶段缓存自动失效；待更新完成后重试同一 route 命令。")
    bx.require(bx.digest(path.read_bytes())==input_sha256,"核对期间源路线变化，请重跑 route。",location="/input",input_file=str(path))
    evidence_file=output.with_suffix(".evidence.json")
    review_file=output.with_suffix(".review.json")
    write_json(review_file,review)
    write_json(evidence_file,{"schema_version":SCHEMA_VERSION,"skill_version":VERSION,"engine_fingerprint":calculation["engine_fingerprint"],"checked_route":checked,"warnings":warnings})
    write_text(output,routes.render(checked,warnings,calculation["engine_version"],calculation["tree_version"],evidence_file=evidence_file.name))
    progress=read_json(output.with_suffix(".state.json"))
    write_json(output.with_suffix(".state.json"),{**progress,"phase":"exported","file_exported":True,"status":checked["delivery_status"],"input":str(path),"input_sha256":input_sha256,"output":str(output),"engine_fingerprint":calculation["engine_fingerprint"],
               "files":{str(p):bx.digest(p.read_bytes()) for p in (output,evidence_file,review_file)},"review_token":review["token"],
               "stages":{row["stage_id"]:{k:v for k,v in row.items() if k not in {"stage_id","name"}} for row in stage_summary if row["status"]!="not_calculated"},
               "delivery_brief":checked["delivery_brief"]})
    return {"status":checked["delivery_status"],"file_exported":True,"delivery_brief":checked["delivery_brief"],"errors":checked["errors"],"stage_summary":stage_summary,"files":[str(output),str(evidence_file)],"review_file":str(review_file),"review_token":review["token"],"stages":len(checked["stages"]),"pending":checked["pending"],"warnings":warnings,
            "scope":"目录核对与实际阶段快照分别列出；仅 calculation 中的数值经过所声明完整输入实算，未物化阶段不宣称精确 DPS/生存/续航。获取位置不证明当前已完成任务。", "next":"先处理 errors 和 stage_summary 的blocked；完成计算不等于输入合法或阶段就绪。诊断XML不能作为可执行配装交付。修订后重跑同一route；剩余真实缺口按delivery_brief明示为条件草案。"}


def workflow_status(args):
    """Read integrity and next operations without repeating native calculation."""
    route_state=getattr(args,"route_state",None)
    if route_state:
        location=Path(route_state).resolve()
        state=read_json(location)
        source=Path(state["input"])
        unchanged=source.is_file() and bx.digest(source.read_bytes())==state.get("input_sha256")
        files=state.get("files",{})
        intact=bool(files) and all(Path(name).resolve().parent==location.parent and Path(name).is_file() and bx.digest(Path(name).read_bytes())==digest for name,digest in files.items())
        root=engine.resolve_root(args.pob)
        current=engine.fingerprint(root)==state.get("engine_fingerprint")
        ready=unchanged and intact and current and state.get("file_exported") is True
        return {"phase":state["phase"],"status":state.get("status"),"delivery_brief":state.get("delivery_brief"),"file_exported":ready,"input_unchanged":unchanged,"files_intact":intact,"engine_unchanged":current,
                "state_file":str(location),"completed_stages":[sid for sid,row in state.get("stages",{}).items() if row.get("native_calculated") or row.get("status")=="calculated"],
                "stage_summary":state.get("stages",{}),
                "next_actions":[{"action":"resume_route" if not ready else "resolve_route" if state.get("status") in {"blocked","draft"} else "review_delivery","input":str(source),"output":state.get("output"),"reference":"references/leveling.md"}],
                "scope":"文件完成需要输入、输出与当前引擎一致；completed_stages仅指原生计算已完成，阶段合法性与依赖看stage_summary。恢复时重新核对来源与判断。"}
    run,state=read_run(args.run)
    records=[]
    actions=[]
    if not state.get("intent"): actions.append({"action":"clarify_intent","reference":"references/build.md"})
    for name in state.get("candidates",{}):
        candidate={"name":name}
        try:
            _,_,outcome,_,_=read_candidate(argparse.Namespace(run=str(run),name=name,pob=args.pob))
            pending=outcome["pending"]
            delivery=run/"output"/name/"delivery.json"
            receipt=read_json(delivery) if delivery.is_file() else {}
            files=receipt.get("files",{})
            complete=receipt.get("file_exported") is True and bool(files) and all((delivery.parent/filename).resolve().is_relative_to(delivery.parent) and (delivery.parent/filename).is_file() and bx.digest((delivery.parent/filename).read_bytes())==digest for filename,digest in files.items())
            candidate.update(valid=True,status=outcome["status"],pending=pending[:12],pending_total=len(pending),file_exported=complete,
                             plan_file=outcome["source_plan"],details_file=str(run/"candidates"/name/"evaluation.json"),review_file=str(run/"candidates"/name/"review.json"))
            actions.append({"action":"review_delivery" if complete else "resolve_candidate" if pending or outcome["errors"] else "export","candidate":name,"details_file":candidate["details_file"]})
        except (ValueError,OSError,KeyError) as exc:
            candidate.update(valid=False,reason=str(exc))
            actions.append({"action":"reevaluate","candidate":name,"reason":str(exc),"reference":"references/build.md"})
        records.append(candidate)
    return {"run":str(run),"phase":state["phase"],"pob_path":state["pob_path"],"skill_version":VERSION,"intent":state.get("intent"),
            "candidates":[r["name"] for r in records],"candidate_records":records,"next_actions":actions}


def parser():
    p = argparse.ArgumentParser(description="POE 配装助手：本机 PoB 实算、候选校验和 XML 导出。")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "inspect", "catalog", "data", "research", "decode", "start", "intent", "evaluate", "probe", "export", "status", "route", "view"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--pob", help="PoECharm 文件夹、PoB 文件夹或可执行文件；默认自动发现")
        if name in {"inspect", "decode", "start"}: cmd.add_argument("--input", required=True)
        if name in {"start", "intent", "evaluate", "probe", "export"}: cmd.add_argument("--run", required=True)
        if name=="status":
            context=cmd.add_mutually_exclusive_group(required=True)
            context.add_argument("--run")
            context.add_argument("--route-state",help="route 输出的 .state.json；只读核对交付和可恢复阶段")
        if name == "start": cmd.add_argument("--invoked", action="store_true")
        if name == "intent": cmd.add_argument("--file", required=True)
        if name=="view":
            cmd.add_argument("--file",required=True,help="获准任务工作区中已保存的JSON")
            cmd.add_argument("--pointer",default="",help="JSON pointer，默认顶层对象")
            cmd.add_argument("--fields",default="",help="所选对象的直接字段，逗号分隔；不写表达式")
            cmd.add_argument("--limit",type=int,default=10)
            cmd.add_argument("--offset",type=int,default=0)
        if name in {"evaluate", "probe", "export"}: cmd.add_argument("--name", required=True)
        if name == "evaluate": cmd.add_argument("--plan", required=True)
        if name == "probe":
            cmd.add_argument("--group", type=int)
            cmd.add_argument("--index", type=int)
            variant = cmd.add_mutually_exclusive_group(required=True)
            variant.add_argument("--ids")
            variant.add_argument("--variants", help="JSON variants 使用现有 operations；可含装备、天赋、宝石和组合")
            variant.add_argument("--node-ids", help="逗号分隔 ID，仅快速比较未分配节点词缀")
            cmd.add_argument("--level", type=int)
            cmd.add_argument("--quality", type=int)
            cmd.add_argument("--metrics", help="按需返回的原生数值指标，以逗号分隔")
            cmd.add_argument("--out", help="完整比较矩阵保存到工作区 JSON")
        if name == "decode": cmd.add_argument("--out", required=True)
        if name == "inspect":
            cmd.add_argument("--calculate", action="store_true")
            cmd.add_argument("--metrics", help="按需读取 Calcs 原生明细的指标名，以逗号分隔")
            cmd.add_argument("--skill-ref", help="inspect 返回的 active_skills[].skill_ref；默认当前主技能")
            cmd.add_argument("--actor", choices=["player","minion","mirage"], help="默认选定技能的原生主体")
            cmd.add_argument("--out")
        if name == "catalog":
            cmd.add_argument("--kind", choices=["gems", "skills", "nodes", "uniques", "config", "translations", "links", "classes", "bases", "affixes", "rules", "metrics"])
            cmd.add_argument("--batch")
            cmd.add_argument("--query", default="")
            cmd.add_argument("--input")
            cmd.add_argument("--class-id", type=int, help="新角色的本机职业 ID，按该职业起点查天赋；不能与 --input 同用")
            cmd.add_argument("--out", help="将完整查询保存到工作区 JSON，终端只返回索引")
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--offset", type=int, default=0)
        if name == "data":
            cmd.add_argument("--mode", choices=["list", "search", "read"], default="list")
            cmd.add_argument("--path", default="", help="返回的 engine/... 或 translations/...；列表/搜索可指定目录")
            cmd.add_argument("--query", default="", help="字面关键词，不执行表达式")
            cmd.add_argument("--line", type=int, default=1)
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--offset", type=int, default=0)
            cmd.add_argument("--out", help="可选任务工作区 JSON；禁止写入安装/资料目录")
        if name == "research":
            subject = cmd.add_mutually_exclusive_group(required=True)
            subject.add_argument("--gems", help="逗号分隔的本机宝石 skill_id")
            subject.add_argument("--version", action="store_true", help="读取官方公告正文并返回简短版本依据")
            subject.add_argument("--trials", action="store_true", help="读取当前页面观测到的试炼位置，不推导奖励或玩家进度")
            cmd.add_argument("--league", help="用户确认的目标赛季或永久区，用于 --version")
            cmd.add_argument("--source", help="公开官方公告 URL；本机版本没有已有来源时填写")
            cmd.add_argument("--out", required=True, help="工作区事实记录 JSON")
        if name == "route":
            cmd.add_argument("--invoked", action="store_true")
            cmd.add_argument("--file", required=True)
            cmd.add_argument("--out", required=True)
    return p


def main(argv=None):
    started=time.perf_counter()
    engine.PERFORMANCE.update(requests=0,worker_calls=0,cache_hits=0,worker_seconds=0.0)
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = {"skill_version": VERSION, **engine.health(engine.resolve_root(args.pob))}
        elif args.command == "inspect":
            if args.out:
                bx.require(Path(args.out).resolve() != Path(args.input).resolve(), "检查结果不能覆盖原始角色文件。请为 --out 使用不同文件名。")
            document = bx.read_document(Path(args.input))
            result = bx.summary(document)
            if args.metrics: args.calculate=True
            bx.require(not (args.skill_ref or args.actor) or args.metrics,"--skill-ref/--actor 需要 --metrics。")
            if args.calculate:
                breakdowns=[{"metric":metric,**({"skill_ref":args.skill_ref} if args.skill_ref else {}),**({"actor":args.actor} if args.actor else {})} for metric in args.metrics.split(",")] if args.metrics else None
                computed = engine.calculate(engine.resolve_root(args.pob), bx.serialise(document),breakdowns=breakdowns)
                result["calculation"] = {key: value for key, value in computed.items() if key not in {"xml", "config_keys", "config_lists"}}
                result["diagnosis_summary"] = diagnosis_summary(computed)
            if args.out:
                write_json(args.out, result)
                result = {"inspection": str(Path(args.out).resolve()), "build": result["build"],
                          **({"diagnosis_summary": result["diagnosis_summary"], "reply_policy": "诊断数值原样使用 diagnosis_summary；随后只问尚未明确的玩家偏好，不重新推导有效 DPS。"} if args.calculate else {})}
        elif args.command == "decode":
            destination = Path(args.out).resolve()
            bx.require(destination != Path(args.input).resolve(), "解码输出不能覆盖输入文件。")
            bx.require(not destination.exists(), "解码输出已存在，请换一个文件名。")
            write_text(destination, bx.serialise(bx.read_document(Path(args.input))))
            result = {"xml": str(destination)}
        elif args.command == "start": result = begin(args)
        elif args.command == "intent": result = set_intent(args)
        elif args.command == "evaluate": result = evaluate(args)
        elif args.command == "probe":
            result = probe(args)
            if args.out:
                output=Path(args.out).resolve()
                bx.require(output.suffix.lower()==".json" and not output.is_relative_to(Path(args.run).resolve()) and (not args.variants or output!=Path(args.variants).resolve()),"probe --out 使用会话目录外的独立 JSON，不能覆盖候选或变体输入。")
                write_json(output,result)
                result={"status":result["status"],"probe_file":str(output),"alternatives":[{"id":v.get("id",v.get("nodes")),"status":v["status"]} for v in result["alternatives"]],"next":result["next"]}
            else:
                bx.require(len(json.dumps(result,ensure_ascii=False))<=12000,"比较结果较大；请加 --out '<工作区>/比较.json'，按字段读取，避免平台截断或转存内部目录。")
        elif args.command == "export": result = export(args)
        elif args.command == "research": result = research(args)
        elif args.command == "data":
            root=engine.resolve_root(args.pob)
            result=engine.query_sources(root,mode=args.mode,path=args.path,query=args.query,line=args.line,limit=args.limit,offset=args.offset)
            if args.out:
                output=Path(args.out).resolve()
                bx.require(output.suffix.lower()==".json","data --out 使用任务工作区 JSON 文件。")
                write_json(output,result)
                result={"data_file":str(output),"mode":args.mode,"total":result.get("total",result.get("total_lines")),"next_offset":result.get("next_offset"),"next_line":result.get("next_line"),"read_only":True}
            else:
                bx.require(len(json.dumps(result,ensure_ascii=False))<=12000,"资料输出较大；缩小 --limit 或加 --out '<工作区>/资料.json'，不要读取平台内部工具输出目录。")
        elif args.command == "catalog":
            bx.require(1 <= args.limit <= 100, "limit 应在 1–100。")
            if args.out:
                output = Path(args.out).resolve()
                bx.require(output.suffix.lower() == ".json", "catalog --out 请指定工作区 JSON 文件。")
                bx.require(all(output != Path(value).resolve() for value in (args.input, args.batch) if value), "查询结果不能覆盖输入角色或查询参数文件。")
            result = catalog(args)
            if args.out:
                write_json(output, result)
                queries = result.get("queries", [{"kind": args.kind, "query": args.query, **result}])
                result = {"catalog_file": str(output), "queries": [{"kind": query["kind"], "query": query["query"], "returned": len(query["entries"]), "total": query.get("total")} for query in queries],
                          "next": "完整结果已保存到工作区。按 queries[].entries 或单项 entries 读取需要的字段；不要一次输出整份大 JSON。"}
            else:
                bx.require(len(json.dumps(result, ensure_ascii=False)) <= 12000, "查询结果较大；请加 --out '<工作区>/查询结果.json' 保存完整结果，再按相关字段读取，避免 TeleAgent 截断或转存至额外授权目录。")
        elif args.command == "route": result = route_command(args)
        elif args.command == "view": result=view_json(args)
        else: result=workflow_status(args)
        if args.command in {"evaluate","route","export"} and len(json.dumps(result,ensure_ascii=False))>12000:
            details=(Path(args.out).resolve().with_suffix(".evidence.json") if args.command=="route" else
                     Path(args.run).resolve()/("candidates" if args.command=="evaluate" else "output")/args.name/("evaluation.json" if args.command=="evaluate" else "final-assessment.json"))
            concise={"status","file_exported","files","candidate","review_file","review_token","evaluation_file","xml","roundtrip_verified","name","stages","stage_summary","delivery_brief","facts_summary","budget_status","scope","next","reply_policy","errors","pending","warnings"}
            result={k:v for k,v in result.items() if k in concise}
            for field in ("errors","pending","warnings"):
                if field in result:
                    result[field+"_total"]=len(result[field]);result[field]=[v[:160] if isinstance(v,str) else v for v in result[field][:12]]
            for field in ("facts_summary","next","reply_policy","scope"):
                if field in result and isinstance(result[field],str): result[field]=result[field][:1800]
            result.update(details_file=str(details),output_scope="终端摘要；完整数值、条件和全部缺口已保存在 details_file，请按字段读取。")
        result["runtime"]={**engine.PERFORMANCE,"elapsed_seconds":round(time.perf_counter()-started,4)}
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        diagnostic=dict(exc.diagnostic) if isinstance(exc,bx.ContractError) else {"code":"engine_failure" if isinstance(exc,engine.EngineError) else "io_error" if isinstance(exc,OSError) else "invalid_input","message":str(exc),"action":"inspect_error","retryable":False}
        source=getattr(args,"plan",None) or getattr(args,"file",None) or getattr(args,"input",None)
        if source: diagnostic["input_file"]=str(Path(source).resolve())
        if isinstance(exc,json.JSONDecodeError): diagnostic.update(line=exc.lineno,column=exc.colno,action="edit_input")
        print(json.dumps({"ok": False, "error": str(exc),"diagnostic":diagnostic}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
