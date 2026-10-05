"""Linear route plans grounded in current native catalogs and explicit evidence."""
from __future__ import annotations
import copy
import html
import json
import math
import buildxml as bx
import engine
import facts

ATTRIBUTES = {"strength":"力量", "dexterity":"敏捷", "intelligence":"智慧"}
ROUTE_FIELDS={"schema_version","name","confirmed","goal","objectives","league","game_version","class_id","ascendancy","trade_mode","budget","version_source","scope","gems","stages","risks","notes","allow_minions","allow_mule","constraint_sources","evidence","claims","assessment","inventory","shopping"}
STAGE_FIELDS={"stage_id","name","level","act","completed_act","entry_conditions","entry_checks","weapon_setup","bases","skill_groups","gear","passives","ascendancy_points","ascendancy_nodes","resources","checks","transition","required_items","key_transition","snapshot"}
GROUP_FIELDS={"name","slot","use","enabled","gems"}
GEM_FIELDS={"instance_id","skill_id","level","quality","level_selection"}
SNAPSHOT_FIELDS={"template_xml","operations","assumptions","usage","timeline","metrics"}
SNAPSHOT_OPERATION_FIELDS={"items","equip","tree","config","main_group","weapon_set","gems","groups"}


def preflight(route, *, complete=True):
    """Check shape before queries or web requests; incomplete drafts remain editable."""
    bx.require_keys(route,ROUTE_FIELDS,"/","路线含旧字段或未知字段。")
    bx.require(route.get("schema_version")==2,"路线使用 schema_version=2。",location="/schema_version",expected=2,actual=route.get("schema_version"))
    for field in ("evidence","claims","inventory","shopping","objectives","stages"):
        if field in route: bx.require(isinstance(route[field],list),field+" 应为数组。",location="/"+field,expected="array",actual=type(route[field]).__name__)
    facts.evidence_shape(route.get("evidence",[]),complete=complete)
    stages=route.get("stages",[])
    if complete:
        bx.require(route.get("confirmed") is True,"先确认路线方向。",location="/confirmed",expected=True,actual=route.get("confirmed"))
        bx.require(stages,"stages 至少一个阶段。",location="/stages",expected="nonempty array",actual=stages)
    seen=set()
    for index,stage in enumerate(stages):
        path=f"/stages/{index}"
        bx.require_keys(stage,STAGE_FIELDS,path,"阶段含未知字段。")
        sid=stage.get("stage_id")
        bx.require(isinstance(sid,str) and sid.strip() and "#" not in sid and sid not in seen,"stage_id 需要唯一、非空且不含引用分隔符 #。",location=path+"/stage_id",expected="unique stage identifier",actual=sid)
        seen.add(sid)
        if "bases" in stage: bx.require(isinstance(stage["bases"],dict) and all(v is None or isinstance(v,str) and v for v in stage["bases"].values()),"bases 为槽位到基底名称或明确空槽 null 的对象。",location=path+"/bases",expected="object of names or explicit null slots",actual=stage["bases"])
        for field in ("level","act","completed_act","ascendancy_points"):
            if stage.get(field) is not None: bx.require(type(stage[field]) is int and stage[field]>=(0 if field in {"completed_act","ascendancy_points"} else 1),field+" 使用相应非负/正整数。",location=path+"/"+field,expected="nonnegative integer" if field in {"completed_act","ascendancy_points"} else "positive integer",actual=stage[field])
        for field in ("skill_groups","required_items","ascendancy_nodes","entry_conditions","entry_checks"):
            if field in stage: bx.require(isinstance(stage[field],list),field+" 应为数组。",location=path+"/"+field,expected="array",actual=type(stage[field]).__name__)
        for group_index,group in enumerate(stage.get("skill_groups",[])):
            group_path=path+f"/skill_groups/{group_index}"
            bx.require_keys(group,GROUP_FIELDS,group_path)
            bx.require(isinstance(group.get("gems"),list),"技能组 gems 应为数组。",location=group_path+"/gems",expected="array",actual=type(group.get("gems")).__name__)
            for gem_index,gem in enumerate(group["gems"]): bx.require_keys(gem,GEM_FIELDS,group_path+f"/gems/{gem_index}")
        for item_index,item in enumerate(stage.get("required_items",[])):
            bx.require(isinstance(item,dict),"物资需要对象。",location=path+f"/required_items/{item_index}",expected="object",actual=type(item).__name__)
            facts.material_ref(item)
        if "snapshot" in stage:
            snap=stage["snapshot"]
            bx.require_keys(snap,SNAPSHOT_FIELDS,path+"/snapshot")
            if "operations" in snap:
                bx.require_keys(snap["operations"],SNAPSHOT_OPERATION_FIELDS,path+"/snapshot/operations","阶段等级和基础技能列表来自 stage；snapshot.operations 不能另设 level/replace_skills。")
                try: bx.plan_shape({"operations":snap["operations"]},complete=False)
                except bx.ContractError as exc:
                    exc.diagnostic["location"]=path+"/snapshot"+exc.diagnostic.get("location","")
                    raise
    return route


