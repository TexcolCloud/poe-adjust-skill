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
PLAN_FIELDS={"schema_version", "name", "mode", "summary", "template_xml", "operations", "sources", "shopping", "inventory", "assumptions", "migration_steps", "legality_notes", "scenario", "measurement", "claims", "usage", "timeline", "assessment", "evidence"}
OPERATION_FIELDS={"gems", "groups", "replace_skills", "items", "equip", "tree", "level", "main_group", "config", "weapon_set"}
TREE_FIELDS={"nodes", "nodes_add", "nodes_remove", "class_id", "ascendancy_id", "secondary_ascendancy_id", "masteries", "masteries_add"}
_UNSET = object()


class ContractError(ValueError):
    """An input error with a concrete field and observable repair information."""
    def __init__(self, message, *, code="invalid_input", action="edit_input", location=None, expected=None, actual=_UNSET, input_file=None, available_options=None):
        super().__init__(message)
        self.diagnostic={"code":code,"message":message,"action":action,"retryable":False}
        for key,value in (("location",location),("expected",expected),("input_file",input_file),("available_options",available_options)):
            if value is not None: self.diagnostic[key]=value
        if actual is not _UNSET: self.diagnostic["actual"]=actual


def require(condition, message, **details):
    if not condition:
        raise ContractError(message, **details)


def finite_number(value):
    """Numbers crossing Lua/native arithmetic must fit a finite double."""
    if type(value) not in (int,float): return False
    try: return math.isfinite(value)
    except OverflowError: return False


def require_keys(value, allowed, location, message="包含未知字段。"):
    """Report the contract at the failing object, rather than guessing a repair."""
    require(isinstance(value, dict), "需要 JSON 对象。", location=location, expected="object", actual=type(value).__name__)
    unknown = sorted(set(value) - set(allowed))
    require(not unknown, message + " 未知：" + ", ".join(unknown), location=location,
            expected=sorted(allowed), actual=unknown)


def game_version_shape(value):
    require(value is None or isinstance(value,str) and re.fullmatch(r"3\.\d+",value),
            "game_version 使用已确认的机制版本；未知用 null，说明另写 notes/version_source。",
            location="/game_version",expected="3.<integer> or null",actual=value)


def gem_shape(values, location, *, allowed=None, auto=False):
    require_keys(values,allowed if allowed is not None else set(GEM_FIELDS)|{"group","index"},location)
    for key,value in values.items():
        path=location+"/"+key
        if key in {"skill_id","instance_id","skill_minion","level_selection"}:
            require(isinstance(value,str) and value.strip(),key+" 应为非空文字。",location=path,expected="nonempty string",actual=value)
        if key in {"level","count","skill_part","group","index"}:
            require(type(value) is int and value>0 or key=="level" and auto and value=="auto",key+" 应为正整数。",location=path,expected="positive integer or auto" if key=="level" and auto else "positive integer",actual=value)
        if key in {"quality","skill_stage_count"}:
            require(type(value) is int and value>=0,key+" 应为非负整数。",location=path,expected="nonnegative integer",actual=value)
        if key in {"enabled","enable_global1","enable_global2"}:
            require(type(value) is bool,key+" 应为布尔值。",location=path,expected="boolean",actual=value)


def group_shape(values, location, *, allowed=None):
    require_keys(values,allowed if allowed is not None else set(GROUP_FIELDS)|{"index","gems"},location)
    for key,value in values.items():
        path=location+"/"+key
        if key in {"enabled","include_in_full_dps"}:
            require(type(value) is bool,key+" 应为布尔值。",location=path,expected="boolean",actual=value)
        if key in {"slot","label","name","use"}:
            require(isinstance(value,str) and (key in {"slot","label"} or value.strip()),key+" 应为文字。",location=path,expected="string" if key in {"slot","label"} else "nonempty string",actual=value)
        if key in {"index","main_active_skill"}:
            require(type(value) is int and value>0,key+" 应为正整数。",location=path,expected="positive integer",actual=value)
        if key=="group_count":
            require(finite_number(value) and value>0,key+" 应为有限正数。",location=path,expected="finite positive number",actual=value)
        if key=="gems": require(isinstance(value,list),"gems 应为数组。",location=path,expected="array",actual=value)


