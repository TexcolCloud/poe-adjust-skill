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
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import buildxml as bx

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
    key = hashlib.sha256(url.encode()).hexdigest()
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
    for token in locator.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            bx.require(token.isdigit() and int(token) < len(value), "证据数组索引不存在：" + locator)
            value = value[int(token)]
        else:
            bx.require(isinstance(value, dict) and token in value, "证据字段不存在：" + locator)
            value = value[token]
    return value


def load_evidence(declarations, folder, context, *, root=None):
    """Observe source artifacts; caller-written verification flags are not inputs."""
    bx.require(isinstance(declarations, list), "evidence 应为数组。")
    base = Path(folder).resolve()
    result = {}
    for entry in declarations:
        bx.require(isinstance(entry, dict) and set(entry) <= {"evidence_id", "kind", "subject", "text", "url", "path", "excerpt", "context", "checked_at", "message_ref", "source", "line", "count", "content_sha256"}, "证据字段无效；status/digest/origin 由程序生成。")
        key, kind = entry.get("evidence_id"), entry.get("kind")
        bx.require(isinstance(key, str) and key and "#" not in key and not key.startswith("native:") and key not in result, "evidence_id 需要唯一普通标识，native: 留给计算结果。")
        bx.require(kind in {"user_statement", "web", "artifact", "local_data"}, "证据 kind 为 user_statement/web/artifact/local_data。原生字段使用 native:candidate#/stats/Life 等引用。")
        bx.require(kind == "local_data" or not set(entry) & {"source", "line", "count", "content_sha256"}, "source/line/count/content_sha256 仅用于 local_data。")
        bx.require(isinstance(entry.get("subject"), str) and entry["subject"], "证据需要具体 subject。")
        record = {"evidence_id": key, "kind": kind, "subject": entry["subject"], "context": entry.get("context", {}), "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        bx.require(isinstance(record["context"], dict), "证据 context 应为对象。")
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
                bx.require(path.is_relative_to(base) and path.is_file() and path.stat().st_size <= 2_000_000, "资料必须是本任务工作区内不超过 2 MB 的文本文件；不读取平台内部输出目录。")
                text = path.read_text(encoding="utf-8-sig")
                excerpt = entry.get("excerpt")
                bx.require(isinstance(excerpt, str) and excerpt.strip() and excerpt in text, "artifact.excerpt 必须对应实际文件内容。")
                record.update(path=str(path), text=excerpt, content_sha256=bx.digest(path.read_bytes()), url=entry.get("url"), checked_at=entry.get("checked_at"),
                              status="artifact_observed", origin="agent_supplied_artifact", scope="文件片段可追溯，网站真实性及语义匹配仍需模型审查。")
            record["digest"] = bx.digest(text)
        except (OSError, ValueError) as exc:
            record.update(status="unresolved", reason=str(exc))
        result[key] = record
    return result


def resolve_reference(reference, evidence, native=None):
    bx.require(isinstance(reference, str) and reference, "证据引用应为非空字符串。")
    if reference.startswith("native:"):
        name, separator, locator = reference[len("native:"):].partition("#")
        bx.require(separator and locator and name in (native or {}), "原生引用须指定本次结果及字段，例如 native:candidate#/stats/Life。")
        calculation = native[name]
        value = pointer(calculation, locator)
        record = {"evidence_id": reference, "kind": "native", "subject": name, "locator": locator, "value": value,
                  "status": "native_observed", "origin": "program_calculation", "digest": bx.digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)),
                  "context": {"engine_fingerprint": calculation.get("engine_fingerprint"), "xml_sha256": calculation.get("xml_sha256") or calculation.get("input_xml_sha256") or (bx.digest(calculation["xml"]) if calculation.get("xml") else None)}}
        evidence[reference] = record
    bx.require(reference in evidence, "未找到证据引用：" + reference)
    return evidence[reference]