def strings(value, label, *, required=True):
    if isinstance(value,str): value=[value]
    bx.require(isinstance(value,list) and all(isinstance(v,str) and v.strip() for v in value),label+" 应为文字数组。")
    bx.require(not required or value,label+" 不能为空。")
    return value


def gem_ids(route):
    library=route.get("gems",{})
    bx.require(isinstance(library,dict),"gems 使用本机 skill_id 到目录备注的映射。",location="/gems",expected="object",actual=type(library).__name__)
    bx.require(all(isinstance(k,str) and k and isinstance(v,dict) and set(v)<={"note"} for k,v in library.items()),"gems 仅保留 note；旧 acquisition/fallback 移到实际阶段物资及进入条件。")
    return sorted(set(library)|{gem["skill_id"] for stage in route.get("stages",[]) for group in stage.get("skill_groups",[]) for gem in group.get("gems",[]) if isinstance(gem.get("skill_id"),str)})


def link_queries(route):
    return sorted({",".join(g["skill_id"] for g in group.get("gems",[])) for stage in route.get("stages",[]) for group in stage.get("skill_groups",[]) if group.get("gems")})


def base_names(route):
    names=set()
    for stage in route.get("stages",[]):
        bases=stage.get("bases",{})
        bx.require(isinstance(bases,dict) and all(v is None or isinstance(v,str) and v for v in bases.values()),"bases 应为槽位到本机基底名称或明确空槽 null 的映射。")
        names.update(v for v in bases.values() if v is not None)
    return sorted(names)


def resolve_gem_levels(stages,gems):
    for stage_index,stage in enumerate(stages):
        for group_index,group in enumerate(stage.get("skill_groups",[])):
            for gem_index,gem in enumerate(group.get("gems",[])):
                path=f"/stages/{stage_index}/skill_groups/{group_index}/gems/{gem_index}/level"
                native=gems.get(gem.get("skill_id"),{})
                rows=native.get("level_requirements",[])
                if gem.get("level")=="auto":
                    bx.require(type(stage.get("level")) is int,"auto 需要已知阶段角色等级。",location=f"/stages/{stage_index}/level",expected="positive integer",actual=stage.get("level"))
                    choices=[r["gem_level"] for r in rows if r.get("naturally_trainable") and type(r.get("character_level")) is int and r["character_level"]<=stage["level"]]
                    bx.require(choices,"该阶段没有本机确认的可练等级："+str(gem.get("skill_id")),location=path,expected="native trainable level at this character level",actual="auto")
                    gem["level"]=max(choices)
                    gem["level_selection"]="explicit_auto_native_trainable"


