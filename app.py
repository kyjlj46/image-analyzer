"""이미지 분석 에이전트 웹 서버.

- POST /api/analyze : 이미지 여러 장 → batch_graph(병렬 분석 + 검토 + 웹 검증 + 웹서칭 + 종합 리포트)
- POST /api/ask     : 분석이 끝난 뒤 이미지/리포트에 대한 추가 질문 → qa_graph
- GET  /api/download/{session_id} : 결과 .txt (추가 질문 포함)
- GET/POST /api/mcp, POST /api/mcp/test : 웹 도구 MCP 서버 연결 상태·설정·연결 테스트 (agent/mcp_link.py)

응답은 NDJSON(한 줄에 JSON 이벤트 하나) 스트림. 이벤트의 index는 이미지 번호, "report", "qa" 중 하나.
업로드한 이미지와 결과는 sessions/<id>/ 에 보관되어 추가 질문에 쓰이고, .txt는 outputs/ 에 저장된다.
"""

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent import batch_graph, mcp_link, qa_graph
from agent.llm import LLMError
from agent.prompts import IMAGE_TYPES

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "outputs"
SESSION_DIR = BASE_DIR / "sessions"
OUTPUT_DIR.mkdir(exist_ok=True)
SESSION_DIR.mkdir(exist_ok=True)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 10
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MODES = {"auto", *IMAGE_TYPES}

app = FastAPI(title="이미지 분석 에이전트")
_lock = threading.Lock()


