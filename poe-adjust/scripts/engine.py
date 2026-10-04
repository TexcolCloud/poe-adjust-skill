"""Run the installed PoB Lua engine in a disposable, read-only worker.

Python standard library only. The installed lua51.dll supplies LuaJIT; no
separate interpreter or package manager is required on the recipient's PC.
"""
from __future__ import annotations

import ctypes
import csv
from collections import deque
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET

# Historical source records, not an engine compatibility allowlist.
VERSION_EVIDENCE = {
    "3.28": {"season": "S29", "name": "沙海幻境", "url": "https://poe.qq.com/webplat/info/news_version3/22557/22558/22602/m14525/202603/982863.shtml"},
    "3.29": {"season": "S30", "name": "永火之咒", "url": "https://poe.qq.com/webplat/info/news_version3/22557/22558/22601/m14525/202607/988692.shtml"},
}
SCRIPTS = Path(__file__).resolve().parent


class EngineError(ValueError):
    pass


def validate_checked_at(value):
    """A research timestamp records a past observation, never a forecast."""
    if not isinstance(value, str):
        raise EngineError("查阅日期需要实际 YYYY-MM-DD 或带时区的 ISO 时间。")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            future = date.fromisoformat(value) > date.today()
        else:
            checked = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if checked.tzinfo is None:
                raise ValueError("missing timezone")
            future = checked > datetime.now(timezone.utc) + timedelta(minutes=5)
    except ValueError as exc:
        raise EngineError("查阅日期无效；请使用实际 YYYY-MM-DD 或含 +08:00 / Z 时区的 ISO 时间。") from exc
    if future:
        raise EngineError("查阅日期位于未来；请从本机当前日期/时间记录真实查阅时间。")


def version(root: Path) -> str:
    try:
        return ET.parse(root / "manifest.xml").find("Version").get("number")
    except (OSError, ET.ParseError, AttributeError) as exc:
        raise EngineError("这不是完整的 PoECharm/PoB 安装目录。") from exc


def resolve_root(hint: str | None = None) -> Path:
    candidates = []
    if hint:
        candidates.append(Path(hint).expanduser())
    elif os.environ.get("POECHARM_PATH"):
        candidates.append(Path(os.environ["POECHARM_PATH"]).expanduser())
    else:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                 "Get-Process -Name PoeCharm3,'Path of Building' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique"],
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            raw = result.stdout.decode("utf-8-sig", errors="replace")
            candidates.extend(Path(p.strip()) for p in raw.splitlines() if p.strip())
        home = Path.home()
        for base in (home / "Desktop", home / "Downloads", Path("C:/Games"), Path("D:/Games")):
            if base.is_dir():
                candidates.extend(base.glob("PoeCharm*"))
                candidates.extend(base.glob("PathOfBuilding*"))
    found = []
    for path in candidates:
        if path.is_file():
            path = path.parent
        for root in (path / "PathOfBuildingCommunity-Portable", path):
            if (root / "Modules/Build.lua").is_file() and (root / "lua51.dll").is_file():
                root = root.resolve()
                if root not in found:
                    found.append(root)
    if not found:
        raise EngineError("没有找到 PoECharm。请打开已安装的 PoECharm 后重试，或用 --pob 指定其文件夹。")
    if len(found) > 1:
        raise EngineError("找到多个计算引擎，请用 --pob 选择：" + "；".join(map(str, found)))
    return found[0]


def fingerprint(root: Path) -> str:
    """Hash actual calculation inputs, including same-version local updates.

    Excludes user builds/settings and artwork. No cache is written to the app.
    The adapter is part of the identity because it determines exported evidence.
    """
    paths = [root / name for name in ("manifest.xml", "changelog.txt")]
    paths += [path for path in root.iterdir() if path.suffix.lower() in {".lua", ".dll"}]
    for name in ("Classes", "Modules", "Data", "TreeData", "lua"):
        paths += [path for path in (root / name).rglob("*") if path.suffix.lower() in {".lua", ".json", ".dll"}]
    translations = root.parent / "Data/Translate/zh-rCN"
    paths += [translations / name for name in ("GUI.csv", "tree_dn.csv", "Items_Gems.txt.csv")]
    paths += list(SCRIPTS.glob("*.py")) + [SCRIPTS / name for name in ("headless.lua", "pob.lua")]
    digest = hashlib.sha256()
    def label(path):
        # Stable order and namespaces keep identical relocated installs identical.
        if path.is_relative_to(root): return "engine/" + path.relative_to(root).as_posix()
        return ("adapter/" if path.parent == SCRIPTS else "translations/") + path.name
    for path in sorted(set(paths), key=lambda path: label(path).casefold()):
        digest.update(label(path).encode("utf-8") + b"\0")
        if path.is_file():
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(b"MISSING")
        digest.update(b"\0")
    return digest.hexdigest()


