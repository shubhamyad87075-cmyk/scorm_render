import asyncio
import json
import os
import random
import sys
import threading
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

sys.path.insert(0, ".")
from modules.state import StateManager
from modules.proxy import ProxyManager
from modules.browser import BrowserManager
from modules.scorm import SCORMSimulator
from modules.supabase_db import SupabaseDB
from dashboard.app import init_dashboard, set_simulator_runner, run_dashboard, push_update

# ── Config ────────────────────────────────────────────────────
def load_config():
    env_config = os.environ.get("SCORM_CONFIG")
    if env_config:
        cfg = json.loads(env_config)
    else:
        with open("config.json") as f:
            cfg = json.load(f)
    if os.environ.get("NORDVPN_USER"):
        cfg["proxy"]["username"] = os.environ["NORDVPN_USER"]
    if os.environ.get("NORDVPN_PASS"):
        cfg["proxy"]["password"] = os.environ["NORDVPN_PASS"]
    if os.environ.get("TEST_MODE") == "true":
        cfg["test_mode"]["enabled"] = True
    if os.environ.get("TEST_MODE") == "false":
        cfg["test_mode"]["enabled"] = False
    return cfg

CONFIG    = load_config()
COURSES   = CONFIG["courses"]
SIM       = CONFIG["simulation"]
TEST_MODE = CONFIG.get("test_mode", {})
SPEED     = TEST_MODE.get("speed_multiplier", 3600) if TEST_MODE.get("enabled") else 1

# ── Supabase ──────────────────────────────────────────────────
db       = SupabaseDB()
OWNER_ID = os.environ.get("OWNER_ID", "default")  # Unique per Railway instance

def real_wait(seconds):
    return seconds / SPEED

def get_all_accounts():
    accounts = list(CONFIG.get("accounts", []))
    if db.enabled:
        sb = db.get_accounts(owner_id=OWNER_ID)  # Only this Railway's accounts
        existing = {a["email"] for a in accounts}
        for a in sb:
            if a["email"] not in existing:
                accounts.append(a)
    elif os.path.exists("accounts.json"):
        with open("accounts.json") as f:
            local = json.load(f)
        existing = {a["email"] for a in accounts}
        for a in local:
            if a["email"] not in existing:
                accounts.append(a)
    return accounts

# ── State ──────────────────────────────────────────────────────
ACCOUNTS = get_all_accounts()
state    = StateManager(ACCOUNTS, COURSES)

