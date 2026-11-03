"""LLM 호출 계층 - 로그인된 Claude Code CLI(`claude -p`)를 감싼다.

API 키 없이 이 PC의 Claude Code 계정으로 동작한다. 다른 LLM(예: Anthropic API)으로 바꾸려면
이 파일의 stream_text()만 교체하면 그래프/노드는 그대로 쓸 수 있다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

TIMEOUT_SEC = 600


class LLMError(RuntimeError):
    pass


def _find_claude() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(exe).exists():
        raise LLMError("Claude Code(claude)를 찾을 수 없습니다. 설치 후 로그인해 주세요.")
    return exe


def stream_text(prompt: str, system: str, workdir: Path, tools: str = "Read") -> Iterator[str]:
    """Claude Code를 한 번 실행하고, 도구(이미지 읽기) 사용 이후의 답변 텍스트를 스트리밍한다."""
    cmd = [
        _find_claude(), "-p", prompt,
        "--tools", tools,            # 모델에게 보이는 도구 자체를 제한 (파일 쓰기 시도 방지)
        "--allowedTools", tools,
        "--no-session-persistence",  # 분석 호출이 Claude Code 세션 기록에 쌓이지 않게
        "--append-system-prompt", system,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
    ]
    proc = subprocess.Popen(
        cmd, cwd=workdir, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
    )
    tool_used = False  # 도구 사용 전의 "읽어보겠습니다" 같은 안내 문구는 버린다
    got_text = False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "user":  # 도구 결과가 돌아옴
                tool_used = True
            elif kind == "stream_event" and tool_used:
                delta = ev["event"].get("delta", {})
                if delta.get("type") == "text_delta":
                    got_text = True
                    yield delta["text"]
            elif kind == "result":
                if ev.get("is_error"):
                    raise LLMError(str(ev.get("result") or ev.get("subtype")))
                if not got_text and ev.get("result"):
                    yield ev["result"]
        proc.wait(timeout=TIMEOUT_SEC)
        if proc.returncode and not got_text:
            raise LLMError(f"Claude Code 실행 실패: {proc.stderr.read().strip() or proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()


def complete(prompt: str, system: str, workdir: Path) -> str:
    return "".join(stream_text(prompt, system, workdir))


def complete_json(prompt: str, system: str, workdir: Path) -> dict:
    """JSON 한 개로 답하게 하고 파싱한다. 실패하면 빈 dict."""
    text = complete(prompt + "\n\n반드시 JSON 객체 하나만 출력하세요. 다른 말은 쓰지 마세요.", system, workdir)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