def tree_shape(data, location):
    require_keys(data,TREE_FIELDS,location,"tree 包含未知字段；版本沿用输入 XML。")
    require(not ("nodes" in data and ("nodes_add" in data or "nodes_remove" in data)),"完整替换 nodes 不能与增量操作混用。",location=location,expected="nodes OR nodes_add/nodes_remove",actual=sorted(data))
    require(not ("masteries" in data and "masteries_add" in data),"完整替换 masteries 不能与增量操作混用。",location=location,expected="masteries OR masteries_add",actual=sorted(data))
    for key,value in data.items():
        path=location+"/"+key
        if key.startswith("nodes"):
            require(isinstance(value,list) and all(type(v) is int and v>0 for v in value),key+" 应为正整数节点数组。",location=path,expected="positive integer array",actual=value)
            require(len(set(value))==len(value),key+" 不能重复节点。",location=path,expected="distinct node IDs",actual=value)
        elif key in {"class_id","ascendancy_id","secondary_ascendancy_id"}:
            require(type(value) is int and value>=0,key+" 应为非负整数。",location=path,expected="nonnegative integer",actual=value)
        else:
            require(isinstance(value,dict) and all(isinstance(k,str) and k.isdigit() and int(k)>0 and type(v) is int and v>0 for k,v in value.items()),key+" 应为正整数节点到专精效果映射。",location=path,expected="positive node ID keys and positive integer effect IDs",actual=value)
            require(len({int(k) for k in value})==len(value),"专精节点的不同写法不能指向同一节点。",location=path,expected="distinct numeric node IDs",actual=value)
    overlap=set(data.get("nodes_add",[])) & set(data.get("nodes_remove",[]))
    require(not overlap,"同一节点不能同时添加和移除。",location=location,expected="disjoint add/remove",actual=sorted(overlap))


def config_shape(values, location):
    require(isinstance(values,dict),"配置应为对象。",location=location,expected="object",actual=values)
    for key,value in values.items():
        require(isinstance(key,str),"配置字段名应为文字。",location=location,expected="string key",actual=key)
        path=location+"/"+key.replace("~","~0").replace("/","~1")
        require(key and key not in {"customMods","customModsExtra"},"使用真实配置字段和装备，不接受自定义数值补丁。",location=path,expected="nonempty native config key excluding customMods/Extra",actual=key)
        require(value is None or type(value) in (str,int,float,bool),"配置值为文本、数值、布尔或 null。",location=path,expected="scalar or null",actual=value)
        require(type(value) not in (int,float) or finite_number(value),"配置数值应有限。",location=path,expected="finite representable number",actual=value)


def same_scalar(left, right):
    """Lua numeric values may be ints/floats; booleans never equal 0/1."""
    return (type(left) is type(right) or type(left) in (int,float) and type(right) in (int,float)) and left == right


def config_value_shape(key, value, native, location):
    """Validate current native input types, without inventing gameplay limits."""
    if value is None: return  # Deleting an override also permits clearing an obsolete field.
    types=native.get("config_types")
    require(isinstance(types,dict) and native.get("capabilities",{}).get("native_config_types",True) is True,"本机未提供配置字段类型；不能猜测配置输入。",code="native_capability_unavailable",action="inspect_capability",location=location,expected="current native config_types",actual=types)
    require(key in types,"本机未识别配置字段："+key,code="unknown_native_config",location=location,expected="current native config key",actual=key,available_options=sorted(types))
    kind=types[key]
    if kind=="list":
        lists=native.get("config_lists");options=lists.get(key) if isinstance(lists,dict) else None
        require(isinstance(options,list) and options,"本机未提供配置合法选项："+key,code="native_capability_unavailable",action="inspect_capability",location=location,expected="nonempty current native options",actual=options)
        require(any(same_scalar(value,option) for option in options),f"无效配置值：{key}={value!r}；请使用本次 catalog config 的 val。",location=location,expected=options,actual=value,available_options=options)
        return
    if kind=="check": valid,expected=type(value) is bool,"boolean"
    elif kind in {"count","integer","countAllowZero","float"}: valid,expected=finite_number(value),"finite number"
    elif kind=="text": valid,expected=type(value) is str,"string"
    else:
        raise ContractError("当前配置控件类型尚未适配："+str(kind),code="native_capability_unavailable",action="inspect_capability",location=location,expected="supported native input type",actual=kind)
    require(valid,f"配置 {key} 的本机类型为 {kind}，需要 {expected}；实际为 {value!r}。",location=location,expected=expected,actual=value)


