"""TeleAgent-facing POE build workflow. Run --help; stdout is always JSON."""
from __future__ import annotations

import argparse
import copy
import csv
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

VERSION = "1.0.0"
METRICS = {
    "CombinedDPS": "主技能综合 DPS", "FullDPS": "已选技能总 DPS", "HitChance": "命中率 %",
    "Life": "生命", "EnergyShield": "能量护盾", "ManaUnreserved": "可用魔力",
    "LifeRegenRecovery": "生命秒回", "ManaRegenRecovery": "魔力秒回",
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
    info = engine.check(root)
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
    write_json(run / "inspection.json", bx.summary(document))
    return {"run": str(run), "phase": "clarifying", "inspection": str(run / "inspection.json"), "next": "结合角色信息逐轮确认目标、预算、改装范围和场景，再用 intent 保存用户已确认的约束。"}


def validate_intent(value, original):
    allowed = {"confirmed", "goal", "league", "mode", "budget", "target_level", "allow_class_change", "allow_ascendancy_change", "allow_main_skill_change", "keep_slots", "keep_skill_ids", "scenario", "baseline_main_group", "minimum_stats", "quest_passive_points", "ascendancy_points", "notes"}
    bx.require(not (set(value) - allowed), "意图包含未知字段：" + ", ".join(sorted(set(value) - allowed)))
    bx.require(value.get("confirmed") is True, "先向用户归纳并明确目标；confirmed 必须对应用户已表达的意图。")
    for field in ("goal", "league"):
        bx.require(isinstance(value.get(field), str) and value[field].strip(), f"需要已确认的 {field}。")
    bx.require(value.get("mode") in {"refine", "rebuild", "both"}, "mode 应为 refine/rebuild/both。")
    budget = value.get("budget")
    bx.require(isinstance(budget, dict) and set(budget) <= {"amount", "currency"}, "budget 需要 amount 与 currency。")
    bx.require(isinstance(budget.get("amount"), (int, float)) and not isinstance(budget["amount"], bool) and math.isfinite(budget["amount"]) and budget["amount"] >= 0, "预算金额应为有限非负数。")
    bx.require(isinstance(budget.get("currency"), str) and budget["currency"], "需要预算通货单位。")
    defaults = {"target_level": int(original.find("Build").get("level")), "allow_class_change": False,
                "allow_ascendancy_change": False, "allow_main_skill_change": False,
                "keep_slots": [], "keep_skill_ids": [], "scenario": {}, "minimum_stats": {},
                "baseline_main_group": int(original.find("Build").get("mainSocketGroup", "1")),
                "quest_passive_points": 23, "ascendancy_points": 8, "notes": ""}
    result = {**defaults, **value}
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
    state["intent"] = intent
    state["intent_hash"] = bx.digest(json.dumps(intent, ensure_ascii=False, sort_keys=True))
    state["phase"] = "designing"
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"run": str(run), "phase": state["phase"], "intent": intent, "next": "联网核实版本和候选方案，写 plan.json 后 evaluate。旧意图下的候选不能导出。"}


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
    return [gem for gem in result["gems"] if gem.get("enabled") and gem.get("group_enabled") and active_slot(gem.get("slot", ""), result)]


