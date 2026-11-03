# 이미지 분석 에이전트

이미지를 올리면 Claude가 내용을 이해하고 분석해서 텍스트(.txt) 문서로 만들어 줍니다.
API 키 없이, 이 PC에 로그인된 Claude Code 계정으로 동작합니다.

## 실행 (아래 중 아무거나)
- `start.bat` 더블클릭
- VS Code에서 F5 (실행 구성: "이미지 분석 에이전트 실행")
- 터미널에서 `python run.py` 또는 `uv run python run.py`

처음 실행하면 `.venv` 가상환경과 필요한 패키지가 자동으로 설치되고,
브라우저가 http://127.0.0.1:8000 으로 열립니다.
결과는 화면에 실시간으로 표시되고 `outputs/` 폴더에 .txt로 저장됩니다.

## 필요 조건
- Claude Code 설치 및 로그인 (`claude` 명령이 동작해야 함)
- Python 3.10+ (uv가 있으면 uv를 우선 사용)
