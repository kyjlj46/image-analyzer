"""이미지 분석 에이전트 그래프 (LangGraph).

1) image_graph - 이미지 1장 처리
    START ─┬─(auto)─▶ classify ─┐
           └─────────────────────┴─▶ analyze ─▶ [review ⟲ analyze] ─┬─▶ [verify]   ─▶ END
                                                                    └─▶ [research] ─▶ END
       review: 원본과 대조, 수정 필요하면 analyze로 되돌림 (최대 max_revisions회)
       verify: WebSearch로 고유 정보 사실 확인 (옵션)
       research(웹서칭): WebSearch로 이미지 속 대상의 관련 정보 조사 (옵션)
       verify와 research는 둘 다 켜면 병렬로 실행된다.

2) batch_graph - 여러 장을 동시에 처리하고 종합 리포트
    START ─(Send × N, 병렬)─▶ process_image(image_graph) ─▶ collect ─▶ [report] ─▶ END

3) qa_graph - 분석이 끝난 뒤 추가 질문
    START ─▶ answer ─▶ END      (웹 검색 허용 시 Read+WebSearch 도구 사용)

확장 방법:
- 노드 추가: (state) -> dict 함수를 만들고 build_*_graph()에서 add_node / add_edge.
- UI로 진행 상황을 보내려면 노드 안에서 emit(state, type="status", ...) 를 호출한다.
"""

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import llm, prompts


def emit(index, **event) -> None:
    """UI로 이벤트를 보낸다. index: 이미지 번호(int) 또는 "report" / "qa"."""
    get_stream_writer()({"index": index, **event})


def stream_into(index, part: str, chunks) -> str:
    """llm.stream_text() 결과를 UI로 흘려보내면서 최종 텍스트를 모은다."""
    buf: list[str] = []
    for chunk in chunks:
        if chunk is llm.RESET:
            buf.clear()
            emit(index, type="reset", part=part)
        else:
            buf.append(chunk)
            emit(index, type="token", part=part, text=chunk)
    return "".join(buf).strip()


# ═══ 1) 이미지 1장 처리 ═════════════════════════════════════════════════

class ImageState(TypedDict, total=False):
    # 입력
    index: int
    name: str
    image_path: str
    mode: str              # auto | general | document | chart | custom
    instruction: str
    review_enabled: bool
    max_revisions: int
    verify_enabled: bool
    research_enabled: bool
    # 중간/결과
    image_type: str
    classify_reason: str
    draft: str
    feedback: str
    revision: int
    approved: bool
    retry: bool
    verification: str
    research: str


def classify(state: ImageState) -> dict:
    i, p = state["index"], Path(state["image_path"])
    emit(i, type="status", node="classify", message="이미지 유형 판별 중…")
    result = llm.complete_json(prompts.CLASSIFY_PROMPT.format(image_file=p.name), prompts.CLASSIFIER_SYSTEM, p.parent)
    image_type = result.get("type") if result.get("type") in ("general", "document", "chart") else "general"
    reason = result.get("reason", "")
    emit(i, type="status", node="classify",
         message=f"유형: {prompts.IMAGE_TYPES[image_type]}" + (f" — {reason}" if reason else ""))
    emit(i, type="classified", image_type=image_type, label=prompts.IMAGE_TYPES[image_type],
         short=prompts.TYPE_SHORT[image_type], reason=reason, auto=True)
    return {"image_type": image_type, "classify_reason": reason}


def analyze(state: ImageState) -> dict:
    i, p = state["index"], Path(state["image_path"])
    image_type = state.get("image_type") or state["mode"]
    revision = state.get("revision", 0)

    prompt = f"분석할 이미지 파일: {p.name}\n\n{prompts.ANALYZE_PROMPTS[image_type]}"
    if state.get("instruction", "").strip():
        prompt += f"\n\n[사용자 추가 지시]\n{state['instruction'].strip()}"
    if revision > 0:
        prompt += prompts.REVISION_PROMPT.format(draft=state["draft"], feedback=state["feedback"])
        emit(i, type="reset", part="draft")
    elif state.get("mode", "auto") != "auto":  # 사용자가 유형을 직접 지정한 경우
        emit(i, type="classified", image_type=image_type, label=prompts.IMAGE_TYPES[image_type],
             short=prompts.TYPE_SHORT[image_type], reason="", auto=False)

    emit(i, type="status", node="analyze",
         message="분석 작성 중…" if revision == 0 else f"피드백 반영해 다시 작성 중… ({revision}차 수정)")
    draft = stream_into(i, "draft", llm.stream_text(prompt, prompts.ANALYST_SYSTEM, p.parent))
    return {"draft": draft, "image_type": image_type}