def config_input_value(entry, location):
    """Read the actual XML value; never reinterpret truthy strings as false."""
    kinds=[key for key in ("string","number","boolean") if key in entry.attrib]
    require(len(kinds)==1,"配置 XML 需要恰好一种值类型。",location=location,expected="one string/number/boolean attribute",actual=dict(entry.attrib))
    kind=kinds[0]; raw=entry.get(kind)
    if kind=="string": return raw
    if kind=="boolean":
        require(raw in ("true","false"),"配置 XML 布尔值需要 true 或 false。",location=location,expected="true or false",actual=raw)
        return raw=="true"
    try: value=float(raw)
    except (ValueError,TypeError): value=None
    require(finite_number(value),"配置 XML 数值需要有限数。",location=location,expected="finite number",actual=raw)
    return value


def item_shape(item, location, *, complete=True):
    require_keys(item,{"id","text","craft"},location)
    require(not ("text" in item and "craft" in item),"craft 配方不能混入 text。",location=location,expected="text OR craft",actual=sorted(item))
    if complete or "id" in item: require(type(item.get("id")) is int and item["id"]>0,"装备需要正整数 id。",location=location+"/id",expected="positive integer",actual=item.get("id"))
    if "text" in item: require(isinstance(item["text"],str) and 0<len(item["text"])<=40000,"装备 text 不能为空或过长。",location=location+"/text",expected="string length 1..40000",actual=item["text"])
    if "craft" in item:
        path=location+"/craft";spec=item["craft"]
        require_keys(spec,{"base","ilvl","quality","sockets","affixes"},path)
        if complete or "base" in spec: require(isinstance(spec.get("base"),str) and spec["base"].strip(),"base 使用本机完整基底名。",location=path+"/base",expected="nonempty string",actual=spec.get("base"))
        for key,minimum in (("ilvl",1),("quality",0)):
            if key in spec or complete and key=="ilvl": require(type(spec.get(key)) is int and spec[key]>=minimum,key+" 使用整数。",location=path+"/"+key,expected="positive integer" if minimum else "nonnegative integer",actual=spec.get(key))
        if "sockets" in spec: require(isinstance(spec["sockets"],str) and (not spec["sockets"] or re.fullmatch(r"[RGBW](?:[- ][RGBW])*",spec["sockets"])),"sockets 为普通孔色和连线。",location=path+"/sockets",expected="ordinary socket specification or empty string",actual=spec["sockets"])
        if complete or "affixes" in spec:
            affixes=spec.get("affixes");require(isinstance(affixes,list),"affixes 为数组，空数组生成普通基底。",location=path+"/affixes",expected="array",actual=affixes)
            for index,affix in enumerate(affixes):
                loc=path+f"/affixes/{index}";require_keys(affix,{"id","roll"},loc)
                if complete or "id" in affix: require(isinstance(affix.get("id"),str) and affix["id"].strip(),"词缀 id 使用本机值。",location=loc+"/id",expected="nonempty string",actual=affix.get("id"))
                if "roll" in affix: require(finite_number(affix["roll"]) and 0<=affix["roll"]<=1,"roll 是范围位置 0–1。",location=loc+"/roll",expected="finite number 0..1",actual=affix["roll"])
    if complete: require("text" in item or "craft" in item,"装备需要 text 或 craft。",location=location,expected="text OR craft",actual=sorted(item))