# ── Process ONE account's NEXT pending module ─────────────────
async def process_account_module(account: dict, playwright, custom_schedule: dict = None):
    """
    Run the next pending module for this account.
    After completing, schedule the next module in Supabase.
    Browser opens and closes for each module run.
    """
    email    = account["email"]
    password = account["password"]
    country  = account.get("proxy_country", "in")

    # Get next module from Supabase
    next_mod = db.get_next_module(email) if db.enabled else None

    if not next_mod:
        state.log(f"No pending modules for {email}", "info", email)
        state.update(email, status="completed", status_label="✅ All Done!")
        return False

    course_id   = next_mod["course_id"]
    course      = next((c for c in COURSES if c["id"] == course_id), None)
    if not course:
        return False

    module_name = course["name"]
    state.update(email, status="running", current_module=module_name,
                 status_label=f"🌐 Starting",
                 started_at=datetime.now().strftime("%H:%M:%S"))
    state.log(f"Starting via {country.upper()} proxy", "info", email)
    push_update()

    proxy_mgr   = ProxyManager(CONFIG)
    proxy       = proxy_mgr.get_proxy(country)
    browser_mgr = BrowserManager(CONFIG, proxy, db=db)
    context, browser = None, None

    try:
        context, browser = await browser_mgr.launch(playwright)
        page = await browser_mgr.login(context, email, password)
        state.log("✅ Login successful!", "success", email)
        push_update()

        # Get ALL module statuses from Docebo in one call
        courses_status = await browser_mgr.get_course_statuses(page)
        debug = getattr(browser_mgr, '_last_api_debug', '')
        state.log(f"Got {len(courses_status)} courses | {debug}", "info", email)

        # Find the right module to run — skip already completed ones
        target_course    = None
        target_course_id = None
        target_current   = None

        for course_check in COURSES:
            cid     = course_check["id"]
            current = next((c for c in courses_status if c["idCourse"] == cid), None)
            if not current:
                continue

            status    = current.get("status", "")
            can_enter = current.get("can_enter", True)

            # Skip completed
            if status == "completed":
                if db.enabled:
                    db.mark_module_done(email, cid)
                state.log(f"✅ Already done: {course_check['name']}", "info", email)
                continue

            # Skip locked
            if status == "locked" or not can_enter:
                state.log(f"🔒 Locked: {course_check['name']}", "info", email)
                continue

            # This is the next module to run!
            target_course    = course_check
            target_course_id = cid
            target_current   = current
            break

        if not target_course:
            # Only mark as done if API actually returned courses
            # If 0 courses returned = API failure, not actual completion
            if len(courses_status) == 0:
                state.log(f"⚠️ API returned 0 courses — login/token issue, retrying in 30min", "warning", email)
                if db.enabled and next_mod:
                    db.mark_module_failed(email, next_mod["course_id"])
                return False
            # API returned courses but all are done/locked = genuinely complete
            state.log(f"🎉 All modules done for {email}!", "success", email)
            state.update(email, status="completed", status_label="✅ All Done!")
            return True

        module_name   = target_course["name"]
        completed_les = target_current.get("competed_lessons", 0)
        total_les     = target_current.get("all_lessons", 0)
        doc_status    = target_current.get("status", "")

        state.log(f"▶ Running: {module_name} ({doc_status} {completed_les}/{total_les})",
                  "info", email)

        if db.enabled:
            db.mark_module_running(email, target_course_id)

        module_url = browser_mgr.get_module_url(target_course_id, target_course["slug"])
        state.update(email, current_module=module_name, status_label=f"📖 {module_name}")
        state.log(f"🚀 Starting: {module_name}", "info", email)
        push_update()

        scorm   = SCORMSimulator(page, CONFIG, state, email, speed=SPEED)
        success = await scorm.run_module(module_url, target_course,
                                         completed_lessons=completed_les)

        if success:
            done = state.get(email).get("modules_done", [])
            if module_name not in done:
                done.append(module_name)
            state.update(email, modules_done=done)
            state.log(f"✅ Completed: {module_name}", "success", email)
            if db.enabled:
                db.mark_module_done(email, target_course_id)
                _schedule_next(email, target_course_id, custom_schedule)
        else:
            state.log(f"❌ Failed: {module_name}", "error", email)
            if db.enabled:
                db.mark_module_failed(email, target_course_id)

        push_update()
        return success

    except Exception as e:
        state.log(f"ERROR: {e}", "error", email)
        if db.enabled and target_course_id:
            db.mark_module_failed(email, target_course_id)
        return False
    finally:
        if context:
            try: await context.close()
            except: pass
        if browser:
            try: await browser.close()
            except: pass

def _schedule_next(email, current_course_id, custom_schedule=None):
    """Schedule the next module with random 4-7 day delay"""
    current_idx = next((i for i,c in enumerate(COURSES) if c["id"] == current_course_id), None)
    if current_idx is None:
        return
    next_idx = current_idx + 1
    if next_idx >= len(COURSES):
        state.log(f"🎉 All modules completed for {email}!", "success", email)
        state.update(email, status="completed", status_label="✅ All Done!")
        return

    next_course = COURSES[next_idx]

    # Check custom schedule
    custom_dt = None
    if custom_schedule and email in custom_schedule:
        for item in custom_schedule.get(email, []):
            if item["course_id"] == next_course["id"]:
                try:
                    from datetime import timedelta as td
                    dt_naive = datetime.fromisoformat(item["datetime"])
                    custom_dt = dt_naive - td(hours=5, minutes=30) if os.environ.get("RAILWAY_ENVIRONMENT") else dt_naive
                except:
                    pass

    if custom_dt:
        next_run = custom_dt
    else:
        days  = random.randint(SIM.get("days_between_modules_min", 4),
                               SIM.get("days_between_modules_max", 7))
        hours = random.randint(8, 20)
        next_run = datetime.utcnow() + timedelta(days=days, hours=hours)

    db.schedule_next_module_at(email, next_course["id"], next_run)
    display = next_run + timedelta(hours=5, minutes=30) if os.environ.get("RAILWAY_ENVIRONMENT") else next_run
    state.log(f"⏰ Next: {next_course['name']} at {display.strftime('%b %d %H:%M IST')}", "info", email)
    state.update(email, status="waiting", status_label=f"⏰ Next: {next_course['name']}")