def mechanical_issues(document, result, intent):
    errors, warnings = [], list(result.get("warnings", []))
    stats = result["stats"]
    config_keys = set(result["config_keys"])
    for entry in bx.active_config(document).findall("Input"):
        if entry.get("name") not in config_keys:
            errors.append("引擎不识别配置：" + entry.get("name", ""))
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
    for gem in active_gems(result):
        if not gem.get("recognised"):
            errors.append(f"未知宝石：第 {gem['group']} 组第 {gem['index']} 个。")
        for req, available in (("req_level", result["level"]), ("req_str", stats.get("Str", 0)), ("req_dex", stats.get("Dex", 0)), ("req_int", stats.get("Int", 0))):
            if (gem.get(req) or 0) > available:
                errors.append(f"{gem.get('name',gem.get('id'))} 的 {req} 需求 {gem[req]} 超过 {available:g}。")
    equipped = {item["slot"]: item for item in result["equipped"]}
    for name, item in equipped.items():
        if not active_slot(name, result): continue
        if not item.get("base_recognised"):
            errors.append(f"{name} 装备基底未被识别。")
        for req, available in (("level", result["level"]), ("str", stats.get("Str", 0)), ("dex", stats.get("Dex", 0)), ("int", stats.get("Int", 0))):
            if (item.get("requirements", {}).get(req) or 0) > available:
                errors.append(f"{name} 装备的 {req} 需求未满足。")
        if item.get("unparsed"):
            warnings.append(f"{name} 存在引擎未完整解析的词缀：" + "；".join(item["unparsed"]))
    # PoB lets a user create a socket group independently from physical sockets.
    item_texts = {int(item.get("id")): bx.text_item(item) for item in document.findall("Items/Item")}
    used_sockets = {}
    for group in bx.groups(document):
        slot = group.get("slot", "")
        if group.get("source") or not slot or not active_slot(slot, result): continue
        gems = group.findall("Gem")
        if not gems: continue
        item = equipped.get(slot)
        if not item:
            if group.get("enabled", "true") == "true": errors.append(f"技能组的 {slot} 没有装备。")
            continue
        match = re.search(r"^Sockets:\s*(.+)$", item_texts.get(item["id"], ""), re.M)
        socket_groups = [re.findall(r"[RGBWAD]", text) for text in match.group(1).split()] if match else []
        capacity = sum(map(len, socket_groups))
        used_sockets[slot] = used_sockets.get(slot, 0) + len(gems)
        if len(gems) > max(map(len, socket_groups), default=0):
            errors.append(f"{slot} 技能组需要 {len(gems)} 连，装备插槽不足。")
        if used_sockets[slot] > capacity:
            errors.append(f"{slot} 技能宝石总数超过装备的 {capacity} 个插槽。")
    for key, threshold in intent["minimum_stats"].items():
        if key not in stats or stats[key] < threshold:
            errors.append(f"{METRICS[key]} 为 {stats.get(key,'未知')}，低于约定的 {threshold}。")
    if result["main_skill"] == "DefaultAttack":
        errors.append("主技能退回普通攻击，请确认主技能组及技能可用性。")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


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
    return errors


def budget_summary(plan, intent):
    entries = plan.get("shopping", [])
    bx.require(isinstance(entries, list), "shopping 应为数组。")
    low = high = 0
    statuses, warnings = [], []
    for entry in entries:
        bx.require(isinstance(entry, dict) and entry.get("description"), "每项购物清单需要 description。")
        status = entry.get("status", "unknown")
        bx.require(status in {"owned", "verified_cn", "estimate", "unknown"}, "购物状态应为 owned/verified_cn/estimate/unknown。")
        statuses.append(status)
        if status == "owned": continue
        if status == "unknown": continue
        bx.require(entry.get("currency") == intent["budget"]["currency"], "购物清单与预算需要使用同一通货；换算需先核实汇率。")
        lo, hi = entry.get("low"), entry.get("high")
        bx.require(isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi, "价格范围无效。")
        if status == "verified_cn":
            bx.require(entry.get("source", "").startswith(("https://", "http://")) and entry.get("checked_at") and entry.get("league") == intent["league"], "国服已核实价格需要来源链接、时间和相同赛季。")
        low += lo
        high += hi
    if not entries or "unknown" in statuses: status = "unknown"
    elif "estimate" in statuses: status = "estimate"
    elif all(s == "owned" for s in statuses): status = "owned"
    else: status = "verified_cn"
    if status == "unknown": warnings.append("成本尚未完整核实；不得承诺能在预算内买齐。")
    elif status == "estimate": warnings.append("成本含估计价格，尚未证明国服实际可买齐。")
    return {"status": status, "low": low, "high": high, "currency": intent["budget"]["currency"], "limit": intent["budget"]["amount"], "over_budget": high > intent["budget"]["amount"], "warnings": warnings}


def compare(before, after):
    result = {}
    for key in METRICS:
        a, b = before["stats"].get(key), after["stats"].get(key)
        if isinstance(a, (float, int)) and isinstance(b, (float, int)):
            result[key] = {"before": a, "after": b, "delta": b-a, "percent": (b-a)/abs(a)*100 if a else None}
    return result


def semantics(result):
    return {"class": result["class"], "ascendancy": result["ascendancy"], "level": result["level"], "tree_version": result["tree_version"],
            "nodes": result["nodes"], "main_skill": result["main_skill"], "main_group": result["main_group"],
            "gems": [{key: gem.get(key) for key in ("group", "index", "id", "level", "quality", "enabled", "group_enabled", "slot")} for gem in result["gems"]],
            "equipped": [{key: item.get(key) for key in ("slot", "id", "name", "base", "active")} for item in result["equipped"]]}


