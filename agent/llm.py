"""LLM 호출 계층 - 로그인된 Claude Code CLI(`claude -p`)를 감싼다.

API 키 없이 이 PC의 Claude Code 계정으로 동작한다. 다른 LLM(예: Anthropic API)으로 바꾸려면
이 파일의 stream_text() / complete()만 교체하면 그래프와 노드는 그대로 쓸 수 있다.

노드별로 쓸 수 있는 도구를 tools 인자로 제한한다. 예) "Read", WEB_TOOLS
"mcp__"로 시작하는 이름은 MCP 서버 도구다. 웹 도구는 별도 프로젝트 mcp-tools-server(MCP 서버)를 쓰고,
그 프로젝트가 없으면 Claude Code 내장 WebSearch로 대신한다.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

TIMEOUT_SEC = 900

# stream_text()가 내보내는 특수 신호: "지금까지 흘려보낸 텍스트는 도구 사용 전 안내 문구였으니 지워라"
RESET = object()


class LLMError(RuntimeError):
    pass


# ── MCP 서버 (웹 도구) ─────────────────────────────────────────────────
# 기본 위치: 이 프로젝트 옆 폴더 ../mcp-tools-server. 다른 곳이면 환경변수 MCP_TOOLS_DIR로 지정.
# USE_MCP=0 이면 MCP 서버를 쓰지 않고 내장 WebSearch를 쓴다.

MCP_SERVER_NAME = "tools"
MCP_WEB_TOOLS = ("web_search", "news_search", "fetch_url", "wikipedia", "current_time", "calculate")


def _mcp_config() -> dict | None:
    if os.environ.get("USE_MCP", "1") == "0":
        return None
    root = Path(os.environ.get("MCP_TOOLS_DIR") or Path(__file__).resolve().parents[2] / "mcp-tools-server")
    if not (root / "src" / "mcp_tools_server").is_dir():
        return None
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        server = {"command": str(python), "args": ["-m", "mcp_tools_server"]}
    elif shutil.which("uv"):
        server = {"command": shutil.which("uv"), "args": ["run", "--directory", str(root), "mcp-tools-server"]}
    else:
        return None
    return {"mcpServers": {MCP_SERVER_NAME: server}}


MCP_CONFIG = _mcp_config()

# 웹 검색이 필요한 노드가 쓰는 도구 묶음
WEB_TOOLS = ("Read," + ",".join(f"mcp__{MCP_SERVER_NAME}__{t}" for t in MCP_WEB_TOOLS)) if MCP_CONFIG else "Read,WebSearch"
WEB_BACKEND = "MCP 서버 (mcp-tools-server)" if MCP_CONFIG else "Claude Code 내장 WebSearch"


def _find_claude() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(exe).exists():
        raise LLMError("Claude Code(claude)를 찾을 수 없습니다. 설치 후 로그인해 주세요.")
    return exe


def _events(prompt: str, system: str, workdir: Path, tools: str) -> Iterator[dict]:
    names = [t for t in tools.split(",") if t]
    builtin = ",".join(t for t in names if not t.startswith("mcp__"))
    cmd = [
        _find_claude(), "-p", prompt,
        "--tools", builtin,          # 모델에게 보이는 내장 도구 자체를 제한 (파일 쓰기 등 차단)
        "--allowedTools", ",".join(names),
        "--strict-mcp-config",       # 사용자 전역 MCP 서버는 불러오지 않는다
        "--no-session-persistence",  # 분석 호출이 Claude Code 세션 기록에 쌓이지 않게
        "--append-system-prompt", system,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
    ]
    if MCP_CONFIG and any(t.startswith("mcp__") for t in names):
        cmd += ["--mcp-config", json.dumps(MCP_CONFIG)]
    proc = subprocess.Popen(
        cmd, cwd=workdir, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
    )
    got_result = False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                got_result = True
                if ev.get("is_error"):
                    raise LLMError(str(ev.get("result") or ev.get("subtype")))
            yield ev
        proc.wait(timeout=TIMEOUT_SEC)
        if not got_result:
            raise LLMError(f"Claude Code 실행 실패: {proc.stderr.read().strip() or proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()


def stream_text(prompt: str, system: str, workdir: Path, tools: str = "Read") -> Iterator:
    """답변 텍스트를 토큰 단위로 흘려보낸다.

    텍스트가 나온 뒤 도구 호출이 시작되면 RESET을 내보낸다 (그 텍스트는 "확인해 보겠습니다" 같은 안내였음).
    """
    emitted = False
    for ev in _events(prompt, system, workdir, tools):
        if ev.get("type") != "stream_event":
            if ev.get("type") == "result" and not emitted and ev.get("result"):
                yield ev["result"]
            continue
        inner = ev["event"]
        if inner.get("type") == "content_block_start" and inner["content_block"].get("type") == "tool_use":
            if emitted:
                yield RESET
                emitted = False
        elif inner.get("type") == "content_block_delta" and inner["delta"].get("type") == "text_delta":
            emitted = True
            yield inner["delta"]["text"]


def complete(prompt: str, system: str, workdir: Path, tools: str = "Read") -> str:
    """최종 답변 텍스트만 돌려준다 (중간 안내 문구 제외)."""
    for ev in _events(prompt, system, workdir, tools):
        if ev.get("type") == "result":
            return ev.get("result") or ""
    return ""


def complete_json(prompt: str, system: str, workdir: Path, tools: str = "Read") -> dict:
    """JSON 한 개로 답하게 하고 파싱한다. 실패하면 빈 dict."""
    text = complete(prompt + "\n\n반드시 JSON 객체 하나만 출력하세요. 다른 말은 쓰지 마세요.", system, workdir, tools)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
