"""Small, source-backed facts for planning; no bundled seasonal gem database.

Fetches public pages with the standard library. Cached pages stay in the user's
task folder, expire after a day, and are invalidated by the engine fingerprint.
Recognised tables are evidence, not proof of CN parity or of an entire guide.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import operator
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import buildxml as bx
import engine

HOSTS = {"poedb.tw", "poe.qq.com", "www.pathofexile.com", "pathofexile.com"}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}: self.skip += 1
        if tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5"}: self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}: self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5"}: self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


class ArticleText(PageText):
    """Tencent's article container, excluding menus and related-news headings."""
    def __init__(self):
        super().__init__()
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "div" and (self.depth or dict(attrs).get("id") == "newsDetail"):
            self.depth += 1
        if self.depth: super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            super().handle_endtag(tag)
            if tag == "div": self.depth -= 1

    def handle_data(self, data):
        if self.depth: super().handle_data(data)


def text_of(raw, *, article=False):
    parser = ArticleText() if article else PageText()
    parser.feed(raw)
    return "\n".join(line for part in "".join(parser.parts).splitlines() if (line := " ".join(part.split())))


def fetch(url, folder, context):
    """Return bytes-derived evidence; never trust a caller's checked/verified flag."""
    parsed = urlsplit(url)
    bx.require(parsed.scheme == "https" and parsed.hostname in HOSTS and not parsed.username and parsed.port in (None, 443),
               "自动事实抓取只支持所列公开数据/官方站点的 HTTPS 页面；其他资料由联网工具查阅并标注人工核对。")
    folder = Path(folder)
    import engine
    engine.check_output_path(folder / "source.json")
    key = hashlib.sha256(json.dumps({"url":url,"context":context},sort_keys=True,ensure_ascii=False,allow_nan=False).encode("utf-8")).hexdigest()
    path = folder / (key + ".json")
    stamp = datetime.now(timezone.utc)
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            age = stamp - datetime.fromisoformat(record["checked_at"])
            if (record["url"] == url and record["context"] == context and timedelta(0) <= age <= timedelta(days=1)
                    and hashlib.sha256(record["html"].encode()).hexdigest() == record["sha256"]):
                return record
        except (ValueError, KeyError, TypeError):
            pass
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    # One retry for transient transport failures; permanent HTTP errors remain visible.
    for attempt in range(2):
        try:
            response = urlopen(request, timeout=15)
            break
        except HTTPError as exc:
            if attempt or exc.code not in {408, 429, 500, 502, 503, 504}: raise
        except (URLError, TimeoutError):
            if attempt: raise
    with response:
        bx.require(urlsplit(response.url).hostname in HOSTS, "事实来源重定向到未识别站点，需人工核对。")
        body = response.read(2_000_001)
        bx.require(len(body) <= 2_000_000, "事实页面超过读取上限，请用联网工具读取相关章节。")
        encoding = response.headers.get_content_charset()
        if not encoding:
            match = re.search(rb'charset\s*=\s*["\']?([\w-]+)', body[:5000], re.I)
            encoding = match[1].decode("ascii") if match else "utf-8"
        raw = body.decode(encoding, errors="replace")
    record = {"url": url, "checked_at": stamp.isoformat(timespec="seconds"), "context": context,
              "sha256": hashlib.sha256(raw.encode()).hexdigest(), "html": raw}
    folder.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return record


def source_record(page, **extra):
    return {key: page[key] for key in ("url", "checked_at", "sha256")} | extra


def web_template(page, excerpt):
    """Expose a real source span as input, without assigning a conclusion or ID."""
    body = text_of(page["html"], article=urlsplit(page["url"]).hostname == "poe.qq.com")
    if isinstance(excerpt, str) and excerpt.strip() and excerpt in body:
        return {"kind": "web", "url": page["url"], "excerpt": excerpt, "checked_at": page["checked_at"]}
    return None


def pointer(value, locator):
    """Resolve a JSON pointer as data, without evaluating model expressions."""
    bx.require(isinstance(locator, str) and (not locator or locator.startswith("/")), "locator 使用 JSON pointer，例如 /stats/Life。")
    tokens=locator.split("/")[1:]
    for index,token in enumerate(tokens):
        token = token.replace("~1", "/").replace("~0", "~")
        parent="/"+"/".join(tokens[:index]) if index else ""
        if isinstance(value, list):
            bx.require(token.isdigit() and int(token) < len(value), "证据数组索引不存在：" + locator,
                       location=parent,expected={"type":"array","length":len(value)},actual=token)
            value = value[int(token)]
        else:
            bx.require(isinstance(value, dict) and token in value, "证据字段不存在：" + locator,
                       location=parent,expected={"type":"object","fields":sorted(value)[:100],"total_fields":len(value)} if isinstance(value,dict) else {"type":"object","actual_type":type(value).__name__},actual=token)
            value = value[token]
    return value


def evidence_shape(declarations, *, complete=True):
    """Shared cheap input validation; never fetch sources while editing a draft."""
    fields={"evidence_id", "kind", "subject", "text", "url", "path", "excerpt", "context", "checked_at", "message_ref", "source", "line", "count", "content_sha256"}
    kinds={"user_statement", "web", "artifact", "local_data"}
    bx.require(isinstance(declarations,list),"evidence 应为数组。",location="/evidence",expected="array",actual=type(declarations).__name__)
    seen=set()
    for index,entry in enumerate(declarations):
        location=f"/evidence/{index}"
        bx.require_keys(entry,fields,location,"证据字段无效；status/digest/origin 由程序生成。")
        for key in sorted(fields-{"context","line","count"}):
            if key in entry:
                bx.require(isinstance(entry[key],str),key+" 应为字符串。",location=location+"/"+key,expected="string",actual=entry[key])
        for key in ("evidence_id","kind","subject"):
            if complete or key in entry:
                bx.require(isinstance(entry.get(key),str) and entry[key].strip(),"证据需要具体 "+key,location=location+"/"+key,expected="nonempty string",actual=entry.get(key))
        if "kind" in entry: bx.require(entry["kind"] in kinds,"证据 kind 无效。",location=location+"/kind",expected=sorted(kinds),actual=entry["kind"])
        if "evidence_id" in entry:
            key=entry["evidence_id"]
            bx.require(key not in seen and "#" not in key and not key.startswith("native:"),"evidence_id 需要唯一普通标识。",location=location+"/evidence_id",expected="unique identifier without # or native: prefix",actual=key)
            seen.add(key)
        if "context" in entry: bx.require(isinstance(entry["context"],dict),"证据 context 应为对象。",location=location+"/context",expected="object",actual=entry["context"])
        for key in ("line","count"):
            if key in entry: bx.require(type(entry[key]) is int and entry[key]>0,key+" 应为正整数。",location=location+"/"+key,expected="positive integer",actual=entry[key])
        bx.require(isinstance(entry.get("evidence_id"),str) and entry["evidence_id"].strip(),"证据需要稳定 evidence_id。",location=location+"/evidence_id",expected="nonempty identifier",actual=entry.get("evidence_id"))


