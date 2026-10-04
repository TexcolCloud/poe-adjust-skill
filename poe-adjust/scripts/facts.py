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
CLASS_IDS = {name: key for key, name in bx.CLASSES.items()}


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


def quest_rows(raw, name):
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    if not title or not text_of(title[1]).startswith(name + " -"):
        return []  # Redirects/ambiguous variants must not inherit another gem's table.
    section = re.search(r"<h[1-6][^>]*>\s*Quest\s*/\d+\s*</h[1-6]>(.*?)</table>", raw, re.I | re.S)
    if not section: return []
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section[1], re.I | re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) != 4: continue
        act, quest, npc, characters = [text_of(cell).strip() for cell in cells]
        if not act.isdigit(): continue
        classes = sorted(CLASS_IDS.values()) if characters == "All Classes" else sorted(value for key, value in CLASS_IDS.items() if re.search(r"\b" + re.escape(key) + r"\b", characters))
        kind = "vendor" if 'title="Quest Vendor"' in cells[2] else "quest" if 'title="Quest Reward"' in cells[2] or not npc else None
        if not kind or not classes or not quest: continue
        rows.append({"acquisition": kind, "available_from_act": int(act), "quest": quest,
                     "npc": npc if kind == "vendor" else "Quest Reward", "class_ids": classes,
                     "basis": "gem_quest_table"})
    return rows


def universal_rows(gem_rows, general_raw):
    """Use live universal-vendor text only for normal, listed quest-reward gems.

    This deliberately does not infer availability for exceptional, drop-only,
    transfigured or Vaal gems from a minimum level or a model-written label.
    """
    text = " ".join(text_of(general_raw).split())
    rewards = [row for row in gem_rows if row["acquisition"] == "quest"]
    result = []
    siosa = re.search(r"Siosa in Act (\d+) sells all unleveled skill gems that can be received as quest rewards for quests you've completed, across all classes", text)
    lilly = re.search(r"Lilly Roth in Act (\d+), 10 and Karui Shores sells all unleveled skill gems after completing the Act (\d+) quest Fallen from Grace", text)
    if siosa and any(row["available_from_act"] <= int(siosa[1]) for row in rewards):
        result.append({"acquisition": "vendor", "available_from_act": int(siosa[1]), "quest": "A Fixture of Fate",
                       "npc": "Siosa", "class_ids": sorted(bx.CLASSES), "basis": "quest_table_and_universal_vendor_rule",
                       "condition": "完成图书馆 A Fixture of Fate，并完成此宝石原任务进度；普通未升级宝石。"})
    if lilly and rewards:
        result.append({"acquisition": "vendor", "available_from_act": int(lilly[2]), "quest": "Fallen from Grace",
                       "npc": "Lilly Roth", "class_ids": sorted(bx.CLASSES), "basis": "quest_table_and_universal_vendor_rule",
                       "condition": "完成第六章 Fallen from Grace；普通未升级宝石。"})
    return result


def page_version(raw):
    # Read the site's current league marker, not arbitrary version-history rows.
    for tag in re.findall(r"<a\b[^>]+>", raw, re.I):
        if re.search(r'class=["\'][^"\']*\bleague_bar_icon\b', tag):
            title = re.search(r'title=["\'][^"\']*\bv(\d+\.\d+)\b', tag)
            if title: return title[1]
    return None


def trial_facts(folder, context, game_version):
    """Read campaign trial locations; area levels are not character entry levels."""
    result = {"status": "unverified", "scope": "前三次迷宫的剧情试炼位置；不证明角色已完成试炼、通关或满足全部进入条件。"}
    try:
        page = fetch("https://poedb.tw/us/The_Lords_Labyrinth", folder, context)
        result.update(source_record(page), source_game_version=page_version(page["html"]))
        bx.require(result["source_game_version"] == game_version, "试炼页面的当前版本不匹配本机数据。")
        names = ("The Labyrinth", "The Cruel Labyrinth", "The Merciless Labyrinth")
        tables = re.findall(r"<table\b[^>]*>.*?</table>", page["html"], re.I | re.S)
        tables = [table for table in tables if all(name in table for name in names) and "<th>Area</th>" in table and "<th>Level</th>" in table]
        bx.require(len(tables) == 1, "未识别唯一的迷宫试炼表。")
        found = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.I | re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            if len(cells) != 4: continue
            name, locations, area_level = [text_of(cell) for cell in cells[:3]]
            if name not in names: continue
            trials = locations.splitlines()
            acts = [int(match[1]) for line in trials if (match := re.match(r"Act\s+(\d+)\s+\S", line))]
            bx.require(acts and len(acts) == len(trials) and all(1 <= act <= 10 for act in acts) and area_level.isdigit(), "试炼表的章节或区域等级格式变化。")
            found[name] = {"name": name, "points": (names.index(name) + 1) * 2,
                           "last_trial_act": max(acts), "trial_locations": trials, "area_level": int(area_level)}
        bx.require(set(found) == set(names), "试炼表缺少前三次迷宫中的一项。")
        bx.require("each reward you with two Ascendancy points" in text_of(page["html"]), "未核对到每次迷宫的升华点奖励说明。")
        result.update(status="source_checked", labyrinths=[found[name] for name in names])
    except (OSError, ValueError) as exc:
        result["reason"] = "迷宫试炼依据未核实：" + str(exc)
    return result