def review(state: ImageState) -> dict:
    i, p = state["index"], Path(state["image_path"])
    emit(i, type="status", node="review", message="검토자가 원본과 대조 중…")
    instruction = state.get("instruction", "").strip()
    check = f"4. 사용자 지시 준수: \"{instruction}\"\n" if instruction else ""
    result = llm.complete_json(
        prompts.REVIEW_PROMPT.format(image_file=p.name, draft=state["draft"], instruction_check=check),
        prompts.REVIEWER_SYSTEM, p.parent,
    )
    approved = bool(result.get("approved", True))  # 판정 실패 시 통과 처리
    feedback = result.get("feedback", "")
    revision, retry = state.get("revision", 0), False
    if approved:
        emit(i, type="status", node="review", message="검토 통과 ✓")
    elif revision >= state.get("max_revisions", 1):
        emit(i, type="status", node="review", message=f"수정 한도 도달 — 현재본으로 마무리 (지적: {feedback})")
    else:
        emit(i, type="status", node="review", message=f"수정 필요: {feedback}")
        revision, retry = revision + 1, True
    return {"approved": approved, "feedback": feedback, "revision": revision, "retry": retry}


def verify(state: ImageState) -> dict:
    i, p = state["index"], Path(state["image_path"])
    emit(i, type="status", node="verify", message="웹 검색으로 사실 확인 중…")
    text = stream_into(i, "verify", llm.stream_text(
        prompts.VERIFY_PROMPT.format(image_file=p.name, draft=state["draft"], max_items=5),
        prompts.VERIFIER_SYSTEM, p.parent, tools="Read,WebSearch",
    ))
    emit(i, type="status", node="verify", message="웹 검증 완료 ✓")
    return {"verification": text}


def research(state: ImageState) -> dict:
    i, p = state["index"], Path(state["image_path"])
    emit(i, type="status", node="research", message="웹서칭: 관련 정보 조사 중…")
    instruction = state.get("instruction", "").strip()
    text = stream_into(i, "research", llm.stream_text(
        prompts.RESEARCH_PROMPT.format(image_file=p.name, draft=state["draft"], max_items=4,
                                       instruction=f"\n[사용자 추가 지시]\n{instruction}\n" if instruction else ""),
        prompts.RESEARCHER_SYSTEM, p.parent, tools="Read,WebSearch",
    ))
    emit(i, type="status", node="research", message="웹서칭 완료 ✓")
    return {"research": text}


WEB_STEPS = (("verify", "verify_enabled"), ("research", "research_enabled"))


def _after_draft(state: ImageState):
    """분석이 확정된 뒤 켜진 웹 단계들로 보낸다. 여러 개면 리스트를 반환해 병렬 실행."""
    steps = [node for node, flag in WEB_STEPS if state.get(flag)]
    return steps or END


def route_start(state: ImageState) -> str:
    return "classify" if state.get("mode", "auto") == "auto" else "analyze"


def route_after_analyze(state: ImageState) -> str:
    return "review" if state.get("review_enabled", True) else _after_draft(state)


def route_after_review(state: ImageState) -> str:
    return "analyze" if state.get("retry") else _after_draft(state)


def build_image_graph():
    g = StateGraph(ImageState)
    g.add_node("classify", classify)
    g.add_node("analyze", analyze)
    g.add_node("review", review)
    g.add_node("verify", verify)
    g.add_node("research", research)
    g.add_conditional_edges(START, route_start, ["classify", "analyze"])
    g.add_edge("classify", "analyze")
    g.add_conditional_edges("analyze", route_after_analyze, ["review", "verify", "research", END])
    g.add_conditional_edges("review", route_after_review, ["analyze", "verify", "research", END])
    g.add_edge("verify", END)
    g.add_edge("research", END)
    return g.compile()


image_graph = build_image_graph()


# ═══ 2) 여러 장 병렬 처리 + 종합 리포트 ══════════════════════════════════

RESULT_KEYS = ("index", "name", "image_path", "mode", "image_type", "classify_reason", "draft", "revision", "approved", "verification", "research", "error")


class BatchState(TypedDict, total=False):
    images: list[dict]           # [{index, name, image_path}]
    options: dict                # mode, instruction, review_enabled, max_revisions, verify_enabled
    report_enabled: bool
    results: Annotated[list[dict], operator.add]   # 병렬 브랜치 결과가 합쳐짐
    report: str


