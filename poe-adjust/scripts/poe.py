"""TeleAgent-facing POE build workflow. Run --help; stdout is always JSON."""
from __future__ import annotations

import argparse
import copy
import csv
import errno
from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import buildxml as bx
import engine
import facts
import routes
import resources

VERSION = "2.8.13"
SCHEMA_VERSION = 2
JSON_LIMITS={"input":8*1024*1024,"artifact":64*1024*1024}
INTENT_FIELDS={"schema_version", "confirmed", "goal", "objectives", "league", "game_version", "version_source", "version_checked_at", "mode", "budget", "target_level", "allow_class_change", "allow_ascendancy_change", "allow_main_skill_change", "keep_slots", "keep_skill_ids", "scenario", "baseline_main_group", "minimum_stats", "quest_passive_points", "ascendancy_points", "secondary_ascendancy_points", "normal_points_available", "max_changed_equipment", "constraint_sources", "evidence", "notes"}
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


def transient_io(error):
    return getattr(error,"errno",None) in {errno.EAGAIN,errno.EBUSY,errno.EINTR,errno.ETIMEDOUT} or getattr(error,"winerror",None) in {32,33}


def execution_entrypoint():
    return {"script":str(Path(__file__).resolve()),"python":str(Path(sys.executable).resolve()),"skill_version":VERSION}


def input_contract(kind):
    fields={"intent":INTENT_FIELDS,"plan":bx.PLAN_FIELDS,"route":routes.ROUTE_FIELDS}
    return {"kind":kind,"top_level_fields":sorted(fields[kind]),
            "field_owners":{key:[owner for owner,allowed in fields.items() if key in allowed] for key in sorted(set().union(*fields.values()))},
            "scope":"来自当前验证器的顶层字段归属；嵌套格式与语义仍按对应协议。配装目标修改 intent，操作/判断修改 plan。"}


def read_json(path, *, role="artifact"):
    path = Path(path)
    bx.require(path.is_file(),f"JSON 文件不存在：{path}",location="/file",expected="existing JSON file",actual=str(path),input_file=str(path))
    bx.require(path.stat().st_size<=JSON_LIMITS[role],f"JSON 超过 {role} 读取上限。",code="input_too_large" if role=="input" else "artifact_too_large",location="/file",expected={"maximum_bytes":JSON_LIMITS[role],"role":role},actual=path.stat().st_size,input_file=str(path))
    def finite_float(text):
        value=float(text)
        bx.require(math.isfinite(value),"JSON 不接受非有限数字。",location="/",expected="finite number",actual=text)
        return value
    def unique_object(pairs):
        result={}
        for key,value in pairs:
            bx.require(key not in result,"JSON 对象不能重复同一字段。",location="/",expected="unique field",actual=key)
            result[key]=value
        return result
    value = json.loads(path.read_text(encoding="utf-8-sig"),parse_float=finite_float,object_pairs_hook=unique_object,parse_constant=lambda number: bx.require(False,"JSON 不接受非有限数字。",location="/",expected="finite number",actual=number))
    bx.require(isinstance(value, dict), "JSON 顶层应为对象。",location="/",expected="object",actual=type(value).__name__,input_file=str(path))
    return value


def json_text(value, *, role="artifact"):
    text=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    size=len(text.encode("utf-8"))
    bx.require(size<=JSON_LIMITS[role],f"编码后的 JSON 超过 {role} 上限；本次未提交。",code="input_too_large" if role=="input" else "artifact_too_large",location="/",expected={"maximum_bytes":JSON_LIMITS[role],"role":role},actual=size)
    return text


def write_text(path, text):
    path = Path(path)
    engine.check_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Distinct sibling temporaries keep concurrent writes from sharing a .tmp.
    # A failed replace leaves the previous complete file, and no partial output.
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",newline="\n",prefix=path.name+".",suffix=".tmp",dir=path.parent,delete=False) as stream:
            temporary=Path(stream.name);stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path,json_text(value))


def check_path_roles(reads, writes):
    """Resolve role aliases before a transaction writes its first artifact."""
    resolved=[(role,Path(path).resolve()) for role,path in reads if path is not None]
    produced=[]
    for role,value in writes:
        path=Path(value).resolve()
        engine.check_output_path(path)
        for other,target in resolved+produced:
            same=path==target or path.is_file() and target.is_file() and path.samefile(target)
            bx.require(not same,"输出与读取文件或其他输出角色指向同一文件。",code="path_conflict",location="/out",expected={"distinct_roles":[other,role]},actual=str(path))
        produced.append((role,path))


def document_reads(document, folder):
    reads=[]
    objects=[document]+[s.get("snapshot",{}) for s in document.get("stages",[]) if isinstance(s,dict)]
    for index,obj in enumerate(objects):
        if isinstance(obj,dict) and isinstance(obj.get("template_xml"),str): reads.append((f"template[{index}]",folder/obj["template_xml"]))
    for index,row in enumerate(document.get("evidence",[])):
        if isinstance(row,dict) and row.get("kind")=="artifact" and isinstance(row.get("path"),str): reads.append((f"evidence[{index}]",folder/row["path"]))
    return reads


def stage_directory(output, stage_id):
    directory=output.parent/(output.stem+".calculations")/bx.digest(stage_id)[:12]
    bx.require(directory.resolve().is_relative_to(output.parent.resolve()),'阶段目录不能通过链接越出交付目录。',code='path_conflict',location='/out',expected=str(output.parent.resolve()),actual=str(directory.resolve()))
    return directory


def route_outputs(output, route):
    writes=[("route_html",output)]+[("route_"+key,output.with_suffix("."+key+".json")) for key in ("state","evidence","review")]
    for stage in route.get("stages",[]):
        if isinstance(stage,dict) and "snapshot" in stage and isinstance(stage.get("stage_id"),str):
            directory=stage_directory(output,stage["stage_id"])
            writes.extend(("stage_"+stage["stage_id"]+"_"+name,directory/name) for name in ("snapshot.xml","snapshot.json","evaluation.json"))
    return writes


EDIT_COLLECTIONS={"stages":("stage_id",),"claims":("claim_id",),"evidence":("evidence_id",),"objectives":("objective_id",)}
EDIT_OPERATIONS={"items":("id",),"equip":("slot",),"groups":("index",),"gems":("group","index")}


def merge_edit(current, changes, path=""):
    """Merge named records; changing an existing ordinary array needs replace."""
    if not isinstance(changes,dict): return copy.deepcopy(changes)
    bx.require(current is None or isinstance(current,dict),"对象更新不能隐式替换数组/标量。",location=path or "/",expected="object",actual=type(current).__name__)
    result=copy.deepcopy(current or {})
    for key,value in changes.items():
        location=path+"/"+key
        fields=EDIT_COLLECTIONS.get(key) if not path else EDIT_OPERATIONS.get(key) if path=="/operations" or path.endswith("/snapshot/operations") else None
        if fields and isinstance(value,list):
            prior=result.get(key,[])
            bx.require(isinstance(prior,list),"记录集合需要数组。",location=location,expected="array",actual=type(prior).__name__)
            merged=copy.deepcopy(prior); seen=set()
            for index,entry in enumerate(value):
                bx.require(isinstance(entry,dict),"更新条目需要对象。",location=location+f"/{index}",expected="object",actual=type(entry).__name__)
                entry=copy.deepcopy(entry)
                if len(fields)==1 and fields[0].endswith("_id") and fields[0] not in entry:
                    entry[fields[0]]=key[:-1]+"-"+bx.digest(json.dumps(entry,sort_keys=True,ensure_ascii=False,allow_nan=False))[:12]
                identifier=tuple(entry.get(field) for field in fields)
                bx.require(all(type(v) in (int,str) and v!="" for v in identifier),"更新条目需要稳定标识。",location=location+f"/{index}",expected=list(fields),actual=entry)
                bx.require(identifier not in seen,"同次更新出现重复标识。",location=location+f"/{index}",expected="unique selector",actual=list(identifier));seen.add(identifier)
                matches=[i for i,row in enumerate(merged) if tuple(row.get(field) for field in fields)==identifier]
                bx.require(len(matches)<=1,"源记录有重复标识，先修订源文件。",location=location,expected="unique selector",actual=list(identifier))
                if matches: merged[matches[0]]=merge_edit(merged[matches[0]],entry,location+f"/{matches[0]}")
                else: merged.append(entry)
            result[key]=merged
        elif isinstance(value,dict): result[key]=merge_edit(result.get(key),value,location)
        else:
            if isinstance(result.get(key),list) and result[key] and document_changes(result[key],value):
                bx.require(False,"已有数组整体修改需在 replace 中显式指定路径；单字段也可用 replace 精确定位。",code="explicit_replacement_required",location=location,expected="replace JSON pointer",actual=type(value).__name__)
            result[key]=copy.deepcopy(value)
    return result


def document_changes(before, after, pointer=""):
    """Actual semantic delta, retained in the receipt rather than recounted in prose."""
    if isinstance(before,dict) and isinstance(after,dict):
        rows=[]
        for key in sorted(set(before)|set(after)):
            path=pointer+"/"+key.replace("~","~0").replace("/","~1")
            if key not in before: rows.append({"pointer":path,"after":after[key]})
            elif key not in after: rows.append({"pointer":path,"before":before[key]})
            else: rows+=document_changes(before[key],after[key],path)
        return rows
    if isinstance(before,list) and isinstance(after,list) and len(before)==len(after):
        return [row for index,(old,new) in enumerate(zip(before,after)) for row in document_changes(old,new,pointer+"/"+str(index))]
    if type(before) is type(after) and before==after: return []
    return [{"pointer":pointer,"before":before,"after":after}]


def document_counts(document):
    counts={key:len(document.get(key,[])) for key in EDIT_COLLECTIONS}
    groups=[g for s in document.get("stages",[]) for g in s.get("skill_groups",[])]
    counts.update(skill_groups=len(groups),gems=sum(len(g.get("gems",[])) for g in groups))
    return counts


def input_artifacts_current(outcome):
    """Check native input templates recorded by the role or stage calculation."""
    stages=outcome.get("stages",[])
    if isinstance(stages,dict): stages=stages.values()
    groups=[outcome.get("input_artifacts",{})]+[stage.get("calculation",{}).get("input_artifacts",{}) for stage in stages]
    return all(Path(filename).is_file() and bx.digest(Path(filename).read_bytes())==digest for group in groups for filename,digest in group.items())


def proposal_request(args, root, source):
    """Read a saved native proposal through the existing calculation receipts."""
    path=Path(args.proposal_file).resolve()
    bx.require(isinstance(getattr(args,"proposal_id",None),str) and args.proposal_id,"采用提案需提供 next_actions 的 proposal_id。",location="/proposal_id")
    saved=read_json(path); raw_hash=bx.digest(path.read_bytes())
    if args.kind=="plan":
        bx.require(path.name=="evaluation.json" and path.parent.parent.name=="candidates","使用 evaluate 返回的 evaluation_file。",location="/proposal_file")
        _,state=read_run(path.parent.parent.parent)
        bx.require(state.get("candidates",{}).get(path.parent.name,{}).get("evaluation_sha256")==raw_hash,"计算凭证已变化或不完整，请重新 evaluate。",code="proposal_receipt_stale",location="/proposal_file")
        outcome=saved; input_file=saved.get("source_plan"); revision=saved.get("source_plan_sha256")
        bx.require(saved.get("intent_hash")==state.get("intent_hash") and saved.get("original_sha256")==state.get("original_sha256"),"意图或原角色变化，请重新计算提案。",code="proposal_context_stale",location="/proposal_file")
    else:
        bx.require(path.name.endswith(".evidence.json"),"使用 route 返回的 .evidence.json。",location="/proposal_file")
        state=read_json(path.with_name(path.name.removesuffix(".evidence.json")+".state.json"))
        bx.require(state.get("file_exported") is True and state.get("files",{}).get(str(path))==raw_hash,"路线计算凭证已变化或不完整，请重新 route。",code="proposal_receipt_stale",location="/proposal_file")
        outcome=saved.get("checked_route",{});input_file=state.get("input");revision=state.get("input_sha256")
    bx.require(saved.get("skill_version")==VERSION and saved.get("engine_fingerprint")==state.get("engine_fingerprint")==engine.fingerprint(root),"技能或引擎变化，请重新计算提案。",code="proposal_context_stale",location="/proposal_file")
    bx.require(input_artifacts_current(outcome),"外部角色模板变化，请重新计算提案。",code="proposal_context_stale",location="/proposal_file")
    candidates=[row[key] for row in outcome.get("diagnostics",[]) for key in ("connection_proposal","change_proposal") if isinstance(row.get(key),dict) and row[key].get("proposal_id")==getattr(args,"proposal_id",None)]
    bx.require(len(candidates)==1,"proposal_id 必须对应当前计算中唯一的程序提案。",location="/proposal_id",expected="one current saved proposal",actual=getattr(args,"proposal_id",None))
    proposal=candidates[0]
    body={k:v for k,v in proposal.items() if k!="proposal_id"}
    bx.require(bx.digest(json.dumps(body,sort_keys=True,ensure_ascii=False,allow_nan=False))==proposal["proposal_id"],"提案内容被修改，请重新计算。",location="/proposal_id")
    bx.require(input_file is not None and Path(input_file).resolve()==source and proposal.get("input_file")==str(source) and proposal.get("input_revision")==revision and proposal.get("details_file")==str(path) and proposal.get("kind")==args.kind,
               "提案不属于本源方案和本次计算。",code="proposal_context_stale",location="/file",actual=str(source))
    bx.require(proposal.get("adoptable") is True,"提案存在已知冲突；先修订所列目标、点数或操作后重算。",location="/proposal_id",actual=proposal.get("blockers"))
    bx.require(bx.digest(path.read_bytes())==raw_hash,"读取期间计算凭证变化，请重读。",location="/proposal_file")
    return copy.deepcopy(proposal["changes"]), {"file":str(path),"proposal_id":proposal["proposal_id"],"input_revision":revision,"receipt_sha256":raw_hash,"proposal_type":proposal["proposal_type"]}


