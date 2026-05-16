"""
scheduler.py
Automates the Francis-Hayes Trading Bot on a weekly schedule.

Schedule:
  Sunday  6:00 PM ET  — Weekly scan, generates report + saves approved trades
  Mon–Fri 9:45 AM ET  — Morning position check (15 min after open)
  Mon–Fri every 30min — Stop loss monitor during market hours
  Mon–Fri 3:45 PM ET  — End of day position check

Run continuously with:
    python bot.py schedule

Or set up as a cron job (recommended for production):
    crontab -e
    # Then add the lines printed by: python bot.py schedule --cron
"""

import atexit
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from execution.run_logger import CommandRun

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "scheduler_state.json"
PID_FILE = PROJECT_ROOT / "data" / "scheduler.pid"

# ── Schedule definition ───────────────────────────────────
SCHEDULE = [
    # Sunday scan — runs once on Sunday evening
    {
        "name": "Weekly Scan",
        "days": [6],                # 6 = Sunday
        "hour": 18,                 # 6:00 PM ET
        "minute": 0,
        "command": "scan",
        "description": "Scans universe, generates report for Hayes review",
    },
    # Morning check — Mon-Fri, 15 min after open
    {
        "name": "Morning Monitor",
        "days": [0, 1, 2, 3, 4],   # Mon-Fri
        "hour": 9,
        "minute": 45,
        "command": "monitor",
        "description": "First stop loss check of the day",
    },
    # Midday checks — every 30 min during market hours
    *[
        {
            "name": f"Monitor {h:02d}:{m:02d}",
            "days": [0, 1, 2, 3, 4],
            "hour": h,
            "minute": m,
            "command": "monitor",
            "description": "Stop loss check",
        }
        for h in range(10, 16)
        for m in [0, 30]
    ],
    # End of day
    {
        "name": "End of Day Monitor",
        "days": [0, 1, 2, 3, 4],
        "hour": 15,
        "minute": 45,
        "command": "monitor",
        "description": "Final stop loss check before close",
    },
]


class Scheduler:
    """
    Lightweight scheduler — no external dependencies needed.
    Checks every minute if a job should run.
    """

    def __init__(self):
        self._last_run: dict[str, str] = {}   # job name → last run date string
        self._started_at = datetime.now(ET)

    def run_forever(self):
        """Main loop — runs continuously, checks schedule every 60 seconds."""
        self._claim_pid()
        logger.info("Francis-Hayes Scheduler started")
        self._print_schedule()

        try:
            while True:
                now = datetime.now(ET)
                self._write_heartbeat(now)
                self._check_and_run(now)
                time.sleep(60)  # Check every minute
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopping")

    # ─────────────────────────────────────────────
    # PROCESS LIFECYCLE — lets the web UI start/stop us
    # ─────────────────────────────────────────────

    def _claim_pid(self):
        """
        Record our PID so the web UI can detect and signal this daemon.
        Whether launched from a terminal or spawned by the web server,
        the scheduler always owns its own PID file.
        """
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
        atexit.register(self._release_pid)
        # SIGTERM (what the web UI sends to stop us) → graceful exit so
        # the atexit handler runs and clears the PID file.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def _release_pid(self):
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass

    def _check_and_run(self, now: datetime):
        for job in SCHEDULE:
            if self._should_run(job, now):
                key = f"{job['name']}_{now.strftime('%Y-%m-%d')}"
                if key not in self._last_run:
                    self._last_run[key] = now.isoformat()
                    self._run_job(job, now)

    def _should_run(self, job: dict, now: datetime) -> bool:
        return (
            now.weekday() in job["days"]
            and now.hour == job["hour"]
            and now.minute == job["minute"]
        )

    def _run_job(self, job: dict, now: datetime):
        logger.info(f"Running: {job['name']} — {job['description']}")
        print(f"\n[{now.strftime('%H:%M ET')}] Starting: {job['name']}")
        try:
            meta = CommandRun(job["command"], trigger="scheduler").execute()
            if meta["exit_code"] == 0:
                logger.info(f"{job['name']} completed successfully")
            else:
                logger.error(
                    f"{job['name']} exited with code {meta['exit_code']} "
                    f"(see run {meta['id']})"
                )
        except Exception as e:
            logger.error(f"{job['name']} failed: {e}")

    # ─────────────────────────────────────────────
    # HEARTBEAT — lets the web UI see scheduler health
    # ─────────────────────────────────────────────

    def _write_heartbeat(self, now: datetime):
        """Persist liveness + upcoming jobs for the web UI to read."""
        try:
            state = {
                "started_at": self._started_at.isoformat(),
                "last_tick": now.isoformat(),
                "pid": os.getpid(),
                "upcoming": self._upcoming_jobs(now, count=5),
            }
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not write scheduler heartbeat: {e}")

    def _upcoming_jobs(self, now: datetime, count: int) -> list[dict]:
        """The next `count` job fire times across the whole schedule."""
        upcoming = []
        for job in SCHEDULE:
            nxt = self._next_run(job, now)
            if nxt:
                upcoming.append({
                    "name": job["name"],
                    "command": job["command"],
                    "description": job["description"],
                    "at": nxt.isoformat(),
                })
        upcoming.sort(key=lambda j: j["at"])
        return upcoming[:count]

    @staticmethod
    def _next_run(job: dict, now: datetime):
        """Soonest future datetime (ET) this job will fire, or None."""
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            if day.weekday() not in job["days"]:
                continue
            candidate = day.replace(
                hour=job["hour"], minute=job["minute"], second=0, microsecond=0
            )
            if candidate > now:
                return candidate
        return None

    def _print_schedule(self):
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        print("\n" + "=" * 55)
        print("  FRANCIS-HAYES BOT — ACTIVE SCHEDULE (ET)")
        print("=" * 55)
        print(f"  {'Job':<22} {'Days':<12} {'Time'}")
        print("  " + "-" * 50)
        seen = set()
        for job in SCHEDULE:
            day_str = ", ".join(days[d] for d in job["days"])
            label = job["name"]
            if "Monitor" in label and label != "Morning Monitor" and label != "End of Day Monitor":
                # Collapse midday monitors into one line
                if "Midday" not in seen:
                    seen.add("Midday")
                    print(f"  {'Stop Loss Monitor':<22} {'Mon-Fri':<12} 10:00–15:30 every 30min")
                continue
            print(f"  {label:<22} {day_str:<12} {job['hour']:02d}:{job['minute']:02d}")
        print("=" * 55 + "\n")

    def print_cron(self):
        """Prints cron job entries for production setup."""
        print("\n# Francis-Hayes Trading Bot — crontab entries")
        print("# Add with: crontab -e\n")
        print("# Weekly scan — Sunday 6pm ET")
        print("0 18 * * 0 cd ~/trading && python bot.py scan >> logs/scan.log 2>&1\n")
        print("# Stop loss monitor — every 30min Mon-Fri 9:45am-4pm ET")
        print("45 9 * * 1-5 cd ~/trading && python bot.py monitor >> logs/monitor.log 2>&1")
        print("0,30 10-15 * * 1-5 cd ~/trading && python bot.py monitor >> logs/monitor.log 2>&1")
        print("45 15 * * 1-5 cd ~/trading && python bot.py monitor >> logs/monitor.log 2>&1\n")
        print("# Make sure logs directory exists: mkdir -p ~/trading/logs\n")
