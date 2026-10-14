"""
app.py - Flask API for PDF bank statement -> Excel conversion.
"""

import os
import sys
import uuid
import threading
import time

from flask import Flask, request, jsonify, send_file, render_template, Response
from werkzeug.utils import secure_filename

from extractor import get_page_count, estimate_tat, process_pdf, process_pdf_parallel, write_excel
import banks

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

if getattr(sys, "frozen", False):
    # Running as a PyInstaller-frozen executable: --add-data bundles
    # 'frontend' directly at the extraction root (sys._MEIPASS) at build
    # time, NOT as a sibling of this file's own location the way it is in
    # the normal source tree -- BASE_DIR/../frontend does not exist inside
    # a frozen build and would make Flask (and the background server
    # thread that runs it) fail to start with no visible error, since a
    # --windowed exe has no console to show a traceback on.
    FRONTEND_DIR = os.path.join(sys._MEIPASS, "frontend")
else:
    FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = Flask(
    __name__,
    static_folder=FRONTEND_DIR,
    static_url_path="/static",
    template_folder=FRONTEND_DIR,
)

_resolved_frontend_dir = os.path.abspath(FRONTEND_DIR)
if not os.path.isfile(os.path.join(_resolved_frontend_dir, "index.html")):
    parent = os.path.dirname(_resolved_frontend_dir)
    try:
        parent_contents = os.listdir(parent)
    except OSError:
        parent_contents = ["<could not list>"]
    raise RuntimeError(
        "\n\nFATAL: could not find frontend/index.html at "
        f"{_resolved_frontend_dir}\nContents of {parent}: {parent_contents}\n"
        "Check that Dockerfile, backend/, and frontend/ are siblings at the "
        "repo root, and that your host's Root Directory setting matches. "
        "(If this is a PyInstaller build: confirm --add-data \"..\\frontend;frontend\" "
        "was included in the build command.)\n"
    )

app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()
JOB_TTL_SECONDS = 3600

USE_PARALLEL = os.environ.get("USE_PARALLEL", "0") == "1"

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


def _auth_required():
    return bool(APP_USERNAME and APP_PASSWORD)


def _check_auth(auth):
    return auth and auth.username == APP_USERNAME and auth.password == APP_PASSWORD


@app.before_request
def _require_login():
    if not _auth_required():
        return None
    auth = request.authorization
    if not _check_auth(auth):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Bank Statement Converter"'},
        )
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/robots.txt")
def robots():
    return app.send_static_file("robots.txt")


@app.route("/api/banks", methods=["GET"])
def bank_list():
    return jsonify({"banks": banks.public_bank_list()})


def _output_name(original_filename):
    base = os.path.splitext(os.path.basename(original_filename or ""))[0].strip() or "statement"
    base = "".join(ch for ch in base if ch not in '\\/:*?"<>|').strip() or "statement"
    return f"{base}.xlsx"


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    job_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    saved_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    file.save(saved_path)

    try:
        num_pages = get_page_count(saved_path)
    except Exception as e:
        os.remove(saved_path)
        return jsonify({"error": f"Could not read PDF: {e}"}), 400

    detected_bank, is_scanned, detect_reason = banks.detect_bank(saved_path)
    if not is_scanned:
        is_scanned = banks.likely_scanned(saved_path)
    tat_seconds = estimate_tat(num_pages, likely_ocr=is_scanned)
    if USE_PARALLEL:
        cores = max(1, (os.cpu_count() or 2) - 1)
        tat_seconds = max(3, round(tat_seconds / cores))

    with jobs_lock:
        jobs[job_id] = {
            "status": "uploaded",
            "created_at": time.time(),
            "file_path": saved_path,
            "total_pages": num_pages,
            "pages_done": 0,
            "tat_seconds": tat_seconds,
            "output_path": None,
            "error": None,
            "original_name": file.filename,
            "download_name": _output_name(file.filename),
            "is_scanned": is_scanned,
            "detected_bank": detected_bank,
            "stage": "uploaded",
            "started_at": None,
            "extract_started_at": None,
            "finished_at": None,
            "result": None,
        }

    return jsonify(
        {
            "job_id": job_id,
            "num_pages": num_pages,
            "estimated_seconds": tat_seconds,
            "is_scanned": is_scanned,
            "detected_bank": detected_bank,
            "detect_reason": detect_reason,
            "download_name": _output_name(file.filename),
        }
    )


def _set_stage(job_id, stage):
    with jobs_lock:
        jobs[job_id]["stage"] = stage