def edit_document(args):
    """Persist direction or one change without calculating an incomplete document."""
    bx.require(args.invoked,"用户明确调用后才能创建/修改方案。",location="/invoked",expected=True,actual=args.invoked)
    path=Path(args.file).resolve()
    reference=getattr(args,"proposal_file",None)
    changes=getattr(args,"changes",None)
    bx.require(bool(reference)!=bool(changes),"使用 changes 文件或程序 proposal_file 二者之一。",location="/changes")
    changes_path=Path(reference or changes).resolve()
    bx.require(path.suffix.lower()==".json" and path!=changes_path,"方案与修改请求使用两个独立 JSON 文件。",location="/file",actual=str(path))
    root=engine.resolve_root(args.pob); engine.check_output_path(path)
    bx.require(reference is not None or getattr(args,"proposal_id",None) is None,"proposal_id 需配合 proposal_file。",location="/proposal_id")
    request,adoption=proposal_request(args,root,path) if reference else (read_json(changes_path,role="input"),None)
    bx.require_keys(request,{"set","replace","remove","review_claims"},"/","修改请求使用 set/replace/remove/review_claims。")
    bx.require(isinstance(request.get("set",{}),dict),"set 应为对象。",location="/set",expected="object",actual=type(request.get("set")).__name__)
    existing=path.read_bytes() if path.is_file() else None
    stamp=bx.digest(existing) if existing is not None else None
    receipt=path.with_suffix(".edit.json")
    check_path_roles([("change_request",changes_path)],[("editable_input",path),("edit_receipt",receipt)])
    bx.require(receipt!=changes_path,"修改请求不能使用程序的 .edit.json 凭证文件名。",location="/changes",actual=str(changes_path))
    request_hash=bx.digest(json.dumps({"kind":args.kind,"request":request,**({"adoption":adoption} if adoption else {})},sort_keys=True,ensure_ascii=False,allow_nan=False))
    if receipt.is_file() and existing is not None:
        try: old=read_json(receipt)
        except (ValueError,OSError): old={}
        if old.get("request_sha256")==request_hash and old.get("result_sha256")==stamp and old.get("engine_fingerprint")==engine.fingerprint(root):
            if old.get("entrypoint")!=execution_entrypoint() or old.get("input_contract")!=input_contract(args.kind):
                old.update(entrypoint=execution_entrypoint(),input_contract=input_contract(args.kind));write_json(receipt,old)
            return {**old,"replayed":True,"file":str(path),"status":"saved_draft"}
    bx.require(existing is None or getattr(args,"if_sha256",None)==stamp,"已有方案修改需要本次返回的 revision，避免覆盖较新方案。",code="input_revision_stale",location="/if_sha256",expected=stamp,actual=getattr(args,"if_sha256",None))
    if adoption:
        bx.require(existing is not None and adoption["input_revision"]==stamp,"源方案变化，请重算新的提案。",code="proposal_context_stale",location="/if_sha256",expected=stamp,actual=adoption["input_revision"])
    document=read_json(path,role="input") if existing is not None else {"schema_version":SCHEMA_VERSION}
    result=merge_edit(document,request.get("set",{}))
    replacements=request.get("replace",{})
    bx.require(isinstance(replacements,dict),"replace 为现有 JSON pointer 到新值的映射。",location="/replace",expected="object",actual=type(replacements).__name__)
    for locator,value in replacements.items():
        bx.require(isinstance(locator,str) and locator.startswith("/"),"replace 使用现有字段的 JSON pointer。",location="/replace",expected="JSON pointer",actual=locator)
        facts.pointer(result,locator)
        parent_pointer,_,last=locator.rpartition("/")
        parent=facts.pointer(result,parent_pointer);last=last.replace("~1","/").replace("~0","~")
        parent[int(last) if isinstance(parent,list) else last]=copy.deepcopy(value)
    removes=request.get("remove",[])
    bx.require(isinstance(removes,list) and all(isinstance(v,str) and v.startswith("/") for v in removes),"remove 使用非空 JSON pointer 数组。",location="/remove",expected="pointer array",actual=removes)
    deletion_targets=[]
    bx.require(len(set(removes))==len(removes) and not any(b.startswith(a+"/") for a in removes for b in removes if a!=b),"remove 不能重复或同时删除父字段和其子字段。",location="/remove",expected="distinct non-overlapping pointers",actual=removes)
    for locator in removes:
        parent_pointer,_,last=locator.rpartition("/")
        parent=facts.pointer(result,parent_pointer); last=last.replace("~1","/").replace("~0","~")
        facts.pointer(result,locator)
        key=int(last) if isinstance(parent,list) else last
        bx.require(not any(parent is other and key==old for other,old in deletion_targets),"remove 的不同写法指向同一对象。",location="/remove",expected="distinct resolved targets",actual=removes)
        deletion_targets.append((parent,key))
    # Every locator addresses the same document after set/replace. Descending
    # array indices avoid changing the identity of another requested deletion.
    for parent,key in sorted(deletion_targets,key=lambda row: row[1] if isinstance(row[1],int) else -1,reverse=True): del parent[key]
    bx.require(result.get("schema_version")==SCHEMA_VERSION,"方案使用当前 schema_version。",location="/schema_version",expected=SCHEMA_VERSION,actual=result.get("schema_version"))
    if args.kind=="route":
        routes.preflight(result,complete=False)
        for stage in result.get("stages",[]):
            available=[]
            for group_index,group in enumerate(stage.get("skill_groups",[])):
                for gem_index,gem in enumerate(group.get("gems",[])):
                    if "instance_id" not in gem:
                        gem["instance_id"]="gem-"+bx.digest(json.dumps([stage["stage_id"],group_index,gem_index,gem.get("skill_id")],ensure_ascii=False))[:12]
                    available.append(gem)
            for need in stage.get("required_items",[]):
                if need.get("kind")=="gem" and "instance_id" not in need:
                    matches=[g for g in available if g.get("skill_id")==need.get("skill_id")]
                    if len(matches)==1: need["instance_id"]=matches[0]["instance_id"]
                    else: bx.require(False,"同名宝石数量有歧义；用返回的实例 ID 明确取得对象。",location="/stages",expected="unambiguous instance_id",actual=need)
        routes.preflight(result,complete=False)
    else:
        bx.plan_shape(result,complete=False)
        facts.model_shape(result,complete=False)
    requested_reviews=request.get("review_claims",[])
    bx.require(isinstance(requested_reviews,list) and all(isinstance(v,str) and v for v in requested_reviews) and len(requested_reviews)==len(set(requested_reviews)),"review_claims 为已核对当前对象的 claim_id 数组。",location="/review_claims",expected="distinct string array",actual=requested_reviews)
    prior_claims={c["claim_id"]:c for c in document.get("claims",[])}
    claims={c["claim_id"]:c for c in result.get("claims",[])}
    bx.require(set(requested_reviews)<=set(claims),"review_claims 必须指向当前已有判断。",location="/review_claims",expected=sorted(claims),actual=requested_reviews)
    fingerprint=engine.fingerprint(root)
    def claim_body(row): return {k:v for k,v in row.items() if k!="bindings"}
    for key,claim in claims.items():
        old=prior_claims.get(key,{})
        bx.require(claim.get("bindings")==old.get("bindings"),"bindings 由程序生成；核对后用 review_claims 更新。",location="/claims/"+key+"/bindings",expected="program-owned bindings",actual=claim.get("bindings"))
        if key not in prior_claims or document_changes(claim_body(old),claim_body(claim)) or key in requested_reviews:
            claim["bindings"]=facts.claim_bindings(result,claim,fingerprint)
    invalidated=[key for key,claim in claims.items() if claim.get("bindings")!=facts.claim_bindings(result,claim,fingerprint)]
    # Review is a separate final transaction over already committed inputs.
    old_review=document.get("assessment",{}).get("review")
    new_review=result.get("assessment",{}).get("review")
    if new_review is not None and document_changes(old_review,new_review):
        def without_review(value):
            copied=copy.deepcopy(value);copied.get("assessment",{}).pop("review",None)
            return copied
        bx.require(not document_changes(without_review(document),without_review(result)),"先保存方案修改并重算，再单独提交当前复核。",code="review_transaction_mixed",location="/assessment/review",expected="review-only transaction")
    # Validate local structure before touching the only source. Semantic choices
    # and current native legality remain evaluate/route responsibilities.
    bx.require((path.read_bytes() if path.is_file() else None)==existing,"保存前方案已变化，请重新读取 revision。",location="/file",actual=str(path))
    source_text=json_text(result,role="input")
    check_path_roles([("change_request",changes_path)]+document_reads(result,path.parent),[("editable_input",path),("edit_receipt",receipt)])
    revision=bx.digest(source_text)
    handles={key:[{field:row.get(field) for field in fields} for row in result.get(key,[])] for key,fields in EDIT_COLLECTIONS.items() if key in result}
    response={"kind":args.kind,"file":str(path),"receipt_file":str(receipt),"revision":revision,"request_sha256":request_hash,"result_sha256":revision,"engine_fingerprint":fingerprint,"entrypoint":execution_entrypoint(),"handles":handles,"status":"saved_draft",
              "counts":document_counts(result),"actual_changes":document_changes(document,result),"invalidated_claims":invalidated,"input_contract":input_contract(args.kind),
              "artifacts":{"source":[{"path":str(path),"role":"editable_plan"}],"persistent_state":[{"path":str(receipt),"role":"revision_and_retry_receipt"}],"temporary":[]},
              "next_actions":[{"action":"edit_or_calculate","file":str(path),"revision":revision,"reference":"references/leveling.md" if args.kind=="route" else "references/build.md"}],
              "scope":"已保存输入草案及稳定标识；没有计算、确认游戏合法性或自动完成模型评审。"}
    if args.kind=="route": response["stage_instances"]=[{"stage_id":s["stage_id"],"instances":[{k:g.get(k) for k in ("instance_id","skill_id","level")} for group in s.get("skill_groups",[]) for g in group.get("gems",[])]} for s in result.get("stages",[])]
    if adoption: response["adopted_proposal"]=adoption
    response["facts_summary"]=f"已保存草案；实际修改 {len(response['actual_changes'])} 处，阶段 {response['counts']['stages']} 个、技能组 {response['counts']['skill_groups']} 组、宝石 {response['counts']['gems']} 颗、判断 {response['counts']['claims']} 条。待重新评审判断 {len(invalidated)} 条。持久状态凭证保留在 {receipt}；程序临时文件已自动处理。此操作没有计算或证明配装达标。"
    json_text(response)  # Check both encoded documents before committing either.
    try:
        bx.require((path.read_bytes() if path.is_file() else None)==existing,"提交前方案发生变化，请重新读取 revision。",code="input_revision_stale",location="/file",expected=stamp,actual=str(path))
        write_text(path,source_text)
        write_json(receipt,response)
    except OSError as exc:
        current=bx.digest(path.read_bytes()) if path.is_file() else None
        error=bx.ContractError(str(exc),code="io_error",location="/receipt_file" if current==revision else "/file",expected="atomic source and receipt writes",actual=str(receipt if current==revision else path))
        error.diagnostic.update(action="read_current_revision",retryable=current!=revision and transient_io(exc),recoverable=True,source_committed=current==revision,receipt_committed=False,current_revision=current,previous_revision=stamp,receipt_file=str(receipt))
        raise error from exc
    return response