def validate_group(group,stage,gems,library,compatibility,capacity,warnings,pending,path):
    bx.require_keys(group,GROUP_FIELDS,path)
    for field in ("name","use"):
        bx.require(isinstance(group.get(field),str) and group[field].strip(),"技能组需要具体 "+field,location=path+"/"+field,expected="nonempty string",actual=group.get(field))
    bx.require(isinstance(group.get("slot"),str),"slot 应为槽位名，未装备用空字符串。",location=path+"/slot",expected="native slot name or empty string",actual=group.get("slot"))
    bx.require(type(group.get("enabled",True)) is bool,"enabled 应为布尔值；关闭计算仍占孔。",location=path+"/enabled",expected="boolean",actual=group.get("enabled"))
    entries=group.get("gems")
    bx.require(isinstance(entries,list) and entries,"每个物理组需要宝石实例。",location=path+"/gems",expected="nonempty gem array",actual=entries)
    if capacity is not None and group["slot"]: bx.require(len(entries)<=capacity,"单组连线超过基底孔位容量："+group["slot"])
    for gem_index,gem in enumerate(entries):
        gem_path=path+f"/gems/{gem_index}"
        bx.require_keys(gem,GEM_FIELDS,gem_path)
        bx.require(isinstance(gem.get("instance_id"),str) and gem["instance_id"],"宝石实例需要稳定 instance_id。",location=gem_path+"/instance_id",expected="nonempty instance_id",actual=gem.get("instance_id"))
        native=gems.get(gem.get("skill_id"))
        bx.require(native and gem["skill_id"] in library,"本机未识别宝石 ID。",location=gem_path+"/skill_id",expected="current native skill_id",actual=gem.get("skill_id"))
        for field,minimum in (("level",1),("quality",0)):
            value=gem.get(field,0 if field=="quality" else None)
            bx.require(type(value) is int and value>=minimum,"宝石等级/品质应为正整数/非负整数。",location=gem_path+"/"+field,expected={"type":"integer","minimum":minimum},actual=value)
        row=next((r for r in native["level_requirements"] if r["gem_level"]==gem["level"]),None)
        if row is None:
            pending.append("native:gem_level:"+gem["instance_id"])
        elif type(stage.get("level")) is int and type(row.get("character_level")) is int:
            bx.require(row["character_level"]<=stage["level"],gem["instance_id"]+" 使用等级高于该阶段角色等级。",location=gem_path+"/level",expected={"maximum_character_level":stage["level"]},actual={"gem_level":gem["level"],"required_character_level":row["character_level"]})
        else: pending.append("level_requirement:"+gem["instance_id"])
        gem["name"]=native.get("display_name",native["name"])
        gem["attribute_requirements"]={k:(row or {}).get(k) for k in ATTRIBUTES}
        gem["base_resources"]=copy.deepcopy((row or {}).get("base_resources",{}))
        gem["naturally_trainable"]=(row or {}).get("naturally_trainable")
    checked=(compatibility or {}).get(",".join(g["skill_id"] for g in entries))
    if not checked: pending.append("native:links:"+stage["stage_id"]+":"+group["name"])
    else:
        bx.require(not checked.get("errors"),"原生连线查询失败："+"；".join(checked.get("errors",[])))
        group["relations"]=checked.get("relations",[])
        if checked.get("incompatible"): warnings.append(stage["stage_id"]+" / "+group["name"]+" 中有未作用于主动的辅助；核对是否用于练级/后续阶段，不计辅助收益："+",".join(checked["incompatible"]))