def load_evidence(declarations, folder, context, *, root=None):
    """Observe source artifacts; caller-written verification flags are not inputs."""
    evidence_shape(declarations)
    base = Path(folder).resolve()
    result = {}
    for index,entry in enumerate(declarations):
        location=f"/evidence/{index}"
        key, kind = entry.get("evidence_id"), entry.get("kind")
        bx.require(kind == "local_data" or not set(entry) & {"source", "line", "count", "content_sha256"}, "source/line/count/content_sha256 仅用于 local_data。",location=location,expected="local_data for source fields",actual=kind)
        record = {"evidence_id": key, "kind": kind, "subject": entry["subject"], "context": entry.get("context", {}), "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if entry.get("checked_at"):
            import engine
            engine.validate_checked_at(entry["checked_at"])
        try:
            if kind == "user_statement":
                text = entry.get("text")
                bx.require(isinstance(text, str) and text.strip(), "用户声明需要实际表达原文。")
                record.update(text=text, status="user_declared", origin="agent_recorded_user_statement", message_ref=entry.get("message_ref"))
                record["scope"] = "记录用户表达；本程序未独立读取对话、仓库或游戏服务器。"
            elif kind == "web":
                page = fetch(entry.get("url", ""), base / ".poe-evidence", context)
                text = text_of(page["html"], article=urlsplit(page["url"]).hostname == "poe.qq.com")
                excerpt = entry.get("excerpt")
                bx.require(isinstance(excerpt, str) and excerpt.strip() and excerpt in text, "网页证据需实际存在于正文的 excerpt，链接本身不是结论依据。")
                record.update(source_record(page), text=excerpt, status="source_observed", origin="program_fetch")
            elif kind == "local_data":
                import engine
                bx.require(not set(entry) & {"text", "path", "url", "excerpt", "message_ref"}, "local_data 原文由程序读取，不接受替代正文或文件路径。")
                bx.require(isinstance(entry.get("content_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["content_sha256"]), "local_data 使用 data read 返回的 content_sha256。")
                receipt = engine.read_source(root or engine.resolve_root(), entry.get("source", ""), entry.get("line"), entry.get("count"))
                bx.require(receipt["content_sha256"] == entry["content_sha256"], "本机资料已更新，旧内容引用失效；重新 data read 后审查结论。")
                text = receipt["text"]
                record.update({k: v for k, v in receipt.items() if k != "evidence_template"}, status="local_data_observed", origin="program_local_read")
            else:
                path = (base / entry.get("path", "")).resolve()
                record["source_base"]=str(base)
                bx.require(path.is_relative_to(base) and path.is_file() and path.stat().st_size <= 2_000_000,
                           "资料须在声明源 JSON 所在目录及子目录内，且为不超过 2 MB 的文本；按 source_base 保存所需记录。",
                           location="/path",expected={"source_base":str(base),"maximum_bytes":2_000_000,"type":"text file"},actual=str(path))
                text = path.read_text(encoding="utf-8-sig")
                excerpt = entry.get("excerpt")
                bx.require(isinstance(excerpt, str) and excerpt.strip() and excerpt in text, "artifact.excerpt 必须对应实际文件内容。")
                record.update(path=str(path), text=excerpt, content_sha256=bx.digest(path.read_bytes()), url=entry.get("url"), checked_at=entry.get("checked_at"),
                              status="artifact_observed", origin="agent_supplied_artifact", scope="文件片段可追溯，网站真实性及语义匹配仍需模型审查。")
            record["digest"] = bx.digest(text)
        except (OSError, ValueError) as exc:
            record.update(status="unresolved", reason=str(exc))
            if hasattr(exc,"diagnostic"): record["diagnostic"]={**exc.diagnostic,"location":location+exc.diagnostic.get("location","")}
        result[key] = record
    return result


def model_shape(document, *, complete=True):
    """Validate shared model inputs before writes, native queries or source fetches."""
    import resources
    resources.input_shape(document,complete=complete)
    budget=document.get("budget")
    if budget is not None:
        bx.require_keys(budget,{"amount","currency"},"/budget")
        bx.require(set(budget)=={"amount","currency"},"budget 需要 amount/currency；未确定时为 null。",location="/budget",expected=["amount","currency"],actual=sorted(budget))
        bx.require(bx.finite_number(budget["amount"]) and budget["amount"]>=0,"预算金额应为有限非负数。",location="/budget/amount",expected="finite nonnegative number",actual=budget["amount"])
        bx.require(isinstance(budget["currency"],str) and budget["currency"].strip(),"预算需要通货单位。",location="/budget/currency",expected="nonempty string",actual=budget["currency"])
    for field in ('inventory','shopping'):
        seen=set()
        rows=document.get(field,[])
        bx.require(isinstance(rows,list),field+' 应为数组。',location='/'+field,expected='array',actual=type(rows).__name__)
        for index,row in enumerate(rows):
            path=f'/{field}/{index}'
            bx.require(isinstance(row,dict),'物资需要对象。',location=path,expected='object',actual=type(row).__name__)
            ref=material_ref(row,location=path,complete=complete)
            if ref:
                bx.require(ref not in seen,'相同规格物资请合并数量，不能重复登记。',location=path,expected='unique material specification',actual=ref)
                seen.add(ref)
            if complete or 'quantity' in row:
                bx.require(type(row.get('quantity')) is int and row['quantity']>0,'物资 quantity 为正整数。',location=path+'/quantity',expected='positive integer',actual=row.get('quantity'))
            for key in ('low','high'):
                if key in row: bx.require(bx.finite_number(row[key]) and row[key]>=0,'报价需要有限非负数。',location=path+'/'+key,expected='finite nonnegative number',actual=row[key])
    evidence_shape(document.get("evidence",[]),complete=complete)
    sources=document.get("constraint_sources",{})
    bx.require(isinstance(sources,dict),"constraint_sources 为字段路径到 evidence_id 的映射。",location="/constraint_sources",expected="object",actual=type(sources).__name__)
    for key,value in sources.items():
        location="/constraint_sources/"+key.replace("~","~0").replace("/","~1")
        bx.require(key.startswith("/") and isinstance(value,str) and value.strip(),"约束映射的键为字段路径，值为单个 evidence_id。",location=location,expected="nonempty evidence_id string",actual=value)
    objectives=document.get("objectives",[])
    bx.require(isinstance(objectives,list),"objectives 应为数组。",location="/objectives",expected="array",actual=type(objectives).__name__)
    seen=set()
    for index,entry in enumerate(objectives):
        location=f"/objectives/{index}"
        bx.require_keys(entry,{"objective_id","text","evidence_refs"},location)
        for field in ("objective_id","text"):
            if complete or field=="objective_id" or field in entry: bx.require(isinstance(entry.get(field),str) and entry[field].strip(),field+" 应为非空文字。",location=location+"/"+field,expected="nonempty string",actual=entry.get(field))
        if "objective_id" in entry:
            key=entry["objective_id"]
            bx.require(key not in seen,"objective_id 需要唯一标识。",location=location+"/objective_id",expected="unique objective identifier",actual=key)
            seen.add(key)
        if complete or "evidence_refs" in entry:
            refs=entry.get("evidence_refs")
            bx.require(isinstance(refs,list) and all(isinstance(ref,str) and ref for ref in refs),"目标依据为 evidence_id 数组。",location=location+"/evidence_refs",expected="string array",actual=refs)
    claim_shape(document.get("claims",[]),complete=complete)
    assessment=document.get("assessment",{})
    bx.require_keys(assessment,{"goal_claim_ids","scope","unresolved","review"},"/assessment")
    for field in ("goal_claim_ids","unresolved"):
        if field in assessment: bx.require(isinstance(assessment[field],list) and all(isinstance(v,str) and v.strip() for v in assessment[field]),field+" 应为文字数组。",location="/assessment/"+field,expected="string array",actual=assessment[field])
    if "scope" in assessment: bx.require(isinstance(assessment["scope"],str),"scope 应为文字。",location="/assessment/scope",expected="string",actual=assessment["scope"])
    if "review" in assessment: review_shape(assessment["review"],complete=complete)


def review_shape(review, *, complete=True):
    path="/assessment/review"
    bx.require_keys(review,{"token","findings","unresolved","conclusions","objectives"},path)
    for field in ("token","findings"):
        if complete or field in review: bx.require(isinstance(review.get(field),str) and review[field].strip(),field+" 应为非空文字。",location=path+"/"+field,expected="nonempty string",actual=review.get(field))
    if "unresolved" in review: bx.require(isinstance(review["unresolved"],list) and all(isinstance(v,str) and v.strip() for v in review["unresolved"]),"review.unresolved 应为文字数组。",location=path+"/unresolved",expected="string array",actual=review["unresolved"])
    for field,identity in (("conclusions","claim_id"),("objectives","objective_id")):
        rows=review.get(field,[])
        bx.require(isinstance(rows,list),field+" 使用逐项评审数组。",location=path+"/"+field,expected="array",actual=type(rows).__name__)
        seen=set()
        for index,row in enumerate(rows):
            location=path+f"/{field}/{index}"
            bx.require_keys(row,{identity,"scope","findings"},location)
            for key in (identity,"scope","findings"):
                if complete or key in row: bx.require(isinstance(row.get(key),str) and row[key].strip(),key+" 应为非空文字。",location=location+"/"+key,expected="nonempty string",actual=row.get(key))
            key=row.get(identity)
            bx.require(key is None or key not in seen,field+" 不重复评审同一对象。",location=location+"/"+identity,expected="unique "+identity,actual=key);seen.add(key)


def claim_shape(claims, *, complete=True):
    bx.require(isinstance(claims,list),"claims 应为数组。",location="/claims",expected="array",actual=type(claims).__name__)
    seen=set()
    for index,entry in enumerate(claims):
        path=f"/claims/{index}"
        bx.require_keys(entry,{"claim_id","claim_type","text","subjects","evidence_refs","conditions","uncertainties","checks","bindings"},path,"claim 字段无效，状态由程序生成。")
        for field in ("claim_id","text"):
            if complete or field in entry: bx.require(isinstance(entry.get(field),str) and entry[field].strip(),field+" 应为非空文字。",location=path+"/"+field,expected="nonempty string",actual=entry.get(field))
        key=entry.get("claim_id")
        bx.require(key is None or key not in seen,"claim_id 不能重复。",location=path+"/claim_id",expected="unique identifier",actual=key);seen.add(key)
        for field in ("subjects","evidence_refs","conditions","uncertainties"):
            if field in entry or complete and field=="subjects":
                values=entry.get(field)
                bx.require(isinstance(values,list) and all(isinstance(v,str) and v.strip() for v in values) and (field!="subjects" or values),field+" 使用非空文字的数组。",location=path+"/"+field,expected="string array",actual=values)
        kind=entry.get("claim_type","judgment")
        bx.require(isinstance(kind,str) and kind in {"judgment","comparison"},"claim_type 使用 judgment 或 comparison。",location=path+"/claim_type",expected=["judgment","comparison"],actual=kind)
        checks=entry.get("checks",[])
        bx.require(isinstance(checks,list) and (kind!="comparison" or checks),"comparison 必须提供 checks；judgment 不证明可比较命题成立。",location=path+"/checks",expected="nonempty checks array" if kind=="comparison" else "array",actual=checks)
        for ci,check in enumerate(checks):
            cp=path+f"/checks/{ci}"
            bx.require_keys(check,{"reference","op","value"},cp)
            bx.require(set(check)=={"reference","op","value"},"检查使用 reference/op/value。",location=cp,expected=["reference","op","value"],actual=sorted(check))
            bx.require(isinstance(check["reference"],str) and check["reference"].startswith("native:"),"检查使用当前 native 引用。",location=cp+"/reference",expected="native reference",actual=check["reference"])
            try: check_value(None,check["op"],check["value"])
            except bx.ContractError as exc:
                exc.diagnostic["location"]=cp
                raise
        if "bindings" in entry:
            values=entry["bindings"]
            bx.require(isinstance(values,dict) and all(isinstance(k,str) and isinstance(v,str) and (k=="engine" or re.fullmatch(r"[0-9a-f]{64}",v)) for k,v in values.items()),"bindings 使用 edit 返回的依赖摘要。",location=path+"/bindings",expected="digest mapping",actual=values)


def claim_references(claim):
    embedded=[ref for _,ref in re.findall(r"\{\{(value|fact):([^{}]+)\}\}",claim.get("text",""))]
    return list(dict.fromkeys(claim.get("evidence_refs",[])+embedded+[c["reference"] for c in claim.get("checks",[])]))


def claim_bindings(document, claim, engine_fingerprint):
    """Bind a judgment to its declared source objects, not to other judgments."""
    def digest(value): return bx.digest(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False))
    omitted={"stages","claims","evidence","objectives","assessment"}
    result={"document":digest({k:v for k,v in document.items() if k not in omitted}),"engine":engine_fingerprint}
    stages={s["stage_id"]:s for s in document.get("stages",[])}
    objectives={o["objective_id"]:o for o in document.get("objectives",[])}
    for subject in claim.get("subjects",[]):
        kind,_,identifier=subject.partition(":")
        if kind=="configuration" and identifier.startswith("stage:"): result[identifier]=digest(stages.get(identifier[len("stage:"):]))
        elif kind in {"stage","snapshot"}: result["stage:"+identifier]=digest(stages.get(identifier))
        elif kind=="objective": result[subject]=digest(objectives.get(identifier))
        elif stages: result["stages"]=digest(document["stages"])
    declared={e["evidence_id"]:e for e in document.get("evidence",[])}
    for reference in claim_references(claim):
        if reference.startswith("native:stage:"):
            name=reference[len("native:"):].partition("#")[0]
            result[name]=digest(stages.get(name[len("stage:"):]))
        elif not reference.startswith("native:"): result["evidence:"+reference]=digest(declared.get(reference))
    return result


def reference_contract(native, artifacts=None):
    """Describe the current evidence roots, independently of output-file paths."""
    return {name:{"prefix":"native:"+name+"#","origin":value.get("origin","program_calculation"),
                  **({"artifact":artifacts[name]} if name in (artifacts or {}) else {}),
                  "fields":{key:"native:"+name+"#/"+key.replace("~","~0").replace("/","~1") for key in sorted(value)},
                  "identity_references":{key:{"reference":"native:"+name+"#"+locator,"meaning":meaning} for key,locator,meaning in (
                      ("character","/character_identity/digest","当前原生角色状态；适合角色一致性判断"),
                      ("input_bytes","/input_xml_sha256","本次提交给引擎的 XML 字节；保存回读后可改变"),
                      ("saved_bytes","/xml_sha256","已声明保存文件的字节；用于文件完整性")) if locator.split('/')[1] in value}}
            for name,value in native.items()}


def resolve_reference(reference, evidence, native=None):
    bx.require(isinstance(reference, str) and reference, "证据引用应为非空字符串。")
    if reference.startswith("native:"):
        evidence.pop(reference,None)  # Missing current fields never reuse an older value.
        name, separator, locator = reference[len("native:"):].partition("#")
        try:
            bx.require(separator and locator and name in (native or {}), "原生引用须指定本次结果及字段；按当前 reference_contract 选择证据根。",
                       location="native",expected=sorted(native or {}),actual=name)
            calculation = native[name]
            value = pointer(calculation, locator)
        except bx.ContractError as exc:
            exc.diagnostic["reference_roots"]=sorted(native or {})
            parent=exc.diagnostic.get("location","")
            if name in (native or {}) and (not parent or parent.startswith("/")):
                try: value=pointer(native[name],parent)
                except bx.ContractError: parent='';value=native[name]
                prefix="native:"+name+"#"+parent
                exc.diagnostic["reference_prefix"]=prefix
                identity_keys=("resource_id","skill_ref","id","stage_id","claim_id","objective_id","group","index","slot","name")
                entries=list(enumerate(value)) if isinstance(value,list) else list(value.items()) if isinstance(value,dict) else []
                exc.diagnostic["available_reference_count"]=len(entries)
                exc.diagnostic["available_references"]=[
                    {"index" if isinstance(value,list) else "field":key,
                     "reference":prefix+"/"+str(key).replace("~","~0").replace("/","~1"),
                     **({"identity":{k:row[k] for k in identity_keys if k in row}} if isinstance(row,dict) else {})}
                    for key,row in entries[:10]]
            raise
        main=next((s for s in calculation.get("active_skills",[]) if s.get("is_main") and not s.get("disabled")),{})
        record = {"evidence_id": reference, "kind": "native", "subject": name, "locator": locator, "value": value,
                  "status": "native_observed", "origin": calculation.get("origin","program_calculation"), "digest": bx.digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)),
                  "context": {"engine_fingerprint": calculation.get("engine_fingerprint"), "xml_sha256": calculation.get("xml_sha256") or calculation.get("input_xml_sha256") or (bx.digest(calculation["xml"]) if calculation.get("xml") else None),
                              "character_identity":calculation.get("character_identity"),
                              "config_values":calculation.get("config_values",{}),"class_id":calculation.get("class_id"),"ascendancy_id":calculation.get("ascendancy_id"),"main_skill":calculation.get("main_skill"),
                              "effect_state":engine.effect_state(calculation),
                              "main_skill_ref":main.get("skill_ref"),"main_skill_name":main.get("name") or calculation.get("main_skill_name"),"main_group":calculation.get("main_group")},
                  "meaning":reference_meaning(calculation,locator)}
        record["formatted_fact"]=format_observation(record)
        record["text_templates"]={"fact":"{{fact:"+reference+"}}",
                                  **({"value":"{{value:"+reference+"}}"} if type(value) in (int,float,bool,str) else {})} if not re.search(r"[{}]",reference) else {}
        evidence[reference] = record
    bx.require(reference in evidence, "未找到证据引用：" + reference)
    return evidence[reference]


def reference_meaning(calculation, locator):
    """Keep current native labels, qualified identities and uncertainty together."""
    tokens=locator.split("/")[1:]
    metric=tokens[-1] if tokens else ""
    if len(tokens)>=3 and tokens[0]=='config_controls':
        control=calculation.get('config_controls',{}).get(tokens[1],{})
        return {'label':control.get('label') or tokens[1],'unit':None,'unit_status':'not_applicable','display_multiplier':1,
                'object_identity':{key:control[key] for key in ('id','type','label','if_skill','if_option') if key in control},
                'actor':None,'status':'native_configuration_observed','value_basis':control.get('value_basis'),
                'scope':control.get('scope','Native current configuration only; applicability and effect coverage are not measured.')}
    if locator in {"/character_identity/digest","/input_xml_sha256","/xml_sha256"}:
        meanings={"/character_identity/digest":("原生角色状态摘要","native_active_character"),
                  "/input_xml_sha256":("本次输入 XML 字节摘要","input_transport_bytes"),
                  "/xml_sha256":("声明保存 XML 字节摘要","saved_file_bytes")}
        label,identity_scope=meanings[locator]
        return {"label":label,"unit":None,"unit_status":"not_applicable","display_multiplier":1,"object_identity":{},"actor":None,"status":"identity_observed","identity_scope":identity_scope,
                "scope":"文件字节和角色状态分别判断；保存格式变化可改变输入字节，真实角色、引用、文字或条件变化仍使旧复核失效。"}
    metadata=calculation.get("stat_metadata",{})
    meanings=metadata.get("Minion"+metric,metadata.get(metric,[])) if tokens and tokens[0]=="minion" else metadata.get(metric,[]) if tokens and tokens[0]=="stats" else []
    units=[]
    for entry in meanings:
        fmt,label=entry.get("format") or "",entry.get("label") or ""
        # Interpret the observed display contract, never the metric identifier.
        unit=("%" if "%" in fmt else "s" if re.search(r"[dfs]s$",fmt) else "m" if fmt.endswith("fm") else
              "/hit" if "per hit" in label.casefold() else "/s" if re.search(r"\b(rate|dps)\b|per second",label,re.I) else None)
        if unit not in units: units.append(unit)
    identity={}
    for end in range(len(tokens),0,-1):
        try: parent=pointer(calculation,"/"+"/".join(tokens[:end]))
        except ValueError: continue
        if isinstance(parent,dict) and any(key in parent for key in ("id","skill_id","skill_ref","stage_id")):
            identity={key:parent[key] for key in ("id","skill_id","skill_ref","stage_id","name","label","qualified_name","owner_class_id","owner_class_name","ascendancy_internal_name","type","is_ascendancy_start","actor") if key in parent}
            break
    multipliers={e.get("display_multiplier",1) for e in meanings}
    unambiguous=len(units)==1 and len(multipliers)==1
    return {"label":" / ".join(dict.fromkeys(e.get("display_label",e.get("label",metric)) for e in meanings)) or metric or locator,
            "unit":units[0] if unambiguous else None,"unit_status":"observed_display" if unambiguous and units[0] else "unknown",
            "display_multiplier":next(iter(multipliers)) if unambiguous else 1,
            "definitions":meanings,"object_identity":identity,
            "actor":identity.get("actor") or (meanings[0].get("actor") if meanings else None),
            "status":"native_display_observed" if meanings else "identity_observed" if identity else "unknown",
            "scope":"标签和格式来自当前安装；未注明单位保持未知。显示定义不证明取得、效果覆盖或实战可行。"}


def format_observation(record):
    value=record.get("value")
    meaning=record.get("meaning",{})
    if bx.finite_number(value):
        value=format(value*meaning.get("display_multiplier",1),".10g")
    else: value=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)
    identity=meaning.get("object_identity",{})
    subject=identity.get("qualified_name") or identity.get("skill_ref") or {"candidate":"当前方案","baseline":"原方案","budget":"预算核验","resources":"资源核验"}.get(record.get("subject"),record.get("subject",""))
    if meaning.get("actor"): subject+="（"+{"player":"玩家面板","minion":"召唤物面板","mirage":"幻影"}.get(meaning["actor"],meaning["actor"])+"）"
    unit=meaning.get("unit")
    suffix=unit if unit else "（显示单位未注明）" if type(record.get("value")) in (int,float) else ""
    return f"{subject} · {meaning.get('label',record.get('locator',''))} = {value}{suffix}"