def view_json(args):
    """Read ordinary task JSON by the existing pointer contract; never recalculate."""
    path=Path(args.file).resolve()
    bx.require(path.suffix.lower()==".json","view 读取任务工作区已有 JSON，游戏原文使用 data。")
    bx.require(1<=args.limit<=12000 and args.offset>=0,"limit 为正整数，offset为非负整数。",location="/pagination",expected="limit 1..12000, offset >=0",actual={"limit":args.limit,"offset":args.offset})
    revision=bx.digest(path.read_bytes())
    selected=facts.pointer(read_json(path),args.pointer)
    bx.require(bx.digest(path.read_bytes())==revision,"读取期间文件变化；重读当前字段与 revision。",location="/file",actual=str(path))
    fields=args.fields.split(",") if args.fields else []
    bx.require(len(fields)<=100 and all(field.strip()==field and field for field in fields) and len(fields)==len(set(fields)),"fields 使用最多100个不同的直接字段名，逗号分隔。",location="/fields")
    if args.keys:
        bx.require(isinstance(selected,dict) and not fields,"keys 列出当前对象字段；不与 fields 同用。",location=args.pointer)
        selected=sorted(selected)
    if args.query:
        bx.require(isinstance(selected,list) and all(isinstance(value,str) for value in selected),"query 筛选文本数组或 keys 的字段名；对象数组先用 pointer/fields 定位。",location=args.pointer)
        selected=[value for value in selected if args.query.casefold() in value.casefold()]
    missing=[]
    def project(value,index=None):
        if not fields: return value
        bx.require(isinstance(value,dict),"fields 只能选择对象字段；先用 pointer 定位对象或对象数组。",location=args.pointer)
        absent=[field for field in fields if field not in value]
        if absent: missing.append({"index":index,"fields":absent})
        return {key:value[key] for key in fields if key in value}
    result={"file":str(path),"revision":revision,"pointer":args.pointer,"fields":fields,"keys":args.keys,"query":args.query,"offset":args.offset,
            "missing_fields":missing,"read_only":True,"next_actions":[],
            "scope":"当前JSON分页；缺字段保持缺失。offset 对应 page_unit。deferred_values 按子 pointer 继续读取；next_offset 继续同一字段。未重算。"}
    fits=lambda value: len(json.dumps(value,ensure_ascii=False,allow_nan=False))<=9500
    bx.require(len(json.dumps(result,ensure_ascii=False))<=6000,"读取参数过长，请缩小 fields/query。",location="/pagination",expected="bounded reader parameters",actual={"fields_count":len(fields),"query_characters":len(args.query or "")})
    if isinstance(selected,str):
        bx.require(not fields,"字符串用 pointer/offset/limit 分页。",location="/fields")
        total=len(selected);end=min(total,args.offset+args.limit)
        result.update(total=total,page_unit="characters",value=selected[args.offset:end],next_offset=end if end<total else None)
        while not fits(result) and end>args.offset:
            end=args.offset+(end-args.offset)//2
            result.update(value=selected[args.offset:end],next_offset=end if end<total else None)
    elif isinstance(selected,(list,dict)):
        bx.require(args.limit<=100,"数组/对象 limit 为1–100；长字符串可用更大 limit。",location="/limit",expected="1..100",actual=args.limit)
        is_array=isinstance(selected,list)
        if not is_array: selected=project(selected)
        entries=selected if is_array else list(selected)
        total=len(entries);end=min(total,args.offset+args.limit)
        def page(stop):
            return [project(row,args.offset+i) for i,row in enumerate(entries[args.offset:stop])] if is_array else {k:selected[k] for k in entries[args.offset:stop]}
        result.update(total=total,page_unit="items" if is_array else "fields",value=page(end),next_offset=end if end<total else None)
        while not fits(result) and end-args.offset>1:
            end=args.offset+(end-args.offset)//2
            missing.clear();result.update(value=page(end),next_offset=end if end<total else None)
        if not fits(result) and args.offset<total:
            key=str(args.offset) if is_array else entries[args.offset]
            pointer=args.pointer+"/"+key.replace("~","~0").replace("/","~1")
            result.update(value=[] if is_array else {},deferred_values=[pointer],next_offset=args.offset+1 if args.offset+1<total else None,
                          next_actions=[{"action":"view","file":str(path),"pointer":pointer,"offset":0,"limit":20}])
    else:
        bx.require(args.offset==0 and not fields,"标量没有分页字段。",location="/offset",expected=0,actual=args.offset)
        result.update(total=1,page_unit="scalar",value=selected,next_offset=None)
    bx.require(fits(result),"本页元数据过大，请缩小读取参数。",location="/pagination",expected="bounded page",actual={"characters":len(json.dumps(result,ensure_ascii=False))})
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
    rows.append(effect_statement(result,'当前角色')['text'])
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
    state = {"schema_version": SCHEMA_VERSION, "skill_version": VERSION, "entrypoint":execution_entrypoint(), "invoked": True, "created_at": now(), "phase": "clarifying",
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
    bx.require(value.get("schema_version") == SCHEMA_VERSION, "intent 使用 schema_version=2；旧默认策略字段需按 references/build.md 与 evidence.md 迁移。")
    bx.require_keys(value,INTENT_FIELDS,"/","意图包含未知/弃用字段。")
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
    facts.model_shape(result)
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
        bx.require(isinstance(key, str) and key and bx.finite_number(val), "约定最低指标必须是本机指标名和有限数值。",location="/minimum_stats/"+str(key),expected="finite representable number",actual=val)
    if result.get("max_changed_equipment") is not None:
        rule = result["max_changed_equipment"]
        bx.require(isinstance(rule, dict) and set(rule) == {"count", "basis"} and type(rule["count"]) is int and rule["count"] >= 0 and rule["basis"] in {"replaced_items", "changed_slots"}, "max_changed_equipment 需要 count 与 basis=replaced_items/changed_slots。")
    if result.get("baseline_main_group") is not None:
        bx.index_one(bx.groups(original), result["baseline_main_group"], "原角色主技能组")
    bx.set_config(copy.deepcopy(original), result["scenario"],native=native,location="/scenario")
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
    supplied=read_json(args.file,role="input")
    facts.model_shape(supplied)
    native = engine.calculate(root, catalog="rules", query="slots")
    intent = validate_intent(supplied, bx.read_document(run / "original.xml"), native, Path(args.file).resolve().parent)
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

def prepared(original, intent, *, baseline=False, scenario=None, native=None):
    root = copy.deepcopy(original)
    common = dict(scenario or {})
    for key, value in intent.get("scenario", {}).items():
        bx.require(key not in common or bx.same_scalar(common[key],value), "比较场景覆盖了用户限定条件：" + key,location="/scenario/"+key)
        common[key] = value
    bx.set_config(root, common,native=native,location="/scenario")
    root.find("Build").set("characterLevelAutoMode", "false")
    if baseline and intent.get("baseline_main_group") is not None:
        root.find("Build").set("mainSocketGroup", str(intent["baseline_main_group"]))
    return root

def measured_calculation(root, document, measurement):
    result=engine.calculate(root,bx.serialise(document))
    if not measurement: return document,result
    selected_before={key:result.get(key) for key in ("main_skill","main_group")}
    measured,audit=bx.apply_measurement(document,measurement,result)
    if bx.serialise(measured)!=bx.serialise(document): result=engine.calculate(root,bx.serialise(measured))
    for control in measurement.get("effects",[]):
        for gem in result.get("gems",[]):
            for effect in gem.get("granted_effects",[]):
                if effect["id"]==control["skill_id"]:
                    bx.require(effect.get("global_enabled")==control["enabled"],"原生效果开关没有保留共同测量条件。",location="/measurement/effects",expected=control,actual=effect)
    for item in result.get("equipped",[]):
        if item.get("type")=="Flask" and "flasks_enabled" in measurement:
            bx.require(item.get("active")==measurement["flasks_enabled"],"原生药剂开关没有保留共同测量条件。",location="/measurement/flasks_enabled",expected=measurement["flasks_enabled"],actual=item)
    result["measurement_audit"]={**audit,"selected_main_before":selected_before,
                                 "selected_main_after":{key:result.get(key) for key in ("main_skill","main_group")}}
    return measured,result


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
        else:
            location="/config/"+name.replace("~","~0").replace("/","~1")
            try: bx.config_value_shape(name,bx.config_input_value(entry,location),result,location)
            except bx.ContractError as exc:
                if exc.diagnostic["action"]=="inspect_capability":
                    gaps.append("capability:config_type:"+name);warnings.append(str(exc))
                else: errors.append(str(exc))
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
    for node,effect in re.findall(r"\{(\d+),(\d+)\}",spec.get("masteryEffects","")):
        if "mastery_selections" not in result: gaps.append("tree:mastery:"+node)
        elif result["mastery_selections"].get(node)!=int(effect): errors.append("引擎未保留专精效果："+node+"="+effect)
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
        key = facts.add_material(grouped,row)
        grouped[key]['requirement_id']=key
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
        original_value=before.get("measurement_audit",{}).get("selected_main_before",{}).get(field,before[field]) if field=="main_skill" else before[field]
        if original_value != after[field]:
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
        for index,entry in enumerate(rows):
            bx.require(isinstance(entry,dict) and "status" not in entry,"物资状态由程序生成；owned 使用 inventory 与用户声明，价格通过 evidence_refs 关联来源。")
            ref=facts.material_ref(entry,location=f"/{group}/{index}")
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
                bx.require(bx.finite_number(lo) and bx.finite_number(hi) and 0<=lo<=hi,"价格 low/high 是该条全部 quantity 总价，需为有效有限区间。",location=f"/{group}/{index}",expected="finite representable interval 0<=low<=high",actual={"low":lo,"high":hi})
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
    complete=not unresolved and not any_unknown
    within=False if over else True if limit is not None and complete and not estimated and high<=limit else None
    return {"status":status,"low":low,"high":high,"known_lower_bound":known_low,"totals_complete":complete,"within_limit":within,
            "currency":currency,"limit":limit,"over_budget":over,"range_crosses_limit":crossing,"unresolved":unresolved,"warnings":warnings,"materials":materials,"shopping":list(orders.values()),"inventory":list(stocks.values())}

def compare(before, after, comparable=True):
    result={}
    keys=before["stats"].keys() & after["stats"].keys()
    for key in sorted(keys):
        a,b=before["stats"][key],after["stats"][key]
        if bx.finite_number(a) and bx.finite_number(b):
            result[key]={"before":a,"after":b,"delta":b-a,"percent":(b-a)/abs(a)*100 if a and comparable else None,"scope":"native_snapshot"}
    return result

def roundtrip(root, result, document, plan=None):
    bx.verify_selections(document,result,(plan or {}).get('operations'))
    saved = bx.preserve_extensions(document, bx.parse(result["xml"]))
    xml = bx.serialise(saved)
    reloaded = engine.calculate(root, xml, cache=False)
    bx.require(engine.character_state(result) == engine.character_state(reloaded), "XML 回读后配置、装备、天赋或技能发生变化，未通过导出检查。")
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
    """Translate ordinary base/rare recipes using the installed modifier pool."""
    bx.operations_shape(plan.get("operations",{}))
    recipes,source_indices = [],[]
    for index,item in enumerate(plan.get("operations", {}).get("items", [])):
        if "craft" not in item:
            continue
        spec = item["craft"]
        recipes.append({"id": item["id"], **spec})
        source_indices.append(index)
    if not recipes:
        return plan, []
    audit = []
    for offset in range(0,len(recipes),10):
        try: audit.extend(engine.calculate(root, crafts=recipes[offset:offset+10])["crafted_items"])
        except engine.EngineError as exc:
            diagnostic=dict(exc.diagnostic)
            match=re.fullmatch(r"/crafts/(\d+)(/.*)?",diagnostic.get("location",""))
            if match and int(match[1])<len(recipes[offset:offset+10]):
                diagnostic["location"]=f"/operations/items/{source_indices[offset+int(match[1])]}/craft"+(match[2] or "")
            raise engine.EngineError(str(exc),diagnostic=diagnostic) from exc
    generated = {item["id"]: item["text"] for item in audit}
    resolved = copy.deepcopy(plan)
    resolved["operations"]["items"] = [{"id": item["id"], "text": generated[item["id"]]} if "craft" in item else item for item in resolved["operations"]["items"]]
    return resolved, audit


def effect_statement(calculation, name):
    state=engine.effect_state(calculation)
    switches=[key+"="+json.dumps(value,ensure_ascii=False) for key,value in state["config_switches"].items()]
    effects=[row['id']+"="+json.dumps(row['global_enabled'])+"/技能存在="+json.dumps(row['active_skill_present'])+
             f"（组 {row['group']}、宝石 {row['index']}）" for row in state['global_effects']]
    text=(name+" · 当前效果开关：护卫="+json.dumps(state['guard_active'])+"；药剂="+json.dumps(state['flasks'],ensure_ascii=False)+
          "；全局效果="+("、".join(effects) or "无原生条目")+"；配置="+("、".join(sorted(switches)) or "见 config_controls")+
          "。null 为未知；开关仅记录当前配置，启用、装备存在和界面可见均不证明持续覆盖。")
    return {"kind":"effect_state","subject":name,"status":"observed" if state['config_controls_available'] else "unknown","values":state,"text":text}


def calculation_facts(result, claims=()):
    referenced = set()
    for claim in claims:
        for ref in claim.get("evidence_refs", []):
            if "#/stats/" in ref: referenced.add(ref.split("#/stats/", 1)[1])
    return {"xml_sha256": result.get("xml_sha256") or result.get("input_xml_sha256"), "engine_fingerprint": result.get("engine_fingerprint"),
            "character_identity":result.get("character_identity"),
            "measurement_audit":result.get("measurement_audit",{}),
            "level": result.get("level"), "class": result.get("class"), "class_id":result.get("class_id"), "ascendancy": result.get("ascendancy"),"ascendancy_id":result.get("ascendancy_id"),
            "points":result.get("points"),"nodes":result.get("nodes"),"ascendancy_points":result.get("ascendancy_points"),"secondary_ascendancy_points":result.get("secondary_ascendancy_points"),
            "main_skill": result.get("main_skill"), "main_skill_name": result.get("main_skill_name"),
            "config_values": result.get("config_values", {}), "config_labels": result.get("config_labels", {}),
            "config_controls":result.get("config_controls",{}),"effect_state":engine.effect_state(result),
            "stats": {k: v for k, v in result.get("stats", {}).items() if k in set(METRICS) | referenced},
            "available_stat_keys":sorted(result.get("stats",{})),
            "stat_metadata":{k:v for k,v in result.get("stat_metadata",{}).items() if k in set(METRICS)|referenced},
            "active_skills": result.get("active_skills", []), "damage_sources": result.get("damage_sources", []),
            "gems":result.get("gems",[]),"equipped":result.get("equipped",[]),"resource_rules":result.get("resource_rules",{}),
            "scope": "当前配置与所列主体的原生快照；参考技能必须用其 skill_ref，主技能面板不能代替副技能或阶段。"}


def publication(status, errors, pending, budget, calculations, *, readiness=None, resource_audits=None, keys=None, claims=None):
    """One current factual publication for HTML, receipts and the agent's reply."""
    brief=facts.delivery_brief(status,errors,pending)
    measurements=[]
    selected=list(METRICS) if keys is None else keys
    registry={}
    for name,calculation in calculations.items():
        for key in selected:
            if key not in calculation.get("stats",{}): continue
            ref="native:"+name+"#/stats/"+key
            row=facts.resolve_reference(ref,registry,calculations)
            measurements.append({"reference":ref,"subject":name,"metric":key,"value":row["value"],"meaning":row["meaning"],"context":row["context"],"text":row["formatted_fact"]})
    within=budget.get("within_limit")
    budget_text=("已有对应来源的成本下界超过预算。" if within is False else "在所列报价或已声明库存条件下，已核对成本区间在预算内；购买前仍需复核。" if within is True else "预算是否满足尚未证实；现有估算或缺失报价不能作为预算内买齐的承诺。" if budget.get("limit") is not None else "尚未约定预算；物资与价格核验状态："+budget["status"]+"。")
    statements=[{"kind":"delivery","status":status,"text":brief["message"]},
                {"kind":"budget","status":"supported" if within is True else "refuted" if within is False else "unknown","text":budget_text}]
    verified=[{"claim_id":c["claim_id"],**check} for c in claims or [] for check in c.get("checks",[])]
    if claims:
        totals=Counter(check["status"] for check in verified)
        statements.append({"kind":"declared_checks","status":"refuted" if totals["refuted"] else "unknown" if totals["unknown"] else "supported" if verified else "not_requested",
                           "text":f"所列可比较检查：满足 {totals['supported']} 项、不满足 {totals['refuted']} 项、未测得 {totals['unknown']} 项。其余模型解释不构成可比较命题的证明。"})
    for name,calculation in calculations.items():
        statements.append(effect_statement(calculation,name))
        fields={key:calculation.get(key) for key in ("level","points","ascendancy_points")}
        if any(value is not None for value in fields.values()):
            values={key:str(value) if value is not None else "未知" for key,value in fields.items()}
            statements.append({"kind":"native_build","subject":name,"status":"observed","values":fields,
                               "text":f"{name} · 实际角色等级 {values['level']}；普通天赋已分配 {values['points']} 点；升华已分配 {values['ascendancy_points']} 点。文字规划不计入此 XML 的实际分配。"})
        main=next((skill for skill in calculation.get("active_skills",[]) if skill.get("is_main")),None)
        if main:
            statements.append({"kind":"damage_subject","subject":name,"skill_ref":main.get("skill_ref"),"actor":main.get("actor"),"status":"observed",
                               "text":name+" · 当前主技能主体："+str(main.get("actor","未知"))+"；伤害、吸取与回复按该主体和资源账目分别读取，面板值不能互相借用。"})
    audits=resource_audits if resource_audits is not None else {"candidate":readiness} if readiness else {}
    for name,audit in audits.items():
        if audit.get("timeline"):
            statements.append({"kind":"timeline_scope","subject":name,"status":"observed",
                               "text":name+" · 时间线测量范围："+audit["timeline"]["scope"]})
        execution=audit.get("execution_coverage")
        if execution:
            statements.append({"kind":"execution_coverage","subject":name,**execution,
                               "text":name+" · 整套动作执行覆盖："+execution["scope"]})
        payments=audit.get("single_payments",[])
        if payments:
            counts=Counter(row["status"] for row in payments)
            statements.append({"kind":"single_payment","subject":name,"status":"refuted" if counts["refuted"] else "unknown" if counts["unknown"] else "supported",
                               "text":f"{name} · 独立单次支付：可支付 {counts['supported']} 项、不可支付 {counts['refuted']} 项、未测得 {counts['unknown']} 项。每项独立从满可用储量开始，不证明任意时点或连续动作可支付。"})
        sustained=audit.get("sustained_use")
        if sustained:
            state=sustained["status"]
            statements.append({"kind":"sustained_use","subject":name,"status":state,"text":name+" · "+{"supported":"所列离散时间线可在固定净回复下重复；范围不含吸取、充能、药剂补充、动作互斥及条件变化。","refuted":"所列时间线有无法支付、耗尽或频率矛盾，不能作持续使用保证。","unknown":"持续使用尚未测得；资源快照的正余额或独立单次支付不能证明启动、连续动作及效果覆盖。"}[state]})
        resource_messages={"supported":"所列窗口的逐次支付通过；此项仅覆盖窗口内支付，持续使用结论另列。未计吸取、充能与药剂补充。",
                           "refuted":"时间线中有无法支付或耗尽的时点。","unknown":"所列窗口支付尚未证实，具体费用/窗口缺口见时间线。"}
        for resource in audit.get("timeline",{}).get("resources",[]) if audit.get("timeline") else []:
            state=resource["status"]
            window={"at":resource["duration_seconds"],"initial":resource["initial"],"final":resource.get("final"),"last_event_at":resource["trace"][-1]["at"] if resource["trace"] else None,"gaps":resource["gaps"]}
            values=f" 窗口起点 {window['initial']:g}，{window['at']:g} 秒终点 {window['final']:g}；终点包含最后动作后的固定净回复。" if bx.finite_number(window["initial"]) and bx.finite_number(window["final"]) else " 未测得完整窗口终点；不能用最后一次动作储量替代。"
            statements.append({"kind":"resource","calculation":name,"subject":resource["resource_id"],"status":state,
                               "window":window,"text":name+" · "+resource["resource_id"]+"："+resource_messages[state]+values})
    return {"delivery_brief":brief,"statements":statements,"measurements":measurements,"declared_checks":verified,"resource_audits":audits,"capabilities":resources.capabilities(),
            "facts_summary":"\n\n".join([s["text"] for s in statements]+[m["text"] for m in measurements]),
            "scope":"仅由本次实际状态和原生值生成；主观取舍由模型另作说明，计算与回读不等于可取得或实战保证。"}


def delivery_actions(outcome,input_file,details_file,review_file,details_pointer=""):
    """Expose work from observed errors and dependencies, without choosing game policy."""
    shared={"input_file":str(input_file),"details_file":str(details_file)}
    if Path(input_file).is_file(): shared["input_revision"]=bx.digest(Path(input_file).read_bytes())
    actions=[]
    for index,row in enumerate(outcome.get("diagnostics",[])):
        for key in ("connection_proposal","change_proposal"):
            if not isinstance(row.get(key),dict) or "input_revision" not in shared: continue
            proposal=row[key]
            proposal.update(kind="route" if details_pointer else "plan",skill_version=VERSION,**shared)
            proposal.pop("proposal_id",None)
            proposal["proposal_id"]=bx.digest(json.dumps(proposal,sort_keys=True,ensure_ascii=False,allow_nan=False))
            actions.append({**shared,"action":"review_native_proposal","proposal_id":proposal["proposal_id"],"proposal_type":proposal["proposal_type"],
                            "pointer":details_pointer+f"/diagnostics/{index}/"+key,"adoptable":proposal["adoptable"],"blockers":proposal["blockers"],
                            "preview_pointer":details_pointer+f"/diagnostics/{index}/"+key+"/preview",
                            "preview_fields":[field for field in ("point_budgets","errors","pending","warnings") if field in proposal["preview"]],
                            "instruction":"view 用 preview_pointer/preview_fields 先读点数和缺口；comparison 先 keys 再按用户目标选择字段，active_skills 分页读费用。决定采用后 edit --proposal-file 指向 details_file、--proposal-id 使用本标识、--if-sha256 使用 input_revision；程序读取完整修改。保存后重算并复核全部用户目标。"})
    claim_reviews=[row for row in outcome.get("diagnostics",[]) if row.get("action")=="review_claims" and row.get("claim_id")]
    ids=list(dict.fromkeys(row["claim_id"] for row in claim_reviews))
    repairs=[row for row in outcome.get("diagnostics",[]) if row.get("action") in {"edit_input","link_claim","record_user_basis","revise_conclusion"} and row.get("location")
             and not (row.get("code")=="goal_claim_unresolved" and isinstance(row.get("actual"),dict) and row["actual"].get("claim_id") in ids)]
    if outcome["errors"] or repairs:
        actions.append({**shared,"action":"repair_input","pointer":details_pointer+("/errors" if outcome["errors"] else "/diagnostics"),
                        "total":len(outcome["errors"]) if outcome["errors"] else len(repairs),"diagnostics":[{k:v for k,v in row.items() if k not in {"connection_proposal","change_proposal"}} for row in repairs],
                        "instruction":"按具体诊断修订原方案；保留用户目标所需功能，不能靠删除有效组件消除错误。"})
    if outcome.get("reference_contract"):
        actions.append({**shared,"action":"view","file":str(details_file),"pointer":details_pointer+"/reference_contract",
                        "instruction":"读取本次原生证据根和字段前缀；view 的输出文件路径与 native 引用根分别使用。"})
    scenes=[row for row in outcome.get("diagnostics",[]) if row.get("action")=="review_configuration"]
    if scenes:
        actions.append({**shared,"action":"review_configuration","diagnostics":scenes,
                        "instruction":"先将用户目标场景落到计算输入；按实际配置与当前本机配置原文选择条件，确认重算结果后再评审。"})
    if claim_reviews:
        actions.append({**shared,"action":"review_claims","claim_ids":ids,"changes":{"review_claims":ids},"diagnostics":claim_reviews,
                        "instruction":"先逐条核对这些判断与当前事实，修正源文字，再用 edit 提交 changes 并重算。绑定由程序生成；完整结果的最终复核在此后单独提交。"})
    unsupported=[value for value in outcome["pending"] if value.startswith(("capability:","native:"))]
    dependencies=[value for value in outcome["pending"] if not value.startswith(("final_review:","capability:","native:"))]
    if dependencies:
        brief=facts.delivery_brief(outcome["status"],[],dependencies)
        actions.append({**shared,"action":"assess_dependencies","pointer":details_pointer+"/pending","total":len(dependencies),"categories":brief["unresolved_categories"],
                        "instruction":"按类别读取完整缺口，核对最终对象与每项用户目标，补具体来源、物资和使用条件。目标未达到就继续设计；确无法验证的写明未解决，不生成肯定结论。"})
    if unsupported:
        actions.append({**shared,"action":"inspect_capability","subjects":unsupported,"capabilities":resources.capabilities(),
                        "instruction":"先按当前接口能力核对缺口。未支持的机制不能靠补输入变成已测量；模型决定调整方案、取得其他可核实证据或保留明确条件。"})
    if any(value.startswith("final_review:") for value in outcome["pending"]):
        actions.append({**shared,"action":"review_current_result","review_file":str(review_file),
                        "objective_pointer":"/facts/objective_coverage","dependencies_pointer":"/facts/pending","template_pointer":"/review_template",
                        "instruction":"先处理前面的问题并重算；读取新包的用户目标、全部依赖及模板，逐项核对实际依据和未实现部分，再填写源 assessment.review。判断列表完整不代表用户目标完成；不得自动回填 token。"})
    return actions


def native_preview(root,document,before,result,limits,usage_plan):
    """One native save/reload and whole-build impact report for either proposal."""
    bx.require(result["engine_fingerprint"]==before["engine_fingerprint"],"候选预览期间引擎变化，请重算。")
    xml=roundtrip(root,result,document,usage_plan)
    errors,warnings,pending=mechanical_issues(document,result,limits or {})
    audit=resources.assess(result,limits or {},usage_plan or {},[])
    fields=("level","class_id","ascendancy_id","secondary_ascendancy_id","main_skill","main_group","use_second_weapon_set","config_values")
    context_changes=[key for key in fields if before.get(key)!=result.get(key)]
    return {"xml_sha256":bx.digest(xml),"engine_fingerprint":result["engine_fingerprint"],"native_xml":result.get("native_xml"),"roundtrip_verified":True,
            "context":{key:result.get(key) for key in fields},"context_changes":context_changes,"point_budgets":audit["point_budgets"],
            "nodes":result["nodes"],"mastery_selections":result.get("mastery_selections"),"stats":result["stats"],"comparison":compare(before,result,not context_changes),"active_skills":result["active_skills"],
            "errors":errors+audit["errors"],"pending":list(dict.fromkeys(pending+audit["pending"])),"warnings":warnings+audit["warnings"],"readiness":audit}


def native_tree_diagnostics(root,document,result,location="/operations/tree",*,tree_operations=None,limits=None,usage_plan=None):
    """Preview native allocation in isolation; adopting still requires a model choice."""
    requested={int(value) for value in bx.active_spec(document).get("nodes","").split(",") if value}
    missing=sorted(requested-set(result["nodes"]))
    if not missing: return [],[]
    queried=native_queries(root,[{"kind":"nodes","query":"id:"+str(value),"limit":1} for value in missing],result["xml"])
    bx.require(queried["engine_fingerprint"]==result["engine_fingerprint"],"天赋诊断期间引擎变化，请重算。")
    details=[]
    for node_id,row in zip(missing,queried["catalog_batch"]):
        node=next((entry for entry in row["entries"] if entry["id"]==node_id),None)
        details.append({"id":node_id,"found":node is not None,**({key:node[key] for key in ("name","ascendancy_name","reachable","points_to_allocate","path") if key in node} if node else {})})
    details.extend({"id":node_id,"found":False,"query_returned":False} for node_id in missing[len(details):])
    diagnostic={"code":"native_tree_nodes_unretained","action":"edit_input","location":location,
                "expected":sorted(requested),"actual":sorted(result["nodes"]),"tree_diagnostics":details,
                "native_context":{"xml_sha256":bx.digest(result["xml"]),"engine_fingerprint":result["engine_fingerprint"],
                                  "class_id":result.get("class_id"),"ascendancy_id":result.get("ascendancy_id")},
                "message":"请求节点未保留。程序按此实际角色的原生规则预览补路；模型审查目标与完整影响后引用程序提案采用，再重算。单节点查询成本不能相加为总点数，原生补路不证明全局最优或可立即取得。"}
    if tree_operations is not None and all(row["found"] for row in details):
        try:
            masteries={node:int(effect) for node,effect in re.findall(r"\{(\d+),(\d+)\}",bx.active_spec(document).get("masteryEffects","")) if int(node) in missing}
            order=list(dict.fromkeys([node for node in tree_operations.get("nodes_add",tree_operations.get("nodes",[])) if node in missing]+missing))
            allocated=engine.calculate(root,result["xml"],tree_targets=order,tree_masteries=masteries)
            bx.require(allocated["engine_fingerprint"]==result["engine_fingerprint"],"补路期间引擎变化，请重算。")
            diagnostic["native_allocation"]=allocated.get("native_tree_allocation",{"status":"unsupported"})
            remaining=sorted(requested-set(allocated["nodes"]))
            conflicts=sorted(set(allocated["nodes"]) & set(tree_operations.get("nodes_remove",[])))
            if remaining: diagnostic["connection_unretained_targets"]=remaining
            elif conflicts: diagnostic["connection_conflicts_with_removal"]=conflicts
            elif allocated.get("native_tree_allocation"):
                tree=copy.deepcopy(tree_operations)
                tree.pop("nodes_add",None);tree.pop("nodes_remove",None)
                tree["nodes"]=sorted(set(allocated["nodes"]))
                preview_document=bx.apply_plan(document,{"mode":"rebuild","summary":"原生天赋连接预览","operations":{"tree":tree}},native=result)
                preview=engine.calculate(root,bx.serialise(preview_document))
                bx.require(preview["engine_fingerprint"]==result["engine_fingerprint"],"连接预览期间引擎变化，请重算。")
                unretained=sorted(requested-set(preview["nodes"]))
                if unretained: diagnostic["connection_unretained_targets"]=unretained
                else:
                    report=native_preview(root,preview_document,result,preview,limits,usage_plan)
                    budgets=report["point_budgets"]
                    blockers=["point_budget:"+row["pool"] for row in budgets if row["status"]=="exceeded"]
                    diagnostic["connection_proposal"]={"proposal_type":"native_tree_connection","changes":{"replace":{location:tree}},
                        "additional_node_ids":sorted(set(preview["nodes"])-requested-set(result["nodes"])),"requested_node_ids":sorted(requested),"allocation_order":order,"adoptable":not blockers,"blockers":blockers,"preview":report,
                        "scope":"本次原生顺序补路后完整实算并 SaveDB 回读，保留全部请求节点；尚未应用。点数上限仅为声明，未知仍待核实；配装目标、取得、互斥和收益由模型决定，采用后重新 evaluate/route 全面审查。"}
        except (ValueError,KeyError,TypeError,IndexError) as exc:
            diagnostic["connection_preview_failure"]=failure_record(exc)
    return details,[diagnostic]


def native_gem_diagnostics(root,document,result,*,stage_index=None,gems=None,limits=None,usage_plan=None):
    """Expose current level requirements and one optional deferral per unmet gem."""
    affected=[]
    for gem in result.get("gems",[]):
        available={"character_level":result["level"],"strength":result["stats"].get("Str"),"dexterity":result["stats"].get("Dex"),"intelligence":result["stats"].get("Int")}
        required={key:gem.get(field) for key,field in (("character_level","req_level"),("strength","req_str"),("dexterity","req_dex"),("intelligence","req_int"))}
        gaps={key:{"required":value,"available":available[key],"deficit":value-available[key]} for key,value in required.items() if type(value) in (int,float) and type(available[key]) in (int,float) and value>available[key]}
        if gaps and gem.get("recognised") and not gem.get("source") and gem.get("slot"): affected.append((gem,available,gaps))
    if not affected: return []
    if gems is None:
        ids=list(dict.fromkeys(gem["id"] for gem,_,_ in affected))
        catalog=native_queries(root,[{"kind":"gems","query":"id:"+value,"limit":2} for value in ids])
        bx.require(catalog["engine_fingerprint"]==result["engine_fingerprint"],"宝石需求查询期间引擎变化，请重算。")
        gems={row["skill_id"]:row for page in catalog["catalog_batch"] for row in page["entries"]}
    diagnostics=[]; variants=[]
    for gem,available,gaps in affected:
        locator=f"/stages/{stage_index}/skill_groups/{gem['group']-1}/gems/{gem['index']-1}/level" if stage_index is not None else "/operations/gems"
        source_gem=bx.index_one(bx.groups(document),gem["group"],"技能组").findall("Gem")[gem["index"]-1]
        physical_level=int(source_gem.get("level","1"))
        levels=gems.get(gem["id"],{}).get("level_requirements",[])
        usable=[row for row in levels if row.get("naturally_trainable") and row["gem_level"]<=physical_level and all(type(available[key]) in (int,float) and type(row.get(key)) in (int,float) and row[key]<=available[key] for key in available)]
        diagnostic={"code":"native_gem_requirements_unmet","action":"edit_input","location":locator,"skill_id":gem["id"],"group":gem["group"],"index":gem["index"],
                    "actual":available,"required_gaps":gaps,"current_physical_level":physical_level,
                    "native_level_requirements":levels,"usable_natural_levels":[row["gem_level"] for row in usable],
                    "message":"当前角色有宝石等级/属性缺口。模型选择补属性、延期升级或更换技能；原生可用等级只描述当前属性，取得或降级成本仍需核实。延期预览只改这一颗，不自动降低其他宝石。"}
        diagnostics.append(diagnostic)
        if usable:
            level=max(row["gem_level"] for row in usable)
            operation={"gems":[{"group":gem["group"],"index":gem["index"],"level":level}]}
            candidate=bx.apply_plan(document,{"mode":"rebuild","summary":"单颗宝石延期升级预览","operations":operation},native=result)
            changes={"replace":{locator:level}} if stage_index is not None else {"set":{"operations":operation}}
            variants.append((diagnostic,candidate,changes,level))
    for offset in range(0,len(variants),12):
        batch=variants[offset:offset+12]
        try:
            inputs=[bx.serialise(candidate) for _,candidate,_,_ in batch]
            calculated=engine.calculate(root,xml_batch=inputs)
            previews=engine.ordered_batch('xml',inputs,calculated.get('calculations'))
            bx.require(calculated["engine_fingerprint"]==result["engine_fingerprint"],"宝石预览期间引擎变化，请重算。")
        except (ValueError,KeyError,TypeError) as exc:
            for diagnostic,_,_,_ in batch: diagnostic["level_preview_failure"]=failure_record(exc)
            continue
        for index,(diagnostic,candidate,changes,level) in enumerate(batch):
            preview=previews[index]
            try:
                report=native_preview(root,candidate,result,preview,limits,usage_plan)
                diagnostic["change_proposal"]={"proposal_type":"native_gem_level","changes":changes,"adoptable":True,"blockers":[],
                    "selected_level":level,"skill_id":diagnostic["skill_id"],"group":diagnostic["group"],"index":diagnostic["index"],
                    "preview":report,
                    "scope":"当前属性下的单颗自然宝石延期升级候选，已完整原生实算并 SaveDB 回读；模型审查输出/资源代价再选择。未确认实物、训练、降级服务或其他用户目标，应用后继续完整审查。"}
            except (ValueError,KeyError,TypeError,IndexError) as exc: diagnostic["level_preview_failure"]=failure_record(exc)
    return diagnostics


def failure_record(exc,location_prefix="",input_file=None):
    """Preserve native/contract diagnostics at each caller's actual input location."""
    diagnostic=getattr(exc,"diagnostic",None)
    status="blocked" if diagnostic and diagnostic.get("action")=="edit_input" else "unsupported" if diagnostic and diagnostic.get("action")=="inspect_capability" else "failed"
    record={"status":status,"reason":str(exc)}
    if diagnostic:
        diagnostic=dict(diagnostic)
        location=diagnostic.get("location","")
        if location=="/operations" or location.startswith("/operations/"): diagnostic["location"]=location_prefix+location
        if input_file: diagnostic["input_file"]=str(Path(input_file).resolve())
        record["diagnostic"]=diagnostic
    return record


def record_command_failure(args, exc):
    """Invalidate the current attempt, keeping prior calculated artifacts intact."""
    if args.command not in {"evaluate","export"}: return
    if getattr(exc,"diagnostic",{}).get("code")=="path_conflict": return
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}",args.name): return
    run=Path(args.run).resolve()
    state_path=run/"run.json"
    if not state_path.is_file(): return
    _,state=read_run(run)
    record=failure_record(exc,input_file=getattr(args,"plan",None))
    state.update(phase="failed",updated_at=now(),entrypoint=execution_entrypoint(),last_operation={"command":args.command,"candidate":args.name,"file_exported":False,**record})
    write_json(state_path,state)
    if args.command=="export": write_json(run/"output"/args.name/"delivery.json",{"file_exported":False,"status":record["status"],"failure":record})


