"""
notifications/email_notifier.py
Email alert system for the Francis-Hayes Trading Bot.

Sends alerts for:
  - Weekly scan complete (Sunday evening — approved trade list)
  - ITM warning (stock above strike with ≤3 DTE — assignment likely)
  - Assignment (shares called away, cash returned to pool)
  - Expired worthless (call expired below strike, keep shares, write again)
  - Circuit breaker (portfolio down ≥3% today — new trades halted)
  - Expiring soon (≤7 DTE — informational heads-up, once per day)

Uses stdlib smtplib — no extra dependencies.

Gmail setup (recommended):
  1. Enable 2-factor authentication on your Gmail account
  2. Google Account → Security → App Passwords → create one named "mgfbot"
  3. Set ALERT_SMTP_PASSWORD to the 16-character app password (not your login password)

Required environment variables:
  ALERT_EMAIL_FROM      your Gmail address (e.g. you@gmail.com)
  ALERT_EMAIL_TO        where to send alerts (can be same address, or SMS gateway)
  ALERT_SMTP_PASSWORD   Gmail app password

Optional:
  ALERT_SMTP_HOST       default: smtp.gmail.com
  ALERT_SMTP_PORT       default: 587
"""

import json
import logging
import os
import smtplib
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

logger = logging.getLogger(__name__)

COOLDOWN_FILE = "data/email_cooldown.json"

# Alert types that send at most once per day per symbol
DAILY_COOLDOWN_TYPES = {"EXPIRING_SOON", "ITM_WARNING", "CIRCUIT_BREAKER"}


