"""Validate a staged route against native gems, sockets and ascendancy paths."""
from __future__ import annotations

import copy
import html
import math

import buildxml as bx
import engine

ACQUISITIONS = {"quest": "任务奖励", "vendor": "任务解锁商店", "drop": "随机掉落", "trade": "玩家交易", "lab": "迷宫获取", "owned": "确认已有"}
WEAPONS = {"one_hand_shield": (3, 3), "dual_wield": (3, 3), "two_hand": (6, 0), "bow": (6, 0), "unarmed_shield": (0, 3)}
USAGES = {"main": "主要输出", "movement": "位移", "utility": "按需辅助操作", "aura": "常驻开启"}
ATTRIBUTES = {"strength": "力量", "dexterity": "敏捷", "intelligence": "智慧"}


def strings(value, label, *, required=True):
    if isinstance(value, str): value = [value]
    bx.require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value), label + " 应为文字或非空文字组成的数组。")
    bx.require(not required or value, label + " 不能为空。")
    return value


def gem_ids(route):
    library = route.get("gems")
    bx.require(isinstance(library, dict) and library, "路线格式已更新：顶层 gems 保存获取依据，stages[].skill_groups 保存实际装备位置和连线；请按 leveling.md 重新整理旧路线。")
    result = set(library)
    for gem in library.values():
        bx.require(isinstance(gem, dict), "gems 每项应是获取依据对象。")
        if gem.get("fallback_skill_id"): result.add(gem["fallback_skill_id"])
    bx.require(1 <= len(result) <= 80 and all(isinstance(v, str) and v for v in result), "路线需要 1–80 个有效宝石 skill_id。")
    return sorted(result)


def link_queries(route):
    queries = set()
    for stage in route.get("stages", []):
        for group in stage.get("skill_groups", []):
            for key in ("gems", "upgrade_gems"):
                if group.get(key): queries.add(",".join(gem["skill_id"] for gem in group[key]))
    bx.require(queries and len(queries) <= 80, "路线需要 1–80 套技能连线。")
    return sorted(queries)


def resolve_gem_levels(stages, gems):
    """Resolve explicit automatic targets and report all unavailable levels together."""
    errors = []
    for stage in stages:
        for group in stage.get("skill_groups", []):
            for key in ("gems", "upgrade_gems"):
                for skill in group.get(key, []):
                    entry = gems.get(skill.get("skill_id"))
                    if not entry: continue  # The structural validator reports unknown gems.
                    requirements = {row["gem_level"]: row for row in entry["level_requirements"]}
                    available = [level for level, row in requirements.items() if row["character_level"] <= stage["level"]]
                    label = f"{stage['level']} 级阶段的 {entry.get('display_name', entry['name'])}"
                    if skill.get("level") == "auto" and available:
                        skill["level"] = max(available)
                    level = skill.get("level")
                    if type(level) is not int or level not in requirements:
                        hint = f"本阶段最高可练到宝石 {max(available)} 级，可填更低等级或 auto。" if available else f"最早需要角色 {entry['min_level']} 级，必须换过渡技能或延后阶段。"
                        errors.append(label + "宝石等级不在普通可练等级中；" + hint)
                    elif requirements[level]["character_level"] > stage["level"]:
                        hint = f"本阶段最高宝石 {max(available)} 级。" if available else "本阶段尚不能使用，不能靠降低宝石等级解决。"
                        errors.append(f"{label} {level} 级需要角色 {requirements[level]['character_level']} 级；" + hint)
    bx.require(not errors, "宝石等级检查失败（level 是宝石等级，不能填写角色等级）：\n" + "\n".join(dict.fromkeys(errors)))


