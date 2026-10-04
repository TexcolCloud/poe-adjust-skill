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


def strings(value, label, *, required=True):
    if isinstance(value,str): value=[value]
    bx.require(isinstance(value,list) and all(isinstance(v,str) and v.strip() for v in value),label+" 应为文字数组。")
    bx.require(not required or value,label+" 不能为空。")
    return value


def gem_ids(route):
    library=route.get("gems")
    bx.require(isinstance(library,dict) and library,"gems 使用本机 skill_id 到目录备注的映射；获取记录绑定阶段 required_items 的 instance_id。")
    bx.require(all(isinstance(k,str) and k and isinstance(v,dict) and set(v)<={"note"} for k,v in library.items()),"gems 仅保留 note；旧 acquisition/fallback 移到实际阶段物资及进入条件。")
    return sorted(library)


def link_queries(route):
    return sorted({",".join(g["skill_id"] for g in group.get("gems",[])) for stage in route.get("stages",[]) for group in stage.get("skill_groups",[]) if group.get("gems")})


def base_names(route):
    names=set()
    for stage in route.get("stages",[]):
        bases=stage.get("bases",{})
        bx.require(isinstance(bases,dict) and all(isinstance(v,str) and v for v in bases.values()),"bases 应为槽位到本机基底名称的映射。")
        names.update(bases.values())
    return sorted(names)


def resolve_gem_levels(stages,gems):
    for stage in stages:
        for group in stage.get("skill_groups",[]):
            for gem in group.get("gems",[]):
                native=gems.get(gem.get("skill_id"),{})
                rows=native.get("level_requirements",[])
                if gem.get("level")=="auto":
                    bx.require(type(stage.get("level")) is int,"auto 需要已知阶段角色等级。")
                    choices=[r["gem_level"] for r in rows if r.get("naturally_trainable") and type(r.get("character_level")) is int and r["character_level"]<=stage["level"]]
                    bx.require(choices,"该阶段没有本机确认的可练等级："+str(gem.get("skill_id")))
                    gem["level"]=max(choices)
                    gem["level_selection"]="explicit_auto_native_trainable"


def validate_group(group,stage,gems,library,compatibility,capacity,warnings,pending):
    bx.require(isinstance(group,dict) and set(group)<={"name","slot","use","enabled","gems"},"技能组使用 name/slot/use/enabled/gems；升级或替换写后续阶段。")
    for field in ("name","use"):
        bx.require(isinstance(group.get(field),str) and group[field].strip(),"技能组需要具体 "+field)
    bx.require(isinstance(group.get("slot"),str),"slot 应为槽位名，未装备用空字符串。")
    bx.require(type(group.get("enabled",True)) is bool,"enabled 应为布尔值；关闭计算仍占孔。")
    entries=group.get("gems")
    bx.require(isinstance(entries,list) and entries,"每个物理组需要宝石实例。")
    if capacity is not None and group["slot"]: bx.require(len(entries)<=capacity,"单组连线超过基底孔位容量："+group["slot"])
    for gem in entries:
        bx.require(set(gem)<={"instance_id","skill_id","level","quality","level_selection"},"宝石实例使用 instance_id/skill_id/level/quality。")
        bx.require(isinstance(gem.get("instance_id"),str) and gem["instance_id"],"宝石实例需要稳定 instance_id。")
        native=gems.get(gem.get("skill_id"))
        bx.require(native and gem["skill_id"] in library,"本机未识别宝石或顶层 gems 缺少该 ID。")
        bx.require(type(gem.get("level")) is int and gem["level"]>0 and type(gem.get("quality",0)) is int and gem.get("quality",0)>=0,"宝石等级/品质应为正整数/非负整数。")
        row=next((r for r in native["level_requirements"] if r["gem_level"]==gem["level"]),None)
        if row is None:
            pending.append("native:gem_level:"+gem["instance_id"])
        elif type(stage.get("level")) is int and type(row.get("character_level")) is int:
            bx.require(row["character_level"]<=stage["level"],gem["instance_id"]+" 使用等级高于该阶段角色等级。")
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


