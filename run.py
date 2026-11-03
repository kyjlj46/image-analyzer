"""실행: python run.py  (VS Code ▶ 버튼, start.bat 모두 OK)

어떤 Python으로 실행하든 프로젝트 가상환경(.venv)을 자동으로 준비하고 그 안에서 서버를 띄운 뒤
브라우저를 엽니다.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8000"))


def in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def deps_installed(py: Path) -> bool:
    return subprocess.run([py, "-c", "import fastapi, uvicorn, multipart, langgraph"],
                          capture_output=True).returncode == 0


def ensure_venv() -> None:
    """.venv가 없거나 패키지가 빠져 있으면 설치한다 (uv 우선, 없으면 venv + pip)."""
    if VENV_PY.exists() and deps_installed(VENV_PY):
        return
    print("[준비] 처음 실행이라 필요한 패키지를 설치합니다…")
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv.exe")
    if Path(uv).exists():
        subprocess.run([uv, "sync"], cwd=ROOT, check=True)
    else:
        if not VENV_PY.exists():
            subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
        subprocess.run([VENV_PY, "-m", "pip", "install", "-r", ROOT / "requirements.txt"], check=True)


def main() -> None:
    os.chdir(ROOT)
    if not in_project_venv():
        ensure_venv()
        # 프로젝트 가상환경의 Python으로 다시 실행
        sys.exit(subprocess.run([VENV_PY, __file__, *sys.argv[1:]]).returncode)

    import threading
    import webbrowser

    import uvicorn

    url = f"http://{HOST}:{PORT}"
    print(f"[실행] {url}  (종료: Ctrl+C)")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app:app", host=HOST, port=PORT)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