# ── QUEUE WORKER ──────────────────────────────────────────────
async def queue_worker(custom_schedule=None, single_email=None):
    """
    Main queue loop.
    Checks Supabase every 60 seconds for accounts ready to run.
    Runs 1 account at a time (Railway free plan = 512MB).
    """
    ACCOUNTS_ALL = get_all_accounts()

    # Init progress for all accounts
    if db.enabled:
        for acc in ACCOUNTS_ALL:
            db.init_progress(acc["email"], COURSES)

    # If single email mode
    if single_email:
        account = next((a for a in ACCOUNTS_ALL if a["email"] == single_email), None)
        if not account:
            state.log(f"❌ Account not found: {single_email}", "error")
            return
        state.log(f"▶️ Starting: {single_email}", "success")
        push_update()
        async with async_playwright() as playwright:
            await process_account_module(account, playwright, custom_schedule)
        push_update()
        return

    # Full queue mode — all accounts
    mode = f"⚡ TEST ({SPEED}x)" if TEST_MODE.get("enabled") else "🕐 Real timing"
    state.log(f"🚀 Queue started! {len(ACCOUNTS_ALL)} accounts | {mode}", "success")
    state.log(f"Checking every 60s for ready accounts...", "info")
    push_update()

    async with async_playwright() as playwright:
        while not state.is_stop_requested():

            # Pause support
            while state.is_paused():
                await asyncio.sleep(5)

            if not db.enabled:
                # Fallback: run all sequentially (no Supabase)
                state.log("⚠️ No Supabase — running sequentially", "warning")
                for acc in ACCOUNTS_ALL:
                    if state.is_stop_requested():
                        break
                    await _run_all_modules_sequential(acc, playwright, custom_schedule)
                break

            # Find accounts ready to run NOW
            ready_emails = db.get_ready_accounts(COURSES, max_concurrent=1)
            state.log(f"Queue check: {len(ready_emails)} ready | db={db.enabled}", "info")

            if ready_emails:
                email   = ready_emails[0]
                account = next((a for a in ACCOUNTS_ALL if a["email"] == email), None)
                if account:
                    state.log(f"▶ Running: {email}", "info")
                    push_update()
                    await process_account_module(account, playwright, custom_schedule)
                    push_update()
                    await asyncio.sleep(5)
                else:
                    state.log(f"⚠️ Account {email} not in ACCOUNTS_ALL list!", "warning")
                    await asyncio.sleep(60)
            else:
                # Nothing ready — show next scheduled time
                import urllib.request as _ur
                try:
                    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    next_rows = db._get("account_progress",
                        f"status=eq.pending&order=next_run_at.asc&limit=1")
                    if next_rows:
                        nxt = next_rows[0]
                        state.log(f"Next: {nxt['email'].split('@')[0]} at {nxt['next_run_at'][:16]}", "info")
                    else:
                        state.log("No pending modules in DB", "info")
                except Exception as e:
                    state.log(f"Queue debug error: {e}", "warning")
                _update_waiting_statuses(ACCOUNTS_ALL)
                push_update()
                await asyncio.sleep(60)

    state.log("✅ Queue stopped", "success")
    push_update()

def _update_waiting_statuses(accounts):
    """Update dashboard with next run times"""
    if not db.enabled:
        return
    for acc in accounts:
        email = acc["email"]
        next_mod = db.get_next_module_any(email)
        if next_mod:
            next_run_str = next_mod.get("next_run_at", "")
            try:
                from datetime import timezone
                next_run = datetime.fromisoformat(next_run_str.replace("Z", "+00:00"))
                next_run = next_run.replace(tzinfo=None)
                now = datetime.utcnow()
                if next_run > now:
                    diff = next_run - now
                    days = diff.days
                    hrs  = diff.seconds // 3600
                    label = f"⏰ {days}d {hrs}h" if days > 0 else f"⏰ {hrs}h"
                    state.update(email, status="waiting", status_label=label)
                else:
                    state.update(email, status="queued", status_label="Ready")
            except:
                pass