def validate(route,gems,data_version=None,classes=None,compatibility=None,nodes=None,*,bases=None,slots=None,equipment=None,evidence=None,claims=None):
    preflight(route)
    bx.require(route.get("schema_version")==2 and route.get("confirmed") is True,"先确认路线方向，再使用 schema_version=2。")
    for key in ("name","goal","league","scope"):
        bx.require(isinstance(route.get(key),str) and route[key].strip(),"路线需要 "+key)
    engine.validate_game_version(route.get("game_version"),route["league"],data_version)
    selected=next((c for c in classes or [] if c["id"]==route.get("class_id")),None)
    bx.require(selected,"class_id 应来自当前 catalog classes。",location="/class_id",expected=[c["id"] for c in classes or []],actual=route.get("class_id"))
    asc=next((a for a in selected["ascendancies"] if a["name"]==route.get("ascendancy")),None)
    bx.require(not route.get("ascendancy") or asc,"升华不属于所选本机职业。",location="/ascendancy",expected=[a["name"] for a in selected["ascendancies"]],actual=route.get("ascendancy"))
    if route.get("trade_mode") is not None: bx.require(route["trade_mode"] in {"ssf","trade"},"trade_mode 使用 ssf/trade 或暂不填写。")
    for flag in ("allow_minions","allow_mule"):
        if flag in route: bx.require(type(route[flag]) is bool,flag+" 应为用户明确表达的布尔值。")
    if route.get("budget") is not None:
        budget=route["budget"]
        bx.require(isinstance(budget,dict) and set(budget)=={"amount","currency"} and type(budget["amount"]) in (int,float) and math.isfinite(budget["amount"]) and budget["amount"]>=0 and isinstance(budget["currency"],str) and budget["currency"],"路线 budget 需要有限非负 amount 与 currency。")
    evidence,claims=evidence or {},claims or []
    source_map=route.get("constraint_sources",{})
    bx.require(isinstance(source_map,dict),"constraint_sources 应为字段路径映射。")
    for key in ("goal","trade_mode","budget","allow_minions","allow_mule"):
        if key not in route: continue
        source=evidence.get(source_map.get("/"+key),{})
        bx.require(source.get("kind")=="user_statement" and source.get("status")=="user_declared","用户约束需要对应表达依据：/"+key)
    result=copy.deepcopy(route)
    result["class_display_name"]=selected.get("display_name",selected["name"])
    result["ascendancy_display_name"]=(asc or {}).get("display_name",(asc or {}).get("name","未选"))
    library=gem_ids(result)
    stages=result.get("stages")
    bx.require(isinstance(stages,list) and stages,"stages 至少一个阶段；同级切换可分阶段。")
    stage_ids=set(); prior_level=None; instances={}; acquisitions={}; previous_nodes=set(); warnings=[]; pending=[]
    native_slots={s["name"] for s in slots or []}
    coverage,coverage_pending=facts.objective_coverage(route.get("objectives",[]),evidence,claims)
    result["objective_coverage"]=coverage
    pending+=coverage_pending
    if not data_version: pending.append("version:data")
    resolve_gem_levels(stages,gems)
    asc_nodes,paths=engine.ascendancy_paths(route["ascendancy"],nodes or []) if route.get("ascendancy") else ({},{})
    for stage_index,stage in enumerate(stages):
        stage_path=f"/stages/{stage_index}"
        bx.require_keys(stage,STAGE_FIELDS,stage_path)
        sid=stage.get("stage_id")
        bx.require(isinstance(sid,str) and sid and sid not in stage_ids,"stage_id 需要唯一且稳定。")
        stage_ids.add(sid)
        bx.require(type(stage.get("key_transition",False)) is bool,"key_transition 应为布尔值，由模型依据玩法依赖判断。")
        if stage.get("key_transition") and not stage.get("snapshot"): pending.append("snapshot:"+sid)
        if "snapshot" in stage:
            snap=stage["snapshot"]
            bx.require_keys(snap,SNAPSHOT_FIELDS,stage_path+"/snapshot")
            strings(snap.get("assumptions",[]),sid+" snapshot.assumptions",required=False)
            valid={c["claim_id"] for c in claims if c["status"]=="model_assessed" and "snapshot:"+sid in c["subjects"]}
            if not snap.get("assumptions") or not set(snap["assumptions"])<=valid: pending.append("snapshot_assumptions:"+sid)
        bx.require(isinstance(stage.get("name"),str) and stage["name"],sid+" 需要名称。")
        for key in ("level","act","completed_act","ascendancy_points"):
            if stage.get(key) is not None: bx.require(type(stage[key]) is int and stage[key]>=(0 if key in {"completed_act","ascendancy_points"} else 1),sid+" 的 "+key+" 无效。")
        if stage.get("level") is not None:
            bx.require(prior_level is None or stage["level"]>=prior_level,"执行顺序中的角色等级不能倒退；同级阶段允许。")
            prior_level=stage["level"]
        else: pending.append("level:"+sid)
        conditions=strings(stage.get("entry_conditions",[]),sid+" entry_conditions",required=False)
        for key in conditions:
            if not any(c["claim_id"]==key and c["status"]=="model_assessed" and "stage:"+sid in c["subjects"] for c in claims): pending.append("entry_condition:"+sid+":"+key)
        if stage.get("entry_checks") and not stage.get("snapshot"):
            _, gaps = facts.entry_checks(stage["entry_checks"], {}, claims, "stage:"+sid)
            pending += gaps
        for field in ("gear","passives","checks","transition"):
            stage[field]=strings(stage.get(field,[]),sid+" "+field,required=False)
        if not facts.has_claim(claims,"stage:"+sid): pending.append("stage_assessment:"+sid)
        groups=stage.get("skill_groups")
        bx.require(isinstance(groups,list) and groups,sid+" 需要实际技能组。")
        for item_index,item in enumerate(stage.get("required_items",[])):
            item_path=stage_path+f"/required_items/{item_index}"
            bx.require("status" not in item,"物资获取状态由程序生成。",location=item_path+"/status",expected="absent",actual=item.get("status"))
            bx.require(type(item.get("quantity")) is int and item["quantity"]>0,"阶段物资 quantity 为正整数。",location=item_path+"/quantity",expected="positive integer",actual=item.get("quantity"))
            ref=item.get("instance_id")
            if ref:
                bx.require(isinstance(ref,str) and ref not in acquisitions,"同一物理实例只登记首次取得；后续沿用不重复采购："+str(ref),location=item_path+"/instance_id",expected="new instance_id",actual=ref)
                if item["kind"]=="gem": bx.require(item["quantity"]==1,"一个宝石实例数量必须为1；另购使用另一 instance_id。",location=item_path+"/quantity",expected=1,actual=item["quantity"])
                acquisitions[ref]={**item,"stage_id":sid,"source_location":item_path}
            if item.get("acquisition")=="trade": bx.require(route.get("trade_mode")!="ssf","SSF 路线不能依赖交易。",location=item_path+"/acquisition",expected="non-trade acquisition",actual="trade")
            if item.get("via_mule"):
                bx.require(route.get("allow_mule") is not False,"用户不允许小号取物。",location=item_path+"/via_mule",expected=False,actual=True)
                if route.get("allow_mule") is None: pending.append("permission:allow_mule")
        usage={}; seen=set(); stage["socket_evidence"]={}
        layout=(equipment or {}).get(sid,{})
        stage["equipment_validation"]=copy.deepcopy(layout) or {"status":"unknown","scope":"尚无这一阶段实际布局的原生校验；目录 valid_slots 只属于其查询上下文。"}
        for slot,name in stage.get("bases",{}).items():
            base_path=stage_path+"/bases/"+slot.replace("~","~0").replace("/","~1")
            bx.require(slot in native_slots,"不是当前原生槽位："+slot,location=base_path,expected=sorted(native_slots),actual=slot)
            base=(bases or {}).get(name)
            if name is not None: bx.require(base,"未知本机基底："+name,location=base_path,expected="current native base name",actual=name)
            check=layout.get("checks",{}).get(slot,{}) if layout.get("bases")==stage.get("bases",{}) else {}
            if check.get("base")!=name or type(check.get("valid_for_slot")) is not bool:
                pending.append("native:equipment:"+sid+":"+slot)
            else:
                bx.require(check["valid_for_slot"],str(name)+" 与当前布局的 "+slot+" 不相符。",location=base_path,expected="native-compatible item or declared empty slot in this equipment/tree context",actual={"base":name,"context":layout.get("context",{})})
        stage["instance_transitions"]=[]
        for group_index,group in enumerate(groups):
            slot=group.get("slot")
            bx.require(slot=="" or slot in native_slots,"技能组槽位不是当前原生选项。")
            bx.require(slot not in stage.get("bases",{}) or stage["bases"][slot] is not None,"明确空槽不能承载技能组。",location=stage_path+f"/skill_groups/{group_index}/slot",expected="occupied or unspecified slot",actual=slot)
            base=(bases or {}).get(stage.get("bases",{}).get(slot),{})
            capacity=base.get("sockets")
            if slot and type(capacity) is not int: pending.append("native:socket_capacity:"+sid+":"+slot)
            stage["socket_evidence"][slot]={"base":stage.get("bases",{}).get(slot),"capacity":capacity,"scope":"基底最大容量；实际孔色/连线/特殊装备效果需阶段 XML 实算。"}
            group_path=stage_path+f"/skill_groups/{group_index}"
            validate_group(group,stage,gems,library,compatibility,capacity,warnings,pending,group_path)
            usage[slot]=usage.get(slot,0)+len(group["gems"])
            for gem_index,gem in enumerate(group["gems"]):
                gem_path=group_path+f"/gems/{gem_index}"
                ref=gem["instance_id"]
                bx.require(ref not in seen,sid+" 同一颗宝石不能同时占两个位置："+ref,location=gem_path+"/instance_id",expected="one occupied position per instance",actual=ref)
                seen.add(ref)
                old=instances.get(ref) or acquisitions.get(ref)
                current={"skill_id":gem["skill_id"],"level":gem["level"],"quality":gem.get("quality",0),"stage_id":sid}
                transition={"instance_id":ref,"use_location":gem_path,"used":current,"acquired":acquisitions.get(ref),"previous_use":instances.get(ref)}
                if old is None:
                    pending.append("acquisition:"+ref); transition["status"]="acquisition_unknown"
                else:
                    bx.require(old.get("skill_id")==gem["skill_id"],"实例获取的规格不匹配；更换宝石需要新的 instance_id："+ref,
                               location=gem_path+"/skill_id",expected=old.get("skill_id"),actual=gem["skill_id"])
                    changed=old["level"]!=gem["level"] or old.get("quality",0)!=gem.get("quality",0)
                    if changed:
                        natural=old["level"]<gem["level"] and old.get("quality",0)==gem.get("quality",0) and gem.get("naturally_trainable") is True
                        subject=("training:" if natural else "transformation:")+sid+":"+ref
                        transition.update(kind="natural_training" if natural else "transformation",required_subject=subject,
                                          status="model_assessed" if facts.has_claim(claims,subject) else "unresolved",
                                          scope="本机可练等级只证明要求相容；实际取得时点、经验/加工及进入条件由对应判断说明，不证明已发生。")
                        if not facts.has_claim(claims,subject): pending.append(subject)
                    else: transition.update(kind="unchanged",status="spec_matches")
                stage["instance_transitions"].append(transition)
                instances[ref]=current
        for slot,count in usage.items():
            capacity=stage["socket_evidence"][slot]["capacity"]
            if slot and type(capacity) is int: bx.require(count<=capacity,sid+" 在 "+slot+" 的物理占孔超过容量。")
        stage["attribute_requirements"]={key:max(values) if values and all(v is not None for v in values) else None for key in ATTRIBUTES for values in [[g["attribute_requirements"][key] for group in groups for g in group["gems"] if group["slot"]]]}
        allocated=stage.get("ascendancy_nodes",[])
        bx.require(isinstance(allocated,list) and all(type(n) is int for n in allocated) and len(set(allocated))==len(allocated),"ascendancy_nodes 使用本机节点 ID 数组。")
        actual=set()
        for node in allocated:
            bx.require(node in asc_nodes and node in paths,"节点不属于所选升华或没有连接路径："+str(node))
            actual.update(paths[node])
        points=stage.get("ascendancy_points")
        if points is not None: bx.require(len(actual)<=points,sid+" 的升华连接点超过该阶段声明的可用点数。")
        if actual and not facts.has_claim(claims,"progress:"+sid): pending.append("progress:"+sid)
        if previous_nodes-actual and not facts.has_claim(claims,"respec:"+sid): pending.append("respec:"+sid)
        previous_nodes=actual
        stage["ascendancy_used"]=len(actual)
        stage["ascendancy_allocated_nodes"]=sorted(actual)
    if not facts.has_claim(claims,"goal"): pending.append("goal_assessment")
    for key in ("allow_minions",):
        if key in route and not facts.has_claim(claims,"preference:"+key): pending.append("preference:"+key)
    assessment=route.get("assessment",{})
    bx.require(isinstance(assessment,dict) and set(assessment)<={"goal_claim_ids","scope","unresolved","review"},"路线 assessment 使用 goal_claim_ids/scope/unresolved/review。")
    for key in ("goal_claim_ids","unresolved"):
        bx.require(isinstance(assessment.get(key,[]),list) and all(isinstance(v,str) and v for v in assessment.get(key,[])),"assessment."+key+" 需要字符串数组。")
    goal_ids={c["claim_id"] for c in claims if c["status"]=="model_assessed" and "goal" in c["subjects"]}
    if not assessment.get("scope") or not assessment.get("goal_claim_ids") or not set(assessment.get("goal_claim_ids",[]))<=goal_ids: pending.append("goal_assessment")
    pending += ["assessment:"+v for v in assessment.get("unresolved",[])]
    result["pending"]=list(dict.fromkeys(pending))
    result["claims"]=claims
    result["evidence"]=evidence
    result["risks"]=strings(result.get("risks",[]),"risks",required=False)
    result["notes"]=strings(result.get("notes",[]),"notes",required=False)
    return result,list(dict.fromkeys(warnings))