def validate_group(group, stage, gems, library, compatibility, capacity, warnings, allow_minions):
    label = f"{stage['level']} 级阶段"
    bx.require(set(group) <= {"name", "slot", "use", "gems", "upgrade_gems"} and isinstance(group.get("name"), str) and group["name"].strip(), label + "技能组需要 name/slot/use/gems，可选 upgrade_gems。")
    bx.require(group.get("use") in USAGES, label + "技能组 use 应为 main/movement/utility/aura。")
    base = group.get("gems")
    bx.require(isinstance(base, list) and base, label + "基础路线每组需要至少一颗宝石。")
    used = set()
    for key in ("gems", "upgrade_gems"):
        if key not in group: continue
        entries = group[key]
        bx.require(isinstance(entries, list) and 1 <= len(entries) <= capacity, label + "连线数量超过 " + group["slot"] + " 实际孔数。")
        if key == "upgrade_gems":
            bx.require(len(entries) > len(base) and entries[0]["skill_id"] == base[0]["skill_id"], label + "upgrade_gems 应为同一主技能的更多连升级；换主技能另设阶段。")
        ids = [gem.get("skill_id") for gem in entries]
        bx.require(len(set(ids)) == len(ids), label + "同组宝石重复。")
        for index, skill in enumerate(entries):
            bx.require(set(skill) <= {"skill_id", "level", "quality"}, label + "技能组宝石只接受 skill_id/level/quality，获取依据放顶层 gems。")
            skill_id = skill.get("skill_id")
            entry = gems.get(skill_id)
            bx.require(entry and skill_id in library, label + "宝石未识别或缺少获取依据：" + str(skill_id))
            used.add(skill_id)
            active = bool(entry.get("tags", {}).get("grants_active_skill"))
            bx.require(active if index == 0 else not active, label + "每组第一颗为主动技能，其余必须为辅助；第二主动技能另建组并安排位置。")
            bx.require(allow_minions or not (active and entry.get("tags", {}).get("minion")), label + "安排了召唤技能，但用户选择不使用召唤：" + entry["name"])
            if index == 0 and group["use"] == "aura":
                bx.require(entry.get("tags", {}).get("aura") or entry.get("tags", {}).get("herald"), label + "常驻开启须为本机 aura/herald 宝石；其他技能按实际手动用途安排。")
            requirements = {row["gem_level"]: row for row in entry["level_requirements"]}
            requirement = requirements[skill["level"]]
            skill["attribute_requirements"] = {key: requirement.get(key, 0) for key in ATTRIBUTES}
            bx.require(type(skill.get("quality", 0)) is int and 0 <= skill.get("quality", 0) <= 23, label + "宝石品质无效。")
            acquisition = library[skill_id]
            bx.require(acquisition["available_from_act"] <= stage["act"], label + "使用 " + entry["name"] + f" 早于已记录的第 {acquisition['available_from_act']} 章获取进度；改用过渡宝石或将本段延后，不能仅凭角色等级视为已购买。")
            uncertain = acquisition["acquisition"] in {"drop", "lab", "trade"} or entry.get("special_variant") or entry.get("vaal")
            fallback = gems.get(acquisition.get("fallback_skill_id"))
            if uncertain:
                bx.require(fallback and fallback["skill_id"] != skill_id and not fallback.get("special_variant") and not fallback.get("vaal") and fallback["min_level"] <= stage["level"], label + "随机/交易/特殊宝石需有本阶段可用的普通 fallback_skill_id：" + entry["name"])
                bx.require(bool(fallback.get("tags", {}).get("grants_active_skill")) == active, label + "替代宝石的主辅类型不一致。")
                warnings.append(label + "的 " + entry["name"] + " 获取不稳定；未取得先使用 " + fallback.get("display_name", fallback["name"]) + "，复查替代连线和属性。")
            skill["name"] = entry.get("display_name", entry["name"])
            native_level = next(row for row in entry["level_requirements"] if row["gem_level"] == skill["level"])
            skill["base_resources"] = copy.deepcopy(native_level.get("base_resources", {}))
            skill["description"] = entry.get("description", "")
        if compatibility is not None:
            checked = compatibility.get(",".join(ids))
            bx.require(checked, label + "缺少原生辅助兼容性结果。")
            bx.require(not checked.get("errors"), label + "原生连线查询无效：" + "; ".join(checked.get("errors", [])))
            bx.require(not checked["incompatible"], label + "以下辅助不支持该组主技能：" + ", ".join(checked["incompatible"]))
    return used