def assess_candidate(original, document, baseline, result, intent, plan, evidence_folder, intent_evidence_folder=None, *, root=None):
    """One assessment path for evaluation and final export; scopes stay explicit."""
    context = {"engine_fingerprint": result["engine_fingerprint"], "league": intent["league"], "game_version": intent.get("game_version")}
    evidence = facts.load_evidence(intent.get("evidence", []), intent_evidence_folder or evidence_folder, context, root=root)
    plan_evidence = facts.load_evidence(plan.get("evidence", []), evidence_folder, context, root=root)
    bx.require(not (evidence.keys() & plan_evidence.keys()),"intent 和 plan 的 evidence_id 不能重复。")
    evidence.update(plan_evidence)
    native={"baseline":baseline,"candidate":result}
    claims = facts.assess_claims(plan.get("claims", []), evidence, native,specification=plan,engine_fingerprint=result["engine_fingerprint"])
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
    # Budget/resource conclusions can now refer to the actual derived report.
    # Source and inventory judgments were resolved first; they cannot certify
    # themselves by merely referring to their own resulting budget status.
    report_context={"engine_fingerprint":result["engine_fingerprint"],"xml_sha256":result.get("xml_sha256"),"origin":"program_assessment"}
    native.update(budget={**budget,**report_context},resources={**audit,**report_context})
    references=facts.reference_contract(native)
    claims = facts.assess_claims(plan.get("claims",[]),evidence,native,specification=plan,engine_fingerprint=result["engine_fingerprint"])
    coverage,coverage_pending=facts.objective_coverage(intent.get("objectives",[]),evidence,claims)
    if budget["over_budget"]: errors.append("已知采购成本下界超过用户预算。")
    warnings += budget["warnings"]
    pending = native_gaps + constraint_pending + audit["pending"] + coverage_pending
    scene_diagnostics=facts.configuration_diagnostics(claims,{"candidate":result})
    if scene_diagnostics: pending.append("config:scenario")
    measurement=plan.get("measurement",{})
    measurement_diagnostics=[]
    if measurement and not facts.has_claim(claims,"measurement"):
        pending.append("condition:measurement")
        measurement_diagnostics.append({"code":"measurement_review_missing","action":"review_configuration","location":"/measurement","subject":"measurement",
            "message":"按两份实际 gems.granted_effects、equipped 和 measurement_audit 评审共同条件；配置开关不能证明实际效果覆盖。"})
    for control in measurement.get("effects",[]):
        instances=[row for calculation in (baseline,result) for row in calculation.get("measurement_audit",{}).get("effects",[]) if row["skill_id"]==control["skill_id"] and row["status"]=="applied"]
        if not instances:
            pending.append("condition:measurement:"+control["skill_id"])
            measurement_diagnostics.append({"code":"measurement_effect_absent","action":"edit_input","location":"/measurement/effects","subject":control["skill_id"],"message":"两份角色均没有此效果；核对本次效果 ID 或删除无关测量条件。"})
    pending += ["claim_revision:"+claim["claim_id"] for claim in claims if any(d["code"] in {"claim_binding_stale","claim_binding_missing"} for d in claim["diagnostics"])]
    pending += ["claim_check:"+claim["claim_id"] for claim in claims if claim["check_status"] in {"refuted","unknown"}]
    pending += ["material:"+value for value in budget["unresolved"]]
    if budget["status"] in {"unknown", "uncertain_range"}: pending.append("budget")
    if intent.get("budget") and budget["high"] > intent["budget"]["amount"]: pending.append("budget:upper_bound")
    for item in plan.get("operations", {}).get("items", []):
        if not facts.has_claim(claims, "legality:item:"+str(item["id"])): pending.append("legality:item:"+str(item["id"]))
    for key in plan.get("scenario", {}):
        if key not in intent.get("scenario", {}) and not facts.has_claim(claims,"config:"+key): pending.append("config:"+key)
    assessment = plan.get("assessment", {})
    goal_diagnostics=facts.goal_review_diagnostics(assessment,claims)
    if goal_diagnostics: pending.append("goal_assessment")
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
    if "timeline" in plan or any(r["projection"] != "native_full_speed" for r in audit["resources"]): comparison_notes.append("使用节奏调整未重算伤害，数值只代表原生快照。")
    if comparison_notes: warnings.append("前后只展示数值差，不声明同条件提升百分比。")
    review, review_pending = facts.final_review({"intent": intent, "plan": plan}, evidence, claims,
        {"baseline": calculation_facts(baseline, claims), "candidate": calculation_facts(result, claims),
         "resources": audit, "reference_contract":references, "materials": requirements, "budget": budget, "objective_coverage":coverage, "equipment_changes":equipment,"comparison_notes": comparison_notes,
         "pending": list(dict.fromkeys(pending)), "errors": errors}, assessment)
    pending += review_pending
    errors, pending = list(dict.fromkeys(errors)), list(dict.fromkeys(pending))
    diagnostics=list(audit.get("diagnostics",[]))+goal_diagnostics+scene_diagnostics+measurement_diagnostics+facts.claim_diagnostics(claims)
    for objective in coverage:
        diagnostics.extend({**row,"subject":objective["required_subject"],"category":"input","status":"unknown"} for row in objective["diagnostics"])
    for category,messages in (("mechanical",mechanical_errors),("user_constraint",constraint_errors),("usage",audit["errors"]),("budget",["已知采购成本下界超过用户预算。"] if budget["over_budget"] else [])):
        for message in messages:
            diagnostics.append({"code":category+"_violation","category":category,"subject":"candidate","evidence_refs":[],"affects":["delivery"],"message":message,"status":"failed"})
    for subject in pending:
        diagnostics.append({"code":"unresolved_dependency","category":"capability" if subject.startswith(("capability:","native:")) else "evidence","subject":subject,
                            "evidence_refs":[ref for claim in claims if subject in claim["subjects"] for ref in claim["evidence_refs"]],"affects":["delivery",subject],"message":"需要补足对应事实、条件或用户意图："+subject,"status":"unknown"})
    status="blocked" if errors else "draft" if pending else "scoped_result"
    published=publication(status,errors,pending,budget,{"candidate":result},readiness=audit,claims=claims,keys=[key for key in result.get("stats",{}) if key in {"Life","EnergyShield","ManaUnreserved","Armour","CombinedDPS","MinionCombinedDPS","ChaosResist"}])
    return {"status":status,"delivery_brief":published["delivery_brief"],"publication":published, "errors":errors,"pending":pending,"warnings":list(dict.fromkeys(warnings)),
            "validation":{"mechanical":"failed" if mechanical_errors else "unknown" if native_gaps else "passed",
                          "user_constraints":"failed" if constraint_errors else "unknown" if constraint_pending else "passed",
                          "usage":"failed" if audit["errors"] else "unknown" if audit["pending"] else "model_assessed",
                          "target_version":version["status"], "goal":"model_assessed" if "goal_assessment" not in pending else "unresolved",
                          "scope":"程序核对结构、可用原生事实和证据引用；目标取舍、来源语义、效果覆盖仍是模型判断，不是实战保证。"},
            "configuration_audit":{"original_inputs":[dict(n.attrib) for n in bx.active_config(original).findall("Input")],"common_changes":plan.get("scenario",{}),"measurement":measurement,
                "measurement_actual":{"baseline":baseline.get("measurement_audit",{}),"candidate":result.get("measurement_audit",{})},"user_required":intent.get("scenario",{}),"candidate_changes":plan.get("operations",{}).get("config",{}),"baseline_actual":baseline.get("config_values",{}),"candidate_actual":result.get("config_values",{})},
            "diagnostics":diagnostics,"reference_contract":references,"budget":budget,"readiness":audit,"objective_coverage":coverage,"claims":claims,"evidence":evidence,"target_version_evidence":version,
            "review_packet":review,
            "equipment_changes":equipment,"actual_changes":actual_changes(baseline,result,equipment),"comparison_notes":comparison_notes,"comparison":compare(baseline,result,not comparison_notes)}


