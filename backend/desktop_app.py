"""
desktop_app.py - Windows desktop entry point.
"""

import os
import socket
import threading
import time

os.environ.setdefault("USE_PARALLEL", "1")

import webview  # pywebview


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(port):
    import app as flask_app_module
    flask_app_module.app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


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


def main():
    port = _find_free_port()
    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server_thread.start()

    time.sleep(1.0)

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