def render(route,warnings,engine_version,tree_version,*,evidence_file=None):
    esc=lambda v:html.escape(str(v))
    bullets=lambda rows:"<ul>"+"".join("<li>"+esc(r)+"</li>" for r in rows)+"</ul>"
    claims={c["claim_id"]:c for c in route["claims"]}
    def judgment(key):
        claim=claims.get(key,{})
        return claim.get("rendered_text",claim.get("text",key))+" ["+claim.get("status","未找到判断")+"]"
    brief=route.get("delivery_brief") or facts.delivery_brief(route.get("delivery_status","draft"),route.get("errors",[]),route["pending"])
    labels={"blocked":"存在已知错误，不能按本快照执行","draft":"条件草案，依赖仍待核实","calculated":"原生计算完成，是否可执行仍看条件与评审",
            "unsupported":"当前接口不支持本次测量","failed":"本次原生计算失败","not_calculated":"未提供完整阶段快照，不报告精确效果"}
    sections=[]
    for stage in route["stages"]:
        calculation=stage.get("calculation",{})
        status=calculation.get("status","not_calculated")
        groups=[]
        for group in stage["skill_groups"]:
            links=" — ".join(g["name"]+f" {g['level']}/{g.get('quality',0)} ["+g["instance_id"]+"]" for g in group["gems"])
            groups.append(f"<h3>{esc(group['name'])} · {esc(group['slot'] or '未装备')}</h3><p>{esc(group['use'])}：{esc(links)}</p>")
        conditions=bullets([judgment(key) for key in stage["entry_conditions"]]) if stage.get("entry_conditions") else "<p>尚未评审进入条件。</p>"
        stats="".join(f"<tr><td>{esc(key)}</td><td>{esc(format(value,'.10g') if type(value) in (int,float) else value if value is not None else '未知')}</td></tr>" for key,value in calculation.get("stats",{}).items())
        numbers=f"<h3>当前条件下的原生数值</h3><table>{stats}</table><p>这些是实际装入对象的计算值。被忽略的装备/节点或未满足的需求见已知错误，不能把当前值当所声明完整配装已经生效。</p>" if stats else ""
        material_rows=[f"{facts.material_ref(item)} ×{item['quantity']}：{item.get('acquisition','取得方式未定')} · {item.get('evidence_status','未核对')}" for item in stage.get("required_items",[])]
        details={"sockets":stage["socket_evidence"],"equipment_validation":stage.get("equipment_validation",{}),"ascendancy_nodes":stage["ascendancy_allocated_nodes"],
                 "instance_transitions":stage.get("instance_transitions",[]),
                 **{k:v for k,v in calculation.items() if k in {"xml_path","config_values","configuration_context","readiness","material_specs","entry_checks","diagnostic"}}}
        sections.append(f"<section><h2>{esc(stage['name'])} [{esc(stage['stage_id'])}]</h2><p><b>阶段校验：{esc(labels.get(status,status))}</b></p><p>等级 {esc(stage.get('level','未知'))} · 前往第 {esc(stage.get('act','未知'))} 章；已完成进度 {esc(stage.get('completed_act','未声明'))}</p><h3>已知错误</h3>{bullets(calculation.get('errors',[]))}<p>{esc(calculation.get('reason',''))}</p><h3>进入条件</h3>{conditions}{''.join(groups)}<p>所列宝石属性需求：{esc(stage['attribute_requirements'])}。实际装备效果见本阶段计算记录。</p>{numbers}{bullets(stage['gear']+stage['passives']+stage['transition']+stage['checks'])}<p>模型资源安排（未核实部分见缺口）：{esc(stage.get('resources','资源使用安排尚未说明'))}</p><h3>新增物资</h3>{bullets(material_rows)}<details><summary>本阶段待核实项与原生条件</summary>{bullets(calculation.get('pending',[]))}<pre>{esc(json.dumps(details,ensure_ascii=False,indent=2))}</pre></details></section>")
    objectives=bullets([row["text"]+" ["+row["status"]+"]："+"；".join(judgment(key) for key in row["claim_ids"]) for row in route.get("objective_coverage",[])])
    conclusions=bullets([judgment(key) for key in claims])
    source=f'<p>完整数值、资料原文与引用索引：<a href="{esc(evidence_file)}">核对记录</a></p>' if evidence_file else ""
    published=route.get("publication",{})
    current_facts=bullets([s["text"] for s in published.get("statements",[])]+[m["text"] for m in published.get("measurements",[])])
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(route['name'])}</title><style>body{{font:16px/1.7 system-ui;max-width:1050px;margin:auto;padding:28px;background:#f5f6f8;color:#202a38}}section{{background:white;margin:18px 0;padding:24px;border-radius:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}td{{padding:6px;border-bottom:1px solid #ddd}}</style><h1>{esc(route['name'])}</h1><p>{esc(brief['message'])} · {esc(route['class_display_name'])} · {esc(route['ascendancy_display_name'])}</p><section><h2>当前核验结论</h2>{current_facts}</section><p>{esc(route['goal'])}</p>{objectives}<p>范围：{esc(route['scope'])}。这是一条有进入条件的路线；仅有 calculation 记录的阶段按实际装入的装备、技能、天赋与配置实算；其余阶段不报告精确效果。计算完成不代表输入合法、物资取得或阶段就绪。</p><h2>已知错误</h2>{bullets(route.get('errors',[]))}<details><summary>全部待核实项与警告</summary>{bullets(route['pending']+warnings)}</details>{''.join(sections)}<section><h2>判断、来源与风险</h2>{bullets(route['risks']+route['notes'])}{conclusions}{source}</section><p>本机引擎 {esc(engine_version)}；天赋数据 {esc(tree_version)}。</p></html>'''