def validate(route,gems,data_version=None,classes=None,compatibility=None,nodes=None,*,bases=None,slots=None,evidence=None,claims=None):
    allowed={"schema_version","name","confirmed","goal","objectives","league","game_version","class_id","ascendancy","trade_mode","budget","version_source","scope","gems","stages","risks","notes","allow_minions","allow_mule","constraint_sources","evidence","claims","assessment","inventory","shopping"}
    bx.require(isinstance(route,dict) and set(route)<=allowed,"路线含旧字段或未知字段；按 leveling.md 的 schema_version=2 迁移。")
    bx.require(route.get("schema_version")==2 and route.get("confirmed") is True,"先确认路线方向，再使用 schema_version=2。")
    for key in ("name","goal","league","scope"):
        bx.require(isinstance(route.get(key),str) and route[key].strip(),"路线需要 "+key)
    engine.validate_game_version(route.get("game_version"),route["league"],data_version)
    selected=next((c for c in classes or [] if c["id"]==route.get("class_id")),None)
    bx.require(selected,"class_id 应来自当前 catalog classes。")
    asc=next((a for a in selected["ascendancies"] if a["name"]==route.get("ascendancy")),None)
    bx.require(not route.get("ascendancy") or asc,"升华不属于所选本机职业。")
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
    stage_ids=set(); prior_level=None; instances={}; acquired_instances=set(); previous_nodes=set(); warnings=[]; pending=[]
    native_slots={s["name"] for s in slots or []}
    coverage,coverage_pending=facts.objective_coverage(route.get("objectives",[]),evidence,claims)
    result["objective_coverage"]=coverage
    pending+=coverage_pending
    if not data_version: pending.append("version:data")
    resolve_gem_levels(stages,gems)
    asc_nodes,paths=engine.ascendancy_paths(route["ascendancy"],nodes or []) if route.get("ascendancy") else ({},{})
    for stage in stages:
        bx.require(set(stage)<={"stage_id","name","level","act","completed_act","entry_conditions","entry_checks","weapon_setup","bases","skill_groups","gear","passives","ascendancy_points","ascendancy_nodes","resources","checks","transition","required_items","key_transition","snapshot"},"阶段存在旧策略字段或未知字段。")
        sid=stage.get("stage_id")
        bx.require(isinstance(sid,str) and sid and sid not in stage_ids,"stage_id 需要唯一且稳定。")
        stage_ids.add(sid)
        bx.require(type(stage.get("key_transition",False)) is bool,"key_transition 应为布尔值，由模型依据玩法依赖判断。")
        if stage.get("key_transition") and not stage.get("snapshot"): pending.append("snapshot:"+sid)
        if "snapshot" in stage:
            snap=stage["snapshot"]
            bx.require(isinstance(snap,dict) and set(snap)<={"template_xml","operations","assumptions","usage","metrics"},"snapshot 使用 template_xml/operations/assumptions/usage/metrics。")
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
        usage={}; seen=set(); stage["socket_evidence"]={}
        for slot,name in stage.get("bases",{}).items():
            bx.require(slot in native_slots,"不是当前原生槽位："+slot)
            base=(bases or {}).get(name)
            bx.require(base,"未知本机基底："+name)
            if "valid_slots" not in base: pending.append("native:base_slot:"+name)
            else: bx.require(slot in base["valid_slots"],name+" 无法放入 "+slot)
        for group in groups:
            slot=group.get("slot")
            bx.require(slot=="" or slot in native_slots,"技能组槽位不是当前原生选项。")
            base=(bases or {}).get(stage.get("bases",{}).get(slot),{})
            capacity=base.get("sockets")
            if slot and type(capacity) is not int: pending.append("native:socket_capacity:"+sid+":"+slot)
            stage["socket_evidence"][slot]={"base":stage.get("bases",{}).get(slot),"capacity":capacity,"scope":"基底最大容量；实际孔色/连线/特殊装备效果需阶段 XML 实算。"}
            validate_group(group,stage,gems,library,compatibility,capacity,warnings,pending)
            usage[slot]=usage.get(slot,0)+len(group["gems"])
            for gem in group["gems"]:
                ref=gem["instance_id"]
                bx.require(ref not in seen,sid+" 同一颗宝石不能同时占两个位置："+ref)
                seen.add(ref)
                if ref in instances:
                    old=instances[ref]
                    bx.require(old["skill_id"]==gem["skill_id"],"更换宝石需要新的 instance_id："+ref)
                    if old["quality"]!=gem.get("quality",0) or gem["level"]<old["level"] or not gem.get("naturally_trainable") and gem["level"]!=old["level"]:
                        if not facts.has_claim(claims,"transformation:"+sid+":"+ref): pending.append("transformation:"+sid+":"+ref)
                else:
                    needs=[r for r in stage.get("required_items",[]) if r.get("instance_id")==ref and r.get("kind")=="gem"]
                    bx.require(len(needs)<=1,"同一实例获取记录重复："+ref)
                    if not needs: pending.append("acquisition:"+ref)
                    else:
                        need=needs[0]
                        bx.require(all(need.get(k)==gem.get(k,0) for k in ("skill_id","level","quality")) and need.get("quantity")==1,"实例获取的规格/数量不匹配："+ref)
                        if need.get("acquisition")=="trade" and route.get("trade_mode")=="ssf": raise ValueError("SSF 用户约束不允许交易取得："+ref)
                        if need.get("via_mule"):
                            bx.require(route.get("allow_mule") is not False,"用户不允许小号取物。")
                            if route.get("allow_mule") is None: pending.append("permission:allow_mule")
                instances[ref]={"skill_id":gem["skill_id"],"level":gem["level"],"quality":gem.get("quality",0)}
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
        for item in stage.get("required_items",[]):
            bx.require(isinstance(item,dict) and "status" not in item,"物资获取状态由程序生成。")
            facts.material_ref(item)
            bx.require(type(item.get("quantity")) is int and item["quantity"]>0,"阶段物资 quantity 为正整数。")
            if item.get("instance_id"):
                bx.require(item["instance_id"] not in acquired_instances,"同一物理实例只登记首次取得；后续沿用不重复采购："+item["instance_id"])
                acquired_instances.add(item["instance_id"])
            if item.get("acquisition")=="trade" and route.get("trade_mode")=="ssf": raise ValueError("SSF 路线不能依赖交易。")
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


