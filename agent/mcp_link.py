"""MCP 서버(mcp-tools-server) 연결점.

웹 검증·웹서칭·추가 질문(웹 허용)에서 LLM이 tool calling으로 쓸 웹 도구를 어디서 가져올지 정한다.

연결 방식 (화면의 "웹 도구 (MCP)" 설정, 또는 mcp_settings.json)
- stdio : Claude가 호출할 때마다 mcp-tools-server를 하위 프로세스로 자동 실행 (따로 켤 필요 없음)
- http  : 따로 켜 둔 MCP 서버(예: http://127.0.0.1:8765/mcp)에 연결
- off   : MCP를 쓰지 않고 Claude Code 내장 WebSearch 사용

연결할 수 없으면(폴더 없음, HTTP 서버 꺼짐) 분석이 멈추지 않도록 내장 WebSearch로 자동 대체한다.
"""

import json
import os
import shutil
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SERVER_NAME = "tools"
TOOL_NAMES = ("web_search", "news_search", "fetch_url", "wikipedia", "current_time", "calculate")
FALLBACK_TOOLS = "WebSearch"
MODES = ("stdio", "http", "off")

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / "mcp_settings.json"
DEFAULT_DIR = ROOT.parent / "mcp-tools-server"
DEFAULT_URL = "http://127.0.0.1:8765/mcp"

_lock = threading.Lock()


# ── 설정 ───────────────────────────────────────────────────────────────

def _defaults() -> dict:
    """환경변수가 있으면 초기값으로 쓴다: USE_MCP=0, MCP_URL, MCP_TOOLS_DIR."""
    if os.environ.get("USE_MCP") == "0":
        mode = "off"
    elif os.environ.get("MCP_URL"):
        mode = "http"
    else:
        mode = "stdio"
    return {"mode": mode, "dir": os.environ.get("MCP_TOOLS_DIR") or str(DEFAULT_DIR),
            "url": os.environ.get("MCP_URL") or DEFAULT_URL}


def load_settings() -> dict:
    s = _defaults()
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        s.update({k: v for k, v in saved.items() if k in s and isinstance(v, str) and v.strip()})
    except (OSError, ValueError):
        pass
    if s["mode"] not in MODES:
        s["mode"] = "stdio"
    return s


def save_settings(mode: str, dir: str | None = None, url: str | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"알 수 없는 연결 방식: {mode}")
    s = load_settings()
    s["mode"] = mode
    if dir and dir.strip():
        s["dir"] = dir.strip()
    if url and url.strip():
        u = urlparse(url.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("MCP 서버 주소는 http(s)://호스트:포트/mcp 형식이어야 합니다.")
        s["url"] = url.strip()
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


# ── 연결 정보 계산 ──────────────────────────────────────────────────────

@dataclass
class Link:
    mode: str                 # 실제로 쓰는 방식: stdio / http / off
    config: dict | None       # claude --mcp-config 에 넘길 값 (off면 None)
    tools: str                # --allowedTools 에 넣을 웹 도구 목록
    label: str                # 화면 표시용
    target: str               # 폴더 또는 URL
    warning: str = ""         # 설정과 다르게 대체된 이유


def _stdio_server(folder: Path) -> dict | None:
    if not (folder / "src" / "mcp_tools_server").is_dir():
        return None
    python = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        return {"command": str(python), "args": ["-m", "mcp_tools_server"]}
    if shutil.which("uv"):
        return {"command": shutil.which("uv"), "args": ["run", "--directory", str(folder), "mcp-tools-server"]}
    return None


def _port_open(url: str, timeout: float = 1.0) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=timeout):
            return True
    except OSError:
        return False


def _mcp_tools() -> str:
    return ",".join(f"mcp__{SERVER_NAME}__{t}" for t in TOOL_NAMES)


def _fallback(target: str, warning: str) -> Link:
    return Link("off", None, FALLBACK_TOOLS, "내장 WebSearch", target, warning)


def resolve(settings: dict | None = None) -> Link:
    """지금 설정으로 웹 도구를 어떻게 연결할지 정한다. Claude를 부를 때마다 호출되므로 가볍게 유지한다."""
    s = settings or load_settings()
    if s["mode"] == "off":
        return _fallback("", "")
    if s["mode"] == "http":
        if not _port_open(s["url"]):
            return _fallback(s["url"], f"MCP 서버({s['url']})가 꺼져 있어 내장 WebSearch로 대체합니다.")
        return Link("http", {"mcpServers": {SERVER_NAME: {"type": "http", "url": s["url"]}}},
                    _mcp_tools(), "MCP 서버 (HTTP)", s["url"])
    server = _stdio_server(Path(s["dir"]))
    if not server:
        return _fallback(s["dir"], f"mcp-tools-server 폴더({s['dir']})를 찾을 수 없거나 설치(uv sync)되지 않아 내장 WebSearch로 대체합니다.")
    return Link("stdio", {"mcpServers": {SERVER_NAME: server}}, _mcp_tools(), "MCP 서버 (자동 실행)", s["dir"])


# ── 연결 테스트 ─────────────────────────────────────────────────────────

def check(settings: dict | None = None) -> dict:
    """실제로 MCP 서버에 접속해 도구 목록을 받아 본다. 화면의 '연결 테스트'용."""
    s = settings or load_settings()
    link = resolve(s)
    result = {"settings": s, "mode": link.mode, "label": link.label, "target": link.target,
              "warning": link.warning, "ok": link.mode != "off" or s["mode"] == "off", "tools": [], "error": ""}
    if link.mode == "off":
        return result
    try:
        import anyio
        from mcp import Client, StdioServerParameters

        async def probe():
            server = link.config["mcpServers"][SERVER_NAME]
            target = server["url"] if link.mode == "http" else StdioServerParameters(
                command=server["command"], args=server["args"])
            with anyio.fail_after(20):
                async with Client(target) as c:
                    return [t.name for t in (await c.list_tools()).tools]

        result["tools"] = anyio.run(probe)
        missing = [t for t in TOOL_NAMES if t not in result["tools"]]
        if missing:
            result["warning"] = "서버에 없는 도구: " + ", ".join(missing)
    except Exception as e:  # 연결 실패 사유를 그대로 화면에 보여준다
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"[:300]
    return result