def assess_claims(claims, evidence, native=None):
    bx.require(isinstance(claims, list), "claims 应为数组。")
    result, seen = [], set()
    for index, entry in enumerate(claims):
        bx.require(isinstance(entry, dict) and set(entry) <= {"claim_id", "text", "subjects", "evidence_refs", "conditions", "uncertainties"}, "claim 字段无效，判断状态由程序标注。")
        key = entry.get("claim_id")
        bx.require(isinstance(key, str) and key and key not in seen, "claim_id 应唯一且非空。")
        seen.add(key)
        bx.require(isinstance(entry.get("text"), str) and entry["text"].strip(), "claim 需要具体 text。")
        row = dict(entry)
        for field in ("subjects", "evidence_refs", "conditions", "uncertainties"):
            row[field] = entry.get(field, [])
            bx.require(isinstance(row[field], list) and all(isinstance(v, str) and v.strip() for v in row[field]), field + " 应为非空字符串数组。")
        bx.require(row["subjects"], "claim 需要关联具体 subjects。")
        missing, diagnostics = [], []
        embedded=re.findall(r"\{\{value:([^{}]+)\}\}",row["text"])
        references=list(dict.fromkeys(row["evidence_refs"]+embedded))
        row["evidence_refs"]=references
        for reference in references:
            try:
                observed = resolve_reference(reference, evidence, native)
                if observed["status"] == "unresolved":
                    missing.append(reference)
                    diagnostics.append({"code":"unresolved_source","location":f"/claims/{index}/evidence_refs","reference":reference,
                                        "action":"repair_evidence","message":observed.get("reason","来源尚未观测。")})
            except ValueError as exc:
                missing.append(str(exc))
                diagnostics.append({"code":"missing_reference","location":f"/claims/{index}/evidence_refs","reference":reference,
                                    "action":"repair_evidence","message":str(exc)})
        def substitute(match):
            ref=match.group(1)
            observed=evidence.get(ref,{})
            value=observed.get("value")
            if type(value) not in (int,float,bool,str) or observed.get("status")=="unresolved":
                missing.append("inline_value:"+ref)
                return "[未取得实际值："+ref+"]"
            return format(value,".10g") if type(value) in (int,float) else json.dumps(value,ensure_ascii=False) if type(value) is bool else value
        row["rendered_text"]=re.sub(r"\{\{value:([^{}]+)\}\}",substitute,row["text"])
        row["status"] = "model_assessed" if row["evidence_refs"] and not missing and not row["uncertainties"] else "unresolved"
        row["missing_evidence"] = missing
        row["diagnostics"] = diagnostics
        row["observations"] = [{"reference": ref, **{k: evidence[ref][k] for k in ("value", "context", "digest", "content_sha256", "source", "line", "count") if k in evidence[ref]}}
                               for ref in row["evidence_refs"] if ref in evidence and evidence[ref].get("status") != "unresolved"]
        row["scope"] = "model_assessed 表示有引用的模型判断，程序仅核对引用存在及状态；数值见 observations，文字蕴含关系与因果并未自动证明。最终复核另按当前 review token 绑定。"
        result.append(row)
    return result


