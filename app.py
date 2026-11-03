"""이미지 분석 에이전트 - 로그인된 Claude Code(claude -p)로 이미지를 분석해 .txt로 만들어 주는 웹앱.

API 키가 필요 없습니다. 이 PC에서 Claude Code에 로그인한 계정으로 동작합니다.
"""

import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
TIMEOUT_SEC = 600


def find_claude() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(exe).exists():
        raise RuntimeError("Claude Code(claude)를 찾을 수 없습니다. 설치 후 로그인해 주세요.")
    return exe


SYSTEM_PROMPT = """당신은 이미지 분석 전문가입니다. 주어진 이미지 파일을 Read 도구로 연 뒤, 단순 OCR이 아니라 \
'이해'한 내용을 이미지를 보지 못한 사람도 완전히 파악할 수 있는 텍스트 문서로 작성합니다.

원칙:
- "파일을 읽어보겠습니다" 같은 진행 안내 없이 바로 분석 결과만 출력하세요.
- 보이는 것(사실)과 추론한 것(해석)을 구분하고, 추론에는 "~로 보임", "추정"을 붙입니다.
- 이미지 안의 글자는 정확히 옮기되, 그 글자의 맥락·역할(제목, 버튼, 축 레이블 등)도 설명하세요.
- 구조(레이아웃, 표, 순서, 관계)를 살려 정리하고, 표는 텍스트 표(| 구분)로 재구성합니다.
- 결과는 .txt로 저장됩니다. 마크다운 기호(#, **, ```)는 쓰지 말고 섹션 제목은 [제목], 목록은 "- "로 씁니다.
- 사용자가 다른 언어를 요청하지 않으면 한국어로 작성합니다."""

MODE_PROMPTS = {
    "general": """이 이미지를 종합적으로 분석해 주세요:
[요약] 이미지가 무엇인지 2~3문장
[상세 설명] 구성 요소, 배치, 색감, 인물/사물, 행동, 배경
[이미지 속 텍스트] 텍스트가 있다면 위치·역할과 함께 원문 그대로
[맥락과 해석] 이미지의 목적, 상황, 시사점 (추론임을 표시)""",
    "document": """이 이미지는 문서·스크린샷·화면입니다:
[문서 유형] 어떤 종류의 문서/화면인지
[내용 재구성] 원래 구조(제목, 단락, 목록, 표, 버튼 등)를 살려 전체 내용을 텍스트로 재구성
[핵심 정보] 날짜, 금액, 이름, 수치 등 중요한 항목
[요약] 이 문서가 말하는 바""",
    "chart": """이 이미지는 차트·그래프·도표입니다:
[차트 개요] 차트 종류, 제목, 축, 범례, 단위
[데이터] 읽을 수 있는 값을 텍스트 표로 추출 (근사값은 ~ 표시)
[추세와 패턴] 증감, 최댓값/최솟값, 이상치, 비교
[인사이트] 데이터가 시사하는 점""",
    "custom": "아래 사용자 지시에 따라 이미지를 분석해 주세요.",
}

app = FastAPI(title="이미지 분석 에이전트")


def run_claude(image_path: Path, prompt: str):
    """claude -p 를 실행하고, 이미지를 읽은 뒤의 답변 텍스트만 스트리밍으로 돌려준다."""
    cmd = [
        find_claude(), "-p", f"분석할 이미지 파일: {image_path.name}\n\n{prompt}",
        "--allowedTools", "Read",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
    ]
    proc = subprocess.Popen(
        cmd, cwd=image_path.parent, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
    )
    image_read = False  # 이미지를 읽기 전의 "읽어보겠습니다" 같은 안내 문구는 버림
    got_text = False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "user":  # 도구 결과(이미지 읽기 완료)
                image_read = True
            elif t == "stream_event" and image_read:
                delta = ev["event"].get("delta", {})
                if delta.get("type") == "text_delta":
                    got_text = True
                    yield delta["text"]
            elif t == "result":
                if ev.get("is_error"):
                    yield f"\n[오류] {ev.get('result') or ev.get('subtype')}"
                elif not got_text and ev.get("result"):
                    yield ev["result"]
        proc.wait(timeout=TIMEOUT_SEC)
        if proc.returncode not in (0, None) and not got_text:
            err = proc.stderr.read().strip()
            yield f"\n[오류] Claude Code 실행 실패: {err or proc.returncode}"
    finally:
        if proc.poll() is None:
            proc.kill()


def analyze_stream(images: list[tuple[str, str, bytes]], mode: str, instruction: str):
    prompt = MODE_PROMPTS.get(mode, MODE_PROMPTS["general"])
    if instruction.strip():
        prompt += f"\n\n[사용자 추가 지시]\n{instruction.strip()}"

    collected: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for idx, (name, media_type, data) in enumerate(images, start=1):
            path = Path(tmp) / f"image_{idx}{ALLOWED_TYPES[media_type]}"
            path.write_bytes(data)
            header = f"{'=' * 60}\n[{idx}/{len(images)}] {name}\n{'=' * 60}\n\n"
            collected.append(header)
            yield header
            try:
                for text in run_claude(path, prompt):
                    collected.append(text)
                    yield text
            except RuntimeError as e:
                msg = f"[오류] {e}"
                collected.append(msg)
                yield msg
                break
            collected.append("\n\n")
            yield "\n\n"

    stem = re.sub(r"[^\w가-힣-]+", "_", Path(images[0][0]).stem)[:40] or "image"
    filename = f"{datetime.now():%Y%m%d_%H%M%S}_{stem}.txt"
    (OUTPUT_DIR / filename).write_text("".join(collected), encoding="utf-8")
    yield f"\n<<SAVED:{filename}>>"


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] = File(...),
    mode: str = Form("general"),
    instruction: str = Form(""),
):
    if mode == "custom" and not instruction.strip():
        raise HTTPException(400, "사용자 지정 모드에서는 분석 지시를 입력해 주세요.")
    images = []
    for f in files:
        if f.content_type not in ALLOWED_TYPES:
            raise HTTPException(400, f"{f.filename}: JPEG, PNG, GIF, WEBP만 지원합니다.")
        data = await f.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{f.filename}: 5MB 이하 이미지만 지원합니다.")
        images.append((f.filename or "image", f.content_type, data))
    return StreamingResponse(analyze_stream(images, mode, instruction), media_type="text/plain; charset=utf-8")


@app.get("/api/download/{filename}")
def download(filename: str):
    path = (OUTPUT_DIR / filename).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=filename)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