def evaluate(args):
    run, state = read_run(args.run)
    bx.require(state.get("intent"), "先通过多轮对话确认意图，并执行 intent。")
    bx.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.name), "候选名称请使用 1–48 位小写英文、数字或连字符。")
    intent, plan_path = state["intent"], Path(args.plan).resolve()
    source_plan_sha256=bx.digest(plan_path.read_bytes())
    plan = read_json(plan_path,role="input")
    bx.plan_shape(plan)
    facts.model_shape(plan)
    destination=run/"candidates"/args.name
    check_path_roles([("source_plan",plan_path),("original_xml",run/"original.xml")]+document_reads(plan,plan_path.parent),
                     [("run_state",run/"run.json"),("baseline",run/"baseline.json"),("previous_delivery",run/"output"/args.name/"delivery.json")]+[("candidate_"+name,destination/name) for name in ("requested.xml","candidate.xml","plan.json","evaluation.json","review.json")])
    bx.require(plan.get("schema_version")==SCHEMA_VERSION,"plan 使用 schema_version=2，旧策略字段请按 references/build.md 与 evidence.md 迁移。")
    original = bx.read_document(run / "original.xml")
    root = engine.resolve_root(args.pob or state["pob_path"])
    info = engine.check(root)
    engine.validate_game_version(intent.get("game_version"), intent["league"], info.get("data_version"))
    native = engine.calculate(root, catalog="rules")
    scenario = plan.get("scenario", {})
    context_hash = bx.digest(json.dumps({"intent":state["intent_hash"], "scenario":scenario,"measurement":plan.get("measurement",{}),"engine":info["engine_fingerprint"],"skill":VERSION},sort_keys=True,ensure_ascii=False,allow_nan=False))
    base_path = run / "baseline.json"
    cached = read_json(base_path) if base_path.exists() else {}
    if (cached.get("context_hash")==context_hash and cached.get("skill_version")==VERSION and cached.get("engine_fingerprint")==info["engine_fingerprint"]
            and bx.digest(base_path.read_text(encoding="utf-8"))==state.get("baseline_sha256")):
        baseline=cached
    else:
        _,baseline=measured_calculation(root,prepared(original,intent,baseline=True,scenario=scenario,native=native),plan.get("measurement",{}))
        baseline.update(context_hash=context_hash,skill_version=VERSION,intent_hash=state["intent_hash"])
        write_json(base_path,baseline)
    state["baseline_sha256"]=bx.digest(base_path.read_text(encoding="utf-8"))
    source=original; input_artifacts={}
    if plan.get("template_xml"):
        path=Path(plan["template_xml"])
        path=(path if path.is_absolute() else plan_path.parent/path).resolve()
        input_artifacts[str(path)]=bx.digest(path.read_bytes())
        source=bx.read_document(path)
    resolved, crafted = materialize_items(plan,root)
    document=bx.apply_plan(prepared(source,intent,scenario=scenario,native=native),resolved,native=native)
    document,result=measured_calculation(root,document,plan.get("measurement",{}))
    bx.require(result["engine_fingerprint"]==baseline["engine_fingerprint"]==native["engine_fingerprint"]==info["engine_fingerprint"],"计算期间引擎内容发生变化，请更新完成后重算。")
    tree_diagnostics,tree_repairs=native_tree_diagnostics(root,document,result,tree_operations=plan.get("operations",{}).get("tree"),limits=intent,usage_plan=plan)
    gem_repairs=native_gem_diagnostics(root,document,result,limits=intent,usage_plan=plan)
    xml=roundtrip(root,result,document,resolved)
    result["xml_sha256"]=bx.digest(xml)
    assessment=assess_candidate(original,document,baseline,result,intent,plan,plan_path.parent,Path(state.get("intent_evidence_folder",str(plan_path.parent))),root=root)
    assessment["diagnostics"]+=tree_repairs+gem_repairs
    bx.require(bx.digest(plan_path.read_bytes())==source_plan_sha256,"计算期间源计划变化，请重新 evaluate。",location="/source_plan",input_file=str(plan_path))
    bx.require(all(Path(path).is_file() and bx.digest(Path(path).read_bytes())==digest for path,digest in input_artifacts.items()),"计算期间角色模板变化，请重新 evaluate。",location="/template_xml")
    destination=run/"candidates"/args.name
    requested=bx.serialise(document)
    write_text(destination/"requested.xml",requested)
    write_text(destination/"candidate.xml",xml)
    write_json(destination/"plan.json",plan)
    outcome={"schema_version":SCHEMA_VERSION,"skill_version":VERSION,"name":plan.get("name",args.name),"evaluated_at":now(),
             "engine_version":result["engine_version"],"engine_fingerprint":result["engine_fingerprint"],"intent_hash":state["intent_hash"],
             "original_sha256":state["original_sha256"],"xml_sha256":bx.digest(xml),"requested_sha256":bx.digest(requested),"roundtrip_verified":True,
             "source_plan":str(plan_path),"source_plan_sha256":source_plan_sha256,"input_artifacts":input_artifacts,
             "evidence_folder":str(plan_path.parent),"baseline":baseline,"result":{k:v for k,v in result.items() if k!="xml"},
             "plan":plan,"crafted_items":crafted,"tree_diagnostics":tree_diagnostics,**assessment}
    outcome["next_actions"]=delivery_actions(outcome,plan_path,destination/"evaluation.json",destination/"review.json")
    delivery=run/"output"/args.name/"delivery.json"
    if delivery.is_file():
        write_json(delivery,{"file_exported":False,"status":outcome["status"],"candidate":args.name,"reason":"superseded_by_evaluation"})
    response={"candidate":str(destination),"evaluation_file":str(destination/"evaluation.json"),"status":outcome["status"],"errors":outcome["errors"],"pending":outcome["pending"],"warnings":outcome["warnings"],
            "budget":outcome["budget"],"readiness":outcome["readiness"],"roundtrip_verified":True,"delivery_brief":outcome["delivery_brief"],
            "comparison":{k:v for k,v in outcome["comparison"].items() if k in METRICS},"baseline_warnings":baseline.get("warnings",[]),
            "tree_diagnostics":tree_diagnostics,"review_file":str(destination/"review.json"),"review_token":outcome["review_packet"]["token"],
            "next_actions":outcome["next_actions"],
            "next":"按 next_actions 处理实际错误和目标缺口，再审查当前 review.json。方案改变会作废旧复核；缺字段与结论不得用 token 或计算成功代替。"}
    outcome["response"]=response
    write_json(destination/"evaluation.json",outcome)
    write_json(destination/"review.json",outcome["review_packet"])
    state.setdefault("candidates",{})[args.name]={"evaluation_sha256":bx.digest((destination/"evaluation.json").read_text(encoding="utf-8")),
                                                "source_plan":str(plan_path),"source_plan_sha256":source_plan_sha256}
    state.update(phase="evaluated",updated_at=now(),engine_fingerprint=result["engine_fingerprint"],entrypoint=execution_entrypoint(),
                 last_operation={"command":"evaluate","candidate":args.name,"file_exported":False,"status":outcome["status"]})
    write_json(run/"run.json",state)
    return response

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
    current_facts=bullets([s["text"] for s in outcome["publication"]["statements"]]+[m["text"] for m in outcome["publication"]["measurements"]])
    detail+="\n"+esc(json.dumps({"single_payments":outcome["readiness"].get("single_payments",[]),"sustained_use":outcome["readiness"].get("sustained_use")},ensure_ascii=False,indent=2))
    timeline=outcome["readiness"].get("timeline")
    if timeline: detail+="\n"+esc(json.dumps(timeline,ensure_ascii=False,indent=2))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(outcome['name'])}</title>
