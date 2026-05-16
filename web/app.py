"""
web/app.py — Francis-Hayes Trading Bot web console.

A localhost-only window into the always-on bot. It does NOT own the bot:
the scheduler runs as its own detached process and writes state to disk.
This server reads that state, can start/stop the scheduler by spawning /
signalling it, and triggers manual runs through the same shared run-logger
the scheduler uses.

Run with:
    python bot.py web          (binds 127.0.0.1:8787)

Network exposure: localhost only by design. No auth. Do not expose this
port — it can submit real trades.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytz
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

from execution.run_logger import (
    ALLOWED_COMMANDS,
    CommandRun,
    get_run,
    list_runs,
    write_external_run,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "scheduler_state.json"
PID_FILE = PROJECT_ROOT / "data" / "scheduler.pid"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
RUN_LOGS_DIR = PROJECT_ROOT / "data" / "run_logs"
ET = pytz.timezone("America/New_York")

# Scheduler ticks every 60s; treat >180s of silence as not-running.
HEARTBEAT_STALE_SECONDS = 180

app = Flask(__name__)

# One manual run at a time — prevents two scans clobbering each other.
_run_lock = threading.Lock()

# Built lazily: importing Alpaca + constructing clients is slow and needs env.
_components = None
_components_lock = threading.Lock()


def get_components():
    global _components
    with _components_lock:
        if _components is None:
            from bot import build_components, load_philosophy
            _components = build_components(load_philosophy())
        return _components


def _executor():
    return get_components()[6]


def _sse(event: str, payload) -> str:
    return f"data: {json.dumps({'event': event, 'payload': payload})}\n\n"


# ─────────────────────────────────────────────
# Scheduler process helpers
# ─────────────────────────────────────────────

def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _scheduler_status() -> dict:
    pid = _read_pid()
    alive = bool(pid and _pid_alive(pid))

    state = {}
    fresh = False
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            last_tick = datetime.fromisoformat(state["last_tick"])
            age = (datetime.now(ET) - last_tick).total_seconds()
            state["last_tick_age_seconds"] = int(age)
            fresh = age < HEARTBEAT_STALE_SECONDS
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            state = {}

    if alive and fresh:
        status = "running"
    elif alive and not fresh:
        status = "stale"          # process up but heartbeat silent — likely hung
    else:
        status = "stopped"

    return {"status": status, "running": status == "running", "pid": pid, **state}


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def api_meta():
    paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    return jsonify({"paper": paper})


# ─────────────────────────────────────────────
# Scheduler health + control
# ─────────────────────────────────────────────

@app.route("/api/scheduler")
def api_scheduler():
    return jsonify(_scheduler_status())


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    if _scheduler_status()["status"] in ("running", "stale"):
        return jsonify({"ok": False, "error": "scheduler already running"}), 409

    RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = open(RUN_LOGS_DIR / "scheduler.out", "a")
    # Detached: own session so it survives this web server restarting.
    subprocess.Popen(
        [sys.executable, "bot.py", "schedule"],
        cwd=PROJECT_ROOT,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return jsonify({"ok": True})


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    pid = _read_pid()
    if not pid or not _pid_alive(pid):
        return jsonify({"ok": False, "error": "scheduler not running"}), 409
    # SIGTERM — the scheduler handles it gracefully and clears its own PID.
    os.kill(pid, signal.SIGTERM)
    return jsonify({"ok": True})


@app.route("/api/scheduler/restart", methods=["POST"])
def api_scheduler_restart():
    pid = _read_pid()
    if pid and _pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):  # up to ~10s for graceful exit
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        if _pid_alive(pid):
            return jsonify({"ok": False, "error": "old scheduler did not stop"}), 500

    RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = open(RUN_LOGS_DIR / "scheduler.out", "a")
    subprocess.Popen(
        [sys.executable, "bot.py", "schedule"],
        cwd=PROJECT_ROOT,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# Portfolio status (read-only, in-process)
# ─────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    try:
        components = get_components()
        client = components[0]
        exit_monitor = components[5]

        account = client.get_account()
        option_positions = client.get_open_option_positions()
        stock_positions = client.get_stock_positions()
        tracked = exit_monitor.get_positions()

        return jsonify({
            "ok": True,
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "stock_positions": len(stock_positions),
            "open_calls": len(option_positions),
            "tracked": [
                {
                    "symbol": p.symbol,
                    "strike": p.strike,
                    "expiry": p.expiry,
                    "dte": p.dte,
                    "contracts": p.contracts,
                    "entry_premium": p.entry_premium,
                }
                for p in tracked
            ],
            "holdings": [
                {
                    "symbol": p.symbol,
                    "qty": int(float(p.qty)),
                    "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                }
                for p in stock_positions
            ],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
# Run history
# ─────────────────────────────────────────────

@app.route("/api/runs")
def api_runs():
    return jsonify(list_runs(limit=100))


@app.route("/api/runs/<run_id>")
def api_run_detail(run_id):
    run = get_run(run_id)
    if run is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(run)


# ─────────────────────────────────────────────
# Manual command trigger — live SSE stream
# ─────────────────────────────────────────────

@app.route("/stream/<command>")
def stream_command(command):
    if command not in ALLOWED_COMMANDS:
        return jsonify({"error": f"command not allowed: {command}"}), 400

    if not _run_lock.acquire(blocking=False):
        return jsonify({"error": "a run is already in progress"}), 409

    def generate():
        try:
            run = CommandRun(command, trigger="manual")
            yield _sse("start", {"id": run.run_id, "command": command})
            for line in run.stream():
                yield _sse("line", line.rstrip("\n"))
            yield _sse("done", run.meta)
        finally:
            _run_lock.release()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────
# Execute — review + confirm in the browser
# ─────────────────────────────────────────────

@app.route("/api/execute/pending")
def api_execute_pending():
    try:
        return jsonify({"ok": True, **_executor().get_pending_trades()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/execute/submit", methods=["POST"])
def api_execute_submit():
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"ok": False, "error": "no symbols selected"}), 400

    if not _run_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "a run is already in progress"}), 409

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = _executor().execute_selected(symbols)
        output = buf.getvalue()
        print(output, end="")  # also surface in the server console
        failed = bool(result.get("failed"))
        meta = write_external_run(
            "execute", "manual", output, exit_code=1 if failed else 0
        )
        return jsonify({"ok": True, "result": result, "run": meta})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _run_lock.release()


# ─────────────────────────────────────────────
# Reports archive
# ─────────────────────────────────────────────

@app.route("/api/reports")
def api_reports():
    if not REPORTS_DIR.exists():
        return jsonify([])
    return jsonify(sorted(
        (p.name for p in REPORTS_DIR.glob("scan_*.txt")), reverse=True
    ))


@app.route("/api/reports/<name>")
def api_report_detail(name):
    # Guard against path traversal: resolve and confirm it stays in REPORTS_DIR.
    target = (REPORTS_DIR / name).resolve()
    if target.parent != REPORTS_DIR.resolve() or not target.is_file():
        return jsonify({"error": "not found"}), 404
    return jsonify({"name": name, "content": target.read_text()})


def main(port: int = None):
    port = port or int(os.environ.get("FH_WEB_PORT", "8787"))
    print(f"\n  Francis-Hayes web console → http://127.0.0.1:{port}")
    print("  Localhost only. Ctrl-C to stop.\n")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
