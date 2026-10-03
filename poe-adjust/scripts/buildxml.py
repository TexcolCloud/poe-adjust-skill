"""PoB document operations. Preserve unrelated sections and inactive sets."""
from __future__ import annotations

import base64
import copy
import hashlib
import re
from pathlib import Path
import xml.etree.ElementTree as ET
import zlib

MAX_XML = 8 * 1024 * 1024
CLASSES = {0: "Scion", 1: "Marauder", 2: "Ranger", 3: "Witch", 4: "Duelist", 5: "Templar", 6: "Shadow"}
SLOTS = {"Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap", "Helmet", "Body Armour", "Gloves", "Boots", "Amulet", "Belt", "Ring 1", "Ring 2", *(f"Flask {i}" for i in range(1, 6))}
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
    require(1 <= int(root.find("Build").get("level", "0")) <= 100, "角色等级必须在 1–100。")
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


def set_config(root, values, *, reset=False):
    require(isinstance(values, dict), "config/scenario 应是对象。")
    config = active_config(root)
    if reset:
        for element in list(config):
            if element.tag == "Input" and element.get("name") not in {"bandit", "pantheonMajorGod", "pantheonMinorGod"}:
                config.remove(element)
    for key, value in values.items():
        require(key not in {"customMods", "customModsExtra"}, "请使用真实装备和技能；适配器不接受自定义数值补丁。")
        require(value is None or isinstance(value, (str, int, float, bool)), "配置值只能是文本、数值、布尔值或 null。")
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
            if key == "level": require(isinstance(value, int) and 1 <= value <= 40, "宝石等级超出 1–40。")
            if key == "quality": require(isinstance(value, int) and 0 <= value <= 100, "宝石品质超出 0–100。")
            if key == "count": require(isinstance(value, int) and 1 <= value <= 100, "宝石数量超出范围。")
            gem.set(GEM_FIELDS[key], attr(value))


def apply_plan(source, plan):
    allowed = {"name", "mode", "summary", "template_xml", "operations", "sources", "shopping", "assumptions", "migration_steps", "config_evidence", "legality_notes"}
    require(not (set(plan) - allowed), "计划包含未知字段：" + ", ".join(sorted(set(plan) - allowed)))
    require(plan.get("mode") in {"refine", "rebuild"}, "mode 应为 refine 或 rebuild。")
    require(isinstance(plan.get("summary"), str) and plan["summary"].strip(), "请说明方案目的。")
    root = copy.deepcopy(source)
    operations = plan.get("operations", {})
    require(isinstance(operations, dict), "operations 应是对象。")
    require(not (set(operations) - {"gems", "groups", "replace_skills", "items", "equip", "tree", "level", "main_group", "config", "weapon_set"}), "operations 包含未知操作。")
    build = root.find("Build")
    if "level" in operations:
        require(isinstance(operations["level"], int) and 1 <= operations["level"] <= 100, "level 超出范围。")
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
            for key, value in data.items():
                if key in GROUP_FIELDS: group.set(GROUP_FIELDS[key], attr(value))
            require(data.get("gems"), "每个新技能组都需要 gems。")
            for values in data["gems"]:
                require(values.get("skill_id"), "新宝石需要 skill_id。")
                gem = ET.SubElement(group, "Gem", {"level": "1", "quality": "0", "enabled": "true", "enableGlobal1": "true", "enableGlobal2": "false"})
                update_gem(gem, values)
    for data in operations.get("groups", []):
        require(not (set(data) - set(GROUP_FIELDS) - {"index"}), "技能组修改包含未知字段。")
        group = index_one(groups(root), data.get("index"), "技能组")
        for key, value in data.items():
            if key in GROUP_FIELDS: group.set(GROUP_FIELDS[key], attr(value))
    for data in operations.get("gems", []):
        group = index_one(groups(root), data.get("group"), "技能组")
        gem = index_one(group.findall("Gem"), data.get("index"), "宝石")
        update_gem(gem, data)
    for data in operations.get("items", []):
        require(set(data) <= {"id", "text"}, "装备包含未知字段。")
        require(isinstance(data.get("id"), int) and data["id"] > 0, "装备需要正整数 id。")
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
        require(name in SLOTS or re.fullmatch(r"Jewel \d+", name), f"未知装备插槽：{name}")
        require(isinstance(data.get("item_id"), int) and data["item_id"] >= 0, "item_id 应为非负整数。")
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
        require(operations["weapon_set"] in (1, 2), "weapon_set 只能为 1 或 2。")
        active_set(root, "Items", "ItemSet", "activeItemSet").set("useSecondWeaponSet", attr(operations["weapon_set"] == 2))
    if "tree" in operations:
        data = operations["tree"]
        require(not (set(data) - {"nodes", "class_id", "ascendancy_id", "secondary_ascendancy_id", "masteries"}), "tree 包含未知字段；天赋版本沿用输入文件。")
        spec = active_spec(root)
        if "nodes" in data:
            nodes = data["nodes"]
            require(isinstance(nodes, list) and all(isinstance(n, int) and n > 0 for n in nodes), "nodes 应为天赋节点 ID 数组。")
            require(len(set(nodes)) == len(nodes), "nodes 包含重复节点。")
            spec.set("nodes", ",".join(map(str, nodes)))
            for node in spec.findall("URL"): spec.remove(node)
        if "class_id" in data:
            require(data["class_id"] in CLASSES, "职业编号无效。")
            spec.set("classId", str(data["class_id"]))
            build.set("className", CLASSES[data["class_id"]])
        for key, xml_key in (("ascendancy_id", "ascendClassId"), ("secondary_ascendancy_id", "secondaryAscendClassId")):
            if key in data:
                require(isinstance(data[key], int) and 0 <= data[key] <= 10, "升华编号无效。")
                spec.set(xml_key, str(data[key]))
        if "masteries" in data:
            require(isinstance(data["masteries"], dict), "masteries 应为节点 ID 到专精效果 ID 的映射。")
            spec.set("masteryEffects", ",".join("{%d,%d}" % (int(k), int(v)) for k, v in data["masteries"].items()))
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