def render(route,warnings,engine_version,tree_version):
    esc=lambda v:html.escape(str(v))
    bullets=lambda rows:"<ul>"+"".join("<li>"+esc(r)+"</li>" for r in rows)+"</ul>"
    sections=[]
    for stage in route["stages"]:
        groups=[]
        for group in stage["skill_groups"]:
            links=" — ".join(g["name"]+f" {g['level']}/{g.get('quality',0)} ["+g["instance_id"]+"]" for g in group["gems"])
            groups.append(f"<h3>{esc(group['name'])} · {esc(group['slot'] or '未装备')}</h3><p>{esc(group['use'])}：{esc(links)}</p>")
        sections.append(f"<section><h2>{esc(stage['name'])} [{esc(stage['stage_id'])}]</h2><p>等级 {esc(stage.get('level','未知'))} · 前往第 {esc(stage.get('act','未知'))} 章；已完成进度 {esc(stage.get('completed_act','未声明'))}</p><p>进入条件：{esc(', '.join(stage.get('entry_conditions',[])))}</p>{''.join(groups)}<p>所列宝石属性需求：{esc(stage['attribute_requirements'])}。实际装备效果见本阶段计算记录。</p>{bullets(stage['gear']+stage['passives']+stage['transition']+stage['checks'])}<p>{esc(stage.get('resources','资源使用安排尚未说明'))}</p><details><summary>本阶段物资与原生核对</summary><pre>{esc(json.dumps({'materials':stage.get('required_items',[]),'sockets':stage['socket_evidence'],'ascendancy_nodes':stage['ascendancy_allocated_nodes'],'calculation':stage.get('calculation',{'status':'not_calculated','scope':'未提供完整阶段快照，不报告精确效果'})},ensure_ascii=False,indent=2))}</pre></details></section>")
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(route['name'])}</title><style>body{{font:16px/1.7 system-ui;max-width:1050px;margin:auto;padding:28px;background:#f5f6f8;color:#202a38}}section{{background:white;margin:18px 0;padding:24px;border-radius:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>{esc(route['name'])}</h1><p>{esc(route.get('delivery_status','draft'))} · {esc(route['class_display_name'])} · {esc(route['ascendancy_display_name'])}</p><p>{esc(route['goal'])}</p><p>范围：{esc(route['scope'])}。这是一条有进入条件的路线；仅有 calculation 记录的阶段按所声明装备、技能、天赋与配置实算；其余阶段不报告精确效果。</p>{bullets(route['pending']+warnings)}{''.join(sections)}<section><h2>判断、来源与风险</h2>{bullets(route['risks']+route['notes'])}<pre>{esc(json.dumps({'claims':route['claims'],'evidence':route['evidence'],'facts':route.get('fact_evidence',{})},ensure_ascii=False,indent=2))}</pre></section><p>本机引擎 {esc(engine_version)}；天赋数据 {esc(tree_version)}。</p></html>'''
