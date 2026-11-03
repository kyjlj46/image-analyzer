# 이미지 분석 에이전트

이미지를 올리면 LangGraph 에이전트가 이미지 유형을 판별하고, 분석하고, 원본과 대조해 스스로 검토한 뒤
텍스트(.txt) 문서로 만들어 줍니다. API 키 없이, 이 PC에 로그인된 Claude Code 계정으로 동작합니다.

## 실행 (아래 중 아무거나)
- `start.bat` 더블클릭
- VS Code에서 F5 (실행 구성: "이미지 분석 에이전트 실행")
- 터미널에서 `python run.py` 또는 `uv run python run.py`  (포트 변경: `PORT=8080`)

처음 실행하면 `.venv` 가상환경과 필요한 패키지가 자동으로 설치되고, 브라우저가 http://127.0.0.1:8000 으로 열립니다.
결과는 화면에 실시간으로 표시되고 `outputs/` 폴더에 .txt로 저장됩니다.

## 에이전트 구조 (LangGraph)

```
START ─┬─(자동 판별)──▶ classify ──┐
       └─(모드 지정)───────────────┴─▶ analyze ─┬─(검토 끔)────────────▶ END
                                       ▲        └─▶ review ─┬─(통과/한도)─▶ END
                                       └────(수정 필요)──────┘
```

| 노드 | 하는 일 |
|---|---|
| classify | 이미지를 보고 document / chart / general 중 분석 방식을 선택 |
| analyze | 유형별 프롬프트로 분석 문서 작성 (검토 피드백이 있으면 반영해 재작성) |
| review | 원본 이미지와 분석 문서를 대조해 정확성·완전성·형식 검토, 통과 여부와 피드백 |

## 파일 구성
| 파일 | 역할 |
|---|---|
| `agent/graph.py` | 상태(ImageState), 노드, 라우팅, 그래프 정의 |
| `agent/prompts.py` | 노드별 프롬프트, 이미지 유형 목록 |
| `agent/llm.py` | LLM 호출 계층 (`claude -p` CLI 래퍼) |
| `app.py` | FastAPI 서버. 그래프 실행 이벤트를 NDJSON으로 스트리밍 |
| `static/index.html` | 웹 화면 (단계 로그 + 결과) |
| `run.py`, `start.bat` | 실행기 (.venv 자동 준비) |

## 확장 방법
- **노드 추가**: `agent/graph.py`에 `(state) -> dict` 함수를 만들고 `build_graph()`에서 `add_node` / `add_edge`.
  UI에 진행 상황을 보내려면 노드 안에서 `get_stream_writer()({"type": "status", "node": ..., "message": ...})`.
- **이미지 유형 추가**: `agent/prompts.py`의 `IMAGE_TYPES`, `ANALYZE_PROMPTS`, `CLASSIFY_PROMPT`에 항목 추가.
- **LLM 교체**: `agent/llm.py`의 `stream_text()`만 바꾸면 됨 (예: Anthropic API).
- **도구 사용(tool calling)**: `llm.stream_text(..., tools="Read,WebSearch")`처럼 Claude Code 도구를 노드별로 허용 가능.

## 필요 조건
- Claude Code 설치 및 로그인 (`claude` 명령이 동작해야 함)
- Python 3.10+ (uv가 있으면 uv를 우선 사용)