def check_value(actual, op, expected):
    """A three-valued scalar comparison shared by claims and stage checks."""
    comparisons={"eq":operator.eq,"ge":operator.ge,"le":operator.le}
    bx.require(isinstance(op,str) and op in comparisons,"op 使用 eq/ge/le。",expected=sorted(comparisons),actual=op)
    bx.require(type(expected) in (int,float,bool,str) and (type(expected) not in (int,float) or bx.finite_number(expected)),"检查目标为有限数值、布尔或字符串。",actual=expected)
    numeric=type(actual) in (int,float) and type(expected) in (int,float)
    comparable=type(actual) in (int,float,bool,str) and (type(actual) not in (int,float) or bx.finite_number(actual)) and (numeric or op=="eq" and type(actual) is type(expected))
    if not comparable: return "unknown"
    return "supported" if comparisons[op](actual,expected) else "refuted"


def assess_claims(claims, evidence, native=None, *, specification=None, engine_fingerprint=None):
    claim_shape(claims)
    result = []
    for index, entry in enumerate(claims):
        key = entry.get("claim_id")
        row = dict(entry)
        for field in ("subjects", "evidence_refs", "conditions", "uncertainties"):
            row[field] = entry.get(field, [])
        missing, diagnostics = [], []
        if specification is not None:
            expected=claim_bindings(specification,entry,engine_fingerprint)
            if entry.get("bindings")!=expected:
                missing.append("claim_revision:"+key)
                diagnostics.append({"code":"claim_binding_stale" if entry.get("bindings") else "claim_binding_missing","location":f"/claims/{index}/bindings","action":"review_claims","claim_id":key,"message":"核对当前对象后用 edit 的 review_claims 提交此判断的重新评审；摘要由程序写入。"})
        checks=entry.get("checks",[])
        references=claim_references(entry)
        row["evidence_refs"]=references
        for reference in references:
            try:
                observed = resolve_reference(reference, evidence, native)
                if observed["status"] == "unresolved":
                    missing.append(reference)
                    diagnostics.append({"code":"unresolved_source","location":f"/claims/{index}/evidence_refs","reference":reference,
                                        "action":"repair_evidence","message":observed.get("reason","来源尚未观测。"),
                                        "source_diagnostic":observed.get("diagnostic")})
            except ValueError as exc:
                missing.append(str(exc))
                diagnostics.append({"code":"missing_reference","location":f"/claims/{index}/evidence_refs","reference":reference,
                                    "action":"repair_evidence","message":str(exc),"source_diagnostic":getattr(exc,"diagnostic",None)})
        def substitute(match):
            mode,ref=match.group(1),match.group(2)
            observed=evidence.get(ref,{})
            value=observed.get("value")
            if mode=="fact" and observed.get("status")=="native_observed": return observed["formatted_fact"]
            if type(value) not in (int,float,bool,str) or observed.get("status")=="unresolved":
                missing.append("inline_value:"+ref)
                return "[未取得实际值："+ref+"]"
            return format(value,".10g") if type(value) in (int,float) else json.dumps(value,ensure_ascii=False) if type(value) is bool else value
        row["rendered_text"]=re.sub(r"\{\{(value|fact):([^{}]+)\}\}",substitute,row["text"])
        checked=[]
        for check_index,check in enumerate(checks):
            observation=evidence.get(check["reference"],{})
            verdict=check_value(observation.get("value"),check["op"],check["value"]) if observation.get("status")=="native_observed" else "unknown"
            checked.append({**check,"actual":observation.get("value"),"status":verdict})
            if verdict!="supported": diagnostics.append({"code":"claim_check_"+verdict,"location":f"/claims/{index}/checks/{check_index}","reference":check["reference"],"expected":check["value"],"actual":observation.get("value"),"action":"revise_conclusion","message":"此数值检查"+("与当前值矛盾。" if verdict=="refuted" else "尚无可比较实际值。")})
        row["checks"]=checked
        row["check_status"]="refuted" if any(c["status"]=="refuted" for c in checked) else "unknown" if any(c["status"]=="unknown" for c in checked) else "supported" if checked else "not_requested"
        row["claim_type"]=entry.get("claim_type","comparison" if checks else "judgment")
        row["verification_scope"]="仅所列可比较检查" if checks else "模型解释；未验证可比较命题"
        row["status"] = "model_assessed" if row["evidence_refs"] and not missing and not row["uncertainties"] and row["check_status"] in {"supported","not_requested"} else "unresolved"
        row["missing_evidence"] = missing
        row["diagnostics"] = diagnostics
        row["observations"] = [{"reference": ref, **{k: evidence[ref][k] for k in ("value", "origin", "status", "scope", "meaning", "formatted_fact", "text_templates", "context", "digest", "content_sha256", "source", "line", "count") if k in evidence[ref]}}
                               for ref in row["evidence_refs"] if ref in evidence and evidence[ref].get("status") != "unresolved"]
        row["scope"] = "checks 仅证明所声明数值命题；model_assessed 仍为模型判断。完整事实含本机标签、单位及上下文，自由文字蕴含关系与因果未自动证明。"
        result.append(row)
    return result