def roundtrip(root, result, document):
    saved = bx.preserve_extensions(document, bx.parse(result["xml"]))
    xml = bx.serialise(saved)
    reloaded = engine.calculate(root, xml)
    bx.require(semantics(result) == semantics(reloaded), "XML 回读后装备、天赋或技能发生变化，未通过导出检查。")
    for key in METRICS:
        a, b = result["stats"].get(key), reloaded["stats"].get(key)
        if a is None and b is None: continue
        bx.require(isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-5), "XML 回读后的计算结果不一致：" + key)
    return xml


def evaluate(args):
    run, state = read_run(args.run)
    bx.require(state.get("intent"), "先通过多轮对话确认意图，并执行 intent。")
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称请使用 1–48 位小写英文、数字或连字符。")
    intent = state["intent"]
    plan_path = Path(args.plan).resolve()
    plan = read_json(plan_path)
    original = bx.read_document(run / "original.xml")
    root = engine.resolve_root(args.pob or state["pob_path"])
    bx.require(engine.version(root) == state["engine_version"], "此会话期间引擎版本发生变化，请重新建立会话。")
    base_path = run / "baseline.json"
    if base_path.exists() and read_json(base_path).get("intent_hash") == state["intent_hash"]:
        baseline = read_json(base_path)
    else:
        baseline = engine.calculate(root, bx.serialise(prepared(original, intent, baseline=True)))
        baseline["intent_hash"] = state["intent_hash"]
        write_json(base_path, baseline)
    source = original
    if plan.get("template_xml"):
        path = Path(plan["template_xml"])
        source = bx.read_document(path if path.is_absolute() else plan_path.parent / path)
    # Apply shared scenario first; candidate-specific conditions then require evidence.
    document = bx.apply_plan(prepared(source, intent), plan)
    result = engine.calculate(root, bx.serialise(document))
    errors, warnings = mechanical_issues(document, result, intent)
    errors.extend(check_constraints(original, document, baseline, result, intent, plan))
    budget = budget_summary(plan, intent)
    if budget["over_budget"]: errors.append("已列出的成本上限超过预算。")
    warnings.extend(budget["warnings"])
    if baseline["level"] != result["level"]:
        warnings.append("候选包含升级收益；对比不是相同等级。")
    if baseline["class"] != result["class"]:
        warnings.append("此方案需要重新练角色，不是对原角色直接洗点。")
    if plan.get("operations", {}).get("items") and not plan.get("legality_notes"):
        warnings.append("新装备的游戏内词缀合法性/可获取性尚无说明；PoB 解析成功不等于装备可获得。")
    xml = roundtrip(root, result, document)
    destination = run / "candidates" / args.name
    write_text(destination / "candidate.xml", xml)
    outcome = {"skill_version": VERSION, "name": plan.get("name", args.name), "evaluated_at": now(), "engine_version": result["engine_version"],
               "intent_hash": state["intent_hash"], "original_sha256": state["original_sha256"], "xml_sha256": bx.digest(xml),
               "status": "invalid" if errors else "validated", "roundtrip_verified": True,
               "errors": list(dict.fromkeys(errors)), "warnings": list(dict.fromkeys(warnings)), "budget": budget,
               "comparison": compare(baseline, result), "plan": plan, "result": {k: v for k, v in result.items() if k != "xml"}}
    write_json(destination / "evaluation.json", outcome)
    write_json(destination / "plan.json", plan)
    state["phase"] = "evaluated"
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"candidate": str(destination), "status": outcome["status"], "errors": outcome["errors"], "warnings": outcome["warnings"], "budget": budget,
            "comparison": outcome["comparison"], "roundtrip_verified": True}


