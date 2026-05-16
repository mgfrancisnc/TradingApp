"""
run_logger.py
Shared run-logging layer for the Francis-Hayes Trading Bot.

Every command execution — whether fired by the scheduler daemon or triggered
manually from the web UI — flows through CommandRun. Each run produces:

  data/run_logs/<run_id>.log         full stdout/stderr capture
  data/run_logs/<run_id>.meta.json   command, trigger, timestamps, exit code

This is the single source of truth for "what has the bot done", and matches
the codebase pattern of persisting state as plain JSON files on disk so the
scheduler and web server can run as independent processes.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_LOGS_DIR = PROJECT_ROOT / "data" / "run_logs"

# Only these bot.py subcommands may be launched through here.
ALLOWED_COMMANDS = {"scan", "monitor", "status"}


def _run_id(command: str) -> str:
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    return f"{stamp}_{command}"


class CommandRun:
    """
    A single execution of `python bot.py <command>`.

    Use .stream() to iterate output lines as they arrive (web SSE), or
    .execute() to run to completion blocking (scheduler). Both write the
    .log and .meta.json side effects.
    """

    def __init__(self, command: str, trigger: str):
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f"command not allowed: {command!r}")

        self.command = command
        self.trigger = trigger
        self.run_id = _run_id(command)
        self.meta = {
            "id": self.run_id,
            "command": command,
            "trigger": trigger,
            "started_at": datetime.now().isoformat(),
            "ended_at": None,
            "exit_code": None,
            "status": "running",
            "log_file": f"{self.run_id}.log",
        }

    @property
    def _log_path(self) -> Path:
        return RUN_LOGS_DIR / f"{self.run_id}.log"

    @property
    def _meta_path(self) -> Path:
        return RUN_LOGS_DIR / f"{self.run_id}.meta.json"

    def _write_meta(self) -> None:
        RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(self._meta_path, "w") as f:
            json.dump(self.meta, f, indent=2)

    def stream(self) -> Iterator[str]:
        """
        Run the subprocess, yielding each output line as it arrives.
        Writes the log file incrementally and the meta file at start and end.
        """
        RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._write_meta()

        with open(self._log_path, "w") as log:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "bot.py", self.command],
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except Exception as e:
                msg = f"Failed to start: {e}\n"
                log.write(msg)
                self.meta.update(
                    ended_at=datetime.now().isoformat(),
                    exit_code=-1,
                    status="error",
                )
                self._write_meta()
                yield msg
                return

            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                yield line

            proc.wait()
            self.meta.update(
                ended_at=datetime.now().isoformat(),
                exit_code=proc.returncode,
                status="success" if proc.returncode == 0 else "error",
            )
            self._write_meta()

    def execute(self) -> dict:
        """Run to completion (blocking). Returns the final meta dict."""
        for _ in self.stream():
            pass
        return self.meta


def write_external_run(command: str, trigger: str, output: str, exit_code: int) -> dict:
    """
    Record a run whose output we already have in hand (e.g. the in-process
    execute flow, where stdout was captured rather than streamed).
    """
    run_id = _run_id(command)
    RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_LOGS_DIR / f"{run_id}.log", "w") as f:
        f.write(output)
    now = datetime.now().isoformat()
    meta = {
        "id": run_id,
        "command": command,
        "trigger": trigger,
        "started_at": now,
        "ended_at": now,
        "exit_code": exit_code,
        "status": "success" if exit_code == 0 else "error",
        "log_file": f"{run_id}.log",
    }
    with open(RUN_LOGS_DIR / f"{run_id}.meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def list_runs(limit: Optional[int] = None) -> list[dict]:
    """All recorded runs, newest first."""
    if not RUN_LOGS_DIR.exists():
        return []
    runs = []
    for meta_file in RUN_LOGS_DIR.glob("*.meta.json"):
        try:
            with open(meta_file) as f:
                runs.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[:limit] if limit else runs


def get_run(run_id: str) -> Optional[dict]:
    """A single run's meta plus its full captured output."""
    meta_path = RUN_LOGS_DIR / f"{run_id}.meta.json"
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        meta = json.load(f)
    log_path = RUN_LOGS_DIR / meta.get("log_file", f"{run_id}.log")
    meta["output"] = log_path.read_text() if log_path.exists() else ""
    return meta