def claim_display(claim):
    """Project assessed prose for delivery; preserve the full claim for audit."""
    diagnostics=claim.get("diagnostics",[])
    invalidated=any(row.get("code") in {"claim_binding_stale","claim_binding_missing"} for row in diagnostics)
    return {"claim_id":claim.get("claim_id"),"status":claim.get("status","unresolved"),"subjects":list(claim.get("subjects",[])),
            "revision_pending":invalidated,
            "text":"此说明尚未与本次方案同步；按当前对象和证据重新评审后提供操作建议。" if invalidated else claim.get("rendered_text",claim.get("text","未取得此判断。")),
            "conditions":[] if invalidated else list(claim.get("conditions",[])),
            "uncertainties":[] if invalidated else list(claim.get("uncertainties",[])),
            "evidence_refs":list(claim.get("evidence_refs",[])),
            "check_status":claim.get("check_status","not_requested"),
            "verification_scope":claim.get("verification_scope","模型解释；未验证可比较命题"),
            "diagnostics":diagnostics}


def has_claim(claims, subject):
    return any(row["status"] == "model_assessed" and subject in row["subjects"] for row in claims)


def claim_diagnostics(claims):
    """Keep current per-claim repair instructions in both delivery paths."""
    return [{**row,"claim_id":claim["claim_id"],"category":"input","status":"unknown"}
            for claim in claims for row in claim.get("diagnostics",[])]