# ── Sequential fallback (no Supabase) ─────────────────────────
async def _run_all_modules_sequential(account, playwright, custom_schedule=None):
    email    = account["email"]
    password = account["password"]
    country  = account.get("proxy_country", "in")
    now      = datetime.now()

    state.update(email, status="running", status_label="🌐 Starting")
    state.log(f"▶ Account: {email}", "info", email)
    push_update()

    proxy_mgr   = ProxyManager(CONFIG)
    proxy       = proxy_mgr.get_proxy(country)
    browser_mgr = BrowserManager(CONFIG, proxy, db=db)
    context, browser = None, None

    try:
        context, browser = await browser_mgr.launch(playwright)
        page = await browser_mgr.login(context, email, password)
        state.log("✅ Login successful!", "success", email)
        push_update()

        prev_date = now
        for idx, course in enumerate(COURSES):
            if state.is_stop_requested():
                break

            # Calculate wait time
            if idx == 0:
                wait_secs = 0
            else:
                days  = random.randint(SIM.get("days_between_modules_min", 4), SIM.get("days_between_modules_max", 7))
                hours = random.randint(8, 20)
                prev_date = prev_date + timedelta(days=days, hours=hours)
                wait_secs = max(0, (prev_date - datetime.now()).total_seconds())

            if wait_secs > 10:
                state.update(email, status="waiting", status_label=f"⏰ Waiting {days}d")
                state.log(f"💤 Waiting {days} days...", "info", email)
                push_update()
                try: await context.close()
                except: pass
                try: await browser.close()
                except: pass
                context, browser = None, None

                waited = 0
                while waited < real_wait(wait_secs):
                    if state.is_stop_requested():
                        return
                    while state.is_paused():
                        await asyncio.sleep(5)
                    await asyncio.sleep(min(10, real_wait(wait_secs) - waited))
                    waited += 10

                context, browser = await browser_mgr.launch(playwright)
                page = await browser_mgr.login(context, email, password)
                state.log("✅ Re-login after wait", "success", email)

            courses_status = await browser_mgr.get_course_statuses(page)
            current = next((c for c in courses_status if c["idCourse"] == course["id"]), None)

            if current:
                if current.get("status") == "completed":
                    state.log(f"✅ Already done: {course['name']}", "success", email)
                    continue
                if current.get("status") == "locked" or not current.get("can_enter", True):
                    state.log(f"🔒 Locked: {course['name']}", "warning", email)
                    continue

            module_url = browser_mgr.get_module_url(course["id"], course["slug"])
            state.update(email, current_module=course["name"], status="running",
                         status_label=f"📖 {course['name']}")
            state.log(f"🚀 {course['name']}", "info", email)
            push_update()

            completed_les = current.get("competed_lessons", 0) if current else 0
            scorm   = SCORMSimulator(page, CONFIG, state, email, speed=SPEED)
            success = await scorm.run_module(module_url, course, completed_lessons=completed_les)

            if success:
                done = state.get(email).get("modules_done", [])
                done.append(course["name"])
                state.update(email, modules_done=done)
                state.log(f"✅ Completed: {course['name']}", "success", email)

            push_update()
            await asyncio.sleep(real_wait(random.randint(15, 45)))

        state.update(email, status="completed", status_label="✅ Done!", progress=100)
        state.log(f"🎉 All done: {email}", "success", email)

    except Exception as e:
        state.log(f"ERROR: {e}", "error", email)
        state.update(email, status="error", status_label=f"❌ {str(e)[:30]}")
    finally:
        if context:
            try: await context.close()
            except: pass
        if browser:
            try: await browser.close()
            except: pass

def simulator_thread(custom_schedule=None, single_email=None):
    asyncio.run(queue_worker(custom_schedule=custom_schedule, single_email=single_email))

# ── Entry ─────────────────────────────────────────────────────
if __name__ == "__main__":
    SIM.setdefault("days_between_modules_min", 4)
    SIM.setdefault("days_between_modules_max", 7)

    ACCOUNTS = get_all_accounts()
    state    = StateManager(ACCOUNTS, COURSES)

    # Pre-populate state so dashboard shows accounts immediately
    for acc in ACCOUNTS:
        state.update(acc["email"],
            name=acc.get("name", acc["email"].split("@")[0]),
            proxy=acc.get("proxy_country", "in"),
            status="queued",
            status_label="Ready"
        )

    init_dashboard(state, db=db)
    set_simulator_runner(simulator_thread)

    port = int(os.environ.get("PORT", 8080))

    print("\n🌿 GREENPATH SCORM Simulator")
    print("=" * 45)
    print(f"Accounts  : {len(ACCOUNTS)}")
    print(f"Queue mode: {'Supabase ✅' if db.enabled else 'Local sequential'}")
    print(f"Mode      : {'⚡ TEST' if TEST_MODE.get('enabled') else '🕐 Real timing'}")
    print(f"Port      : {port}")
    print("=" * 45)

    run_dashboard(port=port)
