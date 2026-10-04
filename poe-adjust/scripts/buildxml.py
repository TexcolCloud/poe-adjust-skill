"""PoB document operations. Preserve unrelated sections and inactive sets."""
from __future__ import annotations

import base64
import copy
import hashlib
import math
import re
from pathlib import Path
import xml.etree.ElementTree as ET
import zlib

MAX_XML = 8 * 1024 * 1024
GEM_FIELDS = {"skill_id": "skillId", "level": "level", "quality": "quality", "enabled": "enabled", "enable_global1": "enableGlobal1", "enable_global2": "enableGlobal2", "count": "count", "skill_part": "skillPart", "skill_stage_count": "skillStageCount", "skill_minion": "skillMinion"}
GROUP_FIELDS = {"enabled": "enabled", "slot": "slot", "label": "label", "include_in_full_dps": "includeInFullDPS", "main_active_skill": "mainActiveSkill", "group_count": "groupCount"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def read_document(path: Path) -> ET.Element:
    require(path.is_file(), f"文件不存在：{path}")
    require(path.stat().st_size <= MAX_XML, "角色文件超过 8 MiB。")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    if not text.lstrip().startswith("<"):
        try:
            code = "".join(text.split())
            compressed = base64.b64decode(code + "=" * (-len(code) % 4), altchars=b"-_", validate=True)
            decoder = zlib.decompressobj()
            data = decoder.decompress(compressed, MAX_XML + 1)
            require(len(data) <= MAX_XML and decoder.eof and not decoder.unused_data, "PoB 分享码过大、不完整或有多余内容。")
            text = data.decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError, zlib.error) as exc:
            raise ValueError("需要 PoB XML 或纯 PoB 分享码文本；网页地址、HTML 和官网 JSON 请先转换为 PoECharm 可导入格式。") from exc
    return parse(text)


def parse(text: str) -> ET.Element:
    require(len(text.encode("utf-8")) <= MAX_XML, "XML 超过 8 MiB。")
    require(not re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I), "角色 XML 不应包含 DTD 或外部实体。")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败：{exc}") from exc
    require(root.tag == "PathOfBuilding", "这不是 PathOfBuilding 角色存档。")
    for name in ("Build", "Tree", "Skills", "Items"):
        require(root.find(name) is not None, f"角色缺少 {name} 区段。")
    require(root.find("Build").get("targetVersion", "3_0") == "3_0", "首版仅支持 PoE1。")
    require(int(root.find("Build").get("level", "0")) > 0, "角色等级应为正整数；游戏使用条件由当前引擎核对。")
    spec = active_spec(root)
    require(spec.get("treeVersion", "").startswith("3_"), "无法识别 PoE1 天赋版本。")
    ids = [item.get("id") for item in root.findall("Items/Item")]
    require(len(ids) == len(set(ids)), "XML 包含重复装备 ID。")
    require(all(i and i.isdigit() and int(i) > 0 for i in ids), "装备 ID 应为正整数。")
    for slot in root.findall("Items/ItemSet/Slot") + root.findall("Items/Slot") + root.findall("Tree/Spec/Sockets/Socket"):
        value = slot.get("itemId", "0")
        require(value == "0" or value in ids, f"{slot.get('name', '珠宝插槽')} 引用了不存在的装备 {value}。")
    active_set(root, "Skills", "SkillSet", "activeSkillSet")
    active_set(root, "Items", "ItemSet", "activeItemSet")
    return root