def configuration_reviewed(claims, calculation_name):
    references=["native:"+calculation_name+"#/"+field for field in ("config_values","config_controls")]
    return any(c["status"]=="model_assessed" and "configuration:"+calculation_name in c["subjects"] and
               any(ref==prefix or ref.startswith(prefix+"/") for ref in c["evidence_refs"] for prefix in references) for c in claims)


def configuration_diagnostics(claims, calculations):
    """Locate missing scene review against actual, successfully calculated roots."""
    return [{"code":"configuration_review_missing","location":"/claims","action":"review_configuration",
             "required_subject":"configuration:"+name,"reference":"native:"+name+"#/config_values",
             "actual":value.get("config_values"),"category":"input","status":"unknown",
             "controls_reference":"native:"+name+"#/config_controls",
             "message":"核对当前实际配置与用户场景，修改对应计算输入并重算，再用具体引用评审此场景。"}
            for name,value in calculations.items() if not configuration_reviewed(claims,name)]


def objective_coverage(objectives, evidence, claims):
    """Trace model-decomposed user goals; completeness and meaning remain model judgments."""
    bx.require(isinstance(objectives,list),"objectives 为用户目标数组。",location="/objectives",expected="array")
    rows,pending,seen=[],[],set()
    if not objectives: pending.append("objective:missing")
    for index,entry in enumerate(objectives):
        path=f"/objectives/{index}"
        bx.require(isinstance(entry,dict) and set(entry)=={"objective_id","text","evidence_refs"},"目标使用 objective_id/text/evidence_refs。",location=path)
        key=entry["objective_id"]
        bx.require(isinstance(key,str) and key.strip() and key not in seen,"objective_id 唯一且非空。",location=path+"/objective_id",actual=key)
        seen.add(key)
        bx.require(isinstance(entry["text"],str) and entry["text"].strip(),"目标需要具体 text。",location=path+"/text")
        refs=entry["evidence_refs"]
        bx.require(isinstance(refs,list) and all(isinstance(ref,str) for ref in refs),"目标依据为 evidence_id 数组。",location=path+"/evidence_refs")
        basis=bool(refs) and all(evidence.get(ref,{}).get("kind")=="user_statement" and evidence[ref].get("status")=="user_declared" for ref in refs)
        linked=[c for c in claims if "objective:"+key in c["subjects"]]
        assessed=basis and any(c["status"]=="model_assessed" for c in linked)
        diagnostics=[]
        if not basis: diagnostics.append({"code":"objective_basis_missing","location":path+"/evidence_refs","action":"record_user_basis","message":"关联实际 user_statement；模型建议不冒充用户要求。"})
        if not linked: diagnostics.append({"code":"objective_subject_missing","location":"/claims","action":"link_claim","required_subject":"objective:"+key,
                                          "message":"在说明本目标的 claim.subjects 中加入 objective:"+key+"；claim_id 只是标识，不能代替 subjects。"})
        checked=[check for claim in linked for check in claim.get("checks",[])]
        measured="refuted" if any(c["status"]=="refuted" for c in checked) else "unknown" if any(c["status"]=="unknown" for c in checked) else "supported" if checked else "not_requested"
        rows.append({**entry,"required_subject":"objective:"+key,"diagnostics":diagnostics,"basis_status":"user_declared" if basis else "unresolved","claim_ids":[c["claim_id"] for c in linked],"declared_checks_status":measured,
                     "status":"model_assessed" if assessed else "unresolved","scope":"目标拆分与证据解释由模型负责，存在关联不证明目标已经达到。"})
        if not assessed: pending.append("objective:"+key)
    return rows,pending


