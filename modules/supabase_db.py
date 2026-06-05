import os
import json
import asyncio
from datetime import datetime, timedelta
import random

try:
    import httpx
    HAS_HTTPX = True
except:
    HAS_HTTPX = False

class SupabaseDB:
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.key = os.environ.get("SUPABASE_KEY", "")
        self.enabled = bool(self.url and self.key)
        if self.enabled:
            print(f"✅ Supabase connected: {self.url[:40]}...")
        else:
            print("⚠️ Supabase not configured — using local mode")

    def _headers(self):
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

    def _get(self, table, params=""):
        import urllib.request
        url = f"{self.url}/rest/v1/{table}?{params}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def _post(self, table, data):
        import urllib.request
        url = f"{self.url}/rest/v1/{table}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read() or b"[]")
        except Exception as e:
            return None

    def _patch(self, table, params, data):
        import urllib.request
        url = f"{self.url}/rest/v1/{table}?{params}"
        body = json.dumps(data).encode()
        headers = {**self._headers(), "Prefer": "return=representation"}
        req = urllib.request.Request(url, data=body, headers=headers, method="PATCH")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read() or b"[]")
        except:
            return None

    def _delete(self, table, params):
        import urllib.request
        url = f"{self.url}/rest/v1/{table}?{params}"
        req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
        try:
            with urllib.request.urlopen(req) as r:
                return True
        except:
            return False

    # ── Setup ─────────────────────────────────────────────────
    def setup_tables(self):
        """Create tables via Supabase SQL if they don't exist"""
        if not self.enabled:
            return
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                proxy_country TEXT DEFAULT 'in',
                name TEXT,
                modules JSONB DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS account_cookies (
                email TEXT PRIMARY KEY,
                cookies JSONB,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS account_progress (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                course_id INTEGER NOT NULL,
                course_name TEXT,
                status TEXT DEFAULT 'pending',
                next_run_at TIMESTAMPTZ DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                attempts INTEGER DEFAULT 0,
                UNIQUE(email, course_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sim_logs (
                id SERIAL PRIMARY KEY,
                email TEXT,
                message TEXT,
                level TEXT DEFAULT 'info',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        ]
        import urllib.request
        for sql in sql_statements:
            try:
                url = f"{self.url}/rest/v1/rpc/exec_sql"
                body = json.dumps({"sql": sql}).encode()
                req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
                urllib.request.urlopen(req)
            except:
                pass  # Tables may already exist

    # ── Accounts ──────────────────────────────────────────────
    def get_accounts(self, owner_id=None):
        if not self.enabled:
            return []
        try:
            if owner_id:
                return self._get("accounts", f"owner_id=eq.{owner_id}&order=created_at.asc")
            return self._get("accounts", "order=created_at.asc")
        except:
            return []

    def add_account(self, email, password, proxy_country, name, modules, owner_id="default"):
        if not self.enabled:
            return False
        try:
            self._post("accounts", {
                "email": email,
                "password": password,
                "proxy_country": proxy_country,
                "name": name,
                "modules": json.dumps(modules),
                "owner_id": owner_id
            })
            return True
        except:
            return False

    def delete_account(self, email):
        if not self.enabled:
            return False
        try:
            self._delete("accounts", f"email=eq.{email}")
            self._delete("account_cookies", f"email=eq.{email}")
            self._delete("account_progress", f"email=eq.{email}")
            return True
        except:
            return False

    # ── Cookies ───────────────────────────────────────────────
    def save_cookies(self, email, cookies: list):
        if not self.enabled:
            return
        try:
            # Upsert cookies
            import urllib.request
            url = f"{self.url}/rest/v1/account_cookies"
            headers = {**self._headers(), "Prefer": "resolution=merge-duplicates"}
            body = json.dumps({
                "email": email,
                "cookies": json.dumps(cookies),
                "updated_at": datetime.utcnow().isoformat()
            }).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req)
        except Exception as e:
            print(f"  ⚠️ Could not save cookies to Supabase: {e}")

    def load_cookies(self, email) -> list:
        if not self.enabled:
            return []
        try:
            rows = self._get("account_cookies", f"email=eq.{email}&select=cookies")
            if rows:
                raw = rows[0].get("cookies", "[]")
                if isinstance(raw, str):
                    return json.loads(raw)
                return raw or []
        except:
            pass
        return []

    # ── Progress ──────────────────────────────────────────────
    def get_progress(self, email) -> list:
        if not self.enabled:
            return []
        try:
            return self._get("account_progress",
                           f"email=eq.{email}&order=course_id.asc")
        except:
            return []

    def init_progress(self, email, courses: list):
        """
        Initialize progress for all modules.
        Only Module 1 starts as pending/ready.
        Modules 2+ start blocked far in future — unlocked when previous completes.
        """
        if not self.enabled:
            return
        existing = self.get_progress(email)
        existing_ids = {r["course_id"] for r in existing}
        FAR_FUTURE = "2099-01-01T00:00:00"
        for idx, course in enumerate(courses):
            if course["id"] not in existing_ids:
                try:
                    # Only first module starts ready (next_run_at = now)
                    # All others blocked until scheduled by previous module completion
                    next_run = datetime.utcnow().isoformat() if idx == 0 else FAR_FUTURE
                    self._post("account_progress", {
                        "email": email,
                        "course_id": course["id"],
                        "course_name": course["name"],
                        "status": "pending",
                        "next_run_at": next_run,
                        "attempts": 0
                    })
                except:
                    pass

    def get_ready_accounts(self, courses: list, max_concurrent: int = 2) -> list:
        """Get accounts that have modules ready to run right now"""
        if not self.enabled:
            return []
        try:
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
            rows = self._get(
                "account_progress",
                f"status=eq.pending&next_run_at=lte.{now}&order=next_run_at.asc"
            )
            ready_emails = []
            seen = set()
            for row in rows:
                email = row["email"]
                if email not in seen:
                    seen.add(email)
                    ready_emails.append(email)
                if len(ready_emails) >= max_concurrent:
                    break
            return ready_emails
        except Exception as e:
            print(f"get_ready_accounts error: {e}")
            return []

    def get_next_module(self, email) -> dict:
        """Get the next pending module for this account"""
        if not self.enabled:
            return None
        try:
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
            rows = self._get(
                "account_progress",
                f"email=eq.{email}&status=eq.pending&next_run_at=lte.{now}&order=course_id.asc&limit=1"
            )
            return rows[0] if rows else None
        except:
            return None

    def mark_module_running(self, email, course_id):
        if not self.enabled:
            return
        try:
            self._patch("account_progress",
                       f"email=eq.{email}&course_id=eq.{course_id}",
                       {"status": "running", "started_at": datetime.utcnow().isoformat()})
        except:
            pass

    def mark_module_done(self, email, course_id, days_until_next: int = 0):
        """Mark module complete. If days_until_next > 0, schedules next check."""
        if not self.enabled:
            return
        try:
            self._patch("account_progress",
                       f"email=eq.{email}&course_id=eq.{course_id}",
                       {
                           "status": "completed",
                           "completed_at": datetime.utcnow().isoformat()
                       })
        except:
            pass

    def mark_module_failed(self, email, course_id, locked=False):
        if not self.enabled:
            return
        try:
            rows = self._get("account_progress", f"email=eq.{email}&course_id=eq.{course_id}&select=attempts")
            attempts = (rows[0]["attempts"] if rows else 0) + 1
            if locked:
                # Locked = prerequisites not met, retry in 24 hours
                retry = (datetime.utcnow() + timedelta(hours=24)).isoformat()
            else:
                # Normal failure = retry in 30 minutes
                retry = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
            self._patch("account_progress",
                       f"email=eq.{email}&course_id=eq.{course_id}",
                       {"status": "pending", "next_run_at": retry, "attempts": attempts})
        except:
            pass

    def schedule_next_module(self, email, next_course_id, days_min=4, days_max=7):
        """Schedule the next module with random delay"""
        if not self.enabled:
            return
        try:
            days = random.randint(days_min, days_max)
            hours = random.randint(8, 20)  # Random hour of day
            next_run = datetime.utcnow() + timedelta(days=days, hours=hours)
            self._patch("account_progress",
                       f"email=eq.{email}&course_id=eq.{next_course_id}",
                       {"status": "pending", "next_run_at": next_run.isoformat()})
        except:
            pass

    def schedule_next_module_at(self, email, next_course_id, run_at):
        """Schedule next module at exact datetime"""
        if not self.enabled:
            return
        try:
            self._patch("account_progress",
                       f"email=eq.{email}&course_id=eq.{next_course_id}",
                       {"status": "pending", "next_run_at": run_at.isoformat()})
        except:
            pass

    def get_ready_accounts(self, courses, max_concurrent=1):
        """Accounts with a module ready to run now"""
        if not self.enabled:
            return []
        try:
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
            rows = self._get("account_progress",
                f"status=eq.pending&next_run_at=lte.{now}&order=next_run_at.asc")
            seen = []
            for row in rows:
                e = row["email"]
                if e not in seen:
                    seen.append(e)
                if len(seen) >= max_concurrent:
                    break
            return seen
        except Exception as e:
            print(f"get_ready_accounts error: {e}")
            return []

    def get_next_module_any(self, email):
        """Next pending module for email (for status display)"""
        if not self.enabled:
            return None
        try:
            rows = self._get("account_progress",
                f"email=eq.{email}&status=eq.pending&order=next_run_at.asc&limit=1")
            return rows[0] if rows else None
        except:
            return None

    # ── Live State Persistence ───────────────────────────────────
    def save_live_state(self, state_data: dict):
        """Save complete simulator state to Supabase every few minutes"""
        if not self.enabled:
            return
        try:
            import json as _json
            import urllib.request
            url = f"{self.url}/rest/v1/account_cookies"
            # Reuse account_cookies table with special key "_live_state"
            headers = {**self._headers(), "Prefer": "resolution=merge-duplicates"}
            body = _json.dumps({
                "email": "_live_state",
                "cookies": _json.dumps(state_data),
                "updated_at": datetime.utcnow().isoformat()
            }).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req)
        except Exception as e:
            pass  # Non-critical, don't crash

    def load_live_state(self) -> dict:
        """Load saved live state on startup"""
        if not self.enabled:
            return {}
        try:
            rows = self._get("account_cookies", "email=eq._live_state&select=cookies")
            if rows:
                raw = rows[0].get("cookies", "{}")
                if isinstance(raw, str):
                    import json as _json
                    return _json.loads(raw)
                return raw or {}
        except:
            pass
        return {}

    def clear_live_state(self):
        """Clear live state after all done"""
        if not self.enabled:
            return
        try:
            self._delete("account_cookies", "email=eq._live_state")
        except:
            pass

    # ── Logs ──────────────────────────────────────────────────
    def save_log(self, email, message, level="info"):
        if not self.enabled:
            return
        try:
            self._post("sim_logs", {
                "email": email,
                "message": message,
                "level": level
            })
        except:
            pass

    def get_logs(self, limit=100) -> list:
        if not self.enabled:
            return []
        try:
            return self._get("sim_logs", f"order=created_at.desc&limit={limit}")
        except:
            return []