def metadata(root: Path) -> dict:
    """Cross-check release notes and tree metadata; never equate a tree to rules."""
    current = version(root)
    tree_text = (root / "GameVersions.lua").read_text(encoding="utf-8-sig")
    tree_list = re.search(r"treeVersionList\s*=\s*\{([^}]+)\}", tree_text)
    trees = re.findall(r'"(\d+_\d+(?:_[a-z_]+)?)"', tree_list[1]) if tree_list else []
    standard = [value for value in trees if re.fullmatch(r"\d+_\d+", value)]
    latest = standard[-1] if standard else None
    notes = (root / "changelog.txt").read_text(encoding="utf-8-sig")
    release = re.search(r"^VERSION\[" + re.escape(current) + r"\]", notes, re.M)
    heading = re.search(r"^---\s+(\d+\.\d+)\b[^\r\n]*", notes[release.start():], re.M) if release else None
    rules = heading[1] if heading else None
    matched = rules is not None and latest == rules.replace(".", "_")
    return {"engine_version": current, "data_version": rules if matched else None,
            "available_tree_versions": trees, "latest_standard_tree": latest,
            "data_version_basis": {"status": "corroborated" if matched else "unresolved",
                "release": current, "changelog_heading": heading[0] if heading else None,
                "tree_metadata": latest,
                "scope": "本机发布记录与标准树交叉确认；不代表已核实国服改动或所有游戏机制。"},
            "version_evidence": VERSION_EVIDENCE.get(rules) if matched else None}


def check(root: Path) -> dict:
    if os.name != "nt":
        raise EngineError("此交付包使用 Windows 版 PoECharm，请在 TeleAgent Windows 桌面版运行。")
    dll = root / "lua51.dll"
    with dll.open("rb") as stream:
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset + 4)
        machine = struct.unpack("<H", stream.read(2))[0]
    if machine != 0x8664 or struct.calcsize("P") != 8:
        raise EngineError("需要 64 位 Python 与 64 位 PoECharm。优先使用 TeleAgent 自带的 Python。")
    return {"pob_path": str(root), **metadata(root), "engine_fingerprint": fingerprint(root),
            "python": sys.version.split()[0], "writes_to_installation": False}


def require_data_version(info):
    if not info.get("data_version"):
        raise EngineError("本机发布记录与天赋元数据不能一致确认机制/宝石版本。可继续查询目录，但不能认证配装或路线；请保留 doctor 结果供维护者核对，不能仅用最新天赋树推定规则。")
    return info["data_version"]


def validate_game_version(game_version, league, data_version=None, version_source=None):
    """The installed gem/mechanics data is not selected by the XML's tree version."""
    if not isinstance(game_version, str) or not re.fullmatch(r"3\.\d+", game_version):
        raise EngineError("请联网确认目标赛季的 game_version（如 3.29），不能从 S29 或 XML 的 treeVersion 推断。")
    season = re.search(r"(?<![A-Za-z0-9])S\s*(\d+)(?!\d)|第\s*(\d+)\s*赛季", league, re.I)
    season_number = int(next(group for group in season.groups() if group)) if season else None
    for documented_version, evidence in VERSION_EVIDENCE.items():
        if (season_number == int(evidence["season"][1:]) or evidence["name"] in league) and game_version != documented_version:
            raise EngineError(f"赛季与游戏版本不对应：官方 {evidence['season']}（{evidence['name']}）对应 {documented_version}，不能按 {game_version} 交付。请澄清是历史赛季、永久区还是旧文件用于当前赛季；不要擅自更改用户目标。")
    if data_version is not None and game_version != data_version:
        raise EngineError(f"目标游戏版本 {game_version} 与本机机制/宝石数据 {data_version} 不一致。仅切换旧天赋树不能还原旧版规则，需要维护者提供经过验证的对应引擎。")
    for documented_version, evidence in VERSION_EVIDENCE.items():
        if isinstance(version_source, str) and version_source.split("?")[0].split("#")[0] == evidence["url"] and documented_version != game_version:
            raise EngineError(f"引用的官方公告实际为 {evidence['season']} / {documented_version}，不能作为 {game_version} 的版本依据。请打开 doctor/start 返回的 version_evidence.url 核对正文。")


