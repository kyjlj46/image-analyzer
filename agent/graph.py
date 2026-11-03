"""이미지 분석 에이전트 그래프 (LangGraph).

    START ─┬─(mode=auto)──▶ classify ──┐
           └─(mode 지정)───────────────┴─▶ analyze ─┬─(검토 끔)──────────────▶ END
                                            ▲       └─▶ review ─┬─(통과/한도)─▶ END
                                            └───(수정 필요)──────┘

확장 방법:
- 노드 추가: 함수 (state) -> dict 를 만들고 build_graph()에 add_node / add_edge.
- 진행 상황을 UI로 보내려면 노드 안에서 get_stream_writer()로 이벤트를 쓴다.
"""

from pathlib import Path
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from . import llm, prompts


class ImageState(TypedDict, total=False):
    # 입력
    image_path: str
    mode: str              # auto | general | document | chart | custom
    instruction: str
    review_enabled: bool
    max_revisions: int
    # 중간/결과
    image_type: str
    classify_reason: str
    draft: str
    feedback: str
    revision: int
    approved: bool
    retry: bool


def _path(state: ImageState) -> Path:
    return Path(state["image_path"])


# ── 노드 ────────────────────────────────────────────────────────────────

def classify(state: ImageState) -> dict:
    """이미지 유형을 판별해 분석 방식을 고른다."""
    write = get_stream_writer()
    write({"type": "status", "node": "classify", "message": "이미지 유형 판별 중…"})
    p = _path(state)
    result = llm.complete_json(prompts.CLASSIFY_PROMPT.format(image_file=p.name),
                               prompts.CLASSIFIER_SYSTEM, p.parent)
    image_type = result.get("type") if result.get("type") in ("general", "document", "chart") else "general"
    reason = result.get("reason", "")
    write({"type": "status", "node": "classify",
           "message": f"유형: {prompts.IMAGE_TYPES[image_type]}" + (f" — {reason}" if reason else "")})
    return {"image_type": image_type, "classify_reason": reason}


def analyze(state: ImageState) -> dict:
    """선택된 유형의 프롬프트로 분석 문서를 작성한다 (검토 피드백이 있으면 반영)."""
    write = get_stream_writer()
    image_type = state.get("image_type") or state["mode"]
    revision = state.get("revision", 0)
    p = _path(state)

    prompt = f"분석할 이미지 파일: {p.name}\n\n{prompts.ANALYZE_PROMPTS[image_type]}"
    if state.get("instruction", "").strip():
        prompt += f"\n\n[사용자 추가 지시]\n{state['instruction'].strip()}"
    if revision > 0:
        prompt += prompts.REVISION_PROMPT.format(draft=state["draft"], feedback=state["feedback"])
        write({"type": "draft_reset"})

    write({"type": "status", "node": "analyze",
           "message": "분석 작성 중…" if revision == 0 else f"피드백 반영해 다시 작성 중… ({revision}차 수정)"})
    chunks = []
    for text in llm.stream_text(prompt, prompts.ANALYST_SYSTEM, p.parent):
        chunks.append(text)
        write({"type": "token", "text": text})
    return {"draft": "".join(chunks).strip(), "image_type": image_type}


def review(state: ImageState) -> dict:
    """원본 이미지와 분석 문서를 대조해 통과 여부와 피드백을 낸다."""
    write = get_stream_writer()
    write({"type": "status", "node": "review", "message": "검토자가 원본과 대조 중…"})
    p = _path(state)
    instruction = state.get("instruction", "").strip()
    instruction_check = f"4. 사용자 지시 준수: \"{instruction}\"\n" if instruction else ""
    result = llm.complete_json(
        prompts.REVIEW_PROMPT.format(image_file=p.name, draft=state["draft"], instruction_check=instruction_check),
        prompts.REVIEWER_SYSTEM, p.parent,
    )
    approved = bool(result.get("approved", True))  # 판정 실패 시 통과 처리
    feedback = result.get("feedback", "")
    revision = state.get("revision", 0)
    retry = False
    if approved:
        write({"type": "status", "node": "review", "message": "검토 통과 ✓"})
    elif revision >= state.get("max_revisions", 1):
        write({"type": "status", "node": "review", "message": f"수정 한도 도달 — 현재본으로 마무리 (지적: {feedback})"})
    else:
        write({"type": "status", "node": "review", "message": f"수정 필요: {feedback}"})
        revision += 1
        retry = True
    return {"approved": approved, "feedback": feedback, "revision": revision, "retry": retry}


# ── 라우팅 ──────────────────────────────────────────────────────────────

def route_start(state: ImageState) -> str:
    return "classify" if state.get("mode", "auto") == "auto" else "analyze"


def route_after_analyze(state: ImageState) -> str:
    return "review" if state.get("review_enabled", True) else END


def route_after_review(state: ImageState) -> str:
    return "analyze" if state.get("retry") else END


def build_graph():
    g = StateGraph(ImageState)
    g.add_node("classify", classify)
    g.add_node("analyze", analyze)
    g.add_node("review", review)

    g.add_conditional_edges(START, route_start, ["classify", "analyze"])
    g.add_edge("classify", "analyze")
    g.add_conditional_edges("analyze", route_after_analyze, ["review", END])
    g.add_conditional_edges("review", route_after_review, ["analyze", END])
    return g.compile()


graph = build_graph()