def serialise(root: ET.Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def active_set(root, section, tag, selector):
    parent = root.find(section)
    require(parent is not None, f"缺少 {section}。")
    sets = parent.findall(tag)
    if not sets:
        return parent
    selected = parent.get(selector, sets[0].get("id", "1"))
    match = next((node for node in sets if node.get("id") == selected), None)
    require(match is not None, f"{section} 的当前启用方案 {selected} 不存在。")
    return match


def active_spec(root):
    tree = root.find("Tree")
    require(tree is not None, "缺少天赋。")
    specs = tree.findall("Spec")
    index = int(tree.get("activeSpec", "1")) - 1
    require(0 <= index < len(specs), "当前天赋方案不存在。")
    return specs[index]


def groups(root):
    return active_set(root, "Skills", "SkillSet", "activeSkillSet").findall("Skill")


def text_item(item):
    return (item.text or "").strip()


def summary(root):
    spec = active_spec(root)
    skill_groups = []
    for index, group in enumerate(groups(root), 1):
        skill_groups.append({"index": index, **group.attrib, "gems": [{"index": n, **gem.attrib} for n, gem in enumerate(group.findall("Gem"), 1)]})
    return {
        "build": dict(root.find("Build").attrib), "tree": dict(spec.attrib),
        "slots": [dict(node.attrib) for node in active_set(root, "Items", "ItemSet", "activeItemSet").findall("Slot")],
        "jewels": [dict(node.attrib) for node in spec.findall("Sockets/Socket")],
        "items": [{"id": int(item.get("id")), "text": text_item(item)} for item in root.findall("Items/Item")],
        "skill_groups": skill_groups,
        "config": [dict(node.attrib) for node in active_config(root).findall("Input")],
        "set_counts": {"skills": len(root.findall("Skills/SkillSet")), "items": len(root.findall("Items/ItemSet")), "trees": len(root.findall("Tree/Spec"))},
        "note": "Build/PlayerStat 是缓存，需实算后比较。文件等级/天赋版本不等于已确认的国服赛季。",
    }


def active_config(root):
    parent = root.find("Config")
    if parent is None:
        parent = ET.SubElement(root, "Config")
    return active_set(root, "Config", "ConfigSet", "activeConfigSet")


def set_config(root, values):
    require(isinstance(values, dict), "config/scenario 应是对象。")
    config = active_config(root)
    for key, value in values.items():
        require(isinstance(key, str) and key, "配置名称应为本机返回的非空字段。")
        require(key not in {"customMods", "customModsExtra"}, "请使用真实装备和技能；适配器不接受自定义数值补丁。")
        require(value is None or isinstance(value, (str, int, float, bool)), "配置值只能是文本、数值、布尔值或 null。")
        require(not isinstance(value, float) or math.isfinite(value), "配置数值必须有限。")
        for element in list(config):
            if element.tag == "Input" and element.get("name") == key:
                config.remove(element)
        if value is not None:
            kind = "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"
            ET.SubElement(config, "Input", {"name": key, kind: attr(value)})


def attr(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def index_one(sequence, number, label):
    require(isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(sequence), f"{label} 索引不存在：{number}；请先 inspect。")
    return sequence[number - 1]


def update_gem(gem, values):
    require(not (set(values) - set(GEM_FIELDS) - {"group", "index"}), "宝石修改包含未知字段。")
    if "skill_id" in values:
        require(isinstance(values["skill_id"], str) and values["skill_id"], "skill_id 不能为空。")
        for key in ("gemId", "variantId", "nameSpec"):
            gem.attrib.pop(key, None)
    for key, value in values.items():
        if key in GEM_FIELDS:
            if key in {"level", "count"}: require(type(value) is int and value > 0, key + " 应为正整数；合法规格由原生数据和获取依据核对。")
            if key == "quality": require(type(value) is int and value >= 0, "宝石品质应为非负整数。")
            if key in {"enabled","enable_global1","enable_global2"}: require(type(value) is bool,key+" 应为布尔值。")
            if key == "skill_part": require(type(value) is int and value>0,"skill_part 应为正整数索引，实际选项由原生核对。")
            if key == "skill_stage_count": require(type(value) is int and value>=0,"skill_stage_count 应为非负整数，实际阶段范围由原生核对。")
            if key == "skill_minion": require(isinstance(value,str) and value,"skill_minion 使用本机名称。")
            gem.set(GEM_FIELDS[key], attr(value))


def update_group(group, values):
    for key,value in values.items():
        if key not in GROUP_FIELDS: continue
        if key in {"enabled","include_in_full_dps"}: require(type(value) is bool,key+" 应为布尔值。")
        if key in {"slot","label"}: require(isinstance(value,str),key+" 应为文本。")
        if key == "main_active_skill": require(type(value) is int and value>0,"main_active_skill 应为正整数。")
        if key == "group_count": require(type(value) in (int,float) and math.isfinite(value) and value>0,"group_count 应为正的有限数值。")
        group.set(GROUP_FIELDS[key],attr(value))


def apply_plan(source, plan, native=None):
    allowed = {"schema_version", "name", "mode", "summary", "template_xml", "operations", "sources", "shopping", "inventory", "assumptions", "migration_steps", "legality_notes", "scenario", "claims", "usage", "assessment", "evidence"}
    require(not (set(plan) - allowed), "计划包含未知字段：" + ", ".join(sorted(set(plan) - allowed)))
    require(plan.get("mode") in {"refine", "rebuild"}, "mode 应为 refine 或 rebuild。")
    require(isinstance(plan.get("summary"), str) and plan["summary"].strip(), "请说明方案目的。")
    root = copy.deepcopy(source)
    operations = plan.get("operations", {})
    require(isinstance(operations, dict), "operations 应是对象。")
    require(not (set(operations) - {"gems", "groups", "replace_skills", "items", "equip", "tree", "level", "main_group", "config", "weapon_set"}), "operations 包含未知操作。")
    build = root.find("Build")
    if "level" in operations:
        require(type(operations["level"]) is int and operations["level"] > 0, "level 应为正整数。")
        build.set("level", str(operations["level"]))
    build.set("characterLevelAutoMode", "false")
    if "replace_skills" in operations:
        new_groups = operations["replace_skills"]
        require(isinstance(new_groups, list) and new_groups, "replace_skills 需要至少一组技能。")
        parent = active_set(root, "Skills", "SkillSet", "activeSkillSet")
        for element in parent.findall("Skill"):
            parent.remove(element)
        for data in new_groups:
            require(not (set(data) - set(GROUP_FIELDS) - {"gems"}), "技能组包含未知字段。")
            group = ET.SubElement(parent, "Skill", {"enabled": "true", "includeInFullDPS": "false", "mainActiveSkill": "1"})
            update_group(group,data)
            require(data.get("gems"), "每个新技能组都需要 gems。")
            for values in data["gems"]:
                require(values.get("skill_id"), "新宝石需要 skill_id。")
                gem = ET.SubElement(group, "Gem", {"level": "1", "quality": "0", "enabled": "true", "enableGlobal1": "true", "enableGlobal2": "false"})
                update_gem(gem, values)
    for data in operations.get("groups", []):
        require(not (set(data) - set(GROUP_FIELDS) - {"index"}), "技能组修改包含未知字段。")
        group = index_one(groups(root), data.get("index"), "技能组")
        update_group(group,data)
    for data in operations.get("gems", []):
        group = index_one(groups(root), data.get("group"), "技能组")
        gem = index_one(group.findall("Gem"), data.get("index"), "宝石")
        update_gem(gem, data)
    changed_item_ids = set()
    for data in operations.get("items", []):
        require(set(data) <= {"id", "text"}, "装备包含未知字段。")
        require(type(data.get("id")) is int and data["id"] > 0, "装备需要正整数 id。")
        require(data["id"] not in changed_item_ids, "同一计划中装备 id 不能重复，避免覆盖已核对的目标装备。")
        changed_item_ids.add(data["id"])
        require(isinstance(data.get("text"), str) and 0 < len(data["text"]) <= 40000, "装备 text 不能为空或过长。")
        parent = root.find("Items")
        item = next((node for node in parent.findall("Item") if node.get("id") == str(data["id"])), None)
        if item is None: item = ET.SubElement(parent, "Item", {"id": str(data["id"])})
        else:
            item.clear()
            item.set("id", str(data["id"]))
        item.text = "\n" + data["text"].strip() + "\n"
    for data in operations.get("equip", []):
        require(set(data) <= {"slot", "item_id", "active"}, "插槽包含未知字段。")
        name = data.get("slot", "")
        known_slots = {slot["name"] for slot in (native or {}).get("slots", [])}
        known_slots.update(slot.get("name") for slot in active_set(source, "Items", "ItemSet", "activeItemSet").findall("Slot"))
        require(name in known_slots or re.fullmatch(r"Jewel \d+", name), f"当前已读数据中没有装备插槽：{name}；请先 catalog rules/inspect。")
        require(type(data.get("item_id")) is int and data["item_id"] >= 0, "item_id 应为非负整数。")
        if "active" in data: require(type(data["active"]) is bool,"装备 active 应为布尔值。")
        if name.startswith("Jewel "):
            spec = active_spec(root)
            parent = spec.find("Sockets")
            if parent is None: parent = ET.SubElement(spec, "Sockets")
            tag, key, key_value = "Socket", "nodeId", name.split()[1]
        else:
            parent = active_set(root, "Items", "ItemSet", "activeItemSet")
            tag, key, key_value = "Slot", "name", name
        element = next((node for node in parent.findall(tag) if node.get(key) == key_value), None)
        if element is None: element = ET.SubElement(parent, tag, {key: key_value})
        element.set("itemId", str(data["item_id"]))
        if "active" in data: element.set("active", attr(data["active"]))
    if "weapon_set" in operations:
        require(type(operations["weapon_set"]) is int and operations["weapon_set"] in (1, 2), "weapon_set 只能为 1 或 2。")
        active_set(root, "Items", "ItemSet", "activeItemSet").set("useSecondWeaponSet", attr(operations["weapon_set"] == 2))
    if "tree" in operations:
        data = operations["tree"]
        require(not (set(data) - {"nodes", "nodes_add", "nodes_remove", "class_id", "ascendancy_id", "secondary_ascendancy_id", "masteries", "masteries_add"}), "tree 包含未知字段；天赋版本沿用输入文件。")
        spec = active_spec(root)
        prior_masteries = {int(node): int(effect) for node, effect in re.findall(r"\{(\d+),(\d+)\}", spec.get("masteryEffects", ""))}
        require(not ("masteries" in data and "masteries_add" in data), "masteries 完整替换不能与 masteries_add 局部修改混用。")
        require(not ("nodes" in data and ("nodes_add" in data or "nodes_remove" in data)), "tree.nodes 完整替换不能与 nodes_add/nodes_remove 增量调整混用。")
        if "nodes_add" in data or "nodes_remove" in data:
            current = {int(n) for n in spec.get("nodes", "").split(",") if n}
            for key in ("nodes_add", "nodes_remove"):
                require(isinstance(data.get(key, []), list) and all(type(n) is int and n > 0 for n in data.get(key, [])), key + " 应为正整数节点 ID 数组。")
            added, removed = set(data.get("nodes_add", [])), set(data.get("nodes_remove", []))
            require(not (added & removed), "同一个节点不能同时添加和移除。")
            require(removed <= current, "nodes_remove 包含原方案未分配的节点。")
            data = {**data, "nodes": sorted((current - removed) | added)}
        if "nodes" in data:
            nodes = data["nodes"]
            require(isinstance(nodes, list) and all(type(n) is int and n > 0 for n in nodes), "nodes 应为天赋节点 ID 数组。")
            require(len(set(nodes)) == len(nodes), "nodes 包含重复节点。")
            spec.set("nodes", ",".join(map(str, nodes)))
            for node in spec.findall("URL"): spec.remove(node)
        if "class_id" in data:
            selected = next((entry for entry in (native or {}).get("classes", []) if type(data["class_id"]) is int and entry["id"] == data["class_id"]), None)
            require(selected is not None, "职业编号不在当前原生职业目录；先读取 classes。")
            spec.set("classId", str(data["class_id"]))
            build.set("className", selected["name"])
        for key, xml_key in (("ascendancy_id", "ascendClassId"), ("secondary_ascendancy_id", "secondaryAscendClassId")):
            if key in data:
                require(type(data[key]) is int and data[key] >= 0, "升华编号应为非负整数；归属由当前原生职业核对。")
                spec.set(xml_key, str(data[key]))
        if any(key in data for key in ("nodes", "masteries", "masteries_add")):
            allocated = {int(node) for node in spec.get("nodes", "").split(",") if node}
            field = "masteries" if "masteries" in data else "masteries_add"
            updates = data.get(field, {})
            require(isinstance(updates, dict) and all(str(node).isdigit() and int(node) > 0 and type(effect) is int and effect > 0 for node, effect in updates.items()), field + " 应为正整数节点 ID 到正整数专精效果 ID 的映射。")
            updates = {int(node): effect for node, effect in updates.items()}
            require(set(updates) <= allocated, "专精效果指向未分配的节点；先用 nodes_add 加入相应节点与连接路径。")
            if field == "masteries":
                missing = (set(prior_masteries) & allocated) - set(updates)
                require(not missing, "masteries 是完整替换，会清除仍保留节点的其他专精：" + ",".join(map(str, sorted(missing))) + "。局部修改请改用 masteries_add，程序会保留其他专精；退点使用 nodes_remove。不能把专精丢失当作普通天赋断路。")
                selected = updates
            else:
                selected = {**prior_masteries, **updates}
            spec.set("masteryEffects", ",".join("{%d,%d}" % (node, effect) for node, effect in sorted(selected.items()) if node in allocated))
    if "main_group" in operations:
        index_one(groups(root), operations["main_group"], "主技能组")
        build.set("mainSocketGroup", str(operations["main_group"]))
    if "config" in operations: set_config(root, operations["config"])
    parse(serialise(root))
    return root


def normal_item(text):
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def preserve_extensions(source, output):
    # PoB does not own extensions unknown to this version. Retain them verbatim.
    for child in source:
        if output.find(child.tag) is None:
            output.append(copy.deepcopy(child))
    return output
