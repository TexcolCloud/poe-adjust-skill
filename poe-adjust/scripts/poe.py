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
import xml.etree.ElementTree as ET

import buildxml as bx
import engine
import facts
import routes

VERSION = "1.7.5"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


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
    bx.require(state.get("invoked") is True, "请先由用户明确调用 POE 配装助手。")
    original = run / "original.xml"
    bx.require(original.is_file() and bx.digest(original.read_bytes()) == state["original_sha256"], "原始快照发生变化。请用用户提供的文件建立新会话。")
    return run, state


def begin(args):
    bx.require(args.invoked, "仅在用户明确调用后传入 --invoked。")
    source = Path(args.input).resolve()
    document = bx.read_document(source)
    root = engine.resolve_root(args.pob)
    info = engine.health(root)
    run = Path(args.run).resolve()
    if (run / "run.json").exists():
        _, state = read_run(run)
        bx.require(state["input_sha256"] == bx.digest(source.read_bytes()), "此会话已有其他角色，请使用新目录。")
        return {"run": str(run), "phase": state["phase"], "already_exists": True}
    bx.require(not run.exists() or not any(run.iterdir()), "请选择空的会话目录，以免覆盖已有文件。")
    run.mkdir(parents=True, exist_ok=True)
    original = bx.serialise(document)
    write_text(run / "original.xml", original)
    state = {"schema_version": 1, "skill_version": VERSION, "invoked": True, "created_at": now(), "phase": "clarifying",
             "source_path": str(source), "input_sha256": bx.digest(source.read_bytes()), "original_sha256": bx.digest(original), **info}
    write_json(run / "run.json", state)
    inspection = bx.summary(document)
    write_json(run / "inspection.json", inspection)
    overview = {"class": inspection["build"].get("className"), "ascendancy": inspection["build"].get("ascendClassName"),
                "level": inspection["build"].get("level"), "tree_version": inspection["tree"].get("treeVersion"),
                "data_version": info["data_version"], "version_evidence": info["version_evidence"], "skill_groups": [
                    {"index": group["index"], "slot": group.get("slot"), "enabled": group.get("enabled"),
                     "gems": [{key: gem.get(key) for key in ("index", "skillId", "nameSpec", "level", "quality")} for gem in group["gems"]]}
                    for group in inspection["skill_groups"]]}
    return {"run": str(run), "phase": "clarifying", "inspection": str(run / "inspection.json"), "overview": overview,
            "next": "先结合以上角色信息逐轮确认目标、赛季版本、预算、改装范围和进度；其余装备原文在 inspection，宝石字段是 skillId/nameSpec。"}


def validate_intent(value, original):
    allowed = {"confirmed", "goal", "league", "game_version", "version_source", "version_checked_at", "mode", "budget", "target_level", "allow_class_change", "allow_ascendancy_change", "allow_main_skill_change", "keep_slots", "keep_skill_ids", "scenario", "baseline_main_group", "minimum_stats", "quest_passive_points", "ascendancy_points", "notes", "calculation_mode", "resource_mode"}
    bx.require(not (set(value) - allowed), "意图包含未知字段：" + ", ".join(sorted(set(value) - allowed)))
    bx.require(value.get("confirmed") is True, "先向用户归纳并明确目标；confirmed 必须对应用户已表达的意图。")
    for field in ("goal", "league"):
        bx.require(isinstance(value.get(field), str) and value[field].strip(), f"需要已确认的 {field}。")
    engine.validate_game_version(value.get("game_version"), value["league"], version_source=value.get("version_source"))
    bx.require(isinstance(value.get("version_source"), str) and value["version_source"].startswith(("https://", "http://")), "version_source 需要实际支持国服赛季与版本对应关系的网页 URL。")
    bx.require(isinstance(value.get("version_checked_at"), str), "version_checked_at 需要实际查阅日期。")
    engine.validate_checked_at(value["version_checked_at"])
    bx.require(value.get("mode") in {"refine", "rebuild", "both"}, "mode 应为 refine/rebuild/both。")
    budget = value.get("budget")
    bx.require(isinstance(budget, dict) and set(budget) <= {"amount", "currency"}, "budget 需要 amount 与 currency。")
    bx.require(isinstance(budget.get("amount"), (int, float)) and not isinstance(budget["amount"], bool) and math.isfinite(budget["amount"]) and budget["amount"] >= 0, "预算金额应为有限非负数。")
    bx.require(isinstance(budget.get("currency"), str) and budget["currency"], "需要预算通货单位。")
    for field in ("target_level", "baseline_main_group", "quest_passive_points", "ascendancy_points"):
        bx.require(field in value, f"请先确认并填写 {field}，不能套用示例或默认角色进度。")
    defaults = {"allow_class_change": False,
                "allow_ascendancy_change": False, "allow_main_skill_change": False,
                "keep_slots": [], "keep_skill_ids": [], "scenario": {}, "minimum_stats": {},
                "notes": "", "calculation_mode": "steady", "resource_mode": "sustained"}
    result = {**defaults, **value}
    bx.require(result["calculation_mode"] in {"steady", "conditional"}, "calculation_mode 应为 steady 或 conditional。")
    bx.require(result["resource_mode"] in {"sustained", "burst"}, "resource_mode 应为 sustained 或 burst。")
    for flag in ("allow_class_change", "allow_ascendancy_change", "allow_main_skill_change"):
        bx.require(isinstance(result[flag], bool), f"{flag} 应为布尔值。")
    for field in ("keep_slots", "keep_skill_ids"):
        bx.require(isinstance(result[field], list) and all(isinstance(v, str) for v in result[field]), f"{field} 应为文本数组。")
    bx.require(all(s in bx.SLOTS or re.fullmatch(r"Jewel \d+", s) for s in result["keep_slots"]), "keep_slots 存在未知插槽。")
    bx.require(isinstance(result["target_level"], int) and 1 <= result["target_level"] <= 100, "目标等级超出范围。")
    bx.require(isinstance(result["quest_passive_points"], int) and 0 <= result["quest_passive_points"] <= 23, "当前适配版本的任务天赋点应为 0–23。")
    bx.require(isinstance(result["ascendancy_points"], int) and 0 <= result["ascendancy_points"] <= 8, "升华点应为 0–8。")
    bx.require(isinstance(result["minimum_stats"], dict), "minimum_stats 应为对象。")
    for key, val in result["minimum_stats"].items():
        bx.require(key in METRICS and isinstance(val, (int, float)) and math.isfinite(val), f"未知或无效的最低指标：{key}")
    bx.index_one(bx.groups(original), result["baseline_main_group"], "原角色主技能组")
    bx.set_config(copy.deepcopy(original), result["scenario"])
    return result


def set_intent(args):
    run, state = read_run(args.run)
    intent = validate_intent(read_json(args.file), bx.read_document(run / "original.xml"))
    info = engine.check(engine.resolve_root(state["pob_path"]))
    engine.validate_game_version(intent["game_version"], intent["league"], engine.require_data_version(info))
    state["intent"] = intent
    state["intent_hash"] = bx.digest(json.dumps(intent, ensure_ascii=False, sort_keys=True))
    state["target_version_evidence"] = facts.version_fact(intent["version_source"], intent["game_version"], intent["league"], run / ".poe-evidence", info["engine_fingerprint"])
    state["phase"] = "designing"
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"run": str(run), "phase": state["phase"], "intent": intent, "target_version_evidence": state["target_version_evidence"], "next": "核对版本正文与关键机制后写 plan.json 并 evaluate。正文未核实时保留为草案；旧意图下的候选不能导出。"}


def prepared(original, intent, *, baseline=False):
    root = copy.deepcopy(original)
    bx.set_config(root, intent["scenario"], reset=True)
    root.find("Build").set("characterLevelAutoMode", "false")
    if baseline:
        root.find("Build").set("mainSocketGroup", str(intent["baseline_main_group"]))
    return root


def active_slot(name, result):
    if name.startswith("Weapon "):
        return name.endswith(" Swap") == bool(result.get("use_second_weapon_set"))
    return True


def active_gems(result):
    return [gem for gem in result.get("gems", []) if gem.get("enabled") and gem.get("group_enabled") and active_slot(gem.get("slot", ""), result)]


def release_inactive_sockets(document):
    """Keep alternate groups but do not let them shift active native socket quality."""
    for group in bx.groups(document):
        # Native granted groups are matched by source AND slot. Clearing their
        # slot makes the engine replace them with newly enabled groups.
        if group.get("enabled", "true") == "false" and not group.get("source"):
            group.set("slot", "")