<style>body{{font:16px/1.7 system-ui;background:#f5f6f8;color:#202a38;max-width:1050px;margin:auto;padding:28px}}section{{background:white;padding:24px;margin:18px 0;border-radius:12px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}small{{color:#526174}}h1{{margin-bottom:6px}}</style>
<h1>{esc(outcome['name'])}</h1><p>{esc(status)} · PoECharm {esc(outcome['engine_version'])}</p>
<section><h2>当前核验结论</h2>{current_facts}<p>{esc(outcome['publication']['scope'])}</p></section>
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
    bx.require((folder/'evaluation.json').stat().st_size<=JSON_LIMITS['artifact'],'候选凭证超出读取上限。',location='/evaluation',expected={'maximum_bytes':JSON_LIMITS['artifact']})
    raw=(folder/"evaluation.json").read_text(encoding="utf-8")
    bx.require(bx.digest(raw)==state.get("candidates",{}).get(args.name,{}).get("evaluation_sha256"),"候选记录被修改或未完成计算，请重新 evaluate。")
    outcome=json.loads(raw)
    bx.require(outcome.get("schema_version")==SCHEMA_VERSION and outcome.get("skill_version")==VERSION,"协议或技能已变化，请重新 evaluate。")
    bx.require(outcome["intent_hash"]==state.get("intent_hash") and outcome["original_sha256"]==state["original_sha256"],"意图或原始角色变化，请重算。")
    source_plan=Path(outcome["source_plan"])
    current_revision=bx.digest(source_plan.read_bytes()) if source_plan.is_file() else None
    bx.require(current_revision==outcome["source_plan_sha256"],"源计划被修改或移动，请使用源计划重新 evaluate。",location="/source_plan",input_file=str(source_plan),expected=outcome["source_plan_sha256"],actual=current_revision)
    bx.require(input_artifacts_current(outcome),"角色模板变化，请重新 evaluate。",location="/template_xml")
    xml=(folder/"candidate.xml").read_text(encoding="utf-8")
    bx.require(bx.digest(xml)==outcome["xml_sha256"],"候选 XML 被修改，请重新 evaluate。")
    bx.require(bx.digest((folder/"requested.xml").read_text(encoding="utf-8"))==outcome["requested_sha256"],"请求 XML 被修改，请重新 evaluate。")
    root=engine.resolve_root(args.pob or state["pob_path"])
    bx.require(engine.check(root)["engine_fingerprint"]==outcome["engine_fingerprint"],"引擎、数据或适配器变化，请重新 evaluate。")
    return run,state,outcome,xml,root

def damage_observation(calculation, name):
    metric="MinionCombinedDPS" if any(s.get("is_main") and not s.get("disabled") and s.get("minion") for s in calculation.get("active_skills",[])) else "CombinedDPS"
    observed={**calculation,"stats":{**calculation.get("stats",{}),metric:calculation.get("stats",{}).get(metric)}}
    record=facts.resolve_reference("native:"+name+"#/stats/"+metric,{}, {name:observed})
    if record["value"] is None: record["status"]="unknown"
    record["scope"]="当前选定主技能/主体、XML 与实际场景的伤害快照；其他组变化下此值不变，不证明被改技能无收益、增益未触发或机制无效。"
    return record


def probe(args):
    input_file=getattr(args,'input',None)
    if input_file:
        bx.require(not getattr(args,'run',None) and not getattr(args,'name',None),'--input 的只读探针不能同时指定 --run/--name。',location='/input')
        original=bx.read_document(Path(input_file));xml=bx.serialise(original)
        root=engine.resolve_root(args.pob);state={'intent':{}}
        outcome={'plan':{'mode':'refine'},'engine_fingerprint':engine.check(root)['engine_fingerprint']}
    else:
        bx.require(getattr(args,'run',None) and getattr(args,'name',None),'候选探针同时需要 --run/--name，已有 XML 只读探针用 --input。',location='/name')
        run,state,outcome,xml,root=read_candidate(args)
        original=bx.read_document(run/'original.xml')
    source=bx.parse(xml)
    baseline=engine.calculate(root,xml)
    if input_file:
        outcome['baseline']=baseline
        bx.require(baseline['engine_fingerprint']==outcome['engine_fingerprint'],'探针读取期间引擎变化，请重算。')
    if getattr(args,"node_ids",None):
        ids=[int(v) for v in args.node_ids.split(",")]
        bx.require(ids and len(set(ids))==len(ids) and all(v>0 for v in ids),"--node-ids 使用不同的正整数节点 ID。")
        screened=engine.calculate(root,xml,node_probes=[[v] for v in ids])
        bx.require(screened["engine_fingerprint"]==outcome["engine_fingerprint"],"筛选期间引擎变化。")
        return {"status":"screened","engine_fingerprint":screened["engine_fingerprint"],"xml_sha256":bx.digest(xml),"alternatives":screened["node_screening"],
                "next":"仅为原生节点词缀筛选，不含连接路径、点数或组合认证；选定完整路径后用 --variants 实算，再 evaluate。"}
    if getattr(args,"variants",None):
        request=read_json(args.variants,role="input")
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
    rows=[]
    for offset in range(0,len(variants),12):
        prepared_variants=[]
        for variant_index,variant in enumerate(variants[offset:offset+12],offset):
            try:
                plan={"mode":outcome["plan"]["mode"],"summary":"候选局部或组合比较","operations":variant["operations"]}
                resolved,crafts=materialize_items(plan,root)
                document=bx.apply_plan(source,resolved,native=baseline)
                prepared_variants.append((variant,document,plan,crafts,variant_index))
            except (ValueError,KeyError,TypeError,IndexError) as exc:
                rows.append({"id":variant["id"],"errors":[str(exc)],**failure_record(exc,f"/variants/{variant_index}",getattr(args,"variants",None))})
        if not prepared_variants: continue
        inputs=[bx.serialise(v[1]) for v in prepared_variants]
        try:
            batch=engine.calculate(root,xml_batch=inputs)
            calculations=engine.ordered_batch('xml',inputs,batch.get('calculations'))
        except engine.EngineError:
            calculations=[]
            for text in inputs:
                try: calculations.append(engine.calculate(root,text))
                except engine.EngineError as exc: calculations.append({"failure":str(exc),"diagnostic":exc.diagnostic})
        for index,(variant,document,plan,crafts,variant_index) in enumerate(prepared_variants):
            result=calculations[index]
            if "failure" in result:
                failure=engine.EngineError(result["failure"],diagnostic=result.get("diagnostic"))
                rows.append({"id":variant["id"],"errors":[str(failure)],**failure_record(failure,f"/variants/{variant_index}",getattr(args,"variants",None))}); continue
            try: bx.verify_selections(document,result,plan['operations'])
            except bx.ContractError as exc:
                rows.append({'id':variant['id'],'errors':[str(exc)],**failure_record(exc,f'/variants/{variant_index}',getattr(args,'variants',None))})
                continue
            bx.require(result["engine_fingerprint"]==outcome["engine_fingerprint"],"比较期间引擎变化，请重算。")
            errors,warnings,pending=mechanical_issues(document,result,state["intent"])
            ce,cp=check_constraints(original,document,outcome["baseline"],result,state["intent"],plan)
            audit=resources.assess(result,state["intent"],plan,[])
            context_changes=[k for k in ("config_values","level","class","main_skill") if baseline.get(k)!=result.get(k)]
            comparison=compare(baseline,result,not context_changes)
            damage=damage_observation(result,"variant")
            dps_metric=damage["locator"].rsplit("/",1)[-1]
            rows.append({"id":variant["id"],**({"skill_id":variant["id"]} if not getattr(args,"variants",None) else {}),"status":"blocked" if errors+ce+audit["errors"] else "screened",
                         "engine_fingerprint":result["engine_fingerprint"],"xml_sha256":result["input_xml_sha256"],"operations":variant["operations"],"crafted_items":crafts,
                         "stats":{k:result["stats"].get(k) for k in metrics},"comparison":{k:v for k,v in comparison.items() if k in metrics},"context_changes":context_changes,
                         "effect_state":engine.effect_state(result),
                         "dps_metric":dps_metric,"dps_delta":comparison.get(dps_metric,{}).get("delta"),"damage_observation":damage,"allocated_points":{k:result[k] for k in ("points","ascendancy_points","secondary_ascendancy_points")},
                         "errors":errors+ce+audit["errors"],"pending":pending+cp+audit["pending"],"warnings":warnings+audit["warnings"],"readiness":audit})
    computed=sum('stats' in row for row in rows)
    status='screened' if computed==len(variants) else 'partial' if computed else 'failed' if any(row['status']=='failed' for row in rows) else 'blocked'
    return {"status":status,"source_mode":"input_xml" if input_file else "evaluated_candidate","source_xml_sha256":bx.digest(xml),"requested_variants":len(variants),"calculated_variants":computed,"baseline_effect_state":engine.effect_state(baseline),"baseline_damage_observation":damage_observation(baseline,"baseline"),"alternatives":rows,"next":"逐变体列出结果或明确失败；有 stats 的变体才通过本次原生实算及请求选择核对。比较前核对实际效果开关、damage_observation 的技能/主体、XML、场景与目标。组合必须单列变体，不能相加单项收益。--input 仅观察已有角色，不建立交付认证。未继承旧操作/证据判断，不改输入或候选，不证明预算、路径可得或全局最优；选定后更新完整 plan 再 evaluate。"}


def export(args):
    run,state,outcome,xml,root=read_candidate(args)
    folder=run/"candidates"/args.name
    output=run/"output"/args.name
    check_path_roles([("source_plan",outcome.get("source_plan")),("original_xml",run/"original.xml"),("requested_xml",folder/"requested.xml"),("candidate_xml",folder/"candidate.xml"),("evaluation",folder/"evaluation.json")]+document_reads(outcome["plan"],Path(outcome["evidence_folder"])),
                     [("run_state",run/"run.json")]+[("delivery_"+name,output/name) for name in ("delivery.json","final-assessment.json","review.json","配装说明.html","配装草案.xml","改进配装.xml")])
    state.update(phase="exporting",selected_candidate=args.name,updated_at=now(),entrypoint=execution_entrypoint(),
                 last_operation={"command":"export","candidate":args.name,"file_exported":False,"status":"preparing"})
    write_json(run/"run.json",state)
    write_json(output/"delivery.json",{"file_exported":False,"status":"preparing","candidate":args.name})
    verification=engine.calculate(root,xml,cache=False)
    verification["xml_sha256"]=bx.digest(xml)
    bx.require(verification["engine_fingerprint"]==outcome["engine_fingerprint"],"最终复算期间引擎变化。")
    bx.require(engine.character_state(verification)==engine.character_state(outcome["result"]),"最终复算角色状态与候选记录不同，请重算。")
    if "measurement_audit" in outcome["result"]: verification["measurement_audit"]=outcome["result"]["measurement_audit"]
    for key in verification["stats"].keys()|outcome["result"]["stats"].keys():
        a,b=verification["stats"].get(key),outcome["result"]["stats"].get(key)
        equal=math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-5) if type(a) in (int,float) and type(b) in (int,float) else a==b
        bx.require(equal,"最终复算数值变化："+key)
    requested=bx.read_document(folder/"requested.xml")
    assessment=assess_candidate(bx.read_document(run/"original.xml"),requested,outcome["baseline"],verification,state["intent"],outcome["plan"],Path(outcome["evidence_folder"]),Path(state.get("intent_evidence_folder",outcome["evidence_folder"])),root=root)
    outcome.update(assessment)
    outcome["result"]={k:v for k,v in verification.items() if k!="xml"}
    outcome["next_actions"]=delivery_actions(outcome,outcome["source_plan"],output/"final-assessment.json",output/"review.json")
    filename="配装草案.xml" if outcome["status"]=="draft" else "改进配装.xml"
    if outcome["status"]=="blocked":
        response={"status":"blocked","file_exported":False,"delivery_brief":outcome["delivery_brief"],"files":[str(output/"配装说明.html")],"errors":outcome["errors"],"pending":outcome["pending"],"next_actions":outcome["next_actions"],"next":"已生成诊断说明；先解决已知矛盾并重新 evaluate。"}
    else:
        summary=outcome["publication"]["facts_summary"]+"\n\n"+scenario_summary(verification)+"\n已导出 XML，通过本机计算、保存回读与最终复算；以上是所列配置下的快照。"
        if outcome["comparison_notes"]: summary+="前后口径差异："+"；".join(outcome["comparison_notes"])
        response={"status":outcome["status"],"file_exported":True,"delivery_brief":outcome["delivery_brief"],"files":[str(output/filename),str(output/"配装说明.html")],"facts_summary":summary,"publication":outcome["publication"],
                  "facts":outcome["review_packet"]["facts"],"objective_coverage":outcome["objective_coverage"],
                  "pending":outcome["pending"],"budget_status":outcome["budget"]["status"],"warnings":outcome["warnings"],
                  "next_actions":outcome["next_actions"],
                  "reply_policy":"直接交付返回的文件，保留草案文件名；先沿用 delivery_brief/facts_summary 的真实状态和全部缺口类别，再解释 claims[].rendered_text 中有依据的取舍。最终聊天与HTML保持同一结论；不把复核token或回读当机制、预算或目标达成证明。"}
    outcome["response"]=response
    write_json(output/"final-assessment.json",outcome)
    write_json(output/"review.json",outcome["review_packet"])
    write_text(output/"配装说明.html",report_html(state,outcome))
    if outcome["status"]=="blocked":
        for old_name in ("配装草案.xml","改进配装.xml"):
            (output/old_name).unlink(missing_ok=True)
        write_json(output/"delivery.json",{"file_exported":False,"status":"blocked","candidate":args.name})
        state.update(phase="evaluated",updated_at=now(),last_operation={"command":"export","candidate":args.name,"file_exported":False,"status":"blocked"}); write_json(run/"run.json",state)
        return response
    write_text(output/filename,xml)
    (output/("改进配装.xml" if filename=="配装草案.xml" else "配装草案.xml")).unlink(missing_ok=True)
    write_json(output/"delivery.json",{"file_exported":True,"status":outcome["status"],"candidate":args.name,
               "files":{name:bx.digest((output/name).read_bytes()) for name in (filename,"配装说明.html")},"review_token":outcome["review_packet"]["token"]})
    state.update(phase="exported",selected_candidate=args.name,updated_at=now(),last_operation={"command":"export","candidate":args.name,"file_exported":True,"status":outcome["status"]})
    write_json(run/"run.json",state)
    return response

def native_queries(root,queries,xml="",class_id=None,equipment_layouts=None):
    bx.require(isinstance(queries,list) and queries,"queries 应为非空数组。")
    result=None
    rows=[]
    for offset in range(0,len(queries),30):
        chunk=engine.calculate(root,xml,queries=queries[offset:offset+30],class_id=class_id,**({"equipment_layouts":equipment_layouts} if offset==0 and equipment_layouts is not None else {}))
        if result is not None: bx.require(chunk["engine_fingerprint"]==result["engine_fingerprint"],"分批查询期间引擎变化，请重试。")
        else: result=chunk
        rows.extend(engine.ordered_batch('query',queries[offset:offset+30],chunk.get('catalog_batch')))
    result["catalog_batch"]=rows
    return result


def catalog(args):
    root=engine.resolve_root(args.pob)
    class_id=getattr(args,"class_id",None)
    bx.require(class_id is None or not args.input,"--class-id 不能与 --input 混用。")
    kinds={"gems","skills","nodes","uniques","config","links","classes","bases","affixes","rules","metrics"}
    document=bx.serialise(bx.read_document(Path(args.input))) if args.input else ""
    if getattr(args,"batch",None):
        queries=read_json(args.batch,role="input").get("queries")
        bx.require(isinstance(queries,list) and queries,"queries 应为非空数组。",location="/queries",expected="nonempty array",actual=queries)
        for index,query in enumerate(queries):
            location=f"/queries/{index}"
            bx.require_keys(query,{"kind","query","limit","offset"},location,"查询使用 kind/query/limit/offset；职业由全局 --class-id 或 --input 提供。")
            bx.require(query.get("kind") in kinds,"查询 kind 无效；translations 单独查询。",location=location+"/kind",expected=sorted(kinds),actual=query.get("kind"))
            for key,default,predicate,expected in (("query","",lambda v:isinstance(v,str),"string"),("limit",20,lambda v:type(v) is int and 1<=v<=100,"integer 1–100"),("offset",0,lambda v:type(v) is int and v>=0,"nonnegative integer")):
                bx.require(predicate(query.get(key,default)),"查询 "+key+" 无效。",location=location+"/"+key,expected=expected,actual=query.get(key,default))
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

def saved_catalog_summary(result, output, kind, query):
    """Expose actual saved locations and field names without flooding the tool UI."""
    batch="queries" in result
    queries=result["queries"] if batch else [{"kind":kind,"query":query,**result}]
    fields={}
    for row in queries:
        fields.setdefault(row["kind"],set()).update(key for entry in row["entries"] for key in entry)
    brief={"catalog_file":str(output),"queries_total":len(queries),"queries":[],
           "entry_fields":{key:sorted(value) for key,value in fields.items()},"summary_complete":True,
           "next":"使用 view 读取 catalog_file 与具体 entries_pointer，按 entry_fields 选择需要的实际字段；本机原文只用 data，任务JSON只用 view。"}
    if len(json.dumps(brief,ensure_ascii=False))>10000:
        brief["entry_fields"]={}
        brief["entry_fields_scope"]="字段名较多；用 view --keys 读取 entries_pointer 下具体条目的字段名。"
    for index,row in enumerate(queries):
        brief["queries"].append({"kind":row["kind"],"query":row["query"],"returned":len(row["entries"]),"total":row.get("total"),
                                 "entries_pointer":f"/queries/{index}/entries" if batch else "/entries"})
        if len(json.dumps(brief,ensure_ascii=False))>11000:
            brief["queries"].pop();break
    brief["summary_complete"]=len(brief["queries"])==len(queries)
    if not brief["summary_complete"]:
        brief["next_actions"]=[{"action":"view","file":str(output),"pointer":"/queries","fields":["kind","query","total","offset","next_offset"],
                                "offset":len(brief["queries"]),"limit":10}]
        brief["summary_scope"]="终端只显示部分查询元数据；完整查询和条目已保存，按 next_actions 继续。"
    return brief


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


def route_snapshots(route,native,root,folder,output,*,gems=None):
    """Materialize declared key stages through the same XML and native interfaces."""
    records={}
    for stage_index,stage in enumerate(route["stages"]):
        if "snapshot" not in stage: continue
        sid=stage["stage_id"]; snap=stage["snapshot"]
        record={"status":"not_calculated","assumptions":snap.get("assumptions",[]),"scope":"仅代表所声明装备、天赋、宝石与配置的条件快照，不是保证按时取得或安全通关。"}
        records[sid]=record
        try:
            if stage.get("level") is None:
                record["reason"]="未确认角色等级，不能生成精确实算。"; continue
            if snap.get("template_xml"):
                path=Path(snap["template_xml"])
                path=(path if path.is_absolute() else folder/path).resolve()
                record["input_artifacts"]={str(path):bx.digest(path.read_bytes())}
                source=bx.read_document(path)
            else:
                source=bx.parse(native["xml"])
            operations=copy.deepcopy(snap.get("operations",{}))
            configuration={"seed_basis":"template_xml" if snap.get("template_xml") else "native_empty_build",
                           "seed_inputs":[dict(row.attrib) for row in bx.active_config(source).findall("Input")],
                           "declared_overrides":copy.deepcopy(operations.get("config",{})),
                           "scope":"种子中的设置不代表本阶段场景已被选择；模型核对当前配置原文与 resolved_values，决定具体条件，未核实部分明示。null 删除覆盖并允许原生默认值，不等于数值0或关闭。"}
            op_path=f"/stages/{stage_index}/snapshot/operations"
            bx.require_keys(operations,routes.SNAPSHOT_OPERATION_FIELDS,op_path,"阶段等级和基础技能列表来自 stage；snapshot.operations 不能另设 level/replace_skills。")
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
            operations["replace_skills"]=[{"label":g["name"],"slot":g["slot"],"enabled":g.get("enabled",True),"gems":[{k:v[k] for k in ("skill_id","level","quality","enabled") if k in v} for v in g["gems"]]} for g in stage["skill_groups"]]
            plan={"mode":"rebuild","summary":"路线阶段条件快照","operations":operations}
            resolved,crafts=materialize_items(plan,root)
            document=bx.apply_plan(source,resolved,native=native)
            directory=stage_directory(output,sid)
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
                saved=roundtrip(root,calculated,document,resolved)
                calculated["xml_sha256"]=bx.digest(saved)
                write_text(directory/"snapshot.xml",saved)
                write_json(cache_path,{"request_sha256":bx.digest(requested),"result":calculated,
                                      "result_sha256":bx.digest(json.dumps(calculated,sort_keys=True,ensure_ascii=False,allow_nan=False))})
            limits={key:stage[key] for key in ("normal_points_available","ascendancy_points","secondary_ascendancy_points") if stage.get(key) is not None}
            tree_details,tree_repairs=native_tree_diagnostics(root,document,calculated,op_path+"/tree",tree_operations=snap.get("operations",{}).get("tree"),limits=limits,usage_plan=snap)
            gem_repairs=native_gem_diagnostics(root,document,calculated,stage_index=stage_index,gems=gems,limits=limits,usage_plan=snap)
            bx.require(all(Path(path).is_file() and bx.digest(Path(path).read_bytes())==digest for path,digest in record.get("input_artifacts",{}).items()),"计算期间阶段模板变化，请重算。",location=f"/stages/{stage_index}/snapshot/template_xml")
            record.update(status="calculated",xml_path=str(directory/"snapshot.xml"),xml_sha256=bx.digest(saved),engine_fingerprint=calculated["engine_fingerprint"],
                          result=calculated,document=document,directory=directory,crafted_items=crafts,
                          tree_diagnostics=tree_details,diagnostics=tree_repairs+gem_repairs,
                          configuration_context={**configuration,"resolved_values":calculated.get("config_values",{})})
        except (ValueError,KeyError,TypeError,IndexError,OSError) as exc:
            record.update(failure_record(exc,f"/stages/{stage_index}/snapshot"))
        finally:
            progress=read_json(output.with_suffix(".state.json"))
            write_json(output.with_suffix(".state.json"),{**progress,"phase":"calculating","file_exported":False,"output":str(output),
                       "engine_fingerprint":native["engine_fingerprint"],"stages":{sid:{k:v for k,v in row.items() if k in {"status","reason","xml_path","xml_sha256"}} for sid,row in records.items()},
                       "next":"处理具体错误后重跑同一 route 命令；已保存阶段按输入和摘要复用。"})
    return records


def route_command(args):
    """Keep failure and recovery state even when preparation itself is rejected."""
    bx.require(args.invoked,"仅在用户明确调用技能后规划路线。")
    path,output=Path(args.file).resolve(),Path(args.out).resolve()
    bx.require(output.suffix.lower()==".html" and output!=path,"--out 需要独立 HTML 文件。",location="/out",actual=str(output))
    root=engine.resolve_root(args.pob)
    state_path=output.with_suffix(".state.json")
    check_path_roles([("route_input",path),("edit_receipt",path.with_suffix(".edit.json"))],route_outputs(output,{}))
    route=read_json(path,role="input")
    check_path_roles([("route_input",path),("edit_receipt",path.with_suffix(".edit.json"))]+document_reads(route,path.parent),route_outputs(output,route))
    initial={"schema_version":SCHEMA_VERSION,"skill_version":VERSION,"entrypoint":execution_entrypoint(),"phase":"preparing","file_exported":False,"input":str(path),"output":str(output),
             "input_sha256":bx.digest(path.read_bytes()) if path.is_file() else None,"pob_path":str(root),"engine_fingerprint":engine.fingerprint(root)}
    if state_path.is_file():
        try:
            old=read_json(state_path)
            if old.get("input")==str(path) and old.get("input_sha256")==initial["input_sha256"] and old.get("engine_fingerprint")==initial["engine_fingerprint"]: initial["stages"]=old.get("stages",{})
        except (OSError,ValueError): pass
    write_json(state_path,initial)
    selected_args=copy.copy(args);selected_args.pob=str(root)
    try: return calculate_route(selected_args)
    except (ValueError,OSError,KeyError,TypeError,IndexError,OverflowError) as exc:
        record=failure_record(exc,input_file=path)
        try: current=read_json(state_path)
        except (OSError,ValueError): current=initial
        current.update(phase="failed",file_exported=False,status=record["status"],failure=record,updated_at=now(),
                       next_actions=[{"action":record.get("diagnostic",{}).get("action","inspect_error"),"input_file":str(path),"output":str(output),"diagnostic":record.get("diagnostic"),"state_file":str(state_path)}])
        try: write_json(state_path,current)
        except OSError: pass  # Preserve the original failure if the output disk is unavailable.
        raise


def calculate_route(args):
    bx.require(args.invoked,"仅在用户明确调用技能后规划路线。")
    path=Path(args.file).resolve(); output=Path(args.out).resolve()
    bx.require(output.suffix.lower()==".html" and output!=path,"--out 需要独立 HTML 文件。")
    input_sha256=bx.digest(path.read_bytes())
    route=read_json(path,role="input")
    routes.preflight(route)
    root=engine.resolve_root(args.pob)
    ids=routes.gem_ids(route)
    queries=[{"kind":"gems","query":"id:"+v,"limit":2} for v in ids]
    queries += [{"kind":"links","query":v,"limit":1} for v in routes.link_queries(route)]
    queries += [{"kind":"bases","query":"id:"+v,"limit":2} for v in routes.base_names(route)]
    layouts=[{"layout_id":s["stage_id"],"location":f"/stages/{index}","bases":{k:v for k,v in s.get("bases",{}).items() if v is not None},"empty_slots":[k for k,v in s.get("bases",{}).items() if v is None]} for index,s in enumerate(route["stages"]) if "snapshot" not in s]
    calculation=native_queries(root,queries,class_id=route.get("class_id"),equipment_layouts=layouts)
    equipment={row["layout_id"]:row for row in calculation.get("equipment_layouts",[])}
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
    claims=facts.assess_claims(route.get("claims",[]),registry,{"catalog":calculation},specification=route,engine_fingerprint=calculation["engine_fingerprint"])
    checked,warnings=routes.validate(route,gems,calculation.get("data_version"),calculation["classes"],links,nodes,bases=bases,slots=calculation["slots"],equipment=equipment,evidence=registry,claims=claims)
    snapshots=route_snapshots(checked,calculation,root,path.parent,output,gems=gems)
    for stage in checked["stages"]:
        value=snapshots.get(stage["stage_id"],{}).get("result")
        if value:
            checks={item["slot"]:{"base":item["base"],"valid_for_slot":item.get("valid_for_slot")} for item in value["equipped"]}
            for slot,base in stage.get("bases",{}).items():
                if base is None: checks[slot]={"base":None,"valid_for_slot":slot not in checks,"status":"observed_empty" if slot not in checks else "occupied"}
            equipment[stage["stage_id"]]={"bases":stage.get("bases",{}),"checks":checks,
                                          "context":{"basis":"snapshot_xml","xml_sha256":value.get("xml_sha256"),"tree":value.get("tree_context"),"scope":"Actual native equipment and tree in this stage XML; not proof of availability."}}
    stage_native={"catalog":calculation,**{"stage:"+sid:r["result"] for sid,r in snapshots.items() if "result" in r}}
    if snapshots:
        claims=facts.assess_claims(route.get("claims",[]),registry,stage_native,specification=route,engine_fingerprint=calculation["engine_fingerprint"])
        checked,warnings=routes.validate(route,gems,calculation.get("data_version"),calculation["classes"],links,nodes,bases=bases,slots=calculation["slots"],equipment=equipment,evidence=registry,claims=claims)
    validation_pending=set(checked["pending"])
    route_errors=[]
    stage_materials={}
    for stage in checked["stages"]:
        sid=stage["stage_id"]
        for item in stage.get("required_items",[]): facts.add_material(stage_materials,item)
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
        limits={key:stage[key] for key in ("normal_points_available","ascendancy_points","secondary_ascendancy_points") if stage.get(key) is not None}
        audit=resources.assess(value,limits,{k:stage["snapshot"][k] for k in ("usage","timeline") if k in stage["snapshot"]},relevant)
        stage_native["resources:stage:"+sid]={**audit,"engine_fingerprint":value["engine_fingerprint"],"xml_sha256":value.get("xml_sha256"),"origin":"program_assessment"}
        errors+=audit["errors"]; notes+=audit["warnings"]; gaps+=audit["pending"]
        texts=bx.item_texts(document)
        material_specs=[]
        equipped_quantities=Counter()
        for item in value["equipped"]:
            spec=bx.digest(texts.get(item["id"],""))
            material={"kind":"item","item_id":item["id"],"slot":item["slot"],"name":item["name"],"spec_sha256":spec,"quantity":1}
            ref=facts.material_ref(material)
            material_specs.append({**material,"requirement_id":ref})
            equipped_quantities[ref]+=1
            if stage_materials.get(ref,{}).get('quantity',0)<equipped_quantities[ref]: gaps.append("material:"+ref)
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
            facts.add_material(requirements,item)
    # A route's explicit held-instance records are already inventory declarations.
    # Reuse their evidence instead of making the model duplicate the same input.
    owned={}
    for stage in checked["stages"]:
        for item in stage.get("required_items",[]):
            if item.get("evidence_status")!="user_declared": continue
            facts.add_material(owned,{k:v for k,v in item.items() if k in {"kind","skill_id","level","quality","item_id","service_id","spec_sha256","quantity","evidence_refs"}})
    declared={facts.material_ref(item):item for item in route.get("inventory",[])}
    budget=budget_summary({**route,"inventory":list({**owned,**declared}.values())},route,list(requirements.values()),registry,claims)
    checked["budget_assessment"]=budget
    stage_native["budget"]={**budget,"engine_fingerprint":calculation["engine_fingerprint"],"origin":"program_assessment"}
    checked["reference_contract"]=facts.reference_contract(stage_native)
    claims=facts.assess_claims(route.get("claims",[]),registry,stage_native,specification=route,engine_fingerprint=calculation["engine_fingerprint"])
    final_checked,final_warnings=routes.validate(route,gems,calculation.get("data_version"),calculation["classes"],links,nodes,bases=bases,slots=calculation["slots"],equipment=equipment,evidence=registry,claims=claims)
    checked["pending"]=[p for p in checked["pending"] if p not in validation_pending]+final_checked["pending"]
    checked["claims"]=claims;checked["objective_coverage"]=final_checked["objective_coverage"];checked["diagnostics"]=final_checked["diagnostics"]
    for index,stage in enumerate(checked["stages"]):
        stage_calculation=stage.get("calculation",{})
        rows=stage_calculation.get("diagnostics",[])+stage_calculation.get("readiness",{}).get("diagnostics",[])
        if stage_calculation.get("diagnostic"): rows.append(stage_calculation["diagnostic"])
        for row in rows:
            diagnostic={**row,"stage_id":stage["stage_id"]}
            if row.get("location","").startswith(("/timeline","/usage")):
                diagnostic["location"]=f"/stages/{index}/snapshot"+row["location"]
            checked["diagnostics"].append(diagnostic)
    for old,final in zip(checked["stages"],final_checked["stages"]): old["instance_transitions"]=final["instance_transitions"]
    checked["pending"] += ["claim_check:"+c["claim_id"] for c in claims if c["check_status"] in {"refuted","unknown"}]
    checked["pending"] += ["claim_revision:"+c["claim_id"] for c in claims if any(d["code"] in {"claim_binding_stale","claim_binding_missing"} for d in c["diagnostics"])]
    scene_diagnostics=facts.configuration_diagnostics(claims,{"stage:"+sid:stage_native["stage:"+sid] for sid in snapshots if "stage:"+sid in stage_native})
    checked["diagnostics"]+=scene_diagnostics
    checked["pending"] += ["config:scenario:"+d["required_subject"][len("configuration:stage:"):] for d in scene_diagnostics]
    warnings=list(dict.fromkeys(warnings+final_warnings))
    if route.get("budget") is not None:
        if budget["status"] in {"unknown","uncertain_range"} or budget["high"]>route["budget"]["amount"]: checked["pending"].append("budget")
    warnings+=budget["warnings"]
    checked["pending"]=list(dict.fromkeys(checked["pending"]+["material:"+ref for ref in budget["unresolved"]]))
    if budget["over_budget"]: route_errors.append("已知采购成本下界超过用户预算。")
    review,review_pending=facts.final_review(route,registry,claims,
        {"engine_fingerprint":calculation["engine_fingerprint"],"reference_contract":checked["reference_contract"],"budget":budget,"pending":checked["pending"],"errors":route_errors,"objective_coverage":checked["objective_coverage"],
         "stages":[{"stage_id":s["stage_id"],"level":s.get("level"),"act":s.get("act"),"completed_act":s.get("completed_act"),
                    "entry_conditions":s.get("entry_conditions",[]),"calculation":s.get("calculation",{"status":"not_calculated"})} for s in checked["stages"]]},route.get("assessment",{}))
    checked["pending"]+=review_pending
    checked["final_review"]={k:review[k] for k in ("token","status","findings","conclusions","objectives","coverage","scope")}
    checked["errors"]=list(dict.fromkeys(route_errors))
    checked["delivery_status"]="blocked" if route_errors else "draft" if checked["pending"] else "scoped_result"
    checked["delivery_brief"]=facts.delivery_brief(checked["delivery_status"],checked["errors"],checked["pending"])
    checked["publication"]=publication(checked["delivery_status"],checked["errors"],checked["pending"],budget,
                                       {"stage:"+s["stage_id"]:s["calculation"]["facts"] for s in checked["stages"] if s.get("calculation",{}).get("facts")},resource_audits={"stage:"+s["stage_id"]:s["calculation"]["readiness"] for s in checked["stages"] if s.get("calculation",{}).get("readiness")},claims=claims,keys=["Life","Armour","EnergyShield","CombinedDPS","ManaUnreserved","LifeRegenRecovery","ManaRegenRecovery","FireResist","ColdResist","LightningResist","ChaosResist"])
    stage_summary=[{"stage_id":s["stage_id"],"name":s["name"],"status":s.get("calculation",{}).get("status","not_calculated"),
                    "native_calculated":s.get("calculation",{}).get("native_calculated",False),
                    "errors_total":len(s.get("calculation",{}).get("errors",[])),"pending_total":len(s.get("calculation",{}).get("pending",[])),
                    **{k:v for k,v in s.get("calculation",{}).items() if k in {"xml_path","xml_sha256","diagnostic","reason"}}} for s in checked["stages"]]
    bx.require(engine.fingerprint(root)==calculation["engine_fingerprint"],"路线核对期间引擎变化，旧阶段缓存自动失效；待更新完成后重试同一 route 命令。")
    bx.require(bx.digest(path.read_bytes())==input_sha256,"核对期间源路线变化，请重跑 route。",location="/input",input_file=str(path))
    bx.require(input_artifacts_current(checked),"核对期间阶段模板变化，请重跑 route。",location="/stages")
    evidence_file=output.with_suffix(".evidence.json")
    review_file=output.with_suffix(".review.json")
    delivered_xmls=[Path(row['xml_path']) for row in stage_summary if row.get('xml_path')]
    actions=delivery_actions({k:checked[k] for k in ("errors","pending","diagnostics","reference_contract")}|{"status":checked["delivery_status"]},path,evidence_file,review_file,"/checked_route")
    response={"status":checked["delivery_status"],"file_exported":True,"delivery_brief":checked["delivery_brief"],"facts_summary":checked["publication"]["facts_summary"],"errors":checked["errors"],"stage_summary":stage_summary,"files":[str(output),str(evidence_file)]+[str(p) for p in delivered_xmls],"review_file":str(review_file),"review_token":review["token"],"stages":len(checked["stages"]),"pending":checked["pending"],"warnings":warnings,
             "next_actions":actions,"scope":"目录核对与实际阶段快照分别列出；仅 calculation 中的数值经过所声明完整输入实算，未物化阶段不宣称精确 DPS/生存/续航。获取位置不证明当前已完成任务。", "next":"按 next_actions 修订再重跑同一route；完成计算不等于输入合法或阶段就绪。诊断XML不能作为可执行配装交付，剩余真实缺口按delivery_brief明示为条件草案。"}
    write_json(review_file,review)
    write_json(evidence_file,{"schema_version":SCHEMA_VERSION,"skill_version":VERSION,"engine_fingerprint":calculation["engine_fingerprint"],"checked_route":checked,"warnings":warnings,"next_actions":actions,"response":response})
    write_text(output,routes.render(checked,warnings,calculation["engine_version"],calculation["tree_version"],evidence_file=evidence_file.name))
    progress=read_json(output.with_suffix(".state.json"))
    write_json(output.with_suffix(".state.json"),{**progress,"phase":"exported","file_exported":True,"status":checked["delivery_status"],"input":str(path),"input_sha256":input_sha256,"output":str(output),"engine_fingerprint":calculation["engine_fingerprint"],
               "input_artifacts":{filename:digest for stage in checked["stages"] for filename,digest in stage.get("calculation",{}).get("input_artifacts",{}).items()},
               "files":{str(p):bx.digest(p.read_bytes()) for p in [output,evidence_file,review_file]+delivered_xmls},"review_token":review["token"],
               "stages":{row["stage_id"]:{k:v for k,v in row.items() if k not in {"stage_id","name"}} for row in stage_summary if row["status"]!="not_calculated"},
               "delivery_brief":checked["delivery_brief"],"next_actions":actions})
    return response

def route_artifacts(state, location):
    """Validate every declared delivery, including deterministic stage folders."""
    output=Path(state['output']).resolve()
    allowed={output,output.with_suffix('.evidence.json'),output.with_suffix('.review.json')}
    files=dict(state.get('files',{}))
    invalid=[]
    for sid,row in state.get('stages',{}).items():
        if not row.get('xml_path'): continue
        path=Path(row['xml_path']).resolve()
        try: expected=(stage_directory(output,sid)/'snapshot.xml').resolve()
        except bx.ContractError:
            invalid.append(str(path))
            continue
        allowed.add(expected)
        digest=row.get('xml_sha256')
        if path!=expected or not digest or str(path) in files and files[str(path)]!=digest: invalid.append(str(path))
        files[str(path)]=digest
    for name,digest in files.items():
        path=Path(name).resolve()
        if (output.parent!=location.parent or path not in allowed or not digest or not path.is_file()
                or bx.digest(path.read_bytes())!=digest): invalid.append(str(path))
    return bool(files) and not invalid,list(dict.fromkeys(invalid))


def candidate_recovery(run,state,name):
    folder=run/'candidates'/name
    context={'details_file':str(folder/'evaluation.json'),'review_file':str(folder/'review.json')}
    source=state['candidates'][name].get('source_plan')
    if source: context['plan_file']=str(source)
    try:
        if (folder/'evaluation.json').stat().st_size<=JSON_LIMITS['artifact']:
            raw=(folder/'evaluation.json').read_bytes()
            if bx.digest(raw)==state['candidates'][name].get('evaluation_sha256'):
                context['plan_file']=str(Path(json.loads(raw)['source_plan']))
    except (OSError,ValueError,KeyError,TypeError): pass
    if context.get('plan_file'):
        try:
            source=Path(context['plan_file'])
            context['source_exists']=source.is_file()
            context['input_revision']=bx.digest(source.read_bytes()) if source.is_file() and source.stat().st_size<=JSON_LIMITS['input'] else None
        except OSError: context['input_revision']=None
    return context


def workflow_status(args):
    """Read integrity and next operations without repeating native calculation."""
    route_state=getattr(args,"route_state",None)
    if route_state:
        location=Path(route_state).resolve()
        state=read_json(location)
        source=Path(state["input"])
        unchanged=source.is_file() and bx.digest(source.read_bytes())==state.get("input_sha256")
        intact,invalid=route_artifacts(state,location)
        root=engine.resolve_root(args.pob or state.get('pob_path'))
        current=engine.fingerprint(root)==state.get("engine_fingerprint")
        dependencies_current=input_artifacts_current(state)
        ready=unchanged and intact and current and dependencies_current and state.get("file_exported") is True
        return {"entrypoint":execution_entrypoint(),"phase":state["phase"],"status":state.get("status"),"failure":state.get("failure"),"delivery_brief":state.get("delivery_brief"),"file_exported":ready,"input_unchanged":unchanged,"files_intact":intact,"engine_unchanged":current,"input_artifacts_unchanged":dependencies_current,
                "state_file":str(location),"completed_stages":[sid for sid,row in state.get("stages",{}).items() if unchanged and current and dependencies_current and (row.get("native_calculated") or row.get("status")=="calculated")],
                "stage_summary":state.get("stages",{}),"invalid_artifacts":invalid,"pob_path":str(root),
                "next_actions":state.get("next_actions",[]) if state.get("next_actions") and (ready or state["phase"]=="failed") else [{"action":"resume_route" if not ready else "resolve_route" if state.get("status") in {"blocked","draft"} else "review_delivery","input":str(source),"output":state.get("output"),"pob_path":str(root),"invalid_artifacts":invalid,"reference":"references/leveling.md"}],
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
            candidate_actions=delivery_actions(outcome,outcome["source_plan"],candidate["details_file"],candidate["review_file"])
            actions.extend([{**action,"candidate":name} for action in candidate_actions] or [{"action":"review_delivery" if complete else "export","candidate":name,"details_file":candidate["details_file"]}])
        except (ValueError,OSError,KeyError) as exc:
            context=candidate_recovery(run,state,name)
            record=failure_record(exc)
            if 'diagnostic' not in record:
                record['diagnostic']={'code':'io_error' if isinstance(exc,OSError) else 'candidate_invalid','action':'reevaluate','retryable':False,
                                      'location':'/candidates/'+name,'expected':'current candidate artifacts','actual':str(exc)}
            candidate.update(valid=False,**context,**record)
            actions.append({"action":"reevaluate","candidate":name,"run":str(run),"pob_path":state['pob_path'],**context,**record,"reference":"references/build.md"})
        records.append(candidate)
    return {"entrypoint":execution_entrypoint(),"run":str(run),"phase":state["phase"],"last_operation":state.get("last_operation"),"pob_path":state["pob_path"],"skill_version":VERSION,"intent":state.get("intent"),
            "candidates":[r["name"] for r in records],"candidate_records":records,"next_actions":actions}


def parser():
    p = argparse.ArgumentParser(description="POE 配装助手：本机 PoB 实算、候选校验和 XML 导出。")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "inspect", "catalog", "data", "research", "decode", "start", "intent", "evaluate", "probe", "export", "status", "route", "view", "edit"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--pob", help="PoECharm 文件夹、PoB 文件夹或可执行文件；默认自动发现")
        if name in {"inspect", "decode", "start"}: cmd.add_argument("--input", required=True)
        if name in {"start", "intent", "evaluate", "export"}: cmd.add_argument("--run", required=True)
        if name=='probe':
            source=cmd.add_mutually_exclusive_group(required=True)
            source.add_argument('--run',help='已 evaluate 的候选会话，配合 --name')
            source.add_argument('--input',help='已有角色 XML 的只读变体观察，不建立候选或交付认证')
        if name=="status":
            context=cmd.add_mutually_exclusive_group(required=True)
            context.add_argument("--run")
            context.add_argument("--route-state",help="route 输出的 .state.json；只读核对交付和可恢复阶段")
        if name == "start": cmd.add_argument("--invoked", action="store_true")
        if name == "intent": cmd.add_argument("--file", required=True)
        if name=="edit":
            cmd.add_argument("--invoked",action="store_true")
            cmd.add_argument("--kind",choices=["plan","route"],required=True)
            cmd.add_argument("--file",required=True,help="唯一源方案 JSON；不存在时创建草案")
            change=cmd.add_mutually_exclusive_group(required=True)
            change.add_argument("--changes",help="set 增量记录、replace 精确字段/数组、remove 路径、review_claims 重新评审标识")
            change.add_argument("--proposal-file",help="evaluate/route 返回的完整计算凭证；程序读取其中的提案修改")
            cmd.add_argument("--proposal-id",help="本次 next_actions 返回的原生候选标识；无需抄写修改内容")
            cmd.add_argument("--if-sha256",help="已有文件需使用上次返回的 revision；同一请求重试自动去重")
        if name=="view":
            cmd.add_argument("--file",required=True,help="获准任务工作区中已保存的JSON")
            cmd.add_argument("--pointer",default="",help="JSON pointer，默认顶层对象")
            cmd.add_argument("--fields",default="",help="所选对象的直接字段，逗号分隔；不写表达式")
            cmd.add_argument("--keys",action="store_true",help="列出当前对象字段名，支持分页")
            cmd.add_argument("--query",default="",help="筛选 keys 字段名或文本数组，忽略大小写")
            cmd.add_argument("--limit",type=int,default=10)
            cmd.add_argument("--offset",type=int,default=0)
        if name in {"evaluate", "probe", "export"}: cmd.add_argument("--name", required=name!='probe')
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


def bound_reply(value, args):
    """Bound the complete envelope; every omitted field remains on disk."""
    size=lambda row:len(json.dumps({"ok":True,**row},ensure_ascii=False,allow_nan=False))+1
    if size(value)<=12000: return value
    prefix="/response"
    if args.command=="edit": details=Path(value["receipt_file"]);prefix=""
    elif args.command=="route": details=Path(args.out).resolve().with_suffix(".evidence.json")
    elif args.command in {"evaluate","export"}:
        details=Path(args.run).resolve()/("candidates" if args.command=="evaluate" else "output")/args.name/("evaluation.json" if args.command=="evaluate" else "final-assessment.json")
    else:
        bx.require(False,"完整返回过大；缩小查询或先保存 --out 后用 view 分页。",code="response_too_large",location="/out",expected="bounded response or saved artifact",actual={"characters":size(value),"command":args.command})
    bx.require(details.is_file(),"完整返回凭证不存在，不能截断后交付。",code="response_artifact_missing",location="/out",expected="saved full response",actual=str(details))
    saved=read_json(details)
    result=dict(value)
    result.update(details_file=str(details),output_scope="终端摘要；continuations 指向完整字段，按 view 分页，无须重算。",continuations=[])
    if args.command=="edit":
        result["next_actions"]=[{"action":"view","file":str(details),"pointer":"/handles","keys":True}]+([{"action":"view","file":str(details),"pointer":"/stage_instances","limit":10}] if result["kind"]=="route" else [])+[{"action":"view","file":str(details),"pointer":"/input_contract"}]
    protected={"entrypoint","runtime","status","file_exported","kind","file","revision","receipt_file","details_file","output_scope","continuations"}
    trimmed=set()
    while size(result)>11200:
        options=[key for key in result if key not in protected and key not in trimmed]
        bx.require(options,"返回元数据过大。",code="response_too_large",location="/",expected="bounded response metadata",actual={"characters":size(result)})
        key=max(options,key=lambda k:len(json.dumps(result[k],ensure_ascii=False)))
        pointer=prefix+"/"+key.replace("~","~0").replace("/","~1")
        target=facts.pointer(saved,pointer)
        if isinstance(target,(list,dict,str)): result[key+"_total"]=len(target)
        result[key]=target[:160] if isinstance(target,str) else [] if isinstance(target,list) else {} if isinstance(target,dict) else target
        result["continuations"].append({"action":"view","file":str(details),"pointer":pointer,"offset":0,"limit":20})
        trimmed.add(key);protected.add(key+"_total")
    return result


def main(argv=None):
    started=time.perf_counter()
    engine.PERFORMANCE.update(requests=0,worker_calls=0,cache_hits=0,worker_seconds=0.0)
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = {"skill_version": VERSION,**engine.health(engine.resolve_root(args.pob)),"delivery_statuses":facts.delivery_brief("draft",[],[])["valid_statuses"]}
            result["capabilities"]={**result["capabilities"],**resources.capabilities()}
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
        elif args.command == "edit": result = edit_document(args)
        elif args.command == "evaluate": result = evaluate(args)
        elif args.command == "probe":
            result = probe(args)
            if args.out:
                output=Path(args.out).resolve()
                bx.require(output.suffix.lower()==".json" and (not args.run or not output.is_relative_to(Path(args.run).resolve())) and all(output!=Path(value).resolve() for value in (args.input,args.variants) if value),"probe --out 使用会话目录外的独立 JSON，不能覆盖角色、候选或变体输入。")
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
                fields=([key for key in ("source","line","count","text","evidence_template","scope") if key in result] if args.mode=="read" else
                        sorted({key for row in result["entries"] for key in row}))
                result={"data_file":str(output),"mode":args.mode,"total":result.get("total",result.get("total_lines")),"next_offset":result.get("next_offset"),"next_line":result.get("next_line"),"read_only":True,
                        "next_actions":[{"action":"view","file":str(output),"pointer":"" if args.mode=="read" else "/entries","fields":fields,"offset":0,"limit":20}],
                        "next":"用 view 读取工作区凭证；继续取本机原文沿用 data 的 source 与行号，安装目录保持只读接口访问。"}
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
                result = saved_catalog_summary(result,output,args.kind,args.query)
            else:
                bx.require(len(json.dumps(result, ensure_ascii=False)) <= 12000, "查询结果较大；请加 --out '<工作区>/查询结果.json' 保存完整结果，再按相关字段读取，避免 TeleAgent 截断或转存至额外授权目录。")
        elif args.command == "route": result = route_command(args)
        elif args.command == "view": result=view_json(args)
        else: result=workflow_status(args)
        result["entrypoint"]=execution_entrypoint()
        result["runtime"]={**engine.PERFORMANCE,"elapsed_seconds":round(time.perf_counter()-started,4)}
        result=bound_reply(result,args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError, OverflowError) as exc:
        diagnostic=dict(exc.diagnostic) if getattr(exc,"diagnostic",None) else {"code":"io_error" if isinstance(exc,OSError) else "adapter_contract_failure" if isinstance(exc,(KeyError,TypeError,IndexError)) else "invalid_input","message":str(exc),"action":"inspect_error" if isinstance(exc,(KeyError,TypeError,IndexError)) else "check_io" if isinstance(exc,OSError) else "edit_input","retryable":isinstance(exc,OSError) and transient_io(exc),"location":"/","expected":"valid input and available interface","actual":type(exc).__name__}
        if diagnostic["code"]=="input_revision_stale": diagnostic["action"]="read_current_revision"
        source=getattr(args,"plan",None) or getattr(args,"file",None) or getattr(args,"input",None)
        if source:
            path=Path(source).resolve();diagnostic.setdefault("input_file",str(path))
            try:
                if path.is_file() and path.stat().st_size<=JSON_LIMITS["artifact"]: diagnostic["input_revision"]=bx.digest(path.read_bytes())
            except OSError: pass
        if isinstance(exc,json.JSONDecodeError): diagnostic.update(line=exc.lineno,column=exc.colno,action="edit_input")
        try: record_command_failure(args,exc)
        except (ValueError,OSError,KeyError) as state_error: diagnostic["state_save_error"]=str(state_error)
        if args.command=="route": diagnostic["state_file"]=str(Path(args.out).resolve().with_suffix(".state.json"))
        for key,value in list(diagnostic.items()):
            if len(json.dumps(value,ensure_ascii=False,default=str))>2000:
                diagnostic[key]={"type":type(value).__name__,"preview":str(value)[:500],"truncated":True}
        print(json.dumps({"ok": False, "error": str(exc)[:2000],"diagnostic":diagnostic,"entrypoint":execution_entrypoint()}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