def validate(route, gems, data_version=None, classes=None, compatibility=None, nodes=None):
    allowed = {"name", "confirmed", "goal", "league", "game_version", "class_id", "ascendancy", "trade_mode", "budget", "version_status", "version_source", "scope", "gems", "stages", "sources", "risks", "notes", "allow_minions", "allow_mule", "shared_trials"}
    bx.require(not (set(route) - allowed), "路线包含未知字段：" + ", ".join(sorted(set(route) - allowed)))
    bx.require(route.get("confirmed") is True, "先完成开荒目标的多轮澄清，再保存 confirmed=true。")
    for key in ("name", "goal", "league", "ascendancy"):
        bx.require(isinstance(route.get(key), str) and route[key].strip(), "路线需要 " + key)
    engine.validate_game_version(route.get("game_version"), route["league"], data_version)
    bx.require(type(route.get("class_id")) is int and route["class_id"] in bx.CLASSES, "class_id 不是 PoE1 职业。")
    for key in ("allow_minions", "allow_mule"):
        bx.require(type(route.get(key)) is bool, "路线需要已确认的 " + key + "：是否允许召唤，以及是否愿意另建小号取宝石。未允许时填 false，不为通过检查擅自改为 true。")
    result = copy.deepcopy(route)
    bx.require(type(route.get("shared_trials", False)) is bool, "shared_trials 应为用户确认的同赛季共享试炼进度，未确认时为 false。")
    if classes is not None:
        selected = next((entry for entry in classes if entry["id"] == route["class_id"]), None)
        bx.require(selected and route["ascendancy"] in {entry["name"] for entry in selected["ascendancies"]}, "升华不属于所选职业或名称无法识别；请从 catalog classes 返回的英文名中选择。")
        result["class_display_name"] = selected.get("display_name", bx.CLASSES[route["class_id"]])
        ascendancy = next(entry for entry in selected["ascendancies"] if entry["name"] == route["ascendancy"])
        result["ascendancy_display_name"] = ascendancy.get("display_name", ascendancy["name"])
    bx.require(route.get("trade_mode") in {"ssf", "trade"}, "trade_mode 应为 ssf 或 trade。")
    bx.require(route.get("scope", "maps") in {"maps", "campaign", "partial"}, "scope 应为 maps/campaign/partial。")
    budget = route.get("budget", {})
    bx.require(isinstance(budget, dict) and type(budget.get("amount")) in (int, float) and math.isfinite(budget["amount"]) and budget["amount"] >= 0 and isinstance(budget.get("currency"), str) and budget["currency"], "需要已确认的开荒预算 amount/currency。")
    bx.require(route.get("version_status") in {"verified", "unverified"}, "明确 version_status=verified 或 unverified。")
    sources = route.get("sources")
    bx.require(isinstance(sources, list) and sources, "开荒路线需要实际查阅的网页来源。")
    source_map = {}
    for source in sources:
        bx.require(isinstance(source, dict) and all(isinstance(source.get(k), str) and source[k].strip() for k in ("id", "title", "url", "checked_at", "version")), "来源需要 id/title/url/checked_at/version。")
        bx.require(source["id"] not in source_map and source["url"].startswith(("https://", "http://")), "来源 ID 重复或 URL 无效。")
        engine.validate_checked_at(source["checked_at"])
        source_map[source["id"]] = source
    if route["version_status"] == "verified":
        bx.require(route.get("version_source") in source_map, "版本已核实需要 version_source 指向支持国服版本映射的网页。")
        engine.validate_game_version(route["game_version"], route["league"], version_source=source_map[route["version_source"]]["url"])
    warnings = []
    if route["version_status"] == "unverified": warnings.append("国服赛季与资料版本的对应关系未完全核实；关键改动需以国服公告为准。")
    library = result.get("gems")
    gem_ids(route)
    for skill_id, acquisition in library.items():
        bx.require(set(acquisition) <= {"acquisition", "source_id", "source_note", "fallback_skill_id", "available_from_act", "class_ids", "via_mule", "quest", "npc"}, "gems 获取依据存在未知字段。")
        bx.require(skill_id in gems, "本机未识别独立宝石：" + skill_id)
        bx.require(acquisition.get("acquisition") in ACQUISITIONS, "宝石需要有效 acquisition。")
        bx.require(acquisition.get("source_id") in source_map and isinstance(acquisition.get("source_note"), str) and acquisition["source_note"].strip(), "宝石获取需要 source_id 与章节/任务/NPC/职业说明 source_note。")
        bx.require(not (route["trade_mode"] == "ssf" and acquisition["acquisition"] == "trade"), "自力开荒不能依赖交易宝石。")
        bx.require(type(acquisition.get("available_from_act")) is int and 1 <= acquisition["available_from_act"] <= 11, "每种宝石需要 available_from_act：按所列任务来源填写第 1–10 章，11 表示异界，不能用可装备等级替代。")
        if acquisition["acquisition"] in {"quest", "vendor"}:
            classes_for_source = acquisition.get("class_ids")
            bx.require(isinstance(classes_for_source, list) and classes_for_source and all(type(value) is int and value in bx.CLASSES for value in classes_for_source), "任务/商店来源需要 class_ids，列出该任务奖励或商店行实际允许的职业；全职业需明确列出 0–6。")
            via_mule = acquisition.get("via_mule", False)
            bx.require(type(via_mule) is bool, "via_mule 应为布尔值。")
            bx.require(not via_mule or route["allow_mule"], "用户未允许另建小号取宝石，不能安排 via_mule：" + skill_id)
            bx.require(route["class_id"] in classes_for_source or via_mule, "该任务/商店的职业范围不含当前职业：" + skill_id + "；使用后续全职业商店、已获准的小号转交或其他过渡，不要把别的职业奖励写成当前职业可领。")
        acquisition["name"] = gems[skill_id].get("display_name", gems[skill_id]["name"])
    stages = result.get("stages")
    bx.require(isinstance(stages, list) and 1 <= len(stages) <= 12, "stages 需要 1–12 个阶段。")
    levels = [stage.get("level") for stage in stages]
    bx.require(all(type(level) is int and 1 <= level <= 100 for level in levels) and levels == sorted(set(levels)), "阶段角色等级必须在 1–100 且严格递增。")
    acts = [stage.get("act") for stage in stages]
    bx.require(all(type(act) is int and 1 <= act <= 11 for act in acts) and acts == sorted(acts), "每个阶段需要按实际进度递增的 act：第 1–10 章，11 为异界；同章可有多个等级阶段。")
    if route.get("scope", "maps") != "partial":
        bx.require(len(stages) >= 3 and levels[0] <= 5 and levels[-1] >= 68, "完整开荒路线至少三个阶段，覆盖起步至剧情结束/异界初期；局部路线请使用 scope=partial。")
    resolve_gem_levels(stages, gems)
    asc_nodes, paths = engine.ascendancy_paths(route["ascendancy"], nodes)
    previous_nodes, used_gems = set(), set()
    stage_allowed = {"name", "level", "act", "weapon_setup", "skill_groups", "gear", "passives", "ascendancy_points", "ascendancy_nodes", "ascendancy_respec_reason", "resources", "checks", "transition", "required_items"}
    for stage in stages:
        bx.require(not (set(stage) - stage_allowed), "阶段存在未知字段：" + ", ".join(sorted(set(stage) - stage_allowed)))
        label = f"{stage['level']} 级阶段"
        bx.require(isinstance(stage.get("name"), str) and stage["name"].strip(), label + "需要名称。")
        for key in ("gear", "passives", "checks", "transition"): stage[key] = strings(stage.get(key), label + "." + key)
        bx.require(isinstance(stage.get("resources"), str) and stage["resources"].strip(), label + "需要 resources。")
        bx.require(stage.get("weapon_setup") in WEAPONS, label + "需要 weapon_setup：one_hand_shield/dual_wield/two_hand/bow/unarmed_shield。")
        weapon_slots = WEAPONS[stage["weapon_setup"]]
        capacities = {"Body Armour": 6, "Helmet": 4, "Gloves": 4, "Boots": 4, "Weapon 1": weapon_slots[0], "Weapon 2": weapon_slots[1]}
        groups = stage.get("skill_groups")
        bx.require(isinstance(groups, list) and 1 <= len(groups) <= 16, label + "需要 1–16 个 skill_groups。")
        bx.require(any(group.get("use") == "main" for group in groups), label + "至少一组 use=main 输出技能。")
        usage, upgraded_usage = {}, {}
        for group in groups:
            slot = group.get("slot")
            bx.require(slot in capacities and capacities[slot] > 0, label + "武器配置不能在此位置放宝石：" + str(slot))
            used_gems.update(validate_group(group, stage, gems, library, compatibility, capacities[slot], warnings, route["allow_minions"]))
            usage[slot] = usage.get(slot, 0) + len(group["gems"])
            upgraded_usage[slot] = upgraded_usage.get(slot, 0) + len(group.get("upgrade_gems", group["gems"]))
        for slot in capacities:
            bx.require(max(usage.get(slot, 0), upgraded_usage.get(slot, 0)) <= capacities[slot], label + "在 " + slot + " 分配的总孔数超过容量；基础与升级都必须能同时装备。")
        stage["attribute_requirements"] = {
            key: {attribute: max(gem["attribute_requirements"][attribute]
                                 for group in groups for gem in group.get(key, group["gems"]))
                  for attribute in ATTRIBUTES}
            for key in ("gems", "upgrade_gems")}
        points, allocated = stage.get("ascendancy_points"), stage.get("ascendancy_nodes")
        bx.require(type(points) is int and points in (0, 2, 4, 6, 8), label + "ascendancy_points 应为已完成迷宫的 0/2/4/6/8 点。")
        bx.require(isinstance(allocated, list) and all(type(node) is int for node in allocated) and len(set(allocated)) == len(allocated), label + "ascendancy_nodes 应为累计大点 ID 数组。")
        requested, actual = set(allocated), set()
        for node_id in allocated:
            bx.require(node_id in asc_nodes and asc_nodes[node_id].get("type") == "Notable" and node_id in paths, label + "升华大点不属于所选升华或无可分配路径：" + str(node_id))
            actual.update(paths[node_id])
        bx.require(len(actual) <= points, f"{label}升华含连接点共需 {len(actual)} 点，超过已安排的 {points} 点；不能在八点之外再补其他升华。")
        bx.require(not (previous_nodes - actual) or isinstance(stage.get("ascendancy_respec_reason"), str) and stage["ascendancy_respec_reason"].strip(), label + "移除上一阶段升华需要说明洗点与切换原因 ascendancy_respec_reason。")
        previous_nodes = actual
        stage["ascendancy_used"] = len(actual)
        stage["ascendancy_details"] = [{"id": node, "name": asc_nodes[node].get("display_name", asc_nodes[node]["name"]), "stats": asc_nodes[node].get("stats", []), "required_by_path": node not in requested}
                                       for node in sorted(actual, key=lambda node: (len(paths[node]), node)) if asc_nodes[node].get("type") == "Notable"]
        if points > len(actual): warnings.append(label + f"有 {points-len(actual)} 点升华尚未安排，按迷宫与后续切换条件核对。")
        items = stage.get("required_items", [])
        bx.require(isinstance(items, list), label + "required_items 应为数组。")
        for item in items:
            bx.require(isinstance(item, dict) and set(item) <= {"name", "acquisition", "fallback", "source_id"} and item.get("name") and item.get("acquisition") in ACQUISITIONS, label + "核心物品字段无效。")
            bx.require(item.get("source_id") in source_map, label + "核心物品需要来源。")
            bx.require(not (route["trade_mode"] == "ssf" and item["acquisition"] == "trade"), "自力开荒不能依赖交易核心物品。")
            if item["acquisition"] in {"drop", "trade", "lab"}:
                bx.require(isinstance(item.get("fallback"), str) and item["fallback"].strip(), label + "随机获取的核心物品需要替代路线。")
    bx.require(used_gems == set(library), "宝石表有未安排到任何阶段的条目；删除闲置条目或落实使用位置：" + ", ".join(sorted(set(library)-used_gems)))
    for key in ("risks", "notes"): result[key] = strings(result.get(key, []), key, required=False)
    return result, list(dict.fromkeys(warnings))