def socket_layout_issues(document, result):
    """Check physical links using the engine's sequential socket assignment."""
    errors = []
    equipped = {item["slot"]: item for item in result["equipped"]}
    texts = {int(item.get("id")): bx.text_item(item) for item in document.findall("Items/Item")}
    offsets, used_links = {}, {}
    for index, group in enumerate(bx.groups(document), 1):
        slot = group.get("slot", "")
        gems = [gem for gem in result["gems"] if gem["group"] == index]
        offset = offsets.get(slot, 0)
        offsets[slot] = offset + len(gems)
        if group.get("source") or group.get("enabled", "true") == "false" or not active_slot(slot, result) or not gems:
            continue
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
            errors.append(f"{slot} 第 {index} 组占用了深渊/特殊插槽，不能视为普通宝石孔。")
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
    errors, warnings = [], list(result.get("warnings", []))
    for node in result["granted_passives"]:
        if node["allocated_on_tree"]:
            warnings.append(f"本机节点 {node['id']} {node.get('display_name', node['name'])} 已在树上分配，同时又被额外授予；原生计算按同一节点计入，不能解释为两份叠加收益。是否调整天赋或授予来源由方案目标决定。")
    engine.validate_game_version(intent.get("game_version"), intent["league"], engine.require_data_version(result), intent.get("version_source"))
    for capability in ("matching_socket_quality", "native_resource_costs", "native_gem_requirements"):
        if not result.get("capabilities", {}).get(capability):
            errors.append(f"本机接口尚未验证 {capability}，不能认证依赖此能力的完整配装；请保留诊断结果供维护者适配。")
    if result["tree_version"] != result["data_version"].replace(".", "_"):
        errors.append("天赋版本与当前机制/宝石数据版本不同；当前适配仅接受对应标准天赋树，不能把旧树或特殊模式树当作当前普通赛季。")
    stats = result["stats"]
    config_keys = set(result["config_keys"])
    for entry in bx.active_config(document).findall("Input"):
        name = entry.get("name", "")
        if name not in config_keys:
            errors.append("引擎不识别配置：" + name)
        elif name in result["config_lists"]:
            allowed = result["config_lists"][name]
            value = None
            kinds = [kind for kind in ("string", "number", "boolean") if kind in entry.attrib]
            if len(kinds) == 1:
                kind = kinds[0]
                if kind == "string": value = entry.get(kind)
                elif kind == "boolean" and entry.get(kind) in ("true", "false"): value = entry.get(kind) == "true"
                elif kind == "number":
                    try: value = float(entry.get(kind))
                    except ValueError: pass
            matches = any((type(value) == type(option) or type(value) in (int, float) and type(option) in (int, float)) and value == option for option in allowed)
            if not matches:
                choices = json.dumps(allowed[:12], ensure_ascii=False) if allowed else "本机选项暂不可读取，不能验证"
                errors.append(f"无效配置值：{name}={value!r}；本机允许值 {choices}。用 catalog config 返回的 val，不要使用显示标签 label。")
    for warning in warnings:
        if "negative" in warning or "not have enough" in warning or "too many" in warning:
            errors.append(warning)
    allowance = result["level"] - 1 + intent["quest_passive_points"] + stats.get("ExtraPoints", 0)
    if result["points"] > allowance:
        errors.append(f"当前等级与任务进度最多 {allowance:g} 点，方案使用 {result['points']} 点。")
    if result["ascendancy_points"] > intent["ascendancy_points"]:
        errors.append("方案超过已确认可用的升华点。")
    requested_nodes = {int(n) for n in bx.active_spec(document).get("nodes", "").split(",") if n}
    missing = requested_nodes - set(result["nodes"])
    if missing: errors.append("引擎未保留部分天赋节点：" + ",".join(map(str, sorted(missing))))
    for group in bx.groups(document):
        if not group.get("source"):
            continue
        for gem in group.findall("Gem"):
            retained = [entry for entry in result["gems"] if entry.get("source") == group.get("source") and entry.get("id") == gem.get("skillId")]
            # A removed item/node may legitimately remove its granted group.
            if retained and not any(entry.get("slot", "") == group.get("slot", "") and
                                    entry["group_enabled"] == (group.get("enabled", "true") != "false") for entry in retained):
                errors.append(f"引擎改变了授予技能状态：{group.get('source')} / {gem.get('skillId')}；槽位与启用状态必须和方案一致，不能将重建后默认开启的技能当作已关闭。")
    for skill in result.get("active_skills", []):
        if skill.get("trigger_target_required_level") and skill.get("trigger_level_limit") is not None:
            if skill["trigger_target_required_level"] > skill["trigger_level_limit"]:
                errors.append(f"第 {skill['group']} 组 {skill.get('display_name', skill['name'])} 的角色等级需求 {skill['trigger_target_required_level']:g} 超过触发辅助等级上限 {skill['trigger_level_limit']:g}；降低被触发宝石等级或提高辅助等级，并重新核对触发条件。")
    for gem in active_gems(result):
        if not gem.get("recognised"):
            errors.append(f"未知宝石：第 {gem['group']} 组第 {gem['index']} 个。")
        if gem.get("support") and not (gem.get("support_applied") or gem.get("supports_excluded_guards")):
            errors.append(f"第 {gem['group']} 组的 {gem.get('display_name',gem.get('name'))} 未辅助任何启用技能；请核对适用类型、触发等级、重复辅助或将备用宝石移出当前组。")
        if not gem.get("source") and gem.get("max_base_level"):
            if gem.get("level", 1) > gem["max_base_level"] or gem.get("quality", 0) > 23:
                errors.append(f"{gem.get('name')} 的宝石自身等级/品质超出普通练级或腐化上限；装备、辅助及匹配孔色加成由引擎另算，不能预加到 gems.level/quality。")
        for req, available in (("req_level", result["level"]), ("req_str", stats.get("Str", 0)), ("req_dex", stats.get("Dex", 0)), ("req_int", stats.get("Int", 0))):
            if (gem.get(req) or 0) > available:
                errors.append(f"{gem.get('name',gem.get('id'))} 的 {req} 需求 {gem[req]} 超过 {available:g}。")
    equipped = {item["slot"]: item for item in result["equipped"]}
    expected_equipment = [(slot.get("name", ""), int(slot.get("itemId", "0")))
                          for slot in bx.active_set(document, "Items", "ItemSet", "activeItemSet").findall("Slot")]
    expected_equipment += [("Jewel " + slot.get("nodeId"), int(slot.get("itemId", "0")))
                           for slot in bx.active_spec(document).findall("Sockets/Socket") if int(slot.get("nodeId", "0")) in requested_nodes]
    for name, item_id in expected_equipment:
        if item_id and active_slot(name, result) and equipped.get(name, {}).get("id") != item_id:
            errors.append(f"{name} 的装备 {item_id} 未被引擎装入；请核对基底名称、物品类型和插槽，不能把被静默移除的装备当作已生效。可用 catalog bases 查询合法基底。")
    for name, item in equipped.items():
        if not active_slot(name, result): continue
        if not item.get("base_recognised"):
            errors.append(f"{name} 装备基底未被识别。")
        for req, available in (("level", result["level"]), ("str", stats.get("Str", 0)), ("dex", stats.get("Dex", 0)), ("int", stats.get("Int", 0))):
            if (item.get("requirements", {}).get(req) or 0) > available:
                errors.append(f"{name} 装备的 {req} 需求未满足。")
        if item.get("unparsed"):
            warnings.append(f"{name} 存在引擎未完整解析的词缀：" + "；".join(item["unparsed"]))
    errors.extend(socket_layout_issues(document, result))
    for key, threshold in intent["minimum_stats"].items():
        if key not in stats or stats[key] < threshold:
            errors.append(f"{METRICS[key]} 为 {stats.get(key,'未知')}，低于约定的 {threshold}。")
    if result["main_skill"] == "DefaultAttack":
        errors.append("主技能退回普通攻击，请确认主技能组及技能可用性。")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def readiness(result, intent, plan):
    """Audit conditions that PoB permits but a usable delivered build must explain."""
    stats = result["stats"]
    errors, warnings, resources = [], [], []
    available = result["level"] - 1 + intent["quest_passive_points"] + stats.get("ExtraPoints", 0)
    unused = {"normal": available - result["points"], "ascendancy": intent["ascendancy_points"] - result["ascendancy_points"]}
    reasons = plan.get("unused_points_reason", {})
    bx.require(isinstance(reasons, dict) and set(reasons) <= {"normal", "ascendancy"}, "unused_points_reason 只能包含 normal/ascendancy 的留点理由。")
    for key, label in (("normal", "普通天赋"), ("ascendancy", "升华")):
        if unused[key] > 0:
            reason = reasons.get(key)
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"尚有 {unused[key]:g} 个可用{label}点未分配；完成分配，或在 unused_points_reason.{key} 写明本方案为何留点。")
            else:
                warnings.append(f"保留 {unused[key]:g} 个{label}点：{reason}")
    guard = bool(stats.get("GuardSkillActive"))
    flasks = [item["slot"] for item in result["equipped"] if item["slot"].startswith("Flask ") and item.get("active")]
    mode = intent.get("calculation_mode", "steady")
    if mode == "steady":
        if guard:
            errors.append("常态计算仍启用了守护增益；对应宝石设置 enable_global1=false 并重算。保留宝石用于应急，不把短时增益当常驻。")
        if flasks:
            errors.append("常态计算仍启用药剂：" + "、".join(flasks) + "；设置 equip[].active=false。若确需计算药剂覆盖，另用 conditional 意图并提供覆盖依据。")
    else:
        evidence = plan.get("config_evidence", {})
        for condition in (["guard"] if guard else []) + flasks:
            if not isinstance(evidence.get(condition), str) or not evidence[condition].strip():
                errors.append("条件面板缺少触发、持续时间和覆盖依据：config_evidence." + condition)
        if guard or flasks:
            warnings.append("此为有条件的守护/药剂面板，不能作为无条件常态承伤或持续输出。")
    duration = plan.get("burst_seconds")
    if duration is not None:
        bx.require(type(duration) in (int, float) and math.isfinite(duration) and duration > 0, "burst_seconds 应为有限正数。")
    eb = result.get("resource_rules", {}).get("energy_shield_protects_mana", False)
    active = [skill for skill in result.get("active_skills", []) if not skill.get("disabled")]
    automatic = [skill for skill in active if not skill.get("is_main") and skill.get("trigger") and skill.get("costs", {}).get("SkillTriggerRate", 0) > 0]
    if result.get("excluded_guard_checks"):
        warnings.append("守护收益未计入常态面板；其连线、等级与单次费用已另作原生检查，自动触发费用按引擎速率计入。实际受击频率与覆盖仍需核对，手动守护须留出单次资源。")
    mirage_main = any(skill.get("is_main") and skill.get("generals_cry") for skill in active)
    multiplied = [gem for gem in active_gems(result) if not gem.get("support") and max(gem.get("count", 1), gem.get("group_count", 1)) > 1]
    if multiplied:
        count_evidence = plan.get("config_evidence", {}).get("skill_counts")
        if not isinstance(count_evidence, str) or not count_evidence.strip():
            errors.append("多个召唤物/重叠技能数量需要 config_evidence.skill_counts：逐组说明可维持数量、取得/施放速率与持续时间，不能直接填理论上限抬高 Full DPS。")
        warnings.append("Full DPS 使用了手动设置的多实例数量，实际覆盖依赖 skill_counts 中的依据；查看报告分项，不能把数量视为引擎已证明可维持。")
    if any(skill.get("generals_cry") for skill in active):
        evidence = plan.get("config_evidence", {}).get("generals_cry")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append("将军之吼方案需要 config_evidence.generals_cry：说明尸体取得、战吼触发/冷却、幻影数量及玩家自身回复来源；幻影攻击不等于本人攻击。")
        corpse_sources = [skill for skill in active if skill.get("creates_corpses")]
        if mode == "steady":
            if not result.get("capabilities", {}).get("native_corpse_sources"):
                errors.append("本机接口无法读取 CreatesCorpse 技能类型，尚不能认证将军之吼的尸体生成来源；请保留诊断供维护者适配。")
            elif not corpse_sources:
                errors.append("将军之吼缺少已装入且启用的尸体生成技能：本机技能类型未识别到 CreatesCorpse。怪物尸体不能保证无小怪首领启动；补齐来源，特殊外部来源须用 conditional 并在 generals_cry 依据中解释。")
        warnings.append("将军之吼按幻影攻击计算；CreatesCorpse 仅识别造尸能力，不证明数量或稳定供尸。尸体生成、接近目标和战吼循环会影响实际覆盖，不能描述成本人无条件自动施放。")
    if automatic:
        warnings.append("持续消耗额外计入启用的自动触发技能：" + "、".join(skill.get("display_name", skill["name"]) for skill in automatic) + "。")
    if any(not skill.get("is_main") and not skill.get("trigger") and any(skill.get("costs", {}).get(key, 0) > 0 for key in ("ManaCost", "LifeCost", "ESCost")) for skill in active):
        warnings.append("主技能与已识别自动触发的持续消耗已核对；位移、手动诅咒、尸体生成或补召唤的额外频率未自动假定。按 active_skills 的单次消耗留出余量，不能将该结果当作所有技能同时满速的续航。")
    for resource, cost_key, label in (("Mana", "Mana", "魔力"), ("Life", "Life", "生命"), ("EnergyShield", "ES", "能量护盾")):
        cost = stats.get(cost_key + "PerSecondCost", 0)
        single = stats.get(cost_key + "Cost", 0)
        automatic_cost = sum(skill.get("costs", {}).get(cost_key + "PerSecondCost", 0) for skill in automatic)
        cost += automatic_cost
        single = max([single] + [skill.get("costs", {}).get(cost_key + "Cost", 0) for skill in automatic])
        raw_regen = stats.get(resource + "RegenRecovery", 0)
        regen = stats.get("Net" + resource + "Regen", raw_regen)
        native_leech = stats.get(resource + "LeechGainRate", 0)
        leech = 0 if mirage_main else native_leech
        if mirage_main and native_leech:
            warnings.append(f"将军之吼主技能面板的{label}吸取/击回 {native_leech:.1f}/秒属于幻影攻击，未计作玩家自身回复；另行使用本人攻击时需核实其命中与频率。")
        pool = stats.get(resource + "Unreserved", stats.get(resource, 0))
        recharge = 0
        if resource == "Mana" and eb:
            single = 0
        if resource == "EnergyShield":
            pool = min(pool, stats.get("EnergyShieldRecoveryCap", pool))
            if eb:
                single += max([stats.get("ManaCost", 0)] + [skill.get("costs", {}).get("ManaCost", 0) for skill in automatic])
            if cost <= 0 and single <= 0 and regen >= 0:
                continue
            safe_eb_recharge = eb and not stats.get("TotalBuildDegen", 0) and not any(stats.get(key, 0) for key in ("sharedMindOverMatter", "PhysicalMindOverMatter", "FireMindOverMatter", "ColdMindOverMatter", "LightningMindOverMatter", "ChaosMindOverMatter"))
            conditional_recharge = mode == "conditional" and isinstance(plan.get("config_evidence", {}).get("energy_shield_recharge"), str) and plan["config_evidence"]["energy_shield_recharge"].strip()
            if stats.get("EnergyShieldRechargeAppliesToEnergyShield") and (safe_eb_recharge or conditional_recharge):
                recharge = stats.get("EnergyShieldRecharge", 0)
                if recharge:
                    delay = stats.get("EnergyShieldRechargeDelay", 2)
                    warnings.append(f"能盾资源计入已启动的充能 {recharge:.1f}/秒；启动延迟 {delay:.2f} 秒。" + ("EB 且未检测到伤害转魔力或自身持续伤害；技能消耗本身不打断充能。" if safe_eb_recharge else "按 energy_shield_recharge 的有条件覆盖依据计算；充能被打断时不能沿用此续航。"))
                    if max(cost - regen - leech, 0) * delay > pool:
                        errors.append("能盾储量不足以覆盖满速使用时的充能启动延迟；先等待充能、降低消耗或增加无需充能的回复，并明确实际操作。")
            if eb:
                warnings.append("EB 消耗按能盾池保守核对，未自动把耗尽后的魔力池/魔力回复合并为能盾供给。")
            if stats.get("EnergyShieldRecharge", 0) and not recharge:
                warnings.append("能盾充能未计入持续回复；伤害可能打断充能。需要条件计算时用 conditional，并在 config_evidence.energy_shield_recharge 说明启动、打断与覆盖依据。")
        net = cost - regen - leech - recharge
        if single > 0 and (pool < single or resource == "Life" and pool <= single):
            errors.append(f"可用{label} {pool:.1f} 不能支付主技能/自动触发技能的单次消耗 {single:.1f}。")
        record = {"resource": label, "pool": pool, "cost_per_second": cost, "regeneration": regen, "raw_regeneration": raw_regen, "leech_on_hit": leech, "recharge": recharge,
                  "automatic_cost_per_second": automatic_cost, "native_leech_on_hit": native_leech,
                  "net_cost": net, "sustainable_while_hitting": net <= 0.01,
                  "seconds_without_hits": max(pool - (1 if resource == "Life" else 0), 0) / (cost - regen - recharge) if cost > regen + recharge else None}
        if net > 0.01:
            record["maximum_burst_seconds"] = max(pool - (1 if resource == "Life" else 0), 0) / net
            message = f"主技能满速及已识别自动触发每秒消耗{label} {cost:.1f}，扣除自身持续伤害后净秒回 {regen:.1f}、命中吸取 {leech:.1f}、计入充能 {recharge:.1f}，净缺口 {net:.1f}；不能持续满速使用。"
            if intent.get("resource_mode", "sustained") == "sustained":
                errors.append(message + "请修复续航。只有用户明确接受爆发/停手玩法时才改用 resource_mode=burst。")
            elif duration is None or duration > record["maximum_burst_seconds"]:
                errors.append(message + f"爆发方案须给出 burst_seconds，且不得超过 {record['maximum_burst_seconds']:.2f} 秒。")
            else:
                downtime = net * duration / regen if regen > 0 else None
                record["recovery_seconds_at_regen_only"] = downtime
                record["duty_cycle"] = duration / (duration + downtime) if downtime is not None else None
                warnings.append(message + (f"按 {duration:g} 秒爆发，停手仅靠秒回至少恢复 {downtime:.1f} 秒。" if downtime is not None else "停手后的恢复时间无法由秒回证明，需要另外核实恢复操作。"))
        if cost > regen + recharge + 0.01 and leech:
            warnings.append(f"{label}续航依赖命中吸取；空挥、无敌阶段和转场不保证满速持续。")
        resources.append(record)
    return {"errors": errors, "warnings": warnings, "unused_points": unused,
            "conditions": {"mode": mode, "guard_active": guard, "active_flasks": flasks}, "resources": resources,
            "automatic_costs": automatic}