def _run_job(job_id, merge_pages, bank_id):
    with jobs_lock:
        job = jobs[job_id]
        job["status"] = "processing"
        job["stage"] = "reading"
        job["pages_done"] = 0
        job["started_at"] = time.time()
        job["extract_started_at"] = None
        job["finished_at"] = None
        job["result"] = None
        job["error"] = None
        job["bank"] = bank_id
        pdf_path = job["file_path"]
        detected = job.get("detected_bank")

    profile = banks.get_profile(bank_id)

    def progress_cb(done, total):
        with jobs_lock:
            j = jobs[job_id]
            if j["extract_started_at"] is None:
                j["extract_started_at"] = j["started_at"]
            j["pages_done"] = done
            j["stage"] = "extracting"

    try:
        with jobs_lock:
            jobs[job_id]["stage"] = "extracting"
            jobs[job_id]["extract_started_at"] = time.time()
        runner = process_pdf_parallel if USE_PARALLEL else process_pdf
        results, methods = runner(pdf_path, progress_callback=progress_cb,
                                  header_hints=profile.get("header_hints") or None)

        _set_stage(job_id, "writing")
        output_path = os.path.join(OUTPUT_DIR, f"{job_id}.xlsx")
        summary = write_excel(results, output_path, merge_pages=merge_pages) or {}

        _set_stage(job_id, "validating")
        printed = banks.read_printed_summary(pdf_path)
        checks = banks.compare_totals(printed, summary)
        warnings = []
        if detected and bank_id not in ("other", detected):
            warnings.append(
                f"You selected {profile['name']}, but this statement looks like "
                f"{banks.get_profile(detected)['name']}. Check the output or re-run with that bank."
            )
        layout_ok = banks.header_matches_profile(summary.get("header"), profile)
        if layout_ok is False:
            warnings.append(f"Column layout differs from the usual {profile['name']} format. Please review the output.")
        if not summary.get("rows"):
            warnings.append("No transaction rows were found. Try 'Other bank (auto-detect layout)'.")
        if profile.get("status") == "beta":
            warnings.append(f"{profile['name']} support is in beta. Please spot-check a few rows.")

        with jobs_lock:
            j = jobs[job_id]
            j["status"] = "done"
            j["stage"] = "done"
            j["finished_at"] = time.time()
            j["output_path"] = output_path
            j["methods"] = methods
            j["result"] = {
                "rows": summary.get("rows", 0),
                "needs_review": summary.get("needs_review", 0),
                "columns": summary.get("header", []),
                "debits": summary.get("debits"),
                "credits": summary.get("credits"),
                "closing": summary.get("closing"),
                "checks": checks,
                "warnings": warnings,
                "bank": {"id": profile["id"], "name": profile["name"], "status": profile["status"]},
                "ocr_pages": sum(1 for m in methods.values() if m == "ocr"),
                "seconds": round(j["finished_at"] - j["started_at"], 1),
            }
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["stage"] = "error"
            jobs[job_id]["error"] = str(e)


@app.route("/api/process/<job_id>", methods=["POST"])
def process(job_id):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "Unknown job_id"}), 404
        if jobs[job_id]["status"] == "processing":
            return jsonify({"error": "Already processing"}), 400

    data = request.get_json(silent=True) or {}
    merge_pages = bool(data.get("merge_pages", True))
    bank_id = banks.get_profile(data.get("bank"))["id"]

    thread = threading.Thread(target=_run_job, args=(job_id, merge_pages, bank_id), daemon=True)
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/status/<job_id>", methods=["GET"])
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job_id"}), 404
        now = time.time()
        started = job.get("started_at")
        elapsed = (job.get("finished_at") or now) - started if started else 0.0
        return jsonify(
            {
                "status": job["status"],
                "stage": job.get("stage"),
                "pages_done": job["pages_done"],
                "total_pages": job["total_pages"],
                "estimated_seconds": job["tat_seconds"],
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": _live_eta(job, now),
                "is_scanned": job.get("is_scanned"),
                "result": job.get("result"),
                "error": job["error"],
            }
        )


def _live_eta(job, now):
    """
    Remaining-time estimate that starts from the upload-time guess and
    shifts to the measured pace as pages complete. After ~5 pages it is
    driven entirely by the real seconds-per-page on this machine.
    """
    if job["status"] != "processing" or not job.get("started_at"):
        return 0 if job["status"] == "done" else None
    total = max(1, job["total_pages"])
    done = job["pages_done"]
    remaining_pages = max(0, total - done)
    tail = 1.5 + 0.01 * total  # writing Excel + totals check
    elapsed = now - job["started_at"]
    initial_remaining = max(0.0, job["tat_seconds"] - elapsed)
    if job.get("stage") in ("writing", "validating"):
        return round(max(1.0, tail * 0.5), 1)
    if done == 0:
        return round(max(1.0, initial_remaining), 1)
    ext_start = job.get("extract_started_at") or job["started_at"]
    pace = (now - ext_start) / done
    live = pace * remaining_pages + tail
    w = min(1.0, done / 5.0)
    return round(max(1.0, w * live + (1 - w) * initial_remaining), 1)


@app.route("/api/download/<job_id>", methods=["GET"])
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 400
    return send_file(
        job["output_path"],
        as_attachment=True,
        download_name=job.get("download_name") or "converted_statement.xlsx",
    )


def _cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL_SECONDS
        with jobs_lock:
            expired = [jid for jid, j in jobs.items() if j["created_at"] < cutoff]
            for jid in expired:
                job = jobs.pop(jid)
                for path in (job.get("file_path"), job.get("output_path")):
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, threaded=True, port=int(os.environ.get("PORT", 5000)), host="0.0.0.0")