def goal_review_diagnostics(assessment, claims):
    """Locate incomplete overall-review references; never choose the conclusion."""
    indexed={c["claim_id"]:c for c in claims}
    available=sorted(c["claim_id"] for c in claims if "goal" in c.get("subjects",[]) and c.get("status")=="model_assessed")
    diagnostics=[]
    def add(code, location, expected, actual, message):
        diagnostics.append({"code":code,"category":"input","subject":"goal_assessment","status":"unknown",
                            "action":"edit_input","location":location,"expected":expected,"actual":actual,
                            "available_goal_claim_ids":available,"message":message})
    ids=assessment.get("goal_claim_ids",[])
    if not ids:
        add("goal_claim_ids_missing","/assessment/goal_claim_ids","nonempty current goal claim ID array",ids,"选定当前总体判断；各项目标的关联另由 objective_coverage 核对。")
    for index,key in enumerate(ids):
        claim=indexed.get(key)
        actual={k:claim.get(k) for k in ("claim_id","subjects","status")} if claim else {"claim_id":key}
        expected={"subjects_include":"goal","status":"model_assessed"}
        if claim is None:
            add("goal_claim_missing",f"/assessment/goal_claim_ids/{index}",expected,actual,"未找到当前判断 ID；使用实际已有记录或补充总体判断。")
        elif "goal" not in claim.get("subjects",[]):
            add("goal_claim_subject_missing",f"/assessment/goal_claim_ids/{index}",expected,actual,"该判断未关联总体 goal；选定适用的总体判断，或核对其用途后修改 subjects。")
        elif claim.get("status")!="model_assessed":
            add("goal_claim_unresolved",f"/assessment/goal_claim_ids/{index}",expected,actual,"总体判断仍有未解决项；处理其当前诊断与证据，不直接填写输出状态。")
    if not assessment.get("scope","").strip():
        add("goal_scope_missing","/assessment/scope","nonempty actual review scope",assessment.get("scope"),"说明实际评审范围，覆盖已确认用户目标。")
    return diagnostics


def delivery_brief(status, errors, pending):
    """Summarize all observed gaps; no game targets or inferred success here."""
    labels={"blocked":"存在已知错误，尚不能按方案执行", "draft":"待核实草案", "scoped_result":"已完成所列范围的评审"}
    bx.require(status in labels,"未知交付状态。")
    categories={}
    for subject in dict.fromkeys(pending):
        parts=subject.split(":")
        if parts[0]=="stage" and len(parts)>2: parts=parts[2:]
        category=parts[0]
        categories[category]=categories.get(category,0)+1
    names={"objective":"目标覆盖","goal_assessment":"总体目标评审","config":"计算条件","condition":"效果覆盖条件",
           "resource":"资源安排","native":"原生测量","capability":"尚未支持的测量","material":"取得与规格","budget":"预算",
           "legality":"物品合法性","skill":"技能使用","progress":"角色进度","version":"版本来源","final_review":"最终复核",
           "acquisition":"获取与成长","entry_condition":"阶段进入条件","entry_check":"阶段数值条件","stage_assessment":"阶段评审",
           "snapshot_assumptions":"快照假设","permission":"用户意图","migration":"迁移步骤","trial_progress":"试炼进度"}
    categories=dict(sorted(categories.items()))
    message=labels[status]+"。"
    if errors: message+=f"已知错误 {len(errors)} 项。"
    if categories: message+="未解决："+"、".join(names.get(k,k)+f"（{v}项）" for k,v in categories.items())+"。"
    return {"status":status,"label":labels[status],"valid_statuses":list(labels),"errors_total":len(errors),"pending_total":len(set(pending)),
            "unresolved_categories":categories,"message":message,
            "scope":"仅汇总实际校验状态；计算/回读成功与方案合法、可取得、可执行、满足目标分别判断。"}


def final_review(specification, evidence, claims, observations, assessment):
    """Bind model review to current inputs, facts and prose, without judging prose in code."""
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in {"recorded_at", "review", "runtime"}}
        if isinstance(value, list): return [stable(v) for v in value]
        return value
    payload = stable({"specification": specification, "evidence": evidence, "claims": claims, "facts": observations})
    token = bx.digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    review = assessment.get("review")
    if review is not None: review_shape(review)
    matches = review is not None and review["token"] == token
    pending = [] if matches else ["final_review:stale" if review else "final_review:missing"]
    if matches: pending += ["final_review:" + value for value in review.get("unresolved", [])]
    required=[c["claim_id"] for c in claims]
    conclusions=review.get("conclusions",[]) if matches else []
    covered={row["claim_id"] for row in conclusions}
    missing=[key for key in required if key not in covered]
    unexpected=sorted(covered-set(required))
    if matches: pending += ["final_review:claim:"+key for key in missing]+["final_review:unknown_claim:"+key for key in unexpected]
    required_objectives=[row["objective_id"] for row in observations.get("objective_coverage",[])]
    objectives=review.get("objectives",[]) if matches else []
    covered_objectives={row["objective_id"] for row in objectives}
    missing_objectives=[key for key in required_objectives if key not in covered_objectives]
    unexpected_objectives=sorted(covered_objectives-set(required_objectives))
    if matches: pending += ["final_review:objective:"+key for key in missing_objectives]+["final_review:unknown_objective:"+key for key in unexpected_objectives]
    packet = {"token": token, "status": "model_reviewed" if matches and not pending else "unresolved",
              "facts": observations, "claims": claims, "findings": review.get("findings") if matches else None,
              "conclusions":conclusions,"objectives":objectives,
              "coverage":{"required_claim_ids":required,"missing_claim_ids":missing,"unknown_claim_ids":unexpected,
                          "required_objective_ids":required_objectives,"missing_objective_ids":missing_objectives,"unknown_objective_ids":unexpected_objectives},
              "review_template":{"token":token,"findings":"","unresolved":[],"conclusions":[{"claim_id":key,"scope":"","findings":""} for key in required],
                                 "objectives":[{"objective_id":key,"scope":"","findings":""} for key in required_objectives]},
              "check": "先核对 facts.objective_coverage 的各项原要求及 facts.pending 的全部依赖，再记录 objectives 和 conclusions。说明当前对象、数字、因果、进入条件及实际范围为何足以或不足以支持该目标；按实际阶段窗口核对安排，基础时间、独立单次与平均投影分别说明。修改后重新生成本包，再填写 assessment.review；不要自动回填 token。",
              "scope": "摘要绑定防止旧复核沿用到新方案；同一模型自审，不能证明自由文字无误或等同独立模型/实战验证。"}
    return packet, pending