def material_requirements(original, document, before, after):
    """Derive purchases/upgrades from actual XML, not a prose claim of ownership."""
    def gem_key(gem):
        return gem.get("id"), gem.get("level", 1), gem.get("quality", 0)
    stock = Counter(gem_key(gem) for gem in before["gems"] if not gem.get("source"))
    needed = Counter()
    names = {}
    for gem in after["gems"]:
        if not gem.get("id") or gem.get("source") or not gem.get("group_enabled") or not active_slot(gem.get("slot", ""), after):
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
    old_items = Counter(bx.normal_item(bx.text_item(item)) for item in original.findall("Items/Item"))
    item_texts = {int(item.get("id")): bx.normal_item(bx.text_item(item)) for item in document.findall("Items/Item")}
    for item in after["equipped"]:
        if not active_slot(item["slot"], after):
            continue
        text = item_texts.get(item["id"], "")
        if old_items[text]:
            old_items[text] -= 1
        else:
            requirements.append({"kind": "item", "item_id": item["id"], "quantity": 1,
                                 "description": f"{item['slot']}：{item['name']}（新物品或原物品改造，含孔色/连线变化）"})
    return requirements


def check_constraints(original, document, before, after, intent, plan):
    errors = []
    if intent["mode"] != "both" and plan["mode"] != intent["mode"]: errors.append("候选超出用户确认的改装模式。")
    if after["level"] != intent["target_level"]: errors.append("候选等级与用户确认的目标等级不一致。")
    if after["tree_version"] != before["tree_version"]: errors.append("候选天赋版本与原角色不同，请先确定统一游戏版本。")
    if not intent["allow_class_change"] and before["class"] != after["class"]: errors.append("用户要求保留职业。")
    if not intent["allow_ascendancy_change"] and before["ascendancy"] != after["ascendancy"]: errors.append("用户要求保留升华。")
    if not intent["allow_main_skill_change"] and before["main_skill"] != after["main_skill"]: errors.append("用户要求保留主技能。")
    before_slots = {entry["slot"]: entry for entry in before["equipped"]}
    after_slots = {entry["slot"]: entry for entry in after["equipped"]}
    original_items = {int(item.get("id")): bx.normal_item(bx.text_item(item)) for item in original.findall("Items/Item")}
    new_items = {int(item.get("id")): bx.normal_item(bx.text_item(item)) for item in document.findall("Items/Item")}
    for slot in intent["keep_slots"]:
        old, new = before_slots.get(slot), after_slots.get(slot)
        if not old or not new or original_items.get(old["id"]) != new_items.get(new["id"]):
            errors.append("必须保留的装备被改动：" + slot)
    enabled_ids = {gem.get("id") for gem in active_gems(after)}
    for skill in intent["keep_skill_ids"]:
        if skill not in enabled_ids: errors.append("必须保留的技能未启用：" + skill)
    config = plan.get("operations", {}).get("config", {})
    for key in config:
        if key in intent["scenario"] and config[key] != intent["scenario"][key]:
            errors.append("候选修改了共同战斗场景：" + key)
        if not plan.get("config_evidence", {}).get(key): errors.append("候选配置缺少持续性/触发来源说明：" + key)
    if plan["mode"] == "rebuild" and not plan.get("migration_steps"):
        errors.append("整体换流派需要说明装备准备和切换顺序。")
    if plan.get("template_xml") and not plan.get("sources"):
        errors.append("引用完整配装模板需要记录来源。")
    for source in plan.get("sources", []):
        if source.get("checked_at"):
            try:
                engine.validate_checked_at(source["checked_at"])
            except ValueError as exc:
                errors.append(str(exc))
    return errors