def validate_contract(result):
    """Reject missing evidence instead of silently treating absent fields as zero."""
    required = {"stats": dict, "resource_rules": dict, "gems": list, "equipped": list,
                "active_skills": list, "damage_sources": list, "classes": list, "excluded_guard_checks": list,
                "nodes": list, "config_keys": list, "warnings": list, "xml": str,
                "tree_version": str, "latest_tree_version": str, "capabilities": dict}
    for key, kind in required.items():
        if not isinstance(result.get(key), kind):
            raise EngineError(f"本机接口缺少有效 {key}；当前适配不能完成此项计算，请维护者检查接口变化。")
    if not result["capabilities"].get("excluded_guard_validation"):
        raise EngineError("本机接口不能识别守护技能；无法核对排除短时收益后的连线与资源，请维护者检查接口变化。")
    for key in ("Life", "Str", "Dex", "Int", "ManaUnreserved"):
        if type(result["stats"].get(key)) not in (int, float):
            raise EngineError("本机计算缺少核心指标：" + key)
    if not result["classes"] or not result["config_keys"] or not result["xml"].startswith("<?xml"):
        raise EngineError("本机目录或 XML 保存能力未通过检查。")
    ET.fromstring(result["xml"])


def health(root: Path) -> dict:
    result = calculate(root, queries=[{"kind": "gems", "query": "", "limit": 1}, {"kind": "config", "query": "", "limit": 1}], health_probe=True)
    if not result.get("health_probe", {}).get("roundtrip") or not all(row["entries"] for row in result["catalog_batch"]):
        raise EngineError("本机启动/目录查询/XML 保存回读自检未通过。")
    frontend = {"name": "未检测到 PoECharm 外壳", "file_version": None}
    wrappers = list(root.parent.glob("PoeCharm*.exe"))
    if len(wrappers) == 1:
        executable = wrappers[0]
        frontend = {"name": executable.name, "path": str(executable), "file_version": None}
        try:
            quoted = str(executable).replace("'", "''")
            process = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                      "(Get-Item -LiteralPath '" + quoted + "').VersionInfo.FileVersion"],
                                     capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            frontend["file_version"] = process.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass  # UI-wrapper metadata does not determine calculation compatibility.
    return {**check(root), "frontend": frontend, "capabilities": result["capabilities"], "health_probe": result["health_probe"],
            "compatibility": "runtime_checked", "limitations": "自检验证接口与基本回读；具体配装仍须 evaluate/export，游戏事实仍须联网查证。"}


def ascendancy_paths(name, nodes):
    selected = {node["id"]: node for node in nodes or [] if node.get("ascendancy_name") == name}
    starts = [node_id for node_id, node in selected.items() if node.get("is_ascendancy_start")]
    if not starts: return selected, {}
    if len(starts) != 1: raise EngineError("升华起点不唯一，需维护者检查本机数据。")
    paths = {starts[0]: []}
    queue = deque(starts)
    while queue:
        current = queue.popleft()
        for linked in selected[current].get("linked", []):
            if linked in selected and linked not in paths:
                paths[linked] = paths[current] + [linked]
                queue.append(linked)
    return selected, paths