def entry_checks(checks, native, claims, subject):
    """Compare model-chosen thresholds with current measurements; no gameplay targets here."""
    bx.require(isinstance(checks, list), "entry_checks 应为数组。")
    rows, pending, seen = [], [], set()
    for check in checks:
        bx.require(isinstance(check, dict) and set(check) == {"check_id", "locator", "op", "value", "claim_id"}, "entry_checks 使用 check_id/locator/op/value/claim_id。")
        key = check["check_id"]
        bx.require(isinstance(key, str) and key and key not in seen, "check_id 应唯一。")
        seen.add(key)
        expected = check["value"]
        check_value(None,check["op"],expected)
        row = dict(check, status="unknown", actual=None)
        try:
            value = pointer(native, check["locator"])
            verdict=check_value(value,check["op"],expected)
            row.update(actual=value,status={"supported":"met","refuted":"unmet","unknown":"unknown"}[verdict])
        except ValueError as exc: row["reason"] = str(exc)
        linked = next((c for c in claims if c["claim_id"] == check["claim_id"] and subject in c["subjects"] and c["status"] == "model_assessed"), None)
        row["basis_status"] = "model_assessed" if linked else "unresolved"
        if row["status"] != "met" or not linked: pending.append("entry_check:" + subject + ":" + key)
        rows.append(row)
    return rows, pending


def material_ref(entry, *, location="/", complete=True):
    kind = entry.get("kind")
    path=location.rstrip('/')
    bx.require(not complete and kind is None or isinstance(kind,str) and kind in {'gem','item','service'},'物资 kind 使用 gem/item/service。',location=path+'/kind',expected=['gem','item','service'],actual=kind)
    if kind is None: return None
    if kind == "gem":
        for key in ('skill_id','level','quality'):
            if not complete and key not in entry: continue
            value=entry.get(key)
            valid=isinstance(value,str) and value.strip() if key=='skill_id' else type(value) is int and value>=(0 if key=='quality' else 1)
            bx.require(valid,'宝石物资需要 skill_id、level、quality。',location=path+'/'+key,expected='nonempty string' if key=='skill_id' else 'nonnegative integer' if key=='quality' else 'positive integer',actual=value)
        if not {'skill_id','level','quality'}<=entry.keys(): return None
        return f"gem:{entry['skill_id']}:{entry['level']}:{entry['quality']}"
    if kind == "item":
        if "item_id" in entry:
            bx.require(type(entry["item_id"]) is int and entry["item_id"]>0,"XML 内装备编号需要正整数。",location=path+"/item_id",expected="positive local XML ID",actual=entry["item_id"])
        spec=entry.get("spec_sha256")
        if not complete and 'spec_sha256' not in entry: return None
        bx.require(isinstance(spec,str) and re.fullmatch(r"[0-9a-f]{64}",spec),"装备物资需要当前实际规格 spec_sha256；从计算返回的 material_specs 读取。",location=path+"/spec_sha256",expected="SHA-256 from material_specs",actual=spec)
        return "item:" + spec
    if not complete and 'service_id' not in entry: return None
    bx.require(isinstance(entry.get("service_id"),str) and entry["service_id"], "服务物资需要 kind=service 和 service_id。",location=path+'/service_id',expected='nonempty string',actual=entry.get('service_id'))
    return "service:" + entry["service_id"]


def add_material(totals,entry):
    """Aggregate acquisition quantities by global specification, never XML ID."""
    ref=material_ref(entry)
    quantity=entry.get('quantity')
    bx.require(type(quantity) is int and quantity>0,"物资 quantity 应为正整数。",location="/quantity",expected="positive integer",actual=quantity)
    if ref not in totals: totals[ref]={**entry,'quantity':0}
    totals[ref]['quantity']+=quantity
    if entry.get('evidence_refs'):
        totals[ref]['evidence_refs']=list(dict.fromkeys(totals[ref].get('evidence_refs',[])+entry['evidence_refs']))
    return ref


def quest_rows(raw, name, classes=()):
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    if not title or not text_of(title[1]).startswith(name + " -"):
        return []  # Redirects/ambiguous variants must not inherit another gem's table.
    section = re.search(r"<h[1-6][^>]*>\s*Quest\s*/\d+\s*</h[1-6]>(.*?)</table>", raw, re.I | re.S)
    if not section: return []
    rows = []
    class_ids = {entry["name"]: entry["id"] for entry in classes}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section[1], re.I | re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) != 4: continue
        act, quest, npc, characters = [text_of(cell).strip() for cell in cells]
        if not act.isdigit(): continue
        allowed_classes = sorted(class_ids.values()) if characters == "All Classes" else sorted(value for key, value in class_ids.items() if re.search(r"\b" + re.escape(key) + r"\b", characters))
        kind = "vendor" if 'title="Quest Vendor"' in cells[2] else "quest" if 'title="Quest Reward"' in cells[2] or not npc else None
        if not kind or not allowed_classes or not quest: continue
        rows.append({"acquisition": kind, "available_from_act": int(act), "quest": quest,
                     "npc": npc if kind == "vendor" else "Quest Reward", "class_ids": allowed_classes,
                     "basis": "gem_quest_table", "source_excerpt":text_of(row)})
    return rows


def universal_rows(gem_rows, general_raw, classes=()):
    """Corroborate a vendor row; never invent its unlock quest or chapter."""
    text = " ".join(text_of(general_raw).split())
    result = []
    for row in gem_rows:
        if row["acquisition"] != "vendor": continue
        pattern = re.escape(row["npc"]) + r" in Act " + str(row["available_from_act"]) + r"\b[^.]*across all classes"
        match = re.search(pattern, text)
        if match:
            result.append({**row, "class_ids": sorted(entry["id"] for entry in classes),
                           "basis": "gem_vendor_row_and_observed_scope", "condition": match[0]})
    return result


def page_version(raw):
    # Read the site's current league marker, not arbitrary version-history rows.
    for tag in re.findall(r"<a\b[^>]+>", raw, re.I):
        if re.search(r'class=["\'][^"\']*\bleague_bar_icon\b', tag):
            title = re.search(r'title=["\'][^"\']*\bv(\d+\.\d+)\b', tag)
            if title: return title[1]
    return None


def trial_facts(folder,context,game_version):
    """Observe rows as written; do not infer rewards or completion from row order."""
    result={"status":"unverified","scope":"网页的迷宫位置与区域等级；不推导角色已完成、奖励总点数或安全通关等级。"}
    try:
        page=fetch("https://poedb.tw/us/The_Lords_Labyrinth",folder,context)
        result.update(source_record(page),source_game_version=page_version(page["html"]))
        bx.require(game_version and result["source_game_version"]==game_version,"试炼页当前版本与目标未确认一致。")
        tables=[t for t in re.findall(r"<table\b[^>]*>.*?</table>",page["html"],re.I|re.S) if "<th>Area</th>" in t and "<th>Level</th>" in t]
        found=[]
        for table in tables:
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>",table,re.I|re.S):
                cells=re.findall(r"<td[^>]*>(.*?)</td>",row,re.I|re.S)
                if len(cells)<3: continue
                name,locations,level=[text_of(c) for c in cells[:3]]
                if not name or not level.isdigit(): continue
                locations=locations.splitlines()
                acts=[int(m[1]) for loc in locations if (m:=re.match(r"Act\s+(\d+)\s+\S",loc))]
                excerpt=text_of(row)
                found.append({"name":name,"trial_locations":locations,"observed_acts":acts,"area_level":int(level),"excerpt":excerpt,
                              "evidence_template":web_template(page,excerpt)})
        bx.require(found,"未识别当前页面的试炼表结构。")
        result.update(status="source_checked",labyrinths=found)
    except (OSError,ValueError) as exc: result["reason"]="试炼依据未核实："+str(exc)
    return result