def budget_summary(plan, intent, requirements=None):
    entries = plan.get("shopping", [])
    bx.require(isinstance(entries, list), "shopping 应为数组。")
    low = high = 0
    statuses, warnings = [], []
    for entry in entries:
        bx.require(isinstance(entry, dict) and entry.get("description"), "每项购物清单需要 description。")
        status = entry.get("status", "unknown")
        bx.require(status in {"owned", "verified_cn", "estimate", "unknown"}, "购物状态应为 owned/verified_cn/estimate/unknown。")
        if entry.get("kind"):
            bx.require(entry["kind"] in {"gem", "item", "service"}, "购物 kind 应为 gem/item/service。")
        statuses.append(status)
        if status == "owned": continue
        if status == "unknown": continue
        bx.require(entry.get("currency") == intent["budget"]["currency"], "购物清单与预算需要使用同一通货；换算需先核实汇率。")
        lo, hi = entry.get("low"), entry.get("high")
        bx.require(isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi, "价格范围无效。")
        if status == "verified_cn":
            bx.require(entry.get("source", "").startswith(("https://", "http://")) and entry.get("checked_at") and entry.get("league") == intent["league"], "国服已核实价格需要来源链接、时间和相同赛季。")
            engine.validate_checked_at(entry["checked_at"])
        low += lo
        high += hi
    materials = []
    for need in requirements or []:
        keys = ("kind", "skill_id", "level", "quality") if need["kind"] == "gem" else ("kind", "item_id")
        matches = [entry for entry in entries if all(entry.get(k) == need[k] for k in keys)]
        bx.require(len(matches) <= 1, "同一物品的购物记录重复：" + need["description"])
        match = matches[0] if matches else {}
        bx.require(match.get("status") != "owned", "原 XML 未记录足量同规格物品，不能直接标 owned：" + need["description"] + "；请列为练级/制作/采购，状态 unknown 或已核实价格。")
        if match:
            bx.require(type(match.get("quantity", 1)) is int and match.get("quantity", 1) >= need["quantity"], "购物数量不足：" + need["description"])
        status = match.get("status", "unknown")
        statuses.append(status)
        materials.append({**need, "status": status, "source": match.get("source")})
    if requirements and any(e.get("status") == "owned" and not e.get("kind") for e in entries):
        warnings.append("文字声明的“已有”未关联具体物品，不能抵消自动检测到的采购/升级需求。")
    if (not entries and requirements != []) or "unknown" in statuses: status = "unknown"
    elif "estimate" in statuses: status = "estimate"
    elif all(s == "owned" for s in statuses): status = "owned"
    else: status = "verified_cn"
    if status == "unknown": warnings.append("成本尚未完整核实；不得承诺能在预算内买齐。")
    elif status == "estimate": warnings.append("成本含估计价格，尚未证明国服实际可买齐。")
    return {"status": status, "low": low, "high": high, "currency": intent["budget"]["currency"], "limit": intent["budget"]["amount"], "over_budget": high > intent["budget"]["amount"], "warnings": warnings, "materials": materials}


def compare(before, after, comparable=True):
    result = {}
    for key in METRICS:
        a, b = before["stats"].get(key), after["stats"].get(key)
        if isinstance(a, (float, int)) and isinstance(b, (float, int)):
            result[key] = {"before": a, "after": b, "delta": b-a, "percent": (b-a)/abs(a)*100 if a and comparable else None}
    return result


def semantics(result):
    return {"class": result["class"], "ascendancy": result["ascendancy"], "level": result["level"], "tree_version": result["tree_version"],
            "config_values": result["config_values"],
            "nodes": result["nodes"], "main_skill": result["main_skill"], "main_group": result["main_group"],
            "gems": [{key: gem.get(key) for key in ("group", "index", "id", "source", "level", "quality", "enabled", "group_enabled", "slot", "matches_socket", "socket_quality_bonus")} for gem in result["gems"]],
            "equipped": [{key: item.get(key) for key in ("slot", "id", "name", "base", "active")} for item in result["equipped"]]}


def roundtrip(root, result, document):
    saved = bx.preserve_extensions(document, bx.parse(result["xml"]))
    xml = bx.serialise(saved)
    reloaded = engine.calculate(root, xml)
    bx.require(semantics(result) == semantics(reloaded), "XML 回读后配置、装备、天赋或技能发生变化，未通过导出检查。")
    for key in METRICS:
        a, b = result["stats"].get(key), reloaded["stats"].get(key)
        if a is None and b is None: continue
        bx.require(isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-5), "XML 回读后的计算结果不一致：" + key)
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
        bx.require(type(spec.get("ilvl")) is int and 1 <= spec["ilvl"] <= 100, "craft.ilvl 应为 1–100 物品等级。")
        bx.require(type(spec.get("quality", 20)) is int and 0 <= spec.get("quality", 20) <= 20, "普通配方品质只接受 0–20；特殊加工需另有真实依据。")
        sockets = spec.get("sockets", "")
        bx.require(isinstance(sockets, str) and (not sockets or re.fullmatch(r"[RGBW](?:[- ][RGBW])*", sockets)), "craft.sockets 只接受普通孔，例如 W-W-W-W 或 R-R G-G。")
        affixes = spec.get("affixes")
        bx.require(isinstance(affixes, list) and 1 <= len(affixes) <= 6, "craft.affixes 应为 1–6 项。")
        for affix in affixes:
            bx.require(isinstance(affix, dict) and set(affix) <= {"id", "roll"} and isinstance(affix.get("id"), str), "词缀配方需要 catalog affixes 返回的 id，可选 roll。")
            roll = affix.get("roll", 0.5)
            bx.require(type(roll) in (float, int) and math.isfinite(roll) and 0 <= roll <= 1, "roll 是词缀范围内位置 0–1，默认 0.5，不是实际数值。")
        recipes.append({"id": item["id"], **spec})
    if not recipes:
        return plan, []
    bx.require(len(recipes) <= 10 and len({spec["id"] for spec in recipes}) == len(recipes), "每次最多 10 件且不能有重复配方物品 ID。")
    audit = engine.calculate(root, crafts=recipes)["crafted_items"]
    generated = {item["id"]: item["text"] for item in audit}
    resolved = copy.deepcopy(plan)
    resolved["operations"]["items"] = [{"id": item["id"], "text": generated[item["id"]]} if "craft" in item else item for item in resolved["operations"]["items"]]
    return resolved, audit


