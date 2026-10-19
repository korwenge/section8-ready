"""GD Claude desktop app: starts the local backend and opens the window.

    GD-Claude.exe              normal start
    GD-Claude.exe --browser    use your normal web browser instead of the app window
    GD-Claude.exe --selftest=FILE   start the backend, check it answers, write the result to FILE
                                    and exit with 0/1 (used by the build; the .exe has no console)
"""

import json
import platform
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))))

from app.server import AppState, config_dir, serve  # noqa: E402


def selftest(out_file):
    try:
        state = AppState()
        server, url, stop = serve(state)
        base, token = url.split("/?t=")
        req = urllib.request.Request(f"{base}/api/status", headers={"X-Token": token})
        status = json.load(urllib.request.urlopen(req, timeout=10))
        page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        if "GD Claude" not in page or "gd_running" not in status:
            raise RuntimeError("unexpected response")
        import webview  # noqa: F401 -- make sure the window library made it into the build
        result, code = f"selftest ok: catalog={status['catalog']} save_found={status['save_found']}", 0
        stop.set()
        server.shutdown()
    except Exception:
        result, code = f"selftest FAILED\n{traceback.format_exc()}", 1
    Path(out_file).write_text(result, encoding="utf-8")
    sys.exit(code)


def run():
    state = AppState()
    server, url, stop = serve(state)
    try:
        import webview
    except ImportError:
        webview = None
    if webview and "--browser" not in sys.argv:
        window = webview.create_window("GD Claude", url, width=1440, height=900, min_size=(1000, 640),
                                       background_color="#0f1117")
        state.window = window
        webview.start()
    else:  # no embedded browser available: use the normal one
        if sys.stdout:  # the windowed .exe has no console
            print(f"GD Claude is running at {url}\nClose this window to quit.")
        webbrowser.open(url)
        threading.Event().wait()
    stop.set()
    server.shutdown()


def report_crash():
    """The .exe has no console, so write the error to a file and show a message box."""
    log = config_dir() / "crash.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{time.ctime()}\n{traceback.format_exc()}", encoding="utf-8")
    message = f"GD Claude couldn't start.\n\nDetails were saved to:\n{log}\n\nSend that file for help."
    if platform.system() == "Windows":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "GD Claude", 0x10)
    elif sys.stderr:
        print(message, file=sys.stderr)


def main():
    for arg in sys.argv[1:]:
        if arg.startswith("--selftest"):
            selftest(arg.partition("=")[2] or config_dir() / "selftest.txt")
    try:
        run()
    except Exception:
        report_crash()
        sys.exit(1)


if __name__ == "__main__":
    main()