def event(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


# ── 세션 저장 ───────────────────────────────────────────────────────────

def session_path(sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise HTTPException(404, "세션을 찾을 수 없습니다.")
    return SESSION_DIR / sid


def load_session(sid: str) -> dict:
    f = session_path(sid) / "session.json"
    if not f.is_file():
        raise HTTPException(404, "세션을 찾을 수 없습니다. 다시 분석해 주세요.")
    return json.loads(f.read_text(encoding="utf-8"))


def save_session(s: dict) -> None:
    with _lock:
        (session_path(s["id"]) / "session.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        (OUTPUT_DIR / s["filename"]).write_text(render_txt(s), encoding="utf-8")


def render_txt(s: dict) -> str:
    line = "=" * 60
    out = []
    for img in s["images"]:
        head = f"{line}\n[{img['index']}/{len(s['images'])}] {img['name']}"
        if img.get("image_type"):
            how = "에이전트 자동 판별" if img.get("mode", "auto") == "auto" else "사용자 지정"
            head += f"\n유형: {IMAGE_TYPES.get(img['image_type'], img['image_type'])} ({how})"
            if img.get("revision"):
                head += f" · 검토 후 {img['revision']}회 수정"
            if img.get("classify_reason"):
                head += f"\n판별 근거: {img['classify_reason']}"
        out.append(f"{head}\n{line}\n\n{img.get('draft') or ('[오류] ' + (img.get('error') or '분석 결과 없음'))}")
        if img.get("verification"):
            out.append(f"\n[웹 검증]\n{img['verification']}")
        if img.get("research"):
            out.append(f"\n[웹서칭]\n{img['research']}")
        out += _qa_txt(img.get("qa"))
        out.append("")
    if s.get("report"):
        out.append(f"{line}\n종합 리포트 (이미지 {len(s['images'])}장)\n{line}\n\n{s['report']['text']}")
        out += _qa_txt(s["report"].get("qa"))
    return "\n".join(out).strip() + "\n"


def _qa_txt(qa: list | None) -> list[str]:
    if not qa:
        return []
    return ["\n[추가 질문]"] + [f"Q. {h['q']}\nA. {h['a']}\n" for h in qa]


# ── 분석 ───────────────────────────────────────────────────────────────

def run_analysis(s: dict, options: dict, report_enabled: bool, concurrency: int):
    folder = session_path(s["id"])
    yield event({"index": None, "type": "session", "session_id": s["id"], "report": report_enabled and len(s["images"]) > 1,
                 "images": [{"index": i["index"], "name": i["name"]} for i in s["images"]]})
    batch_input = {
        "images": [{"index": i["index"], "name": i["name"], "image_path": str(folder / i["file"])} for i in s["images"]],
        "options": options,
        "report_enabled": report_enabled,
    }
    final: dict = {}
    try:
        for ns, kind, data in batch_graph.stream(batch_input, stream_mode=["custom", "values"], subgraphs=True,
                                                 config={"max_concurrency": concurrency}):
            if kind == "custom":
                yield event(data)
            elif not ns:
                final = data
    except LLMError as e:
        yield event({"index": None, "type": "error", "message": str(e)})

    by_index = {r["index"]: r for r in final.get("results", [])}
    for img in s["images"]:
        r = by_index.get(img["index"], {})
        img.update({k: r.get(k) for k in ("mode", "image_type", "classify_reason", "draft", "revision", "approved",
                                   "verification", "research", "error")})
    if final.get("report"):
        s["report"] = {"text": final["report"], "qa": []}
    save_session(s)
    yield event({"index": None, "type": "saved", "filename": s["filename"]})


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] = File(...),
    mode: str = Form("auto"),
    instruction: str = Form(""),
    review: bool = Form(True),
    max_revisions: int = Form(1),
    verify: bool = Form(False),
    research: bool = Form(False),
    report: bool = Form(True),
    concurrency: int = Form(3),
):
    if mode not in MODES:
        raise HTTPException(400, f"알 수 없는 분석 모드: {mode}")
    if mode == "custom" and not instruction.strip():
        raise HTTPException(400, "사용자 지정 모드에서는 분석 지시를 입력해 주세요.")
    if len(files) > MAX_IMAGES:
        raise HTTPException(400, f"한 번에 최대 {MAX_IMAGES}장까지 분석할 수 있습니다.")

    uploads = []
    for f in files:
        if f.content_type not in ALLOWED_TYPES:
            raise HTTPException(400, f"{f.filename}: JPEG, PNG, GIF, WEBP만 지원합니다.")
        data = await f.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{f.filename}: 5MB 이하 이미지만 지원합니다.")
        uploads.append((f.filename or "image", f.content_type, data))

    sid = uuid.uuid4().hex
    folder = session_path(sid)
    folder.mkdir()
    images = []
    for n, (name, ctype, data) in enumerate(uploads, start=1):
        file = f"image_{n}{ALLOWED_TYPES[ctype]}"
        (folder / file).write_bytes(data)
        images.append({"index": n, "name": name, "file": file, "qa": []})
    stem = re.sub(r"[^\w가-힣-]+", "_", Path(uploads[0][0]).stem)[:40] or "image"
    session = {"id": sid, "created": datetime.now().isoformat(timespec="seconds"), "images": images,
               "filename": f"{datetime.now():%Y%m%d_%H%M%S}_{stem}.txt"}

    options = {"mode": mode, "instruction": instruction, "review_enabled": review,
               "max_revisions": max(0, min(max_revisions, 3)), "verify_enabled": verify,
               "research_enabled": research}
    return StreamingResponse(run_analysis(session, options, report, max(1, min(concurrency, 5))),
                             media_type="application/x-ndjson")


# ── 추가 질문 ──────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    session_id: str
    target: str            # 이미지 번호("1", "2", …) 또는 "report"
    question: str
    web: bool = False


def run_qa(s: dict, req: AskRequest):
    folder = session_path(s["id"])
    if req.target == "report":
        holder = s["report"]
        paths = [str(folder / i["file"]) for i in s["images"]]
        names = [i["name"] for i in s["images"]]
        context = holder["text"] + "\n\n" + "\n\n".join(
            f"=== 이미지 {i['index']}: {i['name']} (파일 {i['file']}) ===\n{i.get('draft') or ''}" for i in s["images"])
    else:
        holder = next(i for i in s["images"] if str(i["index"]) == req.target)
        paths = [str(folder / holder["file"])]
        names = [holder["name"]]
        context = ((holder.get("draft") or "")
                   + (f"\n\n[웹 검증]\n{holder['verification']}" if holder.get("verification") else "")
                   + (f"\n\n[웹서칭]\n{holder['research']}" if holder.get("research") else ""))

    final: dict = {}
    try:
        for kind, data in qa_graph.stream(
            {"image_paths": paths, "image_names": names, "context": context, "history": holder.get("qa", []),
             "question": req.question.strip(), "web_enabled": req.web},
            stream_mode=["custom", "values"],
        ):
            if kind == "custom":
                yield event(data)
            else:
                final = data
    except LLMError as e:
        yield event({"index": "qa", "type": "error", "message": str(e)})
        return
    holder.setdefault("qa", []).append({"q": req.question.strip(), "a": final.get("answer", "")})
    save_session(s)
    yield event({"index": "qa", "type": "answer_done"})


@app.post("/api/ask")
def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(400, "질문을 입력해 주세요.")
    s = load_session(req.session_id)
    if req.target == "report":
        if not s.get("report"):
            raise HTTPException(400, "종합 리포트가 없습니다.")
    elif not any(str(i["index"]) == req.target and i.get("draft") for i in s["images"]):
        raise HTTPException(400, "분석이 끝난 이미지에만 질문할 수 있습니다.")
    return StreamingResponse(run_qa(s, req), media_type="application/x-ndjson")


# ── 웹 도구 (MCP 서버 연결) ─────────────────────────────────────────────

class MCPSettings(BaseModel):
    mode: str              # stdio / http / off
    dir: str | None = None
    url: str | None = None


def _link_info() -> dict:
    s = mcp_link.load_settings()
    link = mcp_link.resolve(s)
    return {"settings": s, "mode": link.mode, "label": link.label, "target": link.target, "warning": link.warning,
            "tools": list(mcp_link.TOOL_NAMES) if link.mode != "off" else [], "modes": list(mcp_link.MODES)}


@app.get("/api/mcp")
def mcp_status():
    """현재 설정과 실제 사용할 연결 (빠름, 서버에 접속하지 않음)."""
    return _link_info()


@app.post("/api/mcp")
def mcp_save(body: MCPSettings):
    try:
        mcp_link.save_settings(body.mode, body.dir, body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mcp_link.check()


@app.post("/api/mcp/test")
def mcp_test():
    """MCP 서버에 실제로 접속해 도구 목록을 받아 본다."""
    return mcp_link.check()


# ── 파일 ───────────────────────────────────────────────────────────────

@app.get("/api/download/{session_id}")
def download(session_id: str):
    s = load_session(session_id)
    path = OUTPUT_DIR / s["filename"]
    if not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=s["filename"])


@app.get("/api/image/{session_id}/{index}")
def image(session_id: str, index: int):
    s = load_session(session_id)
    img = next((i for i in s["images"] if i["index"] == index), None)
    if not img:
        raise HTTPException(404, "이미지를 찾을 수 없습니다.")
    return FileResponse(session_path(session_id) / img["file"])


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
