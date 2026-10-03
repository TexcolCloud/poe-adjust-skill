"""Run the installed PoB Lua engine in a disposable, read-only worker.

Python standard library only. The installed lua51.dll supplies LuaJIT; no
separate interpreter or package manager is required on the recipient's PC.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET

SUPPORTED_VERSIONS = {"2.67.2"}
SCRIPTS = Path(__file__).resolve().parent


class EngineError(ValueError):
    pass


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


def check(root: Path) -> dict:
    if os.name != "nt":
        raise EngineError("此交付包使用 Windows 版 PoECharm，请在 TeleAgent Windows 桌面版运行。")
    current = version(root)
    if current not in SUPPORTED_VERSIONS:
        raise EngineError(f"检测到 PoB {current}，本包已验证版本为 {', '.join(sorted(SUPPORTED_VERSIONS))}。请联系交付者更新适配包；不要改写版本号。")
    dll = root / "lua51.dll"
    with dll.open("rb") as stream:
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset + 4)
        machine = struct.unpack("<H", stream.read(2))[0]
    if machine != 0x8664 or struct.calcsize("P") != 8:
        raise EngineError("需要 64 位 Python 与 64 位 PoECharm。优先使用 TeleAgent 自带的 Python。")
    return {"pob_path": str(root), "engine_version": current, "python": sys.version.split()[0], "writes_to_installation": False}


def calculate(root: Path, xml: str = "", *, catalog: str = "", query: str = "", limit: int = 30, queries: list | None = None) -> dict:
    check(root)
    request = {"pob_path": str(root), "xml": xml, "catalog": catalog, "query": query, "limit": limit}
    if queries is not None:
        request["queries"] = queries
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
        raise EngineError("PoB 计算未完成：" + process.stderr.decode("utf-8", errors="replace")[-5000:])
    try:
        result = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineError("计算引擎没有返回有效结果：" + process.stdout.decode("utf-8", errors="replace")[-1000:]) from exc
    result["engine_version"] = version(root)
    return result


def lua_string(value: str) -> str:
    marks = "="
    while "]" + marks + "]" in value:
        marks += "="
    return "[" + marks + "[" + value + "]" + marks + "]"


def worker(request: dict) -> dict:
    root = Path(request["pob_path"]).resolve()
    check(root)
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
