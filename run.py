"""실행: python run.py  → 서버를 띄우고 브라우저를 엽니다."""

import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8000

if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("app:app", host=HOST, port=PORT)
