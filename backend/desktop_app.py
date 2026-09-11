"""
desktop_app.py - Windows desktop entry point.
"""

import os
import sys
import socket
import threading
import time
import traceback

os.environ.setdefault("USE_PARALLEL", "1")

import webview  # pywebview


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_file_path():
    """A location next to the exe (or script, when not frozen) that's
    writable and easy for the person to find and send back if something
    goes wrong -- 'error_log.txt' sitting right next to the exe itself."""
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "error_log.txt")


def _report_startup_failure(exc):
    """
    Surfaces a server-startup failure to the person running the app,
    instead of it being silently swallowed. A --windowed PyInstaller build
    has NO visible console -- an unhandled exception in this background
    thread would previously just vanish, leaving only a browser-style
    'connection refused' page with zero indication of what actually went
    wrong (this is exactly what produced that confusing error previously).
    Shows a native Windows message box with the real error, and also
    writes it to error_log.txt next to the exe so it can be shared for
    troubleshooting even after the dialog is dismissed.
    """
    error_text = f"The app's background server failed to start:\n\n{exc}\n\n{traceback.format_exc()}"

    try:
        log_path = _log_file_path()
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(error_text)
    except Exception:
        log_path = None

    message = error_text
    if log_path:
        message += f"\n\n(This has also been saved to: {log_path})"

    try:
        import ctypes
        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(0, message, "Bank Statement Converter - Startup Error", MB_ICONERROR)
    except Exception:
        # Not on Windows, or MessageBoxW unavailable for some reason --
        # at minimum this prints to stderr, which is better than nothing
        # even though a --windowed build's console is normally hidden.
        print(error_text, file=sys.stderr)


def _run_server(port):
    try:
        import app as flask_app_module
        flask_app_module.app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
    except Exception as e:
        _report_startup_failure(e)


class Api:
    """Exposed to the frontend as `window.pywebview.api`."""

    def __init__(self, port):
        self.port = port

    def save_excel(self, job_id):
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{self.port}/api/download/{job_id}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                if resp.status != 200:
                    return {"ok": False, "error": f"Server returned status {resp.status}"}
                file_bytes = resp.read()
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"File not ready yet ({e.code})."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            directory="",
            save_filename="converted_statement.xlsx",
            file_types=("Excel Files (*.xlsx)",),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        save_path = result[0] if isinstance(result, (list, tuple)) else result
        if not save_path:
            return {"ok": False, "cancelled": True}

        try:
            with open(save_path, "wb") as f:
                f.write(file_bytes)
        except Exception as e:
            return {"ok": False, "error": f"Could not write file: {e}"}

        return {"ok": True, "path": save_path}


def _wait_for_server(port, timeout_seconds=15):
    """Poll the server instead of a single fixed sleep, so a slow-starting
    server (e.g. first-run antivirus scanning the exe) isn't mistaken for
    a failure, while a genuinely failed server doesn't leave the window
    opening against a dead port for the full fixed delay regardless."""
    import urllib.request
    import urllib.error

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # server responded (even an error status means it's up)
        except Exception:
            time.sleep(0.3)
    return False


def main():
    port = _find_free_port()
    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server_thread.start()

    if not _wait_for_server(port):
        # The server never came up -- if it crashed with an exception,
        # _report_startup_failure already showed a dialog explaining why.
        # Don't also open a window pointed at a dead port on top of that.
        return

    api = Api(port)
    webview.create_window(
        "Bank Statement to Excel Converter",
        f"http://127.0.0.1:{port}",
        width=760,
        height=820,
        resizable=True,
        js_api=api,
    )
    webview.start()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