def report_html(state, outcome):
    esc = lambda value: html.escape(str(value), quote=True)
    intent, plan = state["intent"], outcome["plan"]
    def listing(values):
        return "<ul>" + "".join("<li>" + esc(v) + "</li>" for v in values) + "</ul>" if values else "<p>无。</p>"
    rows = []
    for key, metric in outcome["comparison"].items():
        rows.append(f"<tr><th>{esc(METRICS[key])}</th><td>{metric['before']:,.2f}</td><td>{metric['after']:,.2f}</td><td>{metric['delta']:+,.2f}</td></tr>")
    sources = []
    for source in plan.get("sources", []):
        url = source.get("url", "")
        if url.startswith(("http://", "https://")):
            sources.append(f'<li><a href="{esc(url)}" rel="noreferrer">{esc(source.get("title",url))}</a> · {esc(source.get("version","版本待核实"))} · {esc(source.get("checked_at",""))}</li>')
    shopping = []
    labels = {"owned": "已有/无需购买", "verified_cn": "国服价格已查证", "estimate": "估计", "unknown": "待核实"}
    for item in plan.get("shopping", []):
        price = "" if item.get("status") in ("owned", "unknown") else f" · {item.get('low')}–{item.get('high')} {item.get('currency')}"
        shopping.append(item["description"] + " · " + labels.get(item.get("status", "unknown"), "待核实") + price)
    b = outcome["budget"]
    scenario = json.dumps(intent["scenario"], ensure_ascii=False)
    changes = json.dumps(plan.get("operations", {}), ensure_ascii=False, indent=2)
    condition_notes = json.dumps(plan.get("config_evidence", {}), ensure_ascii=False, indent=2)
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>POE 配装说明</title>
<style>body{{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;background:#f5f4f0;color:#1f2933;max-width:1040px;margin:32px auto;padding:0 24px}}header,section{{background:white;border:1px solid #dddcd6;border-radius:12px;padding:24px;margin:18px 0}}h1{{font-size:30px;margin:0}}h2{{font-size:21px;color:#235b53}}small{{color:#58666c}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:right;padding:9px;border-bottom:1px solid #e5e7eb}}th:first-child{{text-align:left}}pre{{white-space:pre-wrap;word-break:break-word;font-size:13px}}a{{color:#17695e}}.note{{background:#fff8df;padding:14px;border-radius:8px}}@media print{{body{{margin:0}}section{{break-inside:avoid}}}}</style>
<header><small>POE 配装助手 · {esc(outcome['evaluated_at'])} · PoB {esc(outcome['engine_version'])}</small><h1>{esc(outcome['name'])}</h1><p>{esc(plan['summary'])}</p><p>计算与 XML 回读已通过。预算状态：{esc(labels[b['status']])}。</p></header>
<section><h2>你的目标</h2><p>{esc(intent['goal'])}</p><p>{esc(intent['league'])} · 目标等级 {intent['target_level']} · 预算 {esc(intent['budget']['amount'])} {esc(intent['budget']['currency'])}</p><p>{esc(intent['notes'])}</p></section>
<section><h2>前后对比</h2><p>原角色：{esc(state.get('baseline_label','用户导入角色'))}；候选：{esc(outcome['result']['main_skill_name'])}。总 DPS 只汇总勾选计入的技能，不能代替实际刷图速度。</p><table><thead><tr><th>指标</th><th>原方案</th><th>新方案</th><th>变化</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section><h2>购买与准备</h2><p>已列成本：{b['low']:g}–{b['high']:g} {esc(b['currency'])}。待核实项目未计入这个范围。</p>{listing(shopping)}<h2>切换顺序</h2>{listing(plan.get('migration_steps',[]))}</section>
<section><h2>计算条件与待核实项</h2><p>共同场景：<code>{esc(scenario)}</code></p>{listing(plan.get('assumptions',[]))}{listing(outcome['warnings'])}<p>{esc(plan.get('legality_notes',''))}</p><p class="note">结果用于配装规划。PoB 未完整支持的机制、实际操作和装备供给可能影响游戏表现。原始角色文件保持不变。</p></section>
<section><h2>资料依据</h2><ul>{''.join(sources)}</ul><details><summary>具体改动及条件依据</summary><pre>{esc(changes)}</pre><pre>{esc(condition_notes)}</pre></details></section>
<section><h2>如何使用</h2><p>在 PoECharm 中打开同目录的“改进配装.xml”，核对当前主技能、装备与天赋；按照上面的准备顺序在游戏内调整。它不会修改服务器角色。</p></section></html>'''


def export(args):
    run, state = read_run(args.run)
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称无效。")
    folder = run / "candidates" / args.name
    outcome = read_json(folder / "evaluation.json")
    bx.require(outcome.get("status") == "validated" and outcome.get("roundtrip_verified"), "候选仍有校验错误，请先修正再导出。")
    bx.require(outcome["intent_hash"] == state.get("intent_hash") and outcome["original_sha256"] == state["original_sha256"], "意图或原角色已变化，必须重新 evaluate。")
    xml = (folder / "candidate.xml").read_text(encoding="utf-8")
    bx.require(bx.digest(xml) == outcome["xml_sha256"], "候选文件在计算后被修改，请重新 evaluate。")
    root = engine.resolve_root(args.pob or state["pob_path"])
    bx.require(engine.version(root) == outcome["engine_version"], "引擎已升级，请重新计算。")
    verification = engine.calculate(root, xml)
    bx.require(semantics(verification) == semantics(outcome["result"]), "最终复算的角色数据与候选记录不同。")
    for key in METRICS:
        a, b = verification["stats"].get(key), outcome["result"]["stats"].get(key)
        if a is None and b is None: continue
        bx.require(isinstance(a,(int,float)) and isinstance(b,(int,float)) and math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-5), "最终复算数值变化：" + key)
    output = run / "output"
    write_text(output / "改进配装.xml", xml)
    write_text(output / "配装说明.html", report_html(state, outcome))
    state["phase"] = "exported"
    state["selected_candidate"] = args.name
    state["updated_at"] = now()
    write_json(run / "run.json", state)
    return {"status": "exported", "files": [str(output / "改进配装.xml"), str(output / "配装说明.html")], "budget_status": outcome["budget"]["status"], "warnings": outcome["warnings"]}


def catalog(args):
    root = engine.resolve_root(args.pob)
    if args.kind == "translations":
        entries = []
        folder = root.parent / "Data" / "Translate" / "zh-rCN"
        bx.require(folder.is_dir(), "该安装目录没有 PoECharm 简体中文翻译数据。")
        for path in folder.glob("*.csv"):
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                for row in csv.reader(stream):
                    if len(row) >= 2 and any(args.query.casefold() in cell.casefold() for cell in row[:2]):
                        entries.append({"english": row[0], "chinese": row[1]})
                        if len(entries) >= args.limit: return {"entries": entries, "truncated": True}
        return {"entries": entries, "truncated": False}
    document = bx.serialise(bx.read_document(Path(args.input))) if args.input else ""
    result = engine.calculate(root, document, catalog=args.kind, query=args.query, limit=args.limit)
    return {"engine_version": result["engine_version"], "tree_version": result["tree_version"], "total": result.get("catalog_total"), "entries": result.get("catalog", [])}


def parser():
    p = argparse.ArgumentParser(description="POE 配装助手：本机 PoB 实算、候选校验和 XML 导出。")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "inspect", "catalog", "decode", "start", "intent", "evaluate", "export", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--pob", help="PoECharm 文件夹、PoB 文件夹或可执行文件；默认自动发现")
        if name in {"inspect", "decode", "start"}: cmd.add_argument("--input", required=True)
        if name in {"start", "intent", "evaluate", "export", "status"}: cmd.add_argument("--run", required=True)
        if name == "start": cmd.add_argument("--invoked", action="store_true")
        if name == "intent": cmd.add_argument("--file", required=True)
        if name in {"evaluate", "export"}: cmd.add_argument("--name", required=True)
        if name == "evaluate": cmd.add_argument("--plan", required=True)
        if name == "decode": cmd.add_argument("--out", required=True)
        if name == "inspect":
            cmd.add_argument("--calculate", action="store_true")
            cmd.add_argument("--out")
        if name == "catalog":
            cmd.add_argument("--kind", choices=["gems", "nodes", "uniques", "config", "translations"], required=True)
            cmd.add_argument("--query", default="")
            cmd.add_argument("--input")
            cmd.add_argument("--limit", type=int, default=20)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = {"skill_version": VERSION, **engine.check(engine.resolve_root(args.pob))}
        elif args.command == "inspect":
            document = bx.read_document(Path(args.input))
            result = bx.summary(document)
            if args.calculate:
                computed = engine.calculate(engine.resolve_root(args.pob), bx.serialise(document))
                result["calculation"] = {key: value for key, value in computed.items() if key not in {"xml", "config_keys"}}
            if args.out:
                write_json(args.out, result)
                result = {"inspection": str(Path(args.out).resolve()), "build": result["build"]}
        elif args.command == "decode":
            destination = Path(args.out).resolve()
            bx.require(destination != Path(args.input).resolve(), "解码输出不能覆盖输入文件。")
            bx.require(not destination.exists(), "解码输出已存在，请换一个文件名。")
            write_text(destination, bx.serialise(bx.read_document(Path(args.input))))
            result = {"xml": str(destination)}
        elif args.command == "start": result = begin(args)
        elif args.command == "intent": result = set_intent(args)
        elif args.command == "evaluate": result = evaluate(args)
        elif args.command == "export": result = export(args)
        elif args.command == "catalog":
            bx.require(1 <= args.limit <= 100, "limit 应在 1–100。")
            result = catalog(args)
        else:
            run, state = read_run(args.run)
            result = {"run": str(run), "phase": state["phase"], "intent": state.get("intent"), "candidates": [p.parent.name for p in (run / "candidates").glob("*/evaluation.json")]}
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
