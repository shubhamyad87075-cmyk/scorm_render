import json
import threading
from datetime import datetime
from typing import Dict, Any

class StateManager:
    """Shared state between simulator and dashboard"""

    def __init__(self, accounts: list, courses: list):
        self._lock = threading.Lock()
        self._accounts = accounts
        self._courses = courses
        self._state: Dict[str, Any] = {}
        self._logs = []
        self._control = {
            "running": False,
            "paused": False,
            "stop_requested": False
        }
        self._schedule: Dict[str, list] = {}

        # Initialize state for each account
        for acc in accounts:
            email = acc["email"]
            self._state[email] = {
                "email": email,
                "name": acc.get("name", email),
                "proxy": acc["proxy_country"].upper(),
                "status": "queued",
                "status_label": "⏳ Queued",
                "current_module": "-",
                "current_lesson": 0,
                "total_lessons": 0,
                "progress": 0,
                "score": "-",
                "time_spent": "-",
                "modules_done": [],
                "modules_pending": [],
                "started_at": None,
                "completed_at": None,
                "next_module_at": None,
                "error": None
            }

    def remove_account(self, email: str):
        """Remove account from live state immediately"""
        with self._lock:
            if email in self._state:
                del self._state[email]

    def reset_accounts(self, accounts: list):
        """Reset state for new account list (called before each run)"""
        with self._lock:
            for acc in accounts:
                email = acc["email"]
                if email not in self._state:
                    self._state[email] = {
                        "email": email,
                        "name": acc.get("name", email),
                        "proxy": acc.get("proxy_country", "").upper(),
                        "status": "queued",
                        "status_label": "⏳ Queued",
                        "current_module": "-",
                        "current_lesson": 0,
                        "total_lessons": 0,
                        "progress": 0,
                        "score": "-",
                        "time_spent": "-",
                        "modules_done": [],
                        "modules_pending": [],
                        "started_at": None,
                        "completed_at": None,
                        "next_module_at": None,
                        "error": None
                    }

    # ── Control ───────────────────────────────────────────────────
    def start(self):
        with self._lock:
            self._control["running"] = True
            self._control["paused"] = False
            self._control["stop_requested"] = False

    def pause(self):
        with self._lock:
            self._control["paused"] = True

    def resume(self):
        with self._lock:
            self._control["paused"] = False

    def stop(self):
        with self._lock:
            self._control["stop_requested"] = True
            self._control["running"] = False

    def is_running(self) -> bool:
        return self._control["running"]

    def is_paused(self) -> bool:
        return self._control["paused"]

    def is_stop_requested(self) -> bool:
        return self._control["stop_requested"]

    # ── Account state ─────────────────────────────────────────────
    def update(self, email: str, **kwargs):
        with self._lock:
            if email in self._state:
                self._state[email].update(kwargs)

    def get_all(self) -> list:
        with self._lock:
            return list(self._state.values())

    def get(self, email: str) -> dict:
        with self._lock:
            return self._state.get(email, {})

    # ── Schedule ──────────────────────────────────────────────────
    def set_schedule(self, email: str, schedule: list):
        with self._lock:
            self._schedule[email] = schedule

    def get_schedule(self) -> dict:
        with self._lock:
            return dict(self._schedule)

    # ── Logs ──────────────────────────────────────────────────────
    def log(self, message: str, level: str = "info", email: str = ""):
        with self._lock:
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
                "level": level,
                "email": email
            }
            self._logs.append(entry)
            if len(self._logs) > 2000:
                self._logs = self._logs[-2000:]

    def get_logs(self, last_n: int = 100) -> list:
        with self._lock:
            return self._logs[-last_n:]

    # ── Export ────────────────────────────────────────────────────
    def export_csv(self) -> str:
        lines = ["Email,Name,Proxy,Status,Progress,Score,Modules Done,Started,Completed"]
        with self._lock:
            for email, data in self._state.items():
                lines.append(
                    f"{data['email']},{data['name']},{data['proxy']},"
                    f"{data['status']},{data['progress']}%,{data['score']},"
                    f"{len(data['modules_done'])},{data['started_at'] or ''},{data['completed_at'] or ''}"
                )
        return "\n".join(lines)