class EmailNotifier:
    """
    Sends email alerts via SMTP.
    Gracefully no-ops if email is not configured — bot continues without alerts.
    """

    def __init__(self):
        self.from_addr = os.environ.get("ALERT_EMAIL_FROM", "")
        self.to_addr = os.environ.get("ALERT_EMAIL_TO", "")
        self.password = os.environ.get("ALERT_SMTP_PASSWORD", "")
        self.smtp_host = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "587"))
        self._cooldown: dict = self._load_cooldown()
        self.enabled = bool(self.from_addr and self.to_addr and self.password)

        if not self.enabled:
            logger.info("Email alerts not configured — set ALERT_EMAIL_FROM, ALERT_EMAIL_TO, ALERT_SMTP_PASSWORD in .env")

    # ─────────────────────────────────────────────
    # PUBLIC — ALERT SENDERS
    # ─────────────────────────────────────────────

    def notify_alerts(self, alerts: list) -> None:
        """Send emails for a list of MonitorAlert objects from exit_monitor."""
        for alert in alerts:
            self._send_alert(alert)

    def notify_scan_complete(self, results: list) -> None:
        """Send scan completion summary email after Sunday scan."""
        if not self.enabled:
            return

        approved = [r for r in results if r.approved]
        lots = [r for r in approved if r.scan_type == "uncovered_lot"]
        new = [r for r in approved if r.scan_type == "new_candidate"]
        total_income = sum(r.premium_total or 0 for r in approved)

        subject = (
            f"[mgfbot] Weekly Scan — {len(approved)} trade(s) approved"
        )

        lines = [
            f"Weekly scan complete — {date.today().strftime('%A %B %d, %Y')}",
            f"{len(approved)} trade(s) approved for review",
            "",
        ]

        if lots:
            lines.append(f"UNCOVERED LOTS — Ready to write ({len(lots)})")
            lines.append("-" * 45)
            for r in lots:
                premium_str = f"${r.premium_total:,.0f}" if r.premium_total else "unknown"
                lines.append(
                    f"  {r.symbol:6s}  Score: {r.score:.0%}  "
                    f"Strike: ${r.recommended_strike:.2f}  "
                    f"Income: {premium_str}"
                )
            lines.append("")

        if new:
            lines.append(f"NEW CANDIDATES — Buy-write ({len(new)})")
            lines.append("-" * 45)
            for r in new:
                premium_str = f"${r.premium_total:,.0f}" if r.premium_total else "unknown"
                lines.append(
                    f"  {r.symbol:6s}  Score: {r.score:.0%}  "
                    f"Strike: ${r.recommended_strike:.2f}  "
                    f"Income: {premium_str}"
                )
            lines.append("")

        if total_income > 0:
            lines.append(f"Total estimated premium income: ${total_income:,.0f}")
            lines.append("")

        if approved:
            lines.append("Run 'python bot.py execute' Monday morning to review and confirm.")
        else:
            lines.append("No trades meet criteria this week. Watchlist maintained.")

        lines += [
            "",
            "Hayes: 'If you're not familiar with the company — don't touch it.'",
        ]

        self._send(subject, "\n".join(lines))

    # ─────────────────────────────────────────────
    # INTERNAL — SINGLE ALERT DISPATCH
    # ─────────────────────────────────────────────

    def _send_alert(self, alert) -> None:
        if not self.enabled:
            return

        alert_type = alert.alert_type
        symbol = alert.symbol

        # Cooldown check — skip if already sent this alert today
        if alert_type in DAILY_COOLDOWN_TYPES:
            key = f"{symbol}_{alert_type}"
            today = str(date.today())
            if self._cooldown.get(key) == today:
                logger.debug(f"Email cooldown: skipping {alert_type} for {symbol} (already sent today)")
                return

        subject, body = self._format_alert(alert)
        if subject and self._send(subject, body):
            # Record cooldown after successful send
            if alert_type in DAILY_COOLDOWN_TYPES:
                self._cooldown[f"{symbol}_{alert_type}"] = str(date.today())
                self._save_cooldown()

    def _format_alert(self, alert) -> tuple[str, str]:
        """Returns (subject, body) for a MonitorAlert."""
        t = alert.alert_type

        if t == "CIRCUIT_BREAKER":
            subject = "[mgfbot] CIRCUIT BREAKER — New trades halted"
            body = "\n".join([
                alert.message,
                "",
                "All new position entries are halted for today.",
                "Existing covered calls are NOT affected — hold to expiration as planned.",
                "",
                "The circuit breaker resets at market open tomorrow.",
            ])

        elif t == "ITM_WARNING":
            subject = f"[mgfbot] ITM WARNING — {alert.symbol} above strike, {alert.dte} DTE"
            lines = [alert.message, ""]
            if alert.current_stock_price and alert.strike:
                lines += [
                    f"Stock price:  ${alert.current_stock_price:.2f}",
                    f"Strike:       ${alert.strike:.2f}",
                    f"Days to expiry: {alert.dte}",
                    "",
                ]
            lines += [
                "Assignment at expiry is likely — this is the intended outcome.",
                "No action required. Hold to expiration.",
            ]
            body = "\n".join(lines)

        elif t == "EXPIRING_SOON":
            subject = f"[mgfbot] Expiring soon — {alert.symbol} ({alert.dte} DTE)"
            lines = [alert.message, ""]
            if alert.current_stock_price and alert.strike:
                itm = alert.current_stock_price >= alert.strike
                lines += [
                    f"Stock price:  ${alert.current_stock_price:.2f}",
                    f"Strike:       ${alert.strike:.2f}",
                    f"Status:       {'IN THE MONEY — assignment likely' if itm else 'Out of the money'}",
                    "",
                ]
            lines.append("Hold to expiration. No action required.")
            body = "\n".join(lines)

        elif t == "ASSIGNED":
            subject = f"[mgfbot] ASSIGNED — {alert.symbol} shares called away"
            body = "\n".join([
                alert.message,
                "",
                "This is the intended outcome — assignment = success.",
                "Cash has returned to the pool.",
                "",
                "Run 'python bot.py scan' on Sunday to find the next opportunity.",
            ])

        elif t == "EXPIRED_WORTHLESS":
            subject = f"[mgfbot] Expired worthless — {alert.symbol}"
            body = "\n".join([
                alert.message,
                "",
                "You keep the premium collected. Shares are still in your account.",
                "This position is now eligible to write a new covered call.",
                "",
                "It will appear as an uncovered lot in next Sunday's scan.",
            ])

        else:
            subject = f"[mgfbot] {t} — {alert.symbol}"
            body = alert.message

        return subject, body

    # ─────────────────────────────────────────────
    # SMTP
    # ─────────────────────────────────────────────

    def _send(self, subject: str, body: str) -> bool:
        """Sends an email. Returns True on success, False on failure."""
        try:
            msg = MIMEMultipart()
            msg["From"] = self.from_addr
            msg["To"] = self.to_addr
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.from_addr, self.password)
                server.sendmail(self.from_addr, self.to_addr, msg.as_string())

            logger.info(f"Email sent: {subject}")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "Email authentication failed. Check ALERT_SMTP_PASSWORD. "
                "Gmail requires an App Password, not your login password."
            )
        except Exception as e:
            logger.error(f"Email send failed: {e}")

        return False

    # ─────────────────────────────────────────────
    # COOLDOWN PERSISTENCE
    # ─────────────────────────────────────────────

    def _load_cooldown(self) -> dict:
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE) as f:
                    data = json.load(f)
                # Prune entries older than today
                today = str(date.today())
                return {k: v for k, v in data.items() if v == today}
        except Exception:
            pass
        return {}

    def _save_cooldown(self):
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, "w") as f:
                json.dump(self._cooldown, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save email cooldown: {e}")