def render(route, warnings, engine_version, tree_version):
    esc = lambda value: html.escape(str(value), quote=True)
    listing = lambda values: "<ul>" + "".join("<li>" + esc(v) + "</li>" for v in values) + "</ul>"
    def resources_text(values):
        parts = []
        pools = {"Mana": "魔力", "Life": "生命", "EnergyShield": "能量护盾", "Rage": "怒火"}
        for pool, amount in values.get("cost", {}).items():
            parts.append(f"单次基础消耗：{pools.get(pool, pool)} {amount:g}")
        for key, label in (("manaReservationPercent", "魔力保留"), ("lifeReservationPercent", "生命保留"),
                           ("manaReservationFlat", "固定魔力保留"), ("lifeReservationFlat", "固定生命保留")):
            if key in values: parts.append(f"{label} {values[key]:g}" + ("%" if key.endswith("Percent") else ""))
        if "manaMultiplier" in values: parts.append(f"辅助的消耗与保留倍率 {100 + values['manaMultiplier']:g}%")
        return "；".join(parts) or "本机等级表未报告可展示的资源字段，不能推断为零消耗。"
    class_name = route.get("class_display_name", bx.CLASSES[route["class_id"]])
    ascendancy_name = route.get("ascendancy_display_name", route["ascendancy"])
    delivery_note = "待核实草案：获取、试炼或版本依据仍有缺口，暂不作为可直接照做的攻略。" if route.get("delivery_status") == "route_draft" else "分阶段路线：已完成列出的程序检查，仍需核对机制、国服差异和实际进度。"
    sections = []
    for stage in route["stages"]:
        chapter = f"第 {stage['act']} 章" if stage["act"] <= 10 else "异界"
        groups = [f"<p>剧情进度：{chapter}。等级里程碑不代表已经完成本章任务，先满足本段列出的领取和切换条件。</p>"]
        if stage.get("trial_requirements"):
            requirements = [f"{lab['name']}：" + "；".join(lab["trial_locations"]) for lab in stage["trial_requirements"]]
            progress = "用户已确认同赛季共享试炼进度；本角色仍须通关对应迷宫。" if route.get("shared_trials", False) else "先完成这些剧情试炼，再逐次通关迷宫取得本段升华点。"
            groups.append("<h3>迷宫前置</h3>" + listing(requirements) + "<p>" + esc(progress) + "迷宫区域等级不等于最低进入等级；达到角色等级也不等于能安全通关。</p>")
            proof = route.get("fact_evidence", {}).get("trials", {})
            if proof.get("url"): groups.append(f'<p><a href="{esc(proof["url"])}">试炼位置依据</a> · {esc(proof.get("checked_at", ""))}</p>')
        for group in stage["skill_groups"]:
            links = lambda key: " + ".join(f"{gem['name']} {gem['level']}级/{gem.get('quality',0)}品质" for gem in group[key])
            upgrade = f"<p>获得更多连线后可升级：{esc(links('upgrade_gems'))}。未获得时继续使用上面的基础连线。</p>" if group.get("upgrade_gems") else ""
            groups.append(f"<h3>{esc(group['name'])} · {esc(group['slot'])} · {esc(USAGES[group['use']])}</h3><p>基础 {len(group['gems'])} 连：{esc(links('gems'))}</p>{upgrade}")
            evidence, seen = [], set()
            for gem in group["gems"] + group.get("upgrade_gems", []):
                identity = (gem["skill_id"], gem["level"])
                if identity in seen: continue
                seen.add(identity)
                text = f"{gem['name']} · 宝石 {gem['level']} 级：" + resources_text(gem.get("base_resources", {}))
                if gem.get("description"): text += " 原文：" + gem["description"]
                evidence.append(text)
            groups.append("<details><summary>本机宝石原文与基础资源</summary><p>按上列宝石等级读取。主动技能数值尚未计入辅助倍率、天赋、装备或其他修改，不能用它们直接证明整套连线的保留或续航；具体角色以 XML 实算为准。光环能否通过降级减少保留，须比较实际等级数据。</p>" + listing(evidence) + "</details>")
        ascendancies = [node["name"] + ("（连接路径必经）" if node.get("required_by_path") else "") + "：" + "；".join(node["stats"]) for node in stage["ascendancy_details"]]
        items = [item["name"] + " · " + ACQUISITIONS[item["acquisition"]] + ("；未获得时：" + item["fallback"] if item.get("fallback") else "") for item in stage.get("required_items", [])]
        attribute_text = lambda key: "、".join(f"{name} {stage['attribute_requirements'][key][attribute]}" for attribute, name in ATTRIBUTES.items())
        attribute_note = "基础宝石所需属性：" + attribute_text("gems") + "。"
        if stage["attribute_requirements"]["upgrade_gems"] != stage["attribute_requirements"]["gems"]:
            attribute_note += "可选连线升级后：" + attribute_text("upgrade_gems") + "。"
        attribute_note += "这是宝石门槛，未计具体装备本身需求或降低需求效果；不代表角色已经满足。换装前核对属性，不足时暂缓升级宝石或补属性。"
        groups.append("<p class=\"note\">" + esc(attribute_note) + "</p>")
        sections.append(f"<section><h2>{stage['level']} 级 · {esc(stage['name'])}</h2>{''.join(groups)}<p>等级是宝石练级目标，刚领取不等于已练到该等级。同一装备的不同组按上述连线分开安排。</p><h3>装备优先级</h3>{listing(stage['gear'])}{listing(items)}<h3>天赋与升华</h3>{listing(stage['passives'])}<p>本段已安排 {stage['ascendancy_used']} / {stage['ascendancy_points']} 点升华（包含连接小点）。</p>{listing(ascendancies)}<p>{esc(stage.get('ascendancy_respec_reason',''))}</p><h3>资源与操作</h3><p>{esc(stage['resources'])}</p><h3>继续推进前检查</h3>{listing(stage['checks'])}<h3>转入下一阶段的条件</h3>{listing(stage['transition'])}</section>")
    sources = {source["id"]: source for source in route["sources"]}
    rows = []
    for skill_id, gem in route["gems"].items():
        fallback = "；未取得时先使用 " + gem["fallback_skill_id"] if gem.get("fallback_skill_id") else ""
        if gem.get("via_mule"): fallback += "；需按已确认方案另建小号取得并转交"
        chapter = f"第 {gem['available_from_act']} 章" if gem["available_from_act"] <= 10 else "异界"
        fallback += "；本路线记录的最早取得进度：" + chapter
        links = ' '.join(f'<a href="{esc(url)}">获取依据</a>' for url in gem.get("source_urls", [sources[gem['source_id']]['url']]))
        state = "网页表格已核对" if gem.get("evidence_status") == "source_checked" else "取得办法待核实"
        rows.append(f"<tr><td>{esc(gem['name'])}</td><td>{esc(ACQUISITIONS[gem['acquisition']])}：{esc(gem['source_note'])}{esc(fallback)} · {state} {links}</td></tr>")
    evidence = [f'<li><a href="{esc(source["url"])}">{esc(source["title"])}</a> · {esc(source["version"])} · {esc(source["checked_at"])}</li>' for source in sources.values()]
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(route['name'])}</title><style>body{{font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif;max-width:1080px;margin:32px auto;padding:0 22px;background:#f4f3ef;color:#23303b}}header,section{{padding:26px;background:white;border:1px solid #ddd;border-radius:12px;margin:18px 0}}h1{{font-size:30px}}h2{{color:#235b53}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}.note{{background:#fff5da;padding:14px}}@media print{{section{{break-inside:avoid}}}}</style><header><h1>{esc(route['name'])}</h1><p class="note">{esc(delivery_note)}</p><p>{esc(route['goal'])}</p><p>{esc(route['league'])} · {esc(class_name)} → {esc(ascendancy_name)} · {'自力获取' if route['trade_mode']=='ssf' else '允许交易'} · 初始预算 {route['budget']['amount']:g} {esc(route['budget']['currency'])}</p><p class="note">这是分阶段行动路线。已按本机 PoB {esc(engine_version)} / 天赋 {esc(tree_version)} 核对宝石、连线和升华结构；任务领取与国服改动以所列资料和实际游戏为准。尚未使用完整阶段装备进行伤害/生存实算，不承诺面板或通关速度。</p></header>{''.join(sections)}<section><h2>宝石取得总表</h2><table><tr><th>宝石</th><th>取得办法</th></tr>{''.join(rows)}</table></section><section><h2>需要留意</h2>{listing(warnings + route.get('risks', []) + route.get('notes', []))}</section><section><h2>查阅资料</h2><ul>{''.join(evidence)}</ul><p>实际推进后，可提供新的角色 XML，继续计算下一阶段的具体装备与天赋。</p></section></html>'''