def has_claim(claims, subject):
    return any(row["status"] == "model_assessed" and subject in row["subjects"] for row in claims)


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
        rows.append({**entry,"required_subject":"objective:"+key,"diagnostics":diagnostics,"basis_status":"user_declared" if basis else "unresolved","claim_ids":[c["claim_id"] for c in linked],
                     "status":"model_assessed" if assessed else "unresolved","scope":"目标拆分与证据解释由模型负责，存在关联不证明目标已经达到。"})
        if not assessed: pending.append("objective:"+key)
    return rows,pending


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
    return {"status":status,"label":labels[status],"errors_total":len(errors),"pending_total":len(set(pending)),
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
    if review is not None:
        bx.require(isinstance(review, dict) and set(review) <= {"token", "findings", "unresolved"}, "assessment.review 使用 token/findings/unresolved。")
        bx.require(isinstance(review.get("token"), str), "review.token 使用本次返回的完整 token。")
        bx.require(isinstance(review.get("findings"), str) and review["findings"].strip(), "review.findings 说明对数字/对象、因果、替代方案排除、切换条件和结论范围的实际复核结果。")
        bx.require(isinstance(review.get("unresolved", []), list) and all(isinstance(v, str) and v.strip() for v in review.get("unresolved", [])), "review.unresolved 应为尚未解决的问题数组。")
    matches = review is not None and review["token"] == token
    pending = [] if matches else ["final_review:stale" if review else "final_review:missing"]
    if matches: pending += ["final_review:" + value for value in review.get("unresolved", [])]
    packet = {"token": token, "status": "model_reviewed" if matches and not pending else "unresolved",
              "facts": observations, "claims": claims, "findings": review.get("findings") if matches else None,
              "check": "逐条核对数字及主体/阶段、因果依据、排除替代方案的理由、切换进入条件与最终保证范围。修改后重新生成本包，再填写 assessment.review；不要自动回填 token。",
              "scope": "摘要绑定防止旧复核沿用到新方案；同一模型自审，不能证明自由文字无误或等同独立模型/实战验证。"}
    return packet, pending


def entry_checks(checks, native, claims, subject):
    """Compare model-chosen thresholds with current measurements; no gameplay targets here."""
    import math
    import operator
    bx.require(isinstance(checks, list), "entry_checks 应为数组。")
    comparisons = {"eq": operator.eq, "ge": operator.ge, "le": operator.le}
    rows, pending, seen = [], [], set()
    for check in checks:
        bx.require(isinstance(check, dict) and set(check) == {"check_id", "locator", "op", "value", "claim_id"}, "entry_checks 使用 check_id/locator/op/value/claim_id。")
        key = check["check_id"]
        bx.require(isinstance(key, str) and key and key not in seen and check["op"] in comparisons, "check_id 应唯一；op 使用 eq/ge/le。")
        seen.add(key)
        expected = check["value"]
        bx.require(type(expected) in (int, float, bool, str) and (type(expected) is not float or math.isfinite(expected)), "检查目标需要有限数字、布尔值或字符串。")
        row = dict(check, status="unknown", actual=None)
        try:
            value = pointer(native, check["locator"])
            scalar = type(value) in (int, float, bool, str)
            numeric = type(value) in (int, float) and type(expected) in (int, float)
            bx.require(scalar and (type(value) is not float or math.isfinite(value)) and (numeric or check["op"] == "eq" and type(value) is type(expected)), "本次字段缺少同类型可比较值。")
            row.update(actual=value, status="met" if comparisons[check["op"]](value, expected) else "unmet")
        except ValueError as exc: row["reason"] = str(exc)
        linked = next((c for c in claims if c["claim_id"] == check["claim_id"] and subject in c["subjects"] and c["status"] == "model_assessed"), None)
        row["basis_status"] = "model_assessed" if linked else "unresolved"
        if row["status"] != "met" or not linked: pending.append("entry_check:" + subject + ":" + key)
        rows.append(row)
    return rows, pending


def material_ref(entry):
    kind = entry.get("kind")
    if kind == "gem":
        bx.require(isinstance(entry.get("skill_id"), str) and entry["skill_id"] and type(entry.get("level")) is int and entry["level"] > 0 and type(entry.get("quality")) is int and entry["quality"] >= 0, "宝石物资需要 skill_id、level、quality。")
        return f"gem:{entry['skill_id']}:{entry['level']}:{entry['quality']}"
    if kind == "item":
        bx.require(type(entry.get("item_id")) is int and entry["item_id"] > 0, "装备物资需要 item_id。")
        return "item:" + str(entry["item_id"])
    bx.require(kind == "service" and isinstance(entry.get("service_id"),str) and entry["service_id"], "服务物资需要 kind=service 和 service_id。")
    return "service:" + entry["service_id"]


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