def operations_shape(operations, *, complete=True):
    require_keys(operations,OPERATION_FIELDS,"/operations","operations 包含未知操作。")
    for key in ("level","main_group","weapon_set"):
        if key in operations: require(type(operations[key]) is int and (operations[key] in (1,2) if key=="weapon_set" else operations[key]>0),key+" 值无效。",location="/operations/"+key,expected=[1,2] if key=="weapon_set" else "positive integer",actual=operations[key])
    if "tree" in operations: tree_shape(operations["tree"],"/operations/tree")
    if "config" in operations: config_shape(operations["config"],"/operations/config")
    for key in ("gems","groups","replace_skills","items","equip"):
        if key not in operations: continue
        values=operations[key];path="/operations/"+key
        require(isinstance(values,list),key+" 应为数组。",location=path,expected="array",actual=values)
        if key=="replace_skills" and complete: require(values,"replace_skills 至少一组。",location=path,expected="nonempty array",actual=values)
        seen=set()
        for index,row in enumerate(values):
            location=path+"/"+str(index)
            if key=="items": item_shape(row,location,complete=complete)
            elif key=="gems":
                gem_shape(row,location)
                if complete:
                    for field in ("group","index"): require(field in row,"修改宝石需要索引。",location=location+"/"+field,expected="positive integer",actual=row.get(field))
            elif key in {"groups","replace_skills"}:
                group_shape(row,location,allowed=set(GROUP_FIELDS)|({"index"} if key=="groups" else {"gems"}))
                if complete:
                    field="index" if key=="groups" else "gems";require(field in row,"技能组缺少 "+field,location=location+"/"+field,expected="positive integer" if key=="groups" else "array",actual=row.get(field))
                for n,gem in enumerate(row.get("gems",[])):
                    loc=location+f"/gems/{n}";gem_shape(gem,loc,allowed=set(GEM_FIELDS))
                    if complete: require("skill_id" in gem,"新宝石需要 skill_id。",location=loc+"/skill_id",expected="nonempty string",actual=gem.get("skill_id"))
            else:
                require_keys(row,{"slot","item_id","active"},location)
                if complete or "slot" in row: require(isinstance(row.get("slot"),str) and row["slot"],"slot 使用本机槽位名。",location=location+"/slot",expected="nonempty string",actual=row.get("slot"))
                if complete or "item_id" in row: require(type(row.get("item_id")) is int and row["item_id"]>=0,"item_id 应为非负整数。",location=location+"/item_id",expected="nonnegative integer",actual=row.get("item_id"))
                if "active" in row: require(type(row["active"]) is bool,"active 应为布尔值。",location=location+"/active",expected="boolean",actual=row["active"])
            fields={"items":("id",),"equip":("slot",),"gems":("group","index"),"groups":("index",)}.get(key,())
            if fields and all(f in row for f in fields):
                identity=tuple(row[f] for f in fields);require(identity not in seen,fields[-1]+" 不能重复操作同一对象。",location=location+"/"+fields[-1],expected="unique operation selector",actual=list(identity));seen.add(identity)


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


def set_config(root, values, *, native=None, location="/operations/config"):
    config_shape(values,location)
    if native is not None:
        for key,value in values.items():
            config_value_shape(key,value,native,location+"/"+key.replace("~","~0").replace("/","~1"))
    config = active_config(root)
    for key, value in values.items():
        for element in list(config):
            if element.tag == "Input" and element.get("name") == key:
                config.remove(element)
        if value is not None:
            kind = "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"
            ET.SubElement(config, "Input", {"name": key, kind: attr(value)})


