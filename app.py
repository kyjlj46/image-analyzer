"""이미지 분석 에이전트 웹 서버 - LangGraph 에이전트(agent/graph.py)를 실행하고 진행 상황을 스트리밍한다.

응답은 NDJSON(한 줄에 JSON 이벤트 하나) 스트림이다:
  image_start / status / token / draft_reset / image_done / error / saved
"""

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from agent import graph
from agent.llm import LLMError
from agent.prompts import IMAGE_TYPES

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MODES = {"auto", *IMAGE_TYPES}

app = FastAPI(title="이미지 분석 에이전트")


def event(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def run_agent(images: list[tuple[str, str, bytes]], options: dict):
    sections: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for idx, (name, media_type, data) in enumerate(images, start=1):
            path = Path(tmp) / f"image_{idx}{ALLOWED_TYPES[media_type]}"
            path.write_bytes(data)
            yield event({"type": "image_start", "index": idx, "total": len(images), "name": name})

            final: dict = {}
            try:
                for mode, chunk in graph.stream({"image_path": str(path), **options},
                                                stream_mode=["custom", "values"]):
                    if mode == "custom":
                        yield event(chunk)
                    else:
                        final = chunk
            except LLMError as e:
                yield event({"type": "error", "message": str(e)})
                break

            image_type = final.get("image_type", options["mode"])
            header = f"{'=' * 60}\n[{idx}/{len(images)}] {name}\n유형: {IMAGE_TYPES.get(image_type, image_type)}"
            if final.get("revision"):
                header += f" · 검토 후 {final['revision']}회 수정"
            sections.append(f"{header}\n{'=' * 60}\n\n{final.get('draft', '')}\n")
            yield event({"type": "image_done", "index": idx, "image_type": image_type,
                         "revisions": final.get("revision", 0), "approved": final.get("approved")})

    if sections:
        stem = re.sub(r"[^\w가-힣-]+", "_", Path(images[0][0]).stem)[:40] or "image"
        filename = f"{datetime.now():%Y%m%d_%H%M%S}_{stem}.txt"
        (OUTPUT_DIR / filename).write_text("\n".join(sections), encoding="utf-8")
        yield event({"type": "saved", "filename": filename})


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] = File(...),
    mode: str = Form("auto"),
    instruction: str = Form(""),
    review: bool = Form(True),
    max_revisions: int = Form(1),
):
    if mode not in MODES:
        raise HTTPException(400, f"알 수 없는 분석 모드: {mode}")
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

    options = {"mode": mode, "instruction": instruction, "review_enabled": review,
               "max_revisions": max(0, min(max_revisions, 3))}
    return StreamingResponse(run_agent(images, options), media_type="application/x-ndjson")


@app.get("/api/download/{filename}")
def download(filename: str):
    path = (OUTPUT_DIR / filename).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=filename)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