def reconcile_trial_progress(route,proof):
    stages=[stage for stage in route["stages"] if stage.get("ascendancy_used",0)]
    if not stages: return [],False
    missing=[stage["stage_id"] for stage in stages if not has_claim(route.get("claims",[]),"progress:"+stage["stage_id"])]
    warnings=["升华进度需逐段对应实际完成或进入条件，区域等级不代表角色已解锁；待审阶段："+", ".join(missing)] if missing else []
    if proof.get("status")!="source_checked": warnings.append(proof.get("reason","试炼页面未观测，采用其他依据时查看 progress claim。"))
    return warnings,bool(missing)

def gem_facts(gems, folder, context, game_version=None, classes=()):
    try:
        general = fetch("https://poedb.tw/us/Gem", folder, context)
    except (OSError, ValueError) as exc:
        general = None
        general_error = str(exc)

    def one(entry):
        name = entry["name"]
        page_name = name + " Support" if entry.get("tags", {}).get("support") and not name.endswith(" Support") else name
        url = "https://poedb.tw/us/" + quote(page_name.replace(" ", "_"), safe="_")
        result = {"skill_id": entry["skill_id"], "name": name, "status": "unverified", "choices": [], "sources": [],
                  "native": {key: entry[key] for key in ("display_name", "description", "min_level", "tags", "level_requirements") if key in entry}}
        try:
            page = fetch(url, folder, context)
            rows = quest_rows(page["html"], page_name, classes)
            for row in rows:
                template=web_template(page,row["source_excerpt"])
                row["evidence_templates"]=[template] if template else []
            result["source_game_version"] = page_version(page["html"])
            result["sources"].append(source_record(page, role="gem_quest_table"))
            if game_version and result["source_game_version"] != game_version:
                result["reason"] = f"数据页当前版本 {result['source_game_version'] or '未识别'} 与目标 {game_version} 未确认一致；不能套用其他版本的领取表。"
            elif not rows:
                result["reason"] = "页面没有可识别的同名普通宝石任务表；不能推断不存在其他获取办法。"
            else:
                if general and (not game_version or page_version(general["html"]) == game_version) and not entry.get("special_variant") and not entry.get("vaal"):
                    extra = universal_rows(rows, general["html"], classes)
                    for row in extra:
                        template=web_template(general,row["condition"])
                        if template: row["evidence_templates"]=row.get("evidence_templates",[])+[template]
                    if extra:
                        rows = [row for row in rows if not any(all(row[k] == extra_row[k] for k in ("acquisition", "quest", "npc", "available_from_act")) for extra_row in extra)] + extra
                        result["sources"].append(source_record(general, role="universal_vendor_rule"))
                result.update(status="source_checked", choices=rows)
            if general is None: result["universal_vendor_note"] = "通用商店规则未读取：" + general_error
        except (OSError, ValueError) as exc:
            result["reason"] = "未能读取来源：" + str(exc)
        result["scope"] = "当前国际服数据页的获取依据；国服差异、物品库存与实际任务完成仍须确认。"
        return entry["skill_id"], result

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, gems.values()))


def reconcile_acquisitions(route,evidence):
    """Bind each physical instance to observed facts or an explicit held declaration."""
    warnings,unresolved=[],[]
    registry,claims=route.get("evidence",{}),route.get("claims",[])
    for stage in route["stages"]:
        sid=stage["stage_id"]
        for need in stage.get("required_items",[]):
            ref=material_ref(need)
            instance=need.get("instance_id",ref)
            subject="acquisition:"+instance
            refs=need.get("evidence_refs",[])
            bx.require(isinstance(refs,list) and all(isinstance(v,str) for v in refs),"获取依据 evidence_refs 无效。")
            records=[registry[r] for r in refs if r in registry and registry[r].get("subject")==ref and registry[r].get("status")!="unresolved"]
            status="unresolved"
            if need.get("acquisition")=="owned":
                declared=[r for r in records if r["kind"]=="user_statement" and r.get("context",{}).get("quantity")==need["quantity"] and (not need.get("instance_id") or r.get("context",{}).get("instance_id")==instance)]
                if declared: status="user_declared"
            elif need.get("kind")=="gem" and need.get("acquisition") in {"quest","vendor"}:
                proof=evidence.get(need["skill_id"],{})
                if proof.get("status")=="source_checked":
                    choices=[row for row in proof["choices"] if all(need.get(k)==row.get(k) for k in ("acquisition","available_from_act","quest","npc")) and (route["class_id"] in row["class_ids"] or need.get("via_mule"))]
                    bx.require(len(choices)==1,"实例 "+instance+" 未匹配唯一获取行；使用 research 的实际职业、任务、NPC 和章节。")
                    row=choices[0]
                    if need.get("class_ids"): bx.require(set(need["class_ids"])<=set(row["class_ids"]),"不得扩大任务表的职业范围。")
                    if stage.get("completed_act") is not None:
                        bx.require(stage["completed_act"]>=row["available_from_act"] or stage.get("act",0)>=row["available_from_act"],"此阶段声明的剧情进度早于获取章节；已持有应使用对应声明。")
                    need["observed_source_row"]=row
                    need["source_urls"]=[source["url"] for source in proof.get("sources",[])]
                    status="source_observed"
            elif records and any(c["status"]=="model_assessed" and subject in c["subjects"] and set(c["evidence_refs"]) & {r["evidence_id"] for r in records} for c in claims):
                status="model_assessed"
            need["evidence_status"]=status
            if status=="unresolved" or status!="user_declared" and not has_claim(claims,subject):
                unresolved.append(subject)
                warnings.append(instance+" 的规格、数量、取得/成长与阶段切换条件尚需判断。")
    return warnings,list(dict.fromkeys(unresolved))

def version_fact(url, game_version, league, folder, context):
    """Capture an official version/season passage, with an explicit narrow scope."""
    result = {"status": "unverified", "url": url}
    if not isinstance(game_version, str) or not isinstance(url, str) or urlsplit(url).hostname not in HOSTS - {"poedb.tw"}:
        return result | {"reason": "版本映射需要游戏官方公告；当前链接须人工核对。"}
    try:
        page = fetch(url, folder, context)
        lines = text_of(page["html"], article=urlsplit(url).hostname == "poe.qq.com").splitlines()
        version_pattern = re.compile(r"(?<![\d.])" + re.escape(game_version) + r"(?:\.\d+)*(?![\d.])")
        passages = [line for line in lines if version_pattern.search(line)]
        season = re.search(r"S\s*(\d+)|第\s*(\d+)\s*赛季", league, re.I)
        if season:
            number = next(value for value in season.groups() if value)
            pattern = re.compile(r"S\s*" + number + r"(?!\d)|第\s*" + number + r"\s*赛季", re.I)
            title_matches = any(pattern.search(line) for line in lines[:2])
            passages = [line for line in passages if pattern.search(line) or title_matches and "挑战赛季" in line and re.search(r"在\s*" + re.escape(game_version) + r"(?:\.\d+)*\s*版本中", line)]
        result.update(source_record(page))
        if passages:
            passage = min(passages, key=len)
            offset = version_pattern.search(passage).start()
            result.update(status="source_observed", excerpt=passage[max(0, offset-110):offset+170],
                          article_heading=lines[0] if lines else "",
                          scope="官方正文中发现对应版本，命名与上下文仍须阅读核对；不代表所有机制已经验证。")
            result["evidence_template"]=web_template(page,result["excerpt"])
        else:
            result["reason"] = "未找到同时支持所填版本/赛季的正文行，不能凭 URL 或页面标题认证。"
    except (OSError, ValueError) as exc:
        result["reason"] = "官方正文读取失败：" + str(exc)
    return result