def fan_out(state: BatchState) -> list[Send]:
    return [Send("process_image", {**img, **state["options"]}) for img in state["images"]]


def process_image(state: ImageState) -> dict:
    i = state["index"]
    emit(i, type="image_start", name=state["name"])
    try:
        final = image_graph.invoke(state)
    except llm.LLMError as e:
        emit(i, type="error", message=str(e))
        final = {**state, "error": str(e)}
    result = {k: final.get(k) for k in RESULT_KEYS}
    emit(i, type="image_done", image_type=result["image_type"],
         short=prompts.TYPE_SHORT.get(result["image_type"] or "", ""), revisions=result["revision"] or 0,
         approved=result["approved"], error=result["error"])
    return {"results": [result]}


def collect(state: BatchState) -> dict:
    return {"results": []}  # 병렬 브랜치가 모두 끝나기를 기다리는 합류 지점


def route_report(state: BatchState) -> str:
    ok = [r for r in state["results"] if r.get("draft")]
    return "report" if state.get("report_enabled") and len(ok) >= 2 else END


def report(state: BatchState) -> dict:
    results = sorted([r for r in state["results"] if r.get("draft")], key=lambda r: r["index"])
    workdir = Path(results[0]["image_path"]).parent
    emit("report", type="status", node="report", message=f"이미지 {len(results)}장 종합 리포트 작성 중…")
    analyses = "\n\n".join(
        f"=== 이미지 {r['index']}: {r['name']} (파일 {Path(r['image_path']).name}) ===\n{r['draft']}"
        + (f"\n\n[웹 검증]\n{r['verification']}" if r.get("verification") else "")
        + (f"\n\n[웹서칭]\n{r['research']}" if r.get("research") else "")
        for r in results
    )
    instruction = state["options"].get("instruction", "").strip()
    text = stream_into("report", "report", llm.stream_text(
        prompts.REPORT_PROMPT.format(
            count=len(results), files=", ".join(Path(r["image_path"]).name for r in results), analyses=analyses,
            instruction=f"\n[사용자 추가 지시]\n{instruction}" if instruction else "",
        ),
        prompts.REPORTER_SYSTEM, workdir,
    ))
    emit("report", type="status", node="report", message="종합 리포트 완료 ✓")
    emit("report", type="image_done", error=None)
    return {"report": text}


def build_batch_graph():
    g = StateGraph(BatchState)
    g.add_node("process_image", process_image)
    g.add_node("collect", collect)
    g.add_node("report", report)
    g.add_conditional_edges(START, fan_out, ["process_image"])
    g.add_edge("process_image", "collect")
    g.add_conditional_edges("collect", route_report, ["report", END])
    g.add_edge("report", END)
    return g.compile()


batch_graph = build_batch_graph()


# ═══ 3) 추가 질문 ══════════════════════════════════════════════════════

class QAState(TypedDict, total=False):
    image_paths: list[str]      # 질문 대상 이미지 (리포트 탭이면 전체)
    image_names: list[str]      # 사용자가 올린 원래 파일명 (image_paths와 같은 순서)
    context: str                # 분석 문서 / 리포트
    history: list[dict]         # [{q, a}]
    question: str
    web_enabled: bool
    answer: str


def answer(state: QAState) -> dict:
    paths = [Path(p) for p in state["image_paths"]]
    emit("qa", type="status", node="answer",
         message="답변 작성 중…" + (" (웹 검색 허용)" if state.get("web_enabled") else ""))
    history = "\n\n".join(f"Q: {h['q']}\nA: {h['a']}" for h in state.get("history", [])) or "(없음)"
    text = stream_into("qa", "answer", llm.stream_text(
        prompts.QA_PROMPT.format(files=_file_names(paths, state.get("image_names")), context=state["context"],
                                 history=history, question=state["question"]),
        prompts.QA_SYSTEM, paths[0].parent,
        tools="Read,WebSearch" if state.get("web_enabled") else "Read",
    ))
    return {"answer": text}


def _file_names(paths: list[Path], names: list[str] | None) -> str:
    names = names or [p.name for p in paths]
    return ", ".join(f"{p.name} (원래 이름: {n})" for p, n in zip(paths, names))


def build_qa_graph():
    g = StateGraph(QAState)
    g.add_node("answer", answer)
    g.add_edge(START, "answer")
    g.add_edge("answer", END)
    return g.compile()


qa_graph = build_qa_graph()