def evaluate(args):
    run, state = read_run(args.run)
    bx.require(state.get("intent"), "先通过多轮对话确认意图，并执行 intent。")
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称请使用 1–48 位小写英文、数字或连字符。")
    intent = state["intent"]
    plan_path = Path(args.plan).resolve()
    plan = read_json(plan_path)
    original = bx.read_document(run / "original.xml")
    root = engine.resolve_root(args.pob or state["pob_path"])
    info = engine.check(root)
    engine.validate_game_version(intent["game_version"], intent["league"], engine.require_data_version(info))
    if state.get("engine_fingerprint") != info["engine_fingerprint"]:
        # Preserve confirmed intent and the original snapshot; invalidate evidence.
        state.update(engine.health(root))
        write_json(run / "run.json", state)
    base_path = run / "baseline.json"
    cached_baseline = read_json(base_path) if base_path.exists() else {}
    if (cached_baseline.get("intent_hash") == state["intent_hash"] and cached_baseline.get("skill_version") == VERSION
            and cached_baseline.get("engine_fingerprint") == info["engine_fingerprint"]):
        baseline = cached_baseline
    else:
        baseline = engine.calculate(root, bx.serialise(prepared(original, intent, baseline=True)))
        baseline["intent_hash"] = state["intent_hash"]
        baseline["skill_version"] = VERSION
        write_json(base_path, baseline)
    source = original
    if plan.get("template_xml"):
        path = Path(plan["template_xml"])
        source = bx.read_document(path if path.is_absolute() else plan_path.parent / path)
    # Apply shared scenario first; candidate-specific conditions then require evidence.
    resolved_plan, crafted_items = materialize_items(plan, root)
    document = bx.apply_plan(prepared(source, intent), resolved_plan)
    release_inactive_sockets(document)
    result = engine.calculate(root, bx.serialise(document))
    bx.require(result["engine_fingerprint"] == baseline["engine_fingerprint"] == info["engine_fingerprint"], "前后计算期间引擎内容发生变化，请更新完成后重新 evaluate。")
    missing_nodes = sorted({int(value) for value in bx.active_spec(document).get("nodes", "").split(",") if value} - set(result["nodes"]))
    tree_diagnostics = []
    if missing_nodes:
        # Inspect the retained native tree, without guessing that dropped nodes
        # are unsupported or automatically changing the player's allocation.
        queried = engine.calculate(root, result["xml"], queries=[{"kind": "nodes", "query": "id:" + str(node), "limit": 1} for node in missing_nodes[:8]])
        bx.require(queried["engine_fingerprint"] == result["engine_fingerprint"], "天赋诊断期间引擎发生变化，请更新完成后重新 evaluate。")
        for node_id, query in zip(missing_nodes, queried["catalog_batch"]):
            node = next((entry for entry in query["entries"] if entry["id"] == node_id), None)
            detail = {"id": node_id, "found": node is not None}
            if node:
                detail.update({key: node[key] for key in ("name", "ascendancy_name", "reachable", "points_to_allocate", "path") if key in node})
                detail["name"] = node.get("display_name", node["name"])
            tree_diagnostics.append(detail)
    errors, warnings = mechanical_issues(document, result, intent)
    mechanical_errors = list(errors)
    constraint_errors = check_constraints(original, document, baseline, result, intent, plan)
    errors.extend(constraint_errors)
    audit = readiness(result, intent, plan)
    errors.extend(audit["errors"])
    warnings.extend(audit["warnings"])
    requirements = material_requirements(original, document, baseline, result)
    budget = budget_summary(plan, intent, requirements)
    if budget["over_budget"]: errors.append("已列出的成本上限超过预算。")
    warnings.extend(budget["warnings"])
    if baseline["level"] != result["level"]:
        warnings.append("候选包含升级收益；对比不是相同等级。")
    if baseline["class"] != result["class"]:
        warnings.append("此方案需要重新练角色，不是对原角色直接洗点。")
    if plan.get("operations", {}).get("items") and not plan.get("legality_notes"):
        warnings.append("新装备的游戏内词缀合法性/可获取性尚无说明；PoB 解析成功不等于装备可获得。")
    comparison_notes = []
    if baseline.get("warnings"):
        comparison_notes.append("原始方案带有引擎诊断提示，原面板不是已验证的实战基准。")
    if baseline["level"] != result["level"]:
        comparison_notes.append("前后等级不同。")
    if baseline["main_skill"] != result["main_skill"]:
        comparison_notes.append("前后主技能不同，覆盖范围、操作与伤害口径需逐项核对。")
    baseline_flasks = [item["slot"] for item in baseline["equipped"] if item["slot"].startswith("Flask ") and item.get("active")]
    if bool(baseline["stats"].get("GuardSkillActive")) != audit["conditions"]["guard_active"] or baseline_flasks != audit["conditions"]["active_flasks"]:
        comparison_notes.append("前后守护/药剂开启条件不同。")
    if plan.get("operations", {}).get("config"):
        comparison_notes.append("候选包含独有战斗条件，收益须结合覆盖依据理解。")
    if comparison_notes:
        warnings.append("只提供诊断数值差，不输出同条件实战提升百分比：" + "".join(comparison_notes))
    xml = roundtrip(root, result, document)
    version_proof = facts.version_fact(intent["version_source"], intent["game_version"], intent["league"], run / ".poe-evidence", result["engine_fingerprint"])
    if version_proof["status"] != "source_observed": warnings.append("目标赛季版本正文未核实：" + version_proof.get("reason", "缺少依据"))
    destination = run / "candidates" / args.name
    write_text(destination / "candidate.xml", xml)
    outcome = {"skill_version": VERSION, "name": plan.get("name", args.name), "evaluated_at": now(), "engine_version": result["engine_version"], "engine_fingerprint": result["engine_fingerprint"],
               "intent_hash": state["intent_hash"], "original_sha256": state["original_sha256"], "xml_sha256": bx.digest(xml),
               "status": "invalid" if errors else "validated", "roundtrip_verified": True,
               "validation": {"mechanical": "passed" if not mechanical_errors + audit["errors"] else "failed",
                              "user_constraints": "passed" if not constraint_errors else "failed",
                              "version_basis": version_proof["status"], "mechanics_sources": "requires_review", "delivery_data": "roundtrip_verified",
                              "scope": "validated 表示已实现的机械/资源/用户约束通过；网页论据与设计文字另行审查。"},
               "actual_changes": actual_changes(baseline, result),
               "tree_diagnostics": tree_diagnostics,
               "target_version_evidence": version_proof,
               "baseline_warnings": baseline.get("warnings", []),
               "errors": list(dict.fromkeys(errors)), "warnings": list(dict.fromkeys(warnings)), "budget": budget, "readiness": audit,
               "comparison_notes": comparison_notes, "comparison": compare(baseline, result, not comparison_notes), "plan": plan, "crafted_items": crafted_items, "result": {k: v for k, v in result.items() if k != "xml"}}
    write_json(destination / "evaluation.json", outcome)
    write_json(destination / "plan.json", plan)
    state["phase"] = "evaluated"
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"candidate": str(destination), "status": outcome["status"], "errors": outcome["errors"], "warnings": outcome["warnings"], "budget": budget,
            "baseline_warnings": outcome["baseline_warnings"], "tree_diagnostics": tree_diagnostics,
            "comparison": outcome["comparison"], "readiness": audit, "roundtrip_verified": True,
            "next": "读取 evaluation.json 中实际技能/触发/伤害分项及计划说明，审查是否达到用户目标。有辅助选择空间时用 probe 比较，再将选定方案 evaluate/export。" if not errors else
                    "先解决 errors；保持已确认的目标、进度和下限，修改方案后重新 evaluate。" + (" tree_diagnostics 列出前8个丢失节点：found=false 只表示当前树/珠宝配置未找到；found=true 时核对升华归属和相对已保留树的 path，不把缺连接当引擎不支持。path 含目标节点，需合并并检查点数，不能代替整个节点表。" if tree_diagnostics else "")}


def actual_changes(before, after):
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
    return rows or ["核心职业、主技能、在用宝石与天赋没有变化；装备和配置差异见后文。"]


def scenario_summary(result):
    """Describe the native calculation input, including engine-provided defaults."""
    values = result["config_values"]
    boss = values.get("enemyIsBoss")
    label = result["config_labels"].get("enemyIsBoss")
    enemy = f"{label}（{boss}）" if label else str(boss) if boss is not None else "未提供（未推断内部默认规则）"
    return f"敌人类型：{enemy}；敌人等级参数：{values.get('enemyLevel', '未提供（按引擎内部规则）')}。"


