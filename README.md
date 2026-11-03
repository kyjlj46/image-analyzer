# 이미지 분석 에이전트

이미지를 올리면 LangGraph 에이전트가 여러 장을 동시에 분석하고, 스스로 검토하고, 웹으로 사실을 확인한 뒤
종합 리포트와 텍스트(.txt) 문서로 만들어 줍니다. 분석 후에는 이미지에 대해 추가로 질문할 수 있습니다. API 키 없이, 이 PC에 로그인된 Claude Code 계정으로 동작합니다.

## 실행 (아래 중 아무거나)
- `start.bat` 더블클릭
- VS Code에서 F5 (실행 구성: "이미지 분석 에이전트 실행")
- 터미널에서 `python run.py` 또는 `uv run python run.py`  (포트 변경: `PORT=8080`)

처음 실행하면 `.venv` 가상환경과 필요한 패키지가 자동으로 설치되고, 브라우저가 http://127.0.0.1:8000 으로 열립니다.
결과는 화면에 실시간으로 표시되고 `outputs/` 폴더에 .txt로 저장됩니다.

## 기능
- **여러 장 동시 분석**: 최대 10장, 동시 처리 개수 1~5 선택 (LangGraph `Send`로 병렬 실행)
- **자동 유형 판별**: 문서 / 차트 / 일반 이미지를 판별해 맞는 분석 방식 선택
- **검토 · 재작성**: 검토자가 원본과 대조해 부족하면 피드백을 주고 다시 작성 (최대 1~3회)
- **웹 검색 검증**: 브랜드·장소·인물 등 고유 정보를 Claude Code WebSearch로 사실 확인 (출처 URL 포함)
- **종합 리포트**: 2장 이상이면 전체를 묶어 공통점·차이점·인사이트 정리
- **추가 질문**: 분석 후 이미지 탭이나 리포트 탭에서 질문 (웹 검색 허용 선택 가능). Q&A도 .txt에 저장

## 에이전트 구조 (LangGraph, `agent/graph.py`)

```
batch_graph   START ─(Send × N, 병렬)─▶ process_image ─▶ collect ─▶ [report] ─▶ END
                                            │
image_graph   START ─┬─(자동)─▶ classify ─┐ ▼
                     └────────────────────┴─▶ analyze ─▶ [review ⟲ analyze] ─▶ [verify] ─▶ END

qa_graph      START ─▶ answer ─▶ END   (Read, 웹 허용 시 Read+WebSearch)
```

| 노드 | 하는 일 | 쓰는 도구 |
|---|---|---|
| classify | 이미지 유형 판별 (document / chart / general) | Read |
| analyze | 유형별 프롬프트로 분석 문서 작성, 피드백 반영 재작성 | Read |
| review | 원본과 대조해 정확성·완전성·형식 검토 | Read |
| verify | 고유 정보를 웹 검색으로 사실 확인 | Read, WebSearch |
| report | 여러 장의 분석을 묶어 종합 리포트 | Read |
| answer | 분석 후 추가 질문에 답변 (이전 대화 기억) | Read (+WebSearch) |

## 파일 구성
| 파일 | 역할 |
|---|---|
| `agent/graph.py` | 상태, 노드, 라우팅, 3개 그래프 정의 |
| `agent/prompts.py` | 노드별 프롬프트, 이미지 유형 목록 |
| `agent/llm.py` | LLM 호출 계층 (`claude -p` CLI 래퍼, 노드별 도구 제한) |
| `app.py` | FastAPI 서버. `/api/analyze`, `/api/ask`, `/api/download`. 이벤트를 NDJSON으로 스트리밍 |
| `static/index.html` | 웹 화면 (이미지별 탭 · 단계 로그 · 결과 · 질문 채팅) |
| `run.py`, `start.bat` | 실행기 (.venv 자동 준비) |
| `sessions/` | 업로드 이미지와 결과 (추가 질문용, git 제외) |
| `outputs/` | 결과 .txt (git 제외) |

## 확장 방법
- **노드 추가**: `agent/graph.py`에 `(state) -> dict` 함수를 만들고 `build_*_graph()`에서 `add_node` / `add_edge`.
  UI에 진행 상황을 보내려면 노드 안에서 `emit(index, type="status", node="...", message="...")`,
  텍스트를 흘려보내려면 `stream_into(index, part, llm.stream_text(...))`.
- **이미지 유형 추가**: `agent/prompts.py`의 `IMAGE_TYPES`, `ANALYZE_PROMPTS`, `CLASSIFY_PROMPT`.
- **도구 추가**: `llm.stream_text(..., tools="Read,WebSearch,WebFetch")`처럼 노드별로 Claude Code 도구 허용.
- **LLM 교체**: `agent/llm.py`의 `stream_text()` / `complete()`만 바꾸면 됨 (예: Anthropic API).

## 필요 조건
- Claude Code 설치 및 로그인 (`claude` 명령이 동작해야 함)
- Python 3.10+ (uv가 있으면 uv를 우선 사용)