def calculate(root: Path, xml: str = "", *, catalog: str = "", query: str = "", limit: int = 30, queries: list | None = None, crafts: list | None = None, xml_batch: list | None = None, health_probe: bool = False) -> dict:
    info = check(root)
    request = {"pob_path": str(root), "xml": xml, "catalog": catalog, "query": query, "limit": limit, "health_probe": health_probe}
    if queries is not None:
        request["queries"] = queries
    if crafts is not None:
        request["crafts"] = crafts
    if xml_batch is not None:
        request["xml_batch"] = xml_batch
    try:
        process = subprocess.run(
            [sys.executable, str(SCRIPTS / "engine.py"), "--worker"],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYTHONUTF8": "1", "REGENERATE_MOD_CACHE": "0", "CI": ""},
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError("PoB 计算超过 120 秒，已结束本次计算。原文件没有改动。") from exc
    if process.returncode:
        detail = process.stderr.decode("utf-8", errors="replace")[-5000:]
        # The headless worker may include desktop hotkey instructions in errors.
        # They are irrelevant to this failed calculation, not UI recovery steps.
        detail = detail.split("Press Enter/Escape to dismiss", 1)[0].rstrip()
        if xml and (catalog or queries):
            detail += "\n目录查询若只查宝石/职业/升华节点 ID 与原文，可去掉 --input 直接重试；所有职业数据都在本机，无需手写最小 XML 或下载模板。若需要角色实际连接路径，请提供能在 PoECharm 打开的真实 XML。"
        raise EngineError("PoB 计算未完成：" + detail)
    try:
        result = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineError("计算引擎没有返回有效结果：" + process.stdout.decode("utf-8", errors="replace")[-1000:]) from exc
    if fingerprint(root) != info["engine_fingerprint"]:
        raise EngineError("计算期间引擎或数据文件发生变化。本次结果已作废，请等待 PoECharm 更新完成后重试。")
    result.update({key: info[key] for key in ("engine_version", "engine_fingerprint", "data_version", "data_version_basis")})
    names = {}
    for filename in ("GUI.csv", "tree_dn.csv", "Items_Gems.txt.csv"):
        translations = root.parent / "Data/Translate/zh-rCN" / filename
        if translations.is_file():
            with translations.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                names.update({row[0]: row[1] for row in csv.reader(stream) if len(row) >= 2})
    for calculation in result.get("calculations", [result]):
        validate_contract(calculation)
        calculation.update({key: info[key] for key in ("engine_version", "engine_fingerprint", "data_version", "data_version_basis")})
        if info["available_tree_versions"] and calculation["latest_tree_version"] not in info["available_tree_versions"]:
            raise EngineError("运行中的天赋数据与安装元数据不一致，请等更新完成后重试。")
        entries = calculation.get("gems", []) + calculation.get("catalog", []) + calculation.get("classes", []) + calculation.get("active_skills", []) + calculation.get("damage_sources", [])
        for query_result in calculation.get("catalog_batch", []):
            entries += query_result["entries"]
        entries += [ascendancy for entry in entries for ascendancy in entry.get("ascendancies", [])]
        for entry in entries:
            if entry.get("name") in names:
                entry["name_cn"] = names[entry["name"]]
                entry["display_name"] = f"{entry['name_cn']}（{entry['name']}）"
        for key in ("main_skill_name", "class", "ascendancy"):
            if calculation.get(key) in names:
                calculation[key + "_cn"] = names[calculation[key]]
        node_queries = [row for row in calculation.get("catalog_batch", []) if row["kind"] == "nodes"]
        if catalog == "nodes": node_queries.append({"query": query, "total": calculation["catalog_total"], "entries": calculation["catalog"]})
        for row in node_queries:
            if row["total"] != len(row["entries"]): continue  # Truncated graphs cannot certify allocation paths.
            query_text = row["query"].casefold()
            for name in {node["ascendancy_name"] for node in row["entries"] if node.get("ascendancy_name")}:
                selected, paths = ascendancy_paths(name, row["entries"])
                if not query_text or not any(query_text in value.casefold() for node in selected.values() for value in (name, node.get("ascendancy_internal_name", ""))): continue
                for node_id, path in paths.items():
                    selected[node_id].update(ascendancy_path=path, ascendancy_path_cost=len(path),
                        ascendancy_path_names=[selected[key].get("display_name", selected[key]["name"]) for key in path])
    return result


def lua_string(value: str) -> str:
    marks = "="
    while "]" + marks + "]" in value:
        marks += "="
    return "[" + marks + "[" + value + "]" + marks + "]"


def worker(request: dict) -> dict:
    root = Path(request["pob_path"]).resolve()
    os.chdir(root)
    directory = os.add_dll_directory(str(root))
    lua = ctypes.CDLL(str(root / "lua51.dll"))
    lua.luaL_newstate.argtypes = []
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_openlibs.restype = None
    lua.luaL_loadbuffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
    lua.luaL_loadbuffer.restype = ctypes.c_int
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_pcall.restype = ctypes.c_int
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
    lua.lua_tolstring.restype = ctypes.c_void_p
    lua.lua_getfield.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    lua.lua_getfield.restype = None
    lua.lua_close.argtypes = [ctypes.c_void_p]
    lua.lua_close.restype = None
    state = lua.luaL_newstate()
    if not state:
        raise EngineError("无法初始化 LuaJIT。")

    def read(index: int) -> str:
        length = ctypes.c_size_t()
        ptr = lua.lua_tolstring(state, index, ctypes.byref(length))
        return ctypes.string_at(ptr, length.value).decode("utf-8", errors="replace") if ptr else ""

    def execute(source: str, name: bytes) -> None:
        data = source.encode("utf-8")
        if lua.luaL_loadbuffer(state, data, len(data), name) or lua.lua_pcall(state, 0, 0, 0):
            raise EngineError(read(-1))

    try:
        lua.luaL_openlibs(state)
        execute("_REQUEST_JSON = " + lua_string(json.dumps(request, ensure_ascii=False)), b"request")
        execute((SCRIPTS / "pob.lua").read_text(encoding="utf-8").split("-- @LOAD_HEADLESS@")[0], b"bootstrap")
        source = (SCRIPTS / "headless.lua").read_text(encoding="utf-8")
        execute(source.split("\n", 1)[1], b"headless")
        execute((SCRIPTS / "pob.lua").read_text(encoding="utf-8").split("-- @LOAD_HEADLESS@")[1], b"adapter")
        lua.lua_getfield(state, -10002, b"_RESULT_JSON")
        return json.loads(read(-1))
    finally:
        lua.lua_close(state)
        directory.close()


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        sys.stdout.buffer.write(json.dumps(worker(request), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except Exception as exc:
        sys.stderr.buffer.write(str(exc).encode("utf-8"))
        raise SystemExit(1)
