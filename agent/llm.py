"""LLM 호출 계층 - 로그인된 Claude Code CLI(`claude -p`)를 감싼다.

API 키 없이 이 PC의 Claude Code 계정으로 동작한다. 다른 LLM(예: Anthropic API)으로 바꾸려면
이 파일의 stream_text() / complete()만 교체하면 그래프와 노드는 그대로 쓸 수 있다.

노드별로 쓸 수 있는 도구를 tools 인자로 제한한다. 예) "Read", WEB_TOOLS
"@web"은 웹 도구 묶음 자리표시로, mcp_link가 정한 MCP 서버 도구(mcp__tools__*) 또는 내장 WebSearch로 바뀐다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

from . import mcp_link

TIMEOUT_SEC = 900

# stream_text()가 내보내는 특수 신호: "지금까지 흘려보낸 텍스트는 도구 사용 전 안내 문구였으니 지워라"
RESET = object()


class LLMError(RuntimeError):
    pass


# 웹 도구 자리표시. 실제 도구(MCP 서버 또는 내장 WebSearch)는 호출할 때마다 mcp_link 설정으로 정한다.
WEB_TOOLS = "Read,@web"


def web_label() -> str:
    """화면 단계 로그에 붙일 웹 도구 이름."""
    link = mcp_link.resolve()
    return link.label + (" · 대체됨" if link.warning else "")


def _find_claude() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(exe).exists():
        raise LLMError("Claude Code(claude)를 찾을 수 없습니다. 설치 후 로그인해 주세요.")
    return exe


def _events(prompt: str, system: str, workdir: Path, tools: str) -> Iterator[dict]:
    names = [t for t in tools.split(",") if t]
    link = None
    if "@web" in names:
        link = mcp_link.resolve()
        names = [n for n in names if n != "@web"] + link.tools.split(",")
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
    if link and link.config:
        cmd += ["--mcp-config", json.dumps(link.config)]
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