def reconcile_trial_progress(route, proof):
    if not any(stage["ascendancy_points"] for stage in route["stages"]): return [], False
    if proof["status"] != "source_checked": return [proof.get("reason", "迷宫试炼依据未核实。")], True
    errors = []
    for stage in route["stages"]:
        required = [lab for lab in proof["labyrinths"] if lab["points"] <= stage["ascendancy_points"]]
        stage["trial_requirements"] = required
        if required and not route.get("shared_trials", False) and stage["act"] < required[-1]["last_trial_act"]:
            lab = required[-1]
            errors.append(f"{stage['level']} 级/第 {stage['act']} 章阶段安排了 {stage['ascendancy_points']} 点升华，但 {lab['name']} 的最后剧情试炼在第 {lab['last_trial_act']} 章；按真实进度延后，不得把首角色改成共享进度来放行。")
    bx.require(not errors, "迷宫进度检查失败：\n" + "\n".join(errors))
    return [], False


def gem_facts(gems, folder, context, game_version=None):
    try:
        general = fetch("https://poedb.tw/us/Gem", folder, context)
    except (OSError, ValueError) as exc:
        general = None
        general_error = str(exc)

    def one(entry):
        name = entry["name"]
        page_name = name + " Support" if entry.get("tags", {}).get("support") and not name.endswith(" Support") else name
        url = "https://poedb.tw/us/" + quote(page_name.replace(" ", "_"), safe="_")
        result = {"skill_id": entry["skill_id"], "name": name, "status": "unverified", "choices": [], "sources": []}
        try:
            page = fetch(url, folder, context)
            rows = quest_rows(page["html"], page_name)
            result["source_game_version"] = page_version(page["html"])
            result["sources"].append(source_record(page, role="gem_quest_table"))
            if game_version and result["source_game_version"] != game_version:
                result["reason"] = f"数据页当前版本 {result['source_game_version'] or '未识别'} 与目标 {game_version} 未确认一致；不能套用其他版本的领取表。"
            elif not rows:
                result["reason"] = "页面没有可识别的同名普通宝石任务表；不能推断不存在其他获取办法。"
            else:
                if general and (not game_version or page_version(general["html"]) == game_version) and not entry.get("special_variant") and not entry.get("vaal"):
                    extra = universal_rows(rows, general["html"])
                    if extra:
                        rows += extra
                        result["sources"].append(source_record(general, role="universal_vendor_rule"))
                result.update(status="source_checked", choices=rows)
            if general is None: result["universal_vendor_note"] = "通用商店规则未读取：" + general_error
        except (OSError, ValueError) as exc:
            result["reason"] = "未能读取来源：" + str(exc)
        result["scope"] = "当前国际服数据页的获取依据；国服差异、物品库存与实际任务完成仍须确认。"
        return entry["skill_id"], result

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, gems.values()))


def reconcile_acquisitions(route, evidence):
    """Choose observed rows; replace explanatory prose with canonical facts."""
    warnings, unresolved = [], []
    for skill_id, acquisition in route["gems"].items():
        proof = evidence.get(skill_id, {})
        if acquisition.get("acquisition") not in {"quest", "vendor"}:
            unresolved.append(skill_id)
            acquisition["evidence_status"] = "user_or_external_confirmation_required"
            continue
        if proof.get("status") != "source_checked":
            unresolved.append(skill_id)
            acquisition["evidence_status"] = "unverified"
            warnings.append(skill_id + " 获取未核实：" + proof.get("reason", "缺少可读来源"))
            continue
        choices = [row for row in proof["choices"]
                   if row["acquisition"] == acquisition["acquisition"]
                   and row["available_from_act"] == acquisition.get("available_from_act")
                   and (row["npc"] == acquisition.get("npc") if acquisition.get("npc") else True)
                   and (row["quest"] == acquisition.get("quest") if acquisition.get("quest") else True)
                   and (route["class_id"] in row["class_ids"] or acquisition.get("via_mule"))]
        bx.require(len(choices) == 1, skill_id + " 获取声明未对应唯一来源行。请从 research 返回的 choices 选择 acquisition/available_from_act/npc/quest，保持用户职业与小号偏好：" + json.dumps(proof["choices"], ensure_ascii=False))
        chosen = choices[0]
        if acquisition.get("class_ids"):
            bx.require(set(acquisition["class_ids"]) <= set(chosen["class_ids"]), skill_id + " 的 class_ids 扩大了网页实际允许的职业范围。")
        acquisition.update(chosen)
        acquisition["evidence_status"] = "source_checked"
        acquisition["source_note"] = f"第 {chosen['available_from_act']} 章 · {chosen['quest']} · {chosen['npc']}。" + chosen.get("condition", "需先完成所列任务。")
        acquisition["source_urls"] = [source["url"] for source in proof["sources"]]
    return warnings, unresolved


def version_fact(url, game_version, league, folder, context):
    """Capture an official version/season passage, with an explicit narrow scope."""
    result = {"status": "unverified", "url": url}
    if urlsplit(url).hostname not in HOSTS - {"poedb.tw"}:
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
        else:
            result["reason"] = "未找到同时支持所填版本/赛季的正文行，不能凭 URL 或页面标题认证。"
    except (OSError, ValueError) as exc:
        result["reason"] = "官方正文读取失败：" + str(exc)
    return result
