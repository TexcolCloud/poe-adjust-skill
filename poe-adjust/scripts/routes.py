"""Validate and render a leveling recommendation; no pretend endgame calculation."""
from __future__ import annotations

import copy
from datetime import datetime
import html
import math

import buildxml as bx


ACQUISITIONS = {"quest": "任务奖励", "vendor": "任务解锁商店", "drop": "随机掉落", "trade": "玩家交易", "lab": "迷宫获取", "owned": "确认已有"}


def strings(value, label, *, required=True):
    bx.require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value), label + " 应为非空文字组成的数组。")
    bx.require(not required or value, label + " 不能为空。")


def gem_ids(route):
    result = set()
    for stage in route.get("stages", []):
        for skill in stage.get("skills", []):
            if skill.get("skill_id"): result.add(skill["skill_id"])
            if skill.get("fallback_skill_id"): result.add(skill["fallback_skill_id"])
    bx.require(result and len(result) <= 80 and all(isinstance(v, str) for v in result), "路线需要 1–80 个有效宝石 skill_id。")
    return sorted(result)


def validate(route, gems):
    allowed = {"name", "confirmed", "goal", "league", "class_id", "ascendancy", "trade_mode", "budget", "version_status", "version_source", "scope", "stages", "sources", "risks", "notes"}
    bx.require(not (set(route) - allowed), "路线包含未知字段：" + ", ".join(sorted(set(route) - allowed)))
    bx.require(route.get("confirmed") is True, "先完成开荒目标的多轮澄清，再保存 confirmed=true。")
    for key in ("name", "goal", "league", "ascendancy"):
        bx.require(isinstance(route.get(key), str) and route[key].strip(), "路线需要 " + key)
    bx.require(type(route.get("class_id")) is int and route["class_id"] in bx.CLASSES, "class_id 不是 PoE1 职业。")
    bx.require(route.get("trade_mode") in {"ssf", "trade"}, "trade_mode 应为 ssf 或 trade。")
    bx.require(route.get("scope", "maps") in {"maps", "campaign", "partial"}, "scope 应为 maps/campaign/partial。")
    budget = route.get("budget", {})
    bx.require(isinstance(budget, dict) and type(budget.get("amount")) in (int, float) and math.isfinite(budget["amount"]) and budget["amount"] >= 0 and isinstance(budget.get("currency"), str) and budget["currency"], "需要已确认的开荒预算 amount/currency。")
    bx.require(route.get("version_status") in {"verified", "unverified"}, "明确 version_status=verified 或 unverified，不能把本机树版本当作国服赛季映射。")
    sources = route.get("sources")
    bx.require(isinstance(sources, list) and sources, "开荒路线需要实际查阅的网页来源。")
    source_map = {}
    for source in sources:
        bx.require(isinstance(source, dict) and all(isinstance(source.get(k), str) and source[k].strip() for k in ("id", "title", "url", "checked_at", "version")), "来源需要 id/title/url/checked_at/version。")
        bx.require(source["id"] not in source_map and source["url"].startswith(("https://", "http://")), "来源 ID 重复或 URL 无效。")
        try:
            datetime.fromisoformat(source["checked_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("checked_at 请使用实际查阅的 ISO 日期或时间。") from exc
        source_map[source["id"]] = source
    if route["version_status"] == "verified":
        bx.require(route.get("version_source") in source_map, "版本已核实需要 version_source 指向支持国服版本映射的网页。")
    warnings = []
    if route["version_status"] == "unverified":
        warnings.append("国服赛季与资料版本的对应关系未完全核实；关键改动需以国服公告为准，不能承诺当前赛季强度。")
    result = copy.deepcopy(route)
    stages = result.get("stages")
    bx.require(isinstance(stages, list) and 1 <= len(stages) <= 12, "stages 需要 1–12 个阶段。")
    levels = [stage.get("level") for stage in stages]
    bx.require(all(type(level) is int and 1 <= level <= 100 for level in levels) and levels == sorted(set(levels)), "阶段角色等级必须在 1–100 且严格递增。")
    if route.get("scope", "maps") != "partial":
        bx.require(len(stages) >= 3 and levels[0] <= 5 and levels[-1] >= 68, "完整开荒路线至少三个阶段，覆盖起步至剧情结束/异界初期；局部路线请使用 scope=partial。")
    stage_allowed = {"name", "level", "main_skill_id", "links", "link_fallback", "skills", "gear", "passives", "ascendancy", "resources", "checks", "transition", "required_items"}
    for stage in stages:
        bx.require(not (set(stage) - stage_allowed), "阶段存在未知字段：" + ", ".join(sorted(set(stage) - stage_allowed)))
        label = f"{stage['level']} 级阶段"
        bx.require(isinstance(stage.get("name"), str) and stage["name"].strip(), label + "需要名称。")
        bx.require(type(stage.get("links")) is int and 1 <= stage["links"] <= 6, label + "主技能 links 应为 1–6。")
        if stage["links"] > 4:
            bx.require(isinstance(stage.get("link_fallback"), str) and stage["link_fallback"].strip(), label + "依赖五/六连，必须说明没获得时的四连替代。")
        for key in ("gear", "passives", "checks", "transition"):
            strings(stage.get(key), label + "." + key)
        for key in ("ascendancy", "resources"):
            bx.require(isinstance(stage.get(key), str) and stage[key].strip(), label + "需要 " + key)
        skills = stage.get("skills")
        bx.require(isinstance(skills, list) and skills, label + "需要 skills。")
        main = [skill for skill in skills if skill.get("role") == "main"]
        bx.require(len(main) == 1 and main[0].get("skill_id") == stage.get("main_skill_id"), label + "必须指定一个一致的主技能。")
        bx.require(sum(skill.get("role") in {"main", "support"} for skill in skills) <= stage["links"], label + "主技能与辅助数量超过连线数。")
        for skill in skills:
            bx.require(set(skill) <= {"skill_id", "level", "quality", "role", "acquisition", "source_id", "source_note", "fallback_skill_id"}, label + "宝石存在未知字段。")
            entry = gems.get(skill.get("skill_id"))
            bx.require(entry, label + "有本机引擎未识别的独立宝石：" + str(skill.get("skill_id")))
            bx.require(skill.get("role") in {"main", "support", "utility"}, label + "role 应为 main/support/utility。")
            if skill["role"] == "main":
                bx.require(entry.get("tags", {}).get("grants_active_skill"), label + "主技能不能是辅助宝石。")
            requirements = {row["gem_level"]: row["character_level"] for row in entry["level_requirements"]}
            bx.require(type(skill.get("level")) is int and skill["level"] in requirements, label + "宝石等级不在本机普通可练等级中：" + entry["name"])
            bx.require(requirements[skill["level"]] <= stage["level"], f"{label}不能使用 {entry['name']} {skill['level']} 级；需要角色 {requirements[skill['level']]} 级。")
            bx.require(type(skill.get("quality", 0)) is int and 0 <= skill.get("quality", 0) <= 23, label + "宝石品质无效。")
            method = skill.get("acquisition")
            bx.require(method in ACQUISITIONS, label + "宝石需要 acquisition 获取方式。")
            bx.require(skill.get("source_id") in source_map and isinstance(skill.get("source_note"), str) and skill["source_note"].strip(), label + "宝石获取需要 source_id 和职业/章节/任务说明 source_note。")
            bx.require(not (route["trade_mode"] == "ssf" and method == "trade"), "自力开荒不能依赖交易宝石。")
            uncertain = method in {"drop", "lab", "trade"} or entry.get("special_variant") or entry.get("vaal")
            fallback = gems.get(skill.get("fallback_skill_id"))
            if uncertain:
                bx.require(fallback and fallback["skill_id"] != entry["skill_id"] and not fallback.get("special_variant") and not fallback.get("vaal") and fallback["min_level"] <= stage["level"], label + "随机/交易/特殊宝石需给出可到等级的普通宝石 fallback_skill_id：" + entry["name"])
                warnings.append(label + "的 " + entry["name"] + " 取决于获取条件；拿不到则使用 " + fallback["name"] + "，并复查辅助和属性需求。")
            skill["name"] = entry["name"]
            skill["minimum_character_level"] = requirements[skill["level"]]
            skill["fallback_name"] = fallback["name"] if fallback else None
        items = stage.get("required_items", [])
        bx.require(isinstance(items, list), label + "required_items 应为数组。")
        for item in items:
            bx.require(isinstance(item, dict) and set(item) <= {"name", "acquisition", "fallback", "source_id"} and item.get("name") and item.get("acquisition") in ACQUISITIONS, label + "核心物品字段无效。")
            bx.require(item.get("source_id") in source_map, label + "核心物品需要来源。")
            bx.require(not (route["trade_mode"] == "ssf" and item["acquisition"] == "trade"), "自力开荒不能依赖交易核心物品。")
            if item["acquisition"] in {"drop", "trade", "lab"}:
                bx.require(isinstance(item.get("fallback"), str) and item["fallback"].strip(), label + "随机获取的核心物品需要缺货/不掉落时的替代路线。")
    strings(result.get("risks", []), "risks", required=False)
    return result, list(dict.fromkeys(warnings))


def render(route, warnings, engine_version, tree_version):
    esc = lambda value: html.escape(str(value), quote=True)
    listing = lambda values: "<ul>" + "".join("<li>" + esc(v) + "</li>" for v in values) + "</ul>"
    sources = {source["id"]: source for source in route["sources"]}
    sections = []
    for stage in route["stages"]:
        rows = []
        for skill in stage["skills"]:
            source = sources[skill["source_id"]]
            fallback = "；未获得时：" + skill["fallback_name"] if skill["fallback_name"] else ""
            rows.append(f'<tr><td>{esc(skill["name"])}</td><td>{skill["level"]} 级 / {skill.get("quality", 0)} 品质</td><td>{esc(ACQUISITIONS[skill["acquisition"]])}：{esc(skill["source_note"])}{esc(fallback)} <a href="{esc(source["url"])}">依据</a></td></tr>')
        items = [item["name"] + " · " + ACQUISITIONS[item["acquisition"]] + ("；未获得时：" + item["fallback"] if item.get("fallback") else "") for item in stage.get("required_items", [])]
        sections.append(f'''<section><h2>{stage['level']} 级 · {esc(stage['name'])}</h2><p>主技能按 {stage['links']} 连安排。{esc(stage.get('link_fallback',''))}</p><table><tr><th>宝石</th><th>目标等级/品质</th><th>获取与替代</th></tr>{''.join(rows)}</table><p>宝石等级为本阶段可用的练级目标，不表示刚领取就达到该等级。</p><h3>装备优先级</h3>{listing(stage['gear'])}{listing(items)}<h3>天赋与升华</h3>{listing(stage['passives'])}<p>{esc(stage['ascendancy'])}</p><h3>资源与操作</h3><p>{esc(stage['resources'])}</p><h3>继续推进前检查</h3>{listing(stage['checks'])}<h3>转入下一阶段的条件</h3>{listing(stage['transition'])}</section>''')
    evidence = [f'<li><a href="{esc(source["url"])}">{esc(source["title"])}</a> · {esc(source["version"])} · {esc(source["checked_at"])}</li>' for source in sources.values()]
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(route['name'])}</title><style>body{{font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif;max-width:1080px;margin:32px auto;padding:0 22px;background:#f4f3ef;color:#23303b}}header,section{{padding:26px;background:white;border:1px solid #ddd;border-radius:12px;margin:18px 0}}h1{{font-size:30px}}h2{{color:#235b53}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}.note{{background:#fff5da;padding:14px}}@media print{{section{{break-inside:avoid}}}}</style><header><h1>{esc(route['name'])}</h1><p>{esc(route['goal'])}</p><p>{esc(route['league'])} · {esc(bx.CLASSES[route['class_id']])} → {esc(route['ascendancy'])} · {'自力获取' if route['trade_mode']=='ssf' else '允许交易'} · 初始预算 {route['budget']['amount']:g} {esc(route['budget']['currency'])}</p><p class="note">这是分阶段行动路线。宝石等级已按本机 PoB {esc(engine_version)} / 天赋 {esc(tree_version)} 核对；任务领取、掉落与国服改动以所列资料和实际游戏为准。尚未使用完整阶段装备进行伤害/生存实算，不承诺面板或通关速度。</p></header>{''.join(sections)}<section><h2>需要留意</h2>{listing(warnings + route.get('risks', []))}<p>{esc(route.get('notes',''))}</p></section><section><h2>查阅资料</h2><ul>{''.join(evidence)}</ul><p>实际推进后，可提供新的角色 XML，继续调用配装助手计算下一阶段的具体装备与天赋。</p></section></html>'''