def report_html(state, outcome):
    esc = lambda value: html.escape(str(value), quote=True)
    intent, plan = state["intent"], outcome["plan"]
    result = outcome["result"]
    def listing(values):
        return "<ul>" + "".join("<li>" + esc(v) + "</li>" for v in values) + "</ul>" if values else "<p>无。</p>"
    rows = []
    for key, metric in outcome["comparison"].items():
        rows.append(f"<tr><th>{esc(METRICS[key])}</th><td>{metric['before']:,.2f}</td><td>{metric['after']:,.2f}</td><td>{metric['delta']:+,.2f}</td></tr>")
    skill_rows = []
    for gem in outcome["result"]["gems"]:
        if not gem.get("group_enabled") or gem.get("source") or not active_slot(gem.get("slot", ""), outcome["result"]):
            continue
        state_label = "启用" if gem.get("enabled") else "未启用（仍占孔）"
        skill_rows.append(f"<tr><td>{esc(gem.get('slot', ''))} · 第 {gem['group']} 组 / 第 {gem['index']} 个</td><td>{esc(gem.get('display_name',gem.get('name',gem.get('id'))))}</td><td>{gem.get('level',1)} 级 / {gem.get('quality',0)} 品质</td><td>+{gem.get('socket_quality_bonus',0)} 品质</td><td>{state_label}</td></tr>")
    sources = []
    for source in plan.get("sources", []):
        url = source.get("url", "")
        label = esc(source.get("title", url))
        if url.startswith(("http://", "https://")):
            label = f'<a href="{esc(url)}" rel="noreferrer">{label}</a>'
        elif url:
            label += " · " + esc(url)
        sources.append(f'<li>{label} · {esc(source.get("version","版本待核实"))} · {esc(source.get("checked_at",""))}</li>')
    shopping = []
    labels = {"owned": "已有/无需购买", "verified_cn": "国服价格已查证", "estimate": "估计", "unknown": "待核实"}
    for item in plan.get("shopping", []):
        price = "" if item.get("status") in ("owned", "unknown") else f" · {item.get('low')}–{item.get('high')} {item.get('currency')}"
        shopping.append(item["description"] + " · " + labels.get(item.get("status", "unknown"), "待核实") + price)
    b = outcome["budget"]
    materials = [item["description"] + " · " + labels[item["status"]] for item in b.get("materials", [])]
    audit = outcome.get("readiness", {})
    conditions = audit.get("conditions", {})
    condition_summary = ("常态" if conditions.get("mode") == "steady" else "条件") + "面板；守护增益：" + ("开启" if conditions.get("guard_active") else "关闭") + "；启用药剂：" + ("、".join(conditions.get("active_flasks", [])) or "无")
    resources = []
    for item in audit.get("resources", []):
        note = f"{item['resource']}：每秒消耗 {item['cost_per_second']:.2f}，扣除自身持续伤害后净秒回 {item['regeneration']:.2f}，命中吸取 {item['leech_on_hit']:.2f}，计入充能 {item.get('recharge', 0):.2f}。"
        if item.get("automatic_cost_per_second"):
            note += f"其中自动触发额外消耗 {item['automatic_cost_per_second']:.2f}/秒。"
        if item.get("maximum_burst_seconds") is not None:
            note += f"静态净消耗估算最多连续 {item['maximum_burst_seconds']:.2f} 秒；不代表可持续。"
        resources.append(note)
    active_skill_rows = []
    for skill in outcome["result"].get("active_skills", []):
        if skill.get("disabled"): continue
        costs = skill.get("costs", {})
        active_skill_rows.append("<tr>" + "".join("<td>" + esc(value) + "</td>" for value in (
            skill.get("display_name", skill["name"]), "守护收益未计入；保留使用检查" if skill.get("benefit_excluded") else "主技能" if skill.get("is_main") else "其他启用技能",
            skill.get("trigger") or skill.get("info") or "按操作说明使用",
            " / ".join(f"{costs[key]:g}" if key in costs else "—" for key in ("ManaCost", "LifeCost", "ESCost")))) + "</tr>")
    skill_table = '<h3>主动技能与单次资源</h3><p>单次消耗依次为魔力 / 生命 / 直接能盾；EB 会改变实际支付池。表格不把手动技能全部假定为同时满速使用。</p><table><tr><th>技能</th><th>作用</th><th>引擎触发/使用信息</th><th>单次消耗</th></tr>' + ''.join(active_skill_rows) + '</table>'
    damage_rows = []
    for source in outcome["result"].get("damage_sources", []):
        damage_rows.append("<tr>" + "".join("<td>" + esc(value) + "</td>" for value in (
            source.get("display_name", source.get("name", "")), source.get("count", 1), f"{source.get('dps',0):,.1f}", source.get("skillPart", ""), source.get("trigger", ""))) + "</tr>")
    damage_table = ('<h3>原生 Full DPS 分项</h3><p>仅包含 XML 中勾选计入总伤害的技能，数量与覆盖须结合实际玩法。主技能综合 DPS 不是所有已装技能的总和。</p><table><tr><th>技能/来源</th><th>计入数量</th><th>分项 DPS</th><th>技能部分</th><th>触发</th></tr>' + ''.join(damage_rows) + '</table>') if damage_rows else '<p>本方案没有原生 Full DPS 分项；不要把主技能面板解释为所有已装技能的总伤害。</p>'
    scenario = json.dumps(intent["scenario"], ensure_ascii=False)
    actual_scenario = json.dumps(result["config_values"], ensure_ascii=False, indent=2)
    changes = json.dumps(plan.get("operations", {}), ensure_ascii=False, indent=2)
    condition_notes = json.dumps(plan.get("config_evidence", {}), ensure_ascii=False, indent=2)
    crafted_note = ""
    if outcome.get("crafted_items"):
        crafted_note = '<h3>目标稀有装备的词缀依据</h3><p>以下目标装由本机普通词缀库生成，已核对基底适用、物等、词缀冲突及最多三前缀/三后缀。它们是采购/制作目标；当前供给、制作方法和费用仍需核实。</p>'
        crafted_note += listing([item["base"] + f"：{sum(a['type'] == 'Prefix' for a in item['affixes'])} 前缀 / {sum(a['type'] == 'Suffix' for a in item['affixes'])} 后缀；具体属性见采购清单与配装文件。" for item in outcome["crafted_items"]])
    baseline_note = ""
    if outcome.get("baseline_warnings"):
        baseline_note = '<p class="note">原始方案存在下列诊断提示；有资源不足等问题时，其面板仅作诊断参考，不能视为游戏内可持续达到的表现。</p>' + listing(outcome["baseline_warnings"])
    version_note = "已抓取官方版本正文；具体机制与国服差异另行核对。" if outcome.get("target_version_evidence", {}).get("status") == "source_observed" else "待核实草案：未完成目标版本正文核对，不能据此确认适用于目标赛季。"
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>POE 配装说明</title>
<style>body{{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;background:#f5f4f0;color:#1f2933;max-width:1040px;margin:32px auto;padding:0 24px}}header,section{{background:white;border:1px solid #dddcd6;border-radius:12px;padding:24px;margin:18px 0}}h1{{font-size:30px;margin:0}}h2{{font-size:21px;color:#235b53}}small{{color:#58666c}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:right;padding:9px;border-bottom:1px solid #e5e7eb}}th:first-child{{text-align:left}}pre{{white-space:pre-wrap;word-break:break-word;font-size:13px}}a{{color:#17695e}}.note{{background:#fff8df;padding:14px;border-radius:8px}}@media print{{body{{margin:0}}section{{break-inside:avoid}}}}</style>
<header><small>POE 配装助手 · {esc(outcome['evaluated_at'])} · PoB {esc(outcome['engine_version'])} · 游戏数据 {esc(outcome['result']['data_version'])}</small><h1>{esc(outcome['name'])}</h1><p>已按最终 XML 复算并回读。预算状态：{esc(labels[b['status']])}。<a href="{esc(intent['version_source'])}">赛季版本依据</a>（{esc(intent['version_checked_at'])} 查阅）</p><p>{esc(version_note)}</p><p>程序核对计算、资源和已记录的用户约束；来源论据与设计说明仍需核对，不代表已经游戏实战验证。</p></header>
<section><h2>实际改动</h2>{listing(outcome['actual_changes'])}</section>
<section><h2>你的目标</h2><p>{esc(intent['goal'])}</p><p>{esc(intent['league'])} · 目标等级 {intent['target_level']} · 预算 {esc(intent['budget']['amount'])} {esc(intent['budget']['currency'])}</p><p>{esc(intent['notes'])}</p></section>
<section><h2>前后对比</h2><p>原角色：{esc(state.get('baseline_label','用户导入角色'))}；候选：{esc(outcome['result']['main_skill_name'])}。总 DPS 只汇总勾选计入的技能，不能代替实际刷图速度。</p>{baseline_note}<table><thead><tr><th>指标</th><th>原方案</th><th>新方案</th><th>变化</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section><h2>技能与插槽安排</h2><p>按下列顺序镶嵌，连线以 XML 中装备为准。本机识别到匹配孔色品质加成为 {esc(result.get('matching_socket_quality_bonus', '未确认'))}。表内为本机按孔位计算的部分，其他装备或辅助的品质影响以 PoECharm 最终面板为准；实际游戏规则须与目标版本一致。</p><table><tr><th>位置与顺序</th><th>宝石</th><th>基础等级 / 品质</th><th>匹配孔色加成</th><th>状态</th></tr>{''.join(skill_rows)}</table></section>
<section><h2>购买与准备</h2><p>已列成本：{b['low']:g}–{b['high']:g} {esc(b['currency'])}。待核实项目未计入这个范围；原 XML 也不代表游戏仓库盘点。</p><h3>程序从前后 XML 检测到的物品准备</h3>{listing(materials)}<h3>采购、制作和其他步骤</h3>{listing(shopping)}{crafted_note}<h2>切换顺序</h2>{listing(plan.get('migration_steps',[]))}</section>
<section><h2>计算条件与待核实项</h2><p>{esc(condition_summary)}</p><p>{esc(scenario_summary(result))}</p><p>共同场景：<code>{esc(scenario)}</code></p><details><summary>引擎实际采用的配置与文件核对依据</summary><p>以下为本次计算读回的配置输入，含引擎填入的默认值；选中条件的实战覆盖由设计说明解释。</p><pre>{esc(actual_scenario)}</pre><p>配装 XML SHA256：<code>{esc(outcome['xml_sha256'])}</code><br>计算文件指纹：<code>{esc(outcome['engine_fingerprint'])}</code></p></details>{listing(resources)}{skill_table}{damage_table}{listing(plan.get('assumptions',[]))}{listing(outcome['warnings'])}<p>{esc(plan.get('legality_notes',''))}</p><p class="note">结果用于配装规划。PoB 未完整支持的机制、实际操作和装备供给可能影响游戏表现。原始角色文件保持不变。</p></section>
<section><h2>资料依据与设计说明</h2><ul>{''.join(sources)}</ul><p>{esc(plan['summary'])}</p><details><summary>具体改动及条件依据</summary><pre>{esc(changes)}</pre><pre>{esc(condition_notes)}</pre></details></section>
<section><h2>如何使用</h2><p>在 PoECharm 中打开同目录的“改进配装.xml”，核对当前主技能、装备与天赋；按照上面的准备顺序在游戏内调整。它不会修改服务器角色。</p></section></html>'''


def read_candidate(args):
    run, state = read_run(args.run)
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称无效。")
    folder = run / "candidates" / args.name
    outcome = read_json(folder / "evaluation.json")
    bx.require(outcome.get("skill_version") == VERSION, "技能校验规则已更新，请用当前版本重新 evaluate。")
    bx.require(outcome.get("status") == "validated" and outcome.get("roundtrip_verified"), "候选仍有校验错误，请先修正并重新 evaluate。")
    bx.require(outcome["intent_hash"] == state.get("intent_hash") and outcome["original_sha256"] == state["original_sha256"], "意图或原角色已变化，必须重新 evaluate。")
    xml = (folder / "candidate.xml").read_text(encoding="utf-8")
    bx.require(bx.digest(xml) == outcome["xml_sha256"], "候选文件在计算后被修改，请重新 evaluate。")
    root = engine.resolve_root(args.pob or state["pob_path"])
    bx.require(engine.check(root)["engine_fingerprint"] == outcome.get("engine_fingerprint"), "引擎、规则数据或适配器已变化，请重新 evaluate；版本号相同也不能沿用旧结果。")
    return run, state, outcome, xml, root


def probe(args):
    """Compare one gem slot in a single native worker, without changing a candidate."""
    run, state, outcome, xml, root = read_candidate(args)
    ids = [value.strip() for value in args.ids.split(",")]
    bx.require(1 <= len(ids) <= 11 and all(re.fullmatch(r"[A-Za-z0-9_]+", value) for value in ids) and len(set(ids)) == len(ids), "--ids 需要 1–11 个不同的本机宝石 skill_id，以逗号分隔。")
    source = bx.parse(xml)
    group = bx.index_one(bx.groups(source), args.group, "技能组")
    bx.require(group.get("enabled", "true") != "false" and not group.get("source"), "probe 只比较当前启用的普通宝石组。")
    bx.index_one(group.findall("Gem"), args.index, "宝石")
    documents = []
    for skill_id in ids:
        change = {"group": args.group, "index": args.index, "skill_id": skill_id}
        for field in ("level", "quality"):
            value = getattr(args, field, None)
            if value is not None: change[field] = value
        documents.append(bx.apply_plan(source, {"mode": outcome["plan"]["mode"], "summary": "单个宝石比较", "operations": {"gems": [change]}}))
    calculations = engine.calculate(root, xml_batch=[xml] + [bx.serialise(document) for document in documents])["calculations"]
    baseline = calculations[0]
    original = bx.read_document(run / "original.xml")
    before = read_json(run / "baseline.json")
    rows = []
    keys = ("CombinedDPS", "MinionCombinedDPS", "FullDPS", "HitChance", "Life", "EnergyShield", "ManaCost", "ManaPerSecondCost", "ManaRegenRecovery", "ManaLeechGainRate", "LifePerSecondCost", "LifeRegenRecovery", "LifeLeechGainRate", "ESCost", "ESPerSecondCost", "EnergyShieldRegenRecovery", "EnergyShieldLeechGainRate", "NetLifeRegen", "NetEnergyShieldRegen")
    dps_metric = "MinionCombinedDPS" if any(skill.get("is_main") and skill.get("minion") for skill in baseline.get("active_skills", [])) else "CombinedDPS"
    for skill_id, document, result in zip(ids, documents, calculations[1:]):
        errors, warnings = mechanical_issues(document, result, state["intent"])
        errors.extend(check_constraints(original, document, before, result, state["intent"], outcome["plan"]))
        audit = readiness(result, state["intent"], outcome["plan"])
        errors.extend(audit["errors"])
        warnings.extend(audit["warnings"])
        gem = next(gem for gem in result["gems"] if gem["group"] == args.group and gem["index"] == args.index)
        rows.append({"skill_id": skill_id, "name": gem.get("display_name", gem.get("name")), "level": gem.get("level"), "quality": gem.get("quality"),
                     "stats": {key: result["stats"].get(key) for key in keys},
                     "dps_metric": dps_metric, "dps_delta": result["stats"].get(dps_metric, 0) - baseline["stats"].get(dps_metric, 0),
                     "errors": list(dict.fromkeys(errors)), "warnings": list(dict.fromkeys(warnings))})
    return {"status": "screened", "baseline": {key: baseline["stats"].get(key) for key in keys}, "alternatives": rows,
            "next": "这是同一配装中单个宝石的比较，不改文件。结合清图/首领、资源和操作选取，不只取最高 DPS。选定后写回完整 plan 并重新 evaluate/export；获取成本及最终 XML 回读尚未在此验证。"}


def export(args):
    run, state, outcome, xml, root = read_candidate(args)
    verification = engine.calculate(root, xml)
    current_errors, _ = mechanical_issues(bx.parse(xml), verification, state["intent"])
    current_errors.extend(readiness(verification, state["intent"], outcome["plan"])["errors"])
    bx.require(not current_errors, "最终校验发现问题，请重新 evaluate：" + "；".join(current_errors))
    bx.require(semantics(verification) == semantics(outcome["result"]), "最终复算的角色数据与候选记录不同。")
    for key in METRICS:
        a, b = verification["stats"].get(key), outcome["result"]["stats"].get(key)
        if a is None and b is None: continue
        bx.require(isinstance(a,(int,float)) and isinstance(b,(int,float)) and math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-5), "最终复算数值变化：" + key)
    output = run / "output"
    intent = state["intent"]
    outcome["target_version_evidence"] = facts.version_fact(intent["version_source"], intent["game_version"], intent["league"], run / ".poe-evidence", verification["engine_fingerprint"])
    facts_ready = outcome["target_version_evidence"]["status"] == "source_observed"
    write_text(output / "改进配装.xml", xml)
    write_text(output / "配装说明.html", report_html(state, outcome))
    state["phase"] = "exported" if facts_ready else "exported_draft"
    state["selected_candidate"] = args.name
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    stats = verification["stats"]
    minion_main = any(skill.get("is_main") and skill.get("minion") for skill in verification.get("active_skills", []))
    damage_keys = ("MinionCombinedDPS", "FullDPS") if minion_main else ("CombinedDPS",)
    hit_key = "MinionHitChance" if minion_main else "HitChance"
    user_summary = [f"配装 XML 已通过本机 PoECharm 计算及保存回读。目标：{state['intent']['league']} / {state['intent']['game_version']}。",
                    scenario_summary(verification),
                    "当前计算场景下：" + "；".join(f"{METRICS[key]} {stats[key]:,.1f}" for key in damage_keys + ("Life", "EnergyShield", hit_key, "ChaosResist") if key in stats) + "。"]
    if not facts_ready:
        user_summary.insert(0, "这是待核实草案：目标版本官方正文尚未核实，暂不能确认为目标赛季可用方案。")
    if outcome.get("comparison_notes"):
        user_summary.append("前后计算存在条件或有效性差异，不提供同条件提升百分比；原面板仅供诊断，比较限制见说明。")
    user_summary.append("DPS 已包含引擎的命中计算。以上是当前主技能口径，其他技能的贡献见说明中实际计入的 Full DPS 分项；实战操作与机制覆盖仍需核实。")
    if outcome["budget"]["status"] in {"unknown", "estimate"}:
        user_summary.append("国服完整采购成本尚未核实，不能承诺预算内买齐。先备齐核心装备和宝石，再按说明切换。")
    if any(resource.get("leech_on_hit", 0) for resource in outcome["readiness"]["resources"]):
        user_summary.append("吸取回复依赖命中；空挥、无敌阶段与转场的续航请看说明中的资源明细。")
    if any(skill.get("is_main") and skill.get("generals_cry") for skill in verification.get("active_skills", [])):
        user_summary.append("此方案由将军之吼幻影攻击，依赖尸体来源与战吼循环；幻影吸取未计入玩家回复。")
    user_summary.append("先打开“配装说明.html”查看准备与切换顺序，再在 PoECharm 中打开“改进配装.xml”。这是规划文件，原角色保持不变。")
    return {"status": state["phase"], "target_version_evidence": outcome["target_version_evidence"], "files": [str(output / "改进配装.xml"), str(output / "配装说明.html")],
            "user_summary": "\n\n".join(user_summary), "final_reply_policy": "交付文件后原样发送 user_summary 作为聊天结论；具体机制、对比和采购步骤已在 HTML，不另行补算或扩写收益。", "budget_status": outcome["budget"]["status"], "warnings": outcome["warnings"]}


def catalog(args):
    root = engine.resolve_root(args.pob)
    class_id = getattr(args, "class_id", None)
    bx.require(class_id is None or not args.input, "--class-id 用于新角色起点查询，不能与 --input 混用。已有角色以 XML 为准。")
    bx.require(class_id is None or args.kind != "translations", "翻译查询不需要 --class-id。")
    if getattr(args, "batch", None):
        queries = read_json(args.batch).get("queries")
        bx.require(isinstance(queries, list) and 1 <= len(queries) <= 30, "batch 的 queries 应包含 1–30 项。")
        for query in queries:
            bx.require(isinstance(query, dict) and set(query) <= {"kind", "query", "limit"}, "批量查询仅接受 kind/query/limit。")
            bx.require(query.get("kind") in {"gems", "skills", "nodes", "uniques", "config", "links", "classes", "bases", "affixes"}, "批量查询不含 translations，请单独查询翻译。")
            bx.require(isinstance(query.get("query", ""), str) and type(query.get("limit", 20)) is int and 1 <= query.get("limit", 20) <= 100, "查询文字或 limit 无效。")
        document = bx.serialise(bx.read_document(Path(args.input))) if args.input else ""
        result = engine.calculate(root, document, queries=queries, class_id=class_id)
        return {"engine_version": result["engine_version"], "tree_version": result["tree_version"], "data_version": result["data_version"],
                "engine_fingerprint": result["engine_fingerprint"], "tree_context": result["tree_context"], "socket_limits": result["socket_limits"], "queries": result["catalog_batch"]}
    bx.require(args.kind, "catalog 需要 --kind 或 --batch。")
    if args.kind == "translations":
        entries = []
        folder = root.parent / "Data" / "Translate" / "zh-rCN"
        bx.require(folder.is_dir(), "该安装目录没有 PoECharm 简体中文翻译数据。")
        for path in sorted(folder.glob("*.csv"), key=lambda path: (path.name != "Items_Gems.txt.csv", path.name)):
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                for row in csv.reader(stream):
                    if len(row) >= 2 and any(args.query.casefold() in cell.casefold() for cell in row[:2]):
                        entries.append({"english": row[0], "chinese": row[1], "source": path.name, "preferred_gem_name": path.name == "Items_Gems.txt.csv"})
                        if len(entries) >= args.limit: return {"entries": entries, "truncated": True}
        return {"entries": entries, "truncated": False}
    document = bx.serialise(bx.read_document(Path(args.input))) if args.input else ""
    result = engine.calculate(root, document, catalog=args.kind, query=args.query, limit=args.limit, class_id=class_id)
    return {"engine_version": result["engine_version"], "tree_version": result["tree_version"], "data_version": result["data_version"],
            "engine_fingerprint": result["engine_fingerprint"], "tree_context": result["tree_context"], "socket_limits": result["socket_limits"], "total": result.get("catalog_total"), "entries": result.get("catalog", [])}


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
        source = args.source or (info.get("version_evidence") or {}).get("url", "")
        engine.validate_game_version(data_version, args.league, data_version, source)
        evidence = facts.version_fact(source, data_version, args.league, output.parent / ".poe-evidence", info["engine_fingerprint"])
        record = {"league": args.league, "game_version": data_version, "engine_fingerprint": info["engine_fingerprint"], "version_evidence": evidence}
        write_json(output, record)
        return {"file": str(output), **record, "next": "阅读正文摘录核对目标赛季；unverified 时联网寻找官方公告，再加 --source URL 重试。不要读取工具内部输出目录或把缺失依据改成 verified。"}
    bx.require(not args.league and not args.source, "--league / --source 仅用于 research --version。")
    ids = args.gems.split(",")
    bx.require(1 <= len(ids) <= 60 and len(set(ids)) == len(ids) and all(re.fullmatch(r"[A-Za-z0-9_]+", value) for value in ids), "--gems 需要 1–60 个不同的本机 skill_id。")
    calculation = engine.calculate(root, queries=[{"kind": "gems", "query": "id:" + value, "limit": 2} for value in ids])
    gems = {entry["skill_id"]: entry for query in calculation["catalog_batch"] for entry in query["entries"] if entry["skill_id"] in ids}
    bx.require(set(gems) == set(ids), "部分宝石 ID 未识别，请先 catalog gems 查询。")
    evidence = facts.gem_facts(gems, output.parent / ".poe-evidence", calculation["engine_fingerprint"], engine.require_data_version(calculation))
    write_json(output, {"gems": evidence, "engine_fingerprint": calculation["engine_fingerprint"], "data_version": calculation["data_version"]})
    return {"file": str(output), "source_checked": [key for key, value in evidence.items() if value["status"] == "source_checked"],
            "unverified": [key for key, value in evidence.items() if value["status"] != "source_checked"],
            "next": "读取 gems[skill_id].native 的技能原文、使用门槛和 level_requirements[].base_resources 基础资源，以及 choices 的网页章节/任务/NPC/职业。基础数据不等于完整连线实算；两类依据不能互相替代。已返回字段无需重复 catalog。未核实条目需另查，不能自填 verified。"}


def parser():
    p = argparse.ArgumentParser(description="POE 配装助手：本机 PoB 实算、候选校验和 XML 导出。")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "inspect", "catalog", "research", "decode", "start", "intent", "evaluate", "probe", "export", "status", "route"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--pob", help="PoECharm 文件夹、PoB 文件夹或可执行文件；默认自动发现")
        if name in {"inspect", "decode", "start"}: cmd.add_argument("--input", required=True)
        if name in {"start", "intent", "evaluate", "probe", "export", "status"}: cmd.add_argument("--run", required=True)
        if name == "start": cmd.add_argument("--invoked", action="store_true")
        if name == "intent": cmd.add_argument("--file", required=True)
        if name in {"evaluate", "probe", "export"}: cmd.add_argument("--name", required=True)
        if name == "evaluate": cmd.add_argument("--plan", required=True)
        if name == "probe":
            cmd.add_argument("--group", type=int, required=True)
            cmd.add_argument("--index", type=int, required=True)
            cmd.add_argument("--ids", required=True)
            cmd.add_argument("--level", type=int)
            cmd.add_argument("--quality", type=int)
        if name == "decode": cmd.add_argument("--out", required=True)
        if name == "inspect":
            cmd.add_argument("--calculate", action="store_true")
            cmd.add_argument("--out")
        if name == "catalog":
            cmd.add_argument("--kind", choices=["gems", "skills", "nodes", "uniques", "config", "translations", "links", "classes", "bases", "affixes"])
            cmd.add_argument("--batch")
            cmd.add_argument("--query", default="")
            cmd.add_argument("--input")
            cmd.add_argument("--class-id", type=int, help="新角色的本机职业 ID，按该职业起点查天赋；不能与 --input 同用")
            cmd.add_argument("--out", help="将完整查询保存到工作区 JSON，终端只返回索引")
            cmd.add_argument("--limit", type=int, default=20)
        if name == "research":
            subject = cmd.add_mutually_exclusive_group(required=True)
            subject.add_argument("--gems", help="逗号分隔的本机宝石 skill_id")
            subject.add_argument("--version", action="store_true", help="读取官方公告正文并返回简短版本依据")
            subject.add_argument("--trials", action="store_true", help="读取当前版本前三次迷宫的剧情试炼位置")
            cmd.add_argument("--league", help="用户确认的目标赛季或永久区，用于 --version")
            cmd.add_argument("--source", help="公开官方公告 URL；本机版本没有已有来源时填写")
            cmd.add_argument("--out", required=True, help="工作区事实记录 JSON")
        if name == "route":
            cmd.add_argument("--invoked", action="store_true")
            cmd.add_argument("--file", required=True)
            cmd.add_argument("--out", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = {"skill_version": VERSION, **engine.health(engine.resolve_root(args.pob))}
        elif args.command == "inspect":
            if args.out:
                bx.require(Path(args.out).resolve() != Path(args.input).resolve(), "检查结果不能覆盖原始角色文件。请为 --out 使用不同文件名。")
            document = bx.read_document(Path(args.input))
            result = bx.summary(document)
            if args.calculate:
                computed = engine.calculate(engine.resolve_root(args.pob), bx.serialise(document))
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
        elif args.command == "probe": result = probe(args)
        elif args.command == "export": result = export(args)
        elif args.command == "research": result = research(args)
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
        elif args.command == "route":
            bx.require(args.invoked, "仅在用户明确调用技能后规划开荒路线。")
            route = read_json(args.file)
            ids = routes.gem_ids(route)
            queries = [{"kind": "gems", "query": "id:" + skill_id, "limit": 2} for skill_id in ids]
            queries += [{"kind": "links", "query": query, "limit": 1} for query in routes.link_queries(route)]
            queries += [{"kind": "bases", "query": "id:" + name, "limit": 2} for name in routes.base_names(route)]
            queries.append({"kind": "nodes", "query": route["ascendancy"], "limit": 100})
            calculation = engine.calculate(engine.resolve_root(args.pob), queries=queries)
            gems = {entry["skill_id"]: entry for query in calculation["catalog_batch"] if query["kind"] == "gems" for entry in query["entries"]}
            compatibility = {query["query"]: query["entries"][0] for query in calculation["catalog_batch"] if query["kind"] == "links"}
            nodes = [entry for query in calculation["catalog_batch"] if query["kind"] == "nodes" for entry in query["entries"]]
            bases = {entry["name"]: entry for query in calculation["catalog_batch"] if query["kind"] == "bases" for entry in query["entries"]}
            checked, warnings = routes.validate(route, gems, engine.require_data_version(calculation), calculation["classes"], compatibility, nodes,
                                                socket_limits=calculation["socket_limits"], bases=bases)
            output = Path(args.out).resolve()
            bx.require(output != Path(args.file).resolve(), "路线说明不能覆盖输入 JSON。")
            bx.require(output.suffix.lower() == ".html", "--out 请指定 HTML 文件。")
            evidence_folder = output.parent / ".poe-evidence"
            evidence = facts.gem_facts(gems, evidence_folder, calculation["engine_fingerprint"], engine.require_data_version(calculation))
            fact_warnings, unresolved = facts.reconcile_acquisitions(checked, evidence)
            warnings += fact_warnings
            trial_proof = facts.trial_facts(evidence_folder, calculation["engine_fingerprint"], checked["game_version"]) if any(stage["ascendancy_points"] for stage in checked["stages"]) else {"status": "not_required"}
            trial_warnings, trials_unverified = facts.reconcile_trial_progress(checked, trial_proof)
            warnings += trial_warnings
            version_source = next((source for source in checked["sources"] if source["id"] == checked.get("version_source")), {})
            version_proof = facts.version_fact(version_source.get("url", ""), checked["game_version"], checked["league"], evidence_folder, calculation["engine_fingerprint"])
            checked["fact_evidence"] = {"gems": evidence, "version": version_proof, "trials": trial_proof}
            if version_proof["status"] != "source_observed": warnings.append("赛季正文依据未完成程序核对：" + version_proof.get("reason", "未核实"))
            facts_ready = not unresolved and not trials_unverified and version_proof["status"] == "source_observed"
            checked["delivery_status"] = "route_checked" if facts_ready else "route_draft"
            evidence_file = output.with_suffix(".evidence.json")
            write_json(evidence_file, {"engine_fingerprint": calculation["engine_fingerprint"], "checked_route": checked, "warnings": warnings})
            write_text(output, routes.render(checked, warnings, calculation["engine_version"], calculation["tree_version"]))
            result = {"status": checked["delivery_status"], "file": str(output), "evidence_file": str(evidence_file), "unverified_gems": unresolved, "trial_evidence_status": trial_proof["status"],
                      "stages": len(checked["stages"]), "gem_types_checked": len(gems), "engine_version": calculation["engine_version"], "warnings": warnings, "not_calculated": "路线等级与依赖校验；未实算完整阶段装备 DPS/生存。需要具体阶段 XML 时，继续建立完整配装候选并 evaluate/export。"}
            result["user_summary"] = (f"已生成《{checked['name']}》，共 {len(checked['stages'])} 个阶段，详见 {output.name}。\n\n"
                                      + ("宝石获取已逐项对应实际抓取的任务表/通用商店依据，已找到所填版本的官方正文。" if facts_ready else "这是待核实草案：宝石获取、迷宫试炼或官方版本正文存在依据缺口，暂不作为可直接照做的攻略。") + "\n\n"
                                      "本机已检查宝石等级、主辅兼容、物理孔位、升华连接，以及章节、职业和偏好约束。来源符合不等于国服差异与全部机制正确，仍需核对目标赛季。\n\n"
                                      "尚未使用完整阶段装备实算伤害、生存和续航。按每段的领取进度与切换条件推进；实际角色形成后，可提供新的 XML 继续计算。")
            result["reply_policy"] = "交付 HTML 后原样使用 user_summary；不要在最终聊天重复编写一套未经实算的伤害、抗性或续航承诺。"
        else:
            run, state = read_run(args.run)
            result = {"run": str(run), "phase": state["phase"], "pob_path": state["pob_path"], "engine_version": state["engine_version"], "skill_version": VERSION, "intent": state.get("intent"), "candidates": [p.parent.name for p in (run / "candidates").glob("*/evaluation.json")]}
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