def attr(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def index_one(sequence, number, label, *, location="/"):
    require(type(number) is int and 1 <= number <= len(sequence), f"{label} 索引不存在：{number}；请先 inspect。",location=location,expected={"minimum":1,"maximum":len(sequence)},actual=number)
    return sequence[number - 1]


def update_gem(gem, values, *, location="/gems"):
    gem_shape(values,location)
    if "skill_id" in values:
        for key in ("gemId", "variantId", "nameSpec"):
            gem.attrib.pop(key, None)
    for key, value in values.items():
        if key in GEM_FIELDS:
            gem.set(GEM_FIELDS[key], attr(value))


def update_group(group, values, *, location="/groups"):
    group_shape(values,location)
    for key,value in values.items():
        if key not in GROUP_FIELDS: continue
        group.set(GROUP_FIELDS[key],attr(value))


def verify_selections(document, result, operations=None):
    """Check requested meaning before accepting the native normalized save."""
    explicit_only=operations is not None
    operations=operations or {}
    selected=document.find('Build').get('mainSocketGroup')
    if selected not in (None,'nil') and (not explicit_only or 'main_group' in operations):
        requested=int(selected)
        require(requested==result.get('main_group'),"原生改写了请求的主技能组。",code="native_selection_changed",location="/operations/main_group",expected=requested,actual=result.get('main_group'))
    observed={row['group']:row for row in result.get('skill_selections',[])}
    saved=parse(result['xml'])
    saved_groups=groups(saved)
    for index,group in enumerate(groups(document),1):
        path=f"/skill_groups/{index-1}"
        replacement=operations.get('replace_skills',[])
        declared=replacement[index-1] if index<=len(replacement) else {}
        if replacement: path=f"/operations/replace_skills/{index-1}"
        for change_index,change in enumerate(operations.get('groups',[])):
            if change['index']==index and 'main_active_skill' in change:
                path=f"/operations/groups/{change_index}"
                declared={**declared,**change}
        raw=group.get('mainActiveSkill')
        if raw not in (None,'nil') and (not explicit_only or 'main_active_skill' in declared):
            requested=int(raw)
            row=observed.get(index)
            require(row is not None,"当前原生接口未返回这一组的可选技能。",code="native_selection_unavailable",location=path+'/main_active_skill',expected="materialized native choices",actual=None)
            choices={choice['index'] for choice in row['choices']}
            actual=row['main_active_skill']
            # Empty/support-only groups have the native default 1 and no skill
            # to select. Omitted native defaults do not change user intent.
            valid=requested in choices or requested==1 and not choices
            require(valid and requested==actual,"请求的主主动技能不可用或被原生改写。",code="native_selection_changed",location=path+'/main_active_skill',expected=requested,actual=actual,available_options=row['choices'])
        for gem_index,gem in enumerate(group.findall('Gem'),1):
            for field in ('skill_part','skill_stage_count','skill_minion'):
                attr_name=GEM_FIELDS[field];expected=gem.get(attr_name)
                entries=declared.get('gems',[])
                explicit=gem_index<=len(entries) and field in entries[gem_index-1]
                explicit=explicit or any(change['group']==index and change['index']==gem_index and field in change for change in operations.get('gems',[]))
                if expected is None or expected=='nil' and not explicit or explicit_only and not explicit: continue
                require(index<=len(saved_groups),"原生保存丢失了请求的技能组。",code="native_selection_changed",location=f"/skill_groups/{index-1}",expected="preserved group",actual=None)
                actual_gems=saved_groups[index-1].findall('Gem')
                require(gem_index<=len(actual_gems),"原生保存丢失了请求的宝石。",code="native_selection_changed",location=f"/skill_groups/{index-1}/gems/{gem_index-1}",expected="preserved gem",actual=None)
                actual=actual_gems[gem_index-1].get(attr_name)
                # Part 1 is the native default when omitted on save.
                if field=='skill_part': expected,actual=int(expected),int(actual or 1)
                elif field=='skill_stage_count': expected,actual=int(expected),int(actual or 0)
                location=f"/operations/replace_skills/{index-1}/gems/{gem_index-1}/{field}" if 'replace_skills' in operations else f"/skill_groups/{index-1}/gems/{gem_index-1}/{field}"
                for change_index,change in enumerate(operations.get('gems',[])):
                    if change['group']==index and change['index']==gem_index and field in change: location=f"/operations/gems/{change_index}/{field}"
                require(expected==actual,"原生保存改写了请求的技能状态。",code="native_selection_changed",location=location,expected=expected,actual=actual)


def measurement_shape(measurement):
    require_keys(measurement,{"effects","flasks_enabled"},"/measurement")
    if "flasks_enabled" in measurement:
        require(type(measurement["flasks_enabled"]) is bool,"flasks_enabled 应为布尔值。",location="/measurement/flasks_enabled",expected="boolean",actual=measurement["flasks_enabled"])
    if "effects" in measurement:
        rows=measurement["effects"];require(isinstance(rows,list),"effects 为数组。",location="/measurement/effects",expected="array",actual=type(rows).__name__)
        seen=set()
        for index,row in enumerate(rows):
            path=f"/measurement/effects/{index}";require_keys(row,{"skill_id","enabled"},path)
            require(isinstance(row.get("skill_id"),str) and row["skill_id"] and row["skill_id"] not in seen,"使用当前 granted_effects 的唯一效果 ID。",location=path+"/skill_id",expected="unique native effect ID",actual=row.get("skill_id"))
            require(type(row.get("enabled")) is bool,"enabled 应为布尔值。",location=path+"/enabled",expected="boolean",actual=row.get("enabled"))
            seen.add(row["skill_id"])


def apply_measurement(source, measurement, native):
    """Resolve common effect controls through the current native gem instances."""
    measurement_shape(measurement)
    root=copy.deepcopy(source);audit={"effects":[],"flasks":[]}
    for index,control in enumerate(measurement.get("effects",[])):
        row={**control,"status":"absent","instances":[]}
        for gem in native.get("gems",[]):
            for effect in gem.get("granted_effects",[]):
                if effect["id"]!=control["skill_id"]: continue
                field=effect.get("operation_field");path=f"/measurement/effects/{index}"
                require(effect.get("has_global_effect") and field in GEM_FIELDS,"此效果没有已适配的原生全局开关。",location=path+"/skill_id",expected="native global effect with operation_field",actual=effect)
                group=index_one(groups(root),gem["group"],"效果所属技能组",location=path)
                actual=index_one(group.findall("Gem"),gem["index"],"效果所属宝石",location=path)
                require(actual.get("skillId")==gem["id"],"原生效果与 XML 宝石归属不符。",location=path,expected=gem["id"],actual=actual.get("skillId"))
                actual.set(GEM_FIELDS[field],attr(control["enabled"]))
                row["instances"].append({"group":gem["group"],"index":gem["index"],"gem_id":gem["id"],"operation_field":field})
                row["status"]="applied"
        audit["effects"].append(row)
    if "flasks_enabled" in measurement:
        items=active_set(root,"Items","ItemSet","activeItemSet")
        for item in native.get("equipped",[]):
            if item.get("type")!="Flask": continue
            slot=next((slot for slot in items.findall("Slot") if slot.get("name")==item["slot"]),None)
            require(slot is not None and slot.get("itemId")==str(item["id"]),"药剂测量条件与 XML 槽位归属不符。",location="/measurement/flasks_enabled",expected=item["slot"],actual=None if slot is None else slot.attrib)
            slot.set("active",attr(measurement["flasks_enabled"]))
            audit["flasks"].append({"slot":item["slot"],"item_id":item["id"],"enabled":measurement["flasks_enabled"]})
    return root,audit


def plan_shape(plan, *, complete=True):
    require_keys(plan,PLAN_FIELDS,"/","计划包含未知字段。")
    if complete or "mode" in plan: require(isinstance(plan.get("mode"),str) and plan["mode"] in {"refine","rebuild"},"mode 应为 refine 或 rebuild。",location="/mode",expected=["refine","rebuild"],actual=plan.get("mode"))
    for key in ("name","summary","template_xml"):
        if key in plan or complete and key=="summary": require(isinstance(plan.get(key),str) and plan[key].strip(),key+" 应为非空文字。",location="/"+key,expected="nonempty string",actual=plan.get(key))
    operations_shape(plan.get("operations",{}),complete=complete)
    if "scenario" in plan: config_shape(plan["scenario"],"/scenario")
    if "measurement" in plan: measurement_shape(plan["measurement"])
    for key in ("claims","usage","evidence","inventory","shopping"):
        if key in plan: require(isinstance(plan[key],list),key+" 应为数组。",location="/"+key,expected="array",actual=type(plan[key]).__name__)
    return plan


def apply_plan(source, plan, native=None):
    plan_shape(plan)
    root = copy.deepcopy(source)
    operations = plan.get("operations", {})
    build = root.find("Build")
    if "level" in operations:
        build.set("level", str(operations["level"]))
    build.set("characterLevelAutoMode", "false")
    if "replace_skills" in operations:
        new_groups = operations["replace_skills"]
        parent = active_set(root, "Skills", "SkillSet", "activeSkillSet")
        for element in parent.findall("Skill"):
            parent.remove(element)
        for group_index,data in enumerate(new_groups):
            path=f"/operations/replace_skills/{group_index}"
            group = ET.SubElement(parent, "Skill", {"enabled": "true", "includeInFullDPS": "false", "mainActiveSkill": "1"})
            update_group(group,data,location=path)
            for gem_index,values in enumerate(data["gems"]):
                gem = ET.SubElement(group, "Gem", {"level": "1", "quality": "0", "enabled": "true", "enableGlobal1": "true", "enableGlobal2": "false"})
                update_gem(gem, values,location=path+f"/gems/{gem_index}")
    for group_index,data in enumerate(operations.get("groups", [])):
        path=f"/operations/groups/{group_index}"
        group = index_one(groups(root), data.get("index"), "技能组",location=path+"/index")
        update_group(group,data,location=path)
    for gem_index,data in enumerate(operations.get("gems", [])):
        group = index_one(groups(root), data.get("group"), "技能组",location=f"/operations/gems/{gem_index}/group")
        gem = index_one(group.findall("Gem"), data.get("index"), "宝石",location=f"/operations/gems/{gem_index}/index")
        update_gem(gem, data,location=f"/operations/gems/{gem_index}")
    for index,data in enumerate(operations.get("items", [])):
        require("text" in data,"先用本机配方接口物化 craft，再应用 XML。",code="unsupported_operation",location=f"/operations/items/{index}/craft",expected="materialized native item text",actual="unmaterialized craft")
        parent = root.find("Items")
        item = next((node for node in parent.findall("Item") if node.get("id") == str(data["id"])), None)
        if item is None: item = ET.SubElement(parent, "Item", {"id": str(data["id"])})
        else:
            item.clear()
            item.set("id", str(data["id"]))
        item.text = "\n" + data["text"].strip() + "\n"
    for index,data in enumerate(operations.get("equip", [])):
        name = data.get("slot", "")
        known_slots = {slot["name"] for slot in (native or {}).get("slots", [])}
        known_slots.update(slot.get("name") for slot in active_set(source, "Items", "ItemSet", "activeItemSet").findall("Slot"))
        require(name in known_slots or re.fullmatch(r"Jewel \d+", name), f"当前已读数据中没有装备插槽：{name}；请先 catalog rules/inspect。",location=f"/operations/equip/{index}/slot",expected=sorted(known_slots),actual=name)
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
        active_set(root, "Items", "ItemSet", "activeItemSet").set("useSecondWeaponSet", attr(operations["weapon_set"] == 2))
    if "tree" in operations:
        data = operations["tree"]
        spec = active_spec(root)
        prior_masteries = {int(node): int(effect) for node, effect in re.findall(r"\{(\d+),(\d+)\}", spec.get("masteryEffects", ""))}
        if "nodes_add" in data or "nodes_remove" in data:
            current = {int(n) for n in spec.get("nodes", "").split(",") if n}
            added, removed = set(data.get("nodes_add", [])), set(data.get("nodes_remove", []))
            require(removed <= current, "nodes_remove 包含原方案未分配的节点。",location="/operations/tree/nodes_remove",expected=sorted(current),actual=sorted(removed))
            data = {**data, "nodes": sorted((current - removed) | added)}
        if "nodes" in data:
            nodes = data["nodes"]
            spec.set("nodes", ",".join(map(str, nodes)))
            for node in spec.findall("URL"): spec.remove(node)
        if "class_id" in data:
            selected = next((entry for entry in (native or {}).get("classes", []) if type(data["class_id"]) is int and entry["id"] == data["class_id"]), None)
            require(selected is not None, "职业编号不在当前原生职业目录；先读取 classes。",location="/operations/tree/class_id",expected=[c["id"] for c in (native or {}).get("classes",[])],actual=data["class_id"])
            spec.set("classId", str(data["class_id"]))
            build.set("className", selected["name"])
        for key, xml_key in (("ascendancy_id", "ascendClassId"), ("secondary_ascendancy_id", "secondaryAscendClassId")):
            if key in data:
                spec.set(xml_key, str(data[key]))
        if any(key in data for key in ("nodes", "masteries", "masteries_add")):
            allocated = {int(node) for node in spec.get("nodes", "").split(",") if node}
            field = "masteries" if "masteries" in data else "masteries_add"
            updates = data.get(field, {})
            updates = {int(node): effect for node, effect in updates.items()}
            require(set(updates) <= allocated, "专精效果指向未分配的节点；先用 nodes_add 加入相应节点与连接路径。",location="/operations/tree/"+field,expected=sorted(allocated),actual=sorted(updates))
            if field == "masteries":
                missing = (set(prior_masteries) & allocated) - set(updates)
                require(not missing, "masteries 是完整替换，会清除仍保留节点的其他专精：" + ",".join(map(str, sorted(missing))) + "。局部修改请改用 masteries_add，程序会保留其他专精；退点使用 nodes_remove。不能把专精丢失当作普通天赋断路。")
                selected = updates
            else:
                selected = {**prior_masteries, **updates}
            spec.set("masteryEffects", ",".join("{%d,%d}" % (node, effect) for node, effect in sorted(selected.items()) if node in allocated))
    if "main_group" in operations:
        index_one(groups(root), operations["main_group"], "主技能组",location="/operations/main_group")
        build.set("mainSocketGroup", str(operations["main_group"]))
    if "config" in operations: set_config(root, operations["config"],native=native)
    parse(serialise(root))
    return root


def normal_item(text):
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def item_texts(document):
    return {int(item.get("id")):normal_item(text_item(item)) for item in document.findall("Items/Item")}


def preserve_extensions(source, output):
    # PoB does not own extensions unknown to this version. Retain them verbatim.
    for child in source:
        if output.find(child.tag) is None:
            output.append(copy.deepcopy(child))
    return output
