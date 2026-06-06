import asyncio
import json
import os

class BrowserManager:
    def __init__(self, config: dict, proxy: dict, db=None):
        self.config          = config
        self.proxy           = proxy
        self.lms_url         = config["lms"]["url"]
        self.lp_id           = config["lms"]["learning_plan_id"]
        self.lp_slug         = config["lms"]["learning_plan_slug"]
        self._last_api_debug = ""
        self._db             = db  # Supabase DB instance

    def _cookie_file(self, email: str) -> str:
        safe = email.replace("@", "_").replace(".", "_")
        return f"cookies_{safe}.json"

    def _save_cookies(self, email: str, cookies: list):
        # Save to local file
        path = self._cookie_file(email)
        with open(path, "w") as f:
            json.dump(cookies, f, indent=2)
        names = [c["name"] for c in cookies]
        print(f"  💾 Saved {len(cookies)} cookies: {names}")
        # Also save to Supabase if available
        if self._db and self._db.enabled:
            self._db.save_cookies(email, cookies)
            print(f"  ☁️ Cookies synced to Supabase")

    def _load_cookies(self, email: str) -> list:
        raw = []
        # Try Supabase first
        if self._db and self._db.enabled:
            sb_cookies = self._db.load_cookies(email)
            if sb_cookies:
                print(f"  ☁️ Loaded {len(sb_cookies)} cookies from Supabase")
                raw = sb_cookies
        # Fallback to local file
        if not raw:
            path = self._cookie_file(email)
            if os.path.exists(path):
                with open(path) as f:
                    raw = json.load(f)
        if not raw:
            return []
        clean = []
        for c in raw:
            entry = {
                "name":   c["name"],
                "value":  c["value"],
                "domain": c.get("domain", "inco.docebosaas.com"),
                "path":   c.get("path", "/"),
            }
            if c.get("secure"):
                entry["secure"] = True
            clean.append(entry)
        return clean

    async def launch(self, playwright):
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True
        )
        return context, browser

    async def login(self, context, email: str, password: str):
        page = await context.new_page()

        # ── Try saved cookies ─────────────────────────────────────
        saved = self._load_cookies(email)
        if saved:
            print(f"  🍪 Trying {len(saved)} saved cookies")
            await context.add_cookies(saved)
            await page.goto(
                f"{self.lms_url}/learn/lp/{self.lp_id}/{self.lp_slug}",
                wait_until="domcontentloaded", timeout=45000
            )
            await asyncio.sleep(3)
            if "signin" not in page.url and "login" not in page.url:
                print(f"  ✅ Cookie login works!")
                return page
            print(f"  ⚠️ Cookies invalid — fresh login")
            await context.clear_cookies()

        # ── Accept cookies banner first ───────────────────────────
        print(f"  🔑 Logging in: {email}")
        await page.goto(f"{self.lms_url}/login", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # Accept cookie banner if present
        try:
            accept = await page.wait_for_selector("button:has-text('ACCEPT')", timeout=3000)
            if accept:
                await accept.click()
                await asyncio.sleep(1)
                print(f"  ✅ Accepted cookie banner")
        except:
            pass

        # ── Fill Username field (NOT email type!) ─────────────────
        username_selectors = [
            "input[placeholder*='sername']",  # Username placeholder
            "input[name='login[username]']",
            "input[name='username']",
            "input[type='text']:not([name*='search'])",  # First text input
            "input[name='login[email]']",
            "input[type='email']",
        ]
        filled = False
        for sel in username_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await page.fill(sel, email)
                    filled = True
                    print(f"  ✅ Username filled: {sel}")
                    break
            except: continue

        if not filled:
            # Last resort — fill first visible input
            try:
                await page.evaluate(f"""
                    const inputs = document.querySelectorAll('input');
                    for (const inp of inputs) {{
                        if (inp.type !== 'password' && inp.type !== 'hidden') {{
                            inp.value = '{email}';
                            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                            break;
                        }}
                    }}
                """)
                print(f"  ✅ Username filled via JS")
            except Exception as e:
                print(f"  ❌ Could not fill username: {e}")

        await asyncio.sleep(0.5)

        # ── Fill Password ─────────────────────────────────────────
        try:
            await page.fill("input[type='password']", password)
            print(f"  ✅ Password filled")
        except Exception as e:
            print(f"  ❌ Could not fill password: {e}")

        await asyncio.sleep(0.5)

        # ── Click SIGN IN ─────────────────────────────────────────
        try:
            await page.click("button:has-text('SIGN IN')")
            print(f"  ✅ Clicked SIGN IN")
        except:
            for sel in ["button[type='submit']", "input[type='submit']"]:
                try:
                    await page.click(sel)
                    print(f"  ✅ Clicked submit: {sel}")
                    break
                except: continue

        await page.wait_for_load_state("networkidle", timeout=45000)
        await asyncio.sleep(3)

        print(f"  After login URL: {page.url}")

        if "login" in page.url and "pages" not in page.url and "learn" not in page.url:
            raise Exception(f"Login failed for {email}")

        print(f"  ✅ Login successful!")

        # Wait for hydra_access_token to be set (may take a moment)
        for wait_i in range(10):
            all_cookies = await context.cookies()
            token_found = any(c["name"] == "hydra_access_token" for c in all_cookies)
            if token_found:
                break
            print(f"  ⏳ Waiting for hydra token... ({wait_i+1}/10)")
            await asyncio.sleep(2)
        else:
            # Token not found - log cookie names for debugging
            all_cookies = await context.cookies()
            names = [c["name"] for c in all_cookies][:10]
            print(f"  ⚠️ hydra_access_token not found! Cookies: {names}")
            # Try navigating to LP page to trigger token
            try:
                await page.goto(
                    f"{self.lms_url}/learn/learning-plans/{self.lp_id}/{self.lp_slug}",
                    wait_until="domcontentloaded", timeout=45000
                )
                await asyncio.sleep(5)
            except:
                pass

        # Save cookies
        cookies = await context.cookies(["https://inco.docebosaas.com"])
        self._save_cookies(email, cookies)

        return page

    async def get_course_statuses(self, page) -> list:
        """
        Get course statuses using Playwright request API (more reliable than fetch).
        Uses cookies from the browser context automatically.
        """
        import asyncio
        try:
            # Navigate to LP page first to ensure session is active
            lp_url = f"{self.lms_url}/learn/learning-plans/{self.lp_id}/{self.lp_slug}"
            await page.goto(lp_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)

            if "signin" in page.url or "login" in page.url:
                self._last_api_debug = "Redirected to signin"
                return []

            # Get token - check cookies first, then localStorage
            cookies = await page.context.cookies()
            token = next(
                (c["value"] for c in cookies if c["name"] == "hydra_access_token"),
                None
            )
            # Fallback: check localStorage (used by some account types e.g. Outlook)
            if not token:
                try:
                    ls_token = await page.evaluate("""
                        () => {
                            try {
                                const raw = localStorage.getItem('access_token');
                                if (!raw) return null;
                                const parsed = JSON.parse(raw);
                                return parsed.access_token || null;
                            } catch(e) { return null; }
                        }
                    """)
                    if ls_token:
                        token = ls_token
                        print(f"  ✅ Got token from localStorage")
                except:
                    pass

            api_url = f"{self.lms_url}/learn/v1/lp/{self.lp_id}?get_courses_instructors=1"

            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            response = await page.context.request.get(api_url, headers=headers)
            token_src = "cookie" if next((c for c in cookies if c["name"] == "hydra_access_token"), None) else "localStorage" if token else "none"
            self._last_api_debug = f"HTTP:{response.status} token={token_src}"

            if response.status == 403:
                # Token expired — re-navigate to LP to refresh session
                self._last_api_debug += f" body:{(await response.text())[:200]}"
                await asyncio.sleep(3)
                await page.goto(
                    f"{self.lms_url}/learn/learning-plans/{self.lp_id}/{self.lp_slug}",
                    wait_until="domcontentloaded", timeout=45000
                )
                await asyncio.sleep(5)
                # Refresh cookies and retry once
                cookies = await page.context.cookies()
                token = next((c["value"] for c in cookies if c["name"] == "hydra_access_token"), None)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                response = await page.context.request.get(api_url, headers=headers)
                self._last_api_debug = f"Retry HTTP:{response.status} token={'yes' if token else 'no'}"

            if response.status != 200:
                self._last_api_debug += f" body:{(await response.text())[:200]}"
                return []

            data = await response.json()
            courses = data.get("data", {}).get("courses", [])
            self._last_api_debug += f" courses:{len(courses)}"
            return courses

        except Exception as e:
            self._last_api_debug = f"Exception: {str(e)[:100]}"
            return []


    async def _ensure_enrolled(self, page):
        """Auto-enroll in LP using API if hydra token missing"""
        try:
            cookies = await page.context.cookies()
            token = next((c["value"] for c in cookies if c["name"] == "hydra_access_token"), None)
            if token:
                return  # Already have token

            print(f"  No hydra token — attempting enrollment via API...")

            # Get CSRF token from cookies
            csrf = next((c["value"] for c in cookies if c["name"] == "_csrf"), None)

            # Try enrollment API
            enroll_url = f"{self.lms_url}/learn/v1/lp/{self.lp_id}/enroll"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if csrf:
                headers["X-CSRF-Token"] = csrf

            try:
                resp = await page.context.request.post(enroll_url, headers=headers)
                print(f"  Enroll API: {resp.status}")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  Enroll API error: {e}")

            # Navigate to LP page to get token after enrollment
            lp_url = f"{self.lms_url}/learn/learning-plans/{self.lp_id}/{self.lp_slug}"
            await page.goto(lp_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(5)

            # Wait for hydra token
            for i in range(15):
                cookies = await page.context.cookies()
                token = next((c["value"] for c in cookies if c["name"] == "hydra_access_token"), None)
                if token:
                    print(f"  Token obtained after enrollment!")
                    return
                await asyncio.sleep(2)

            # Last resort - click play/start button on page
            await page.evaluate("""
                () => {
                    const selectors = [
                        '[class*="play-btn"]', '[class*="lp-play"]',
                        '[class*="start-btn"]', 'button[class*="play"]',
                        '[data-id="play-button"]', '[class*="card"] button',
                        'dcb-play-button button', '[class*="player-button"]'
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) { el.click(); return sel; }
                    }
                    // Click first visible button
                    const btns = [...document.querySelectorAll('button')];
                    const playBtn = btns.find(b => {
                        const t = (b.innerText||b.title||'').toLowerCase();
                        return t.includes('play') || t.includes('start') || t.includes('begin');
                    });
                    if (playBtn) { playBtn.click(); return 'text-match'; }
                    return null;
                }
            """)
            await asyncio.sleep(5)

            cookies = await page.context.cookies()
            token = next((c["value"] for c in cookies if c["name"] == "hydra_access_token"), None)
            if token:
                print(f"  Token obtained after button click!")
            else:
                print(f"  ⚠️ Still no token — account needs manual LP enrollment")
        except Exception as e:
            print(f"  Enroll error: {e}")

    def get_module_url(self, course_id: int, slug: str) -> str:
        return (
            f"{self.lms_url}/learn/learning-plans/{self.lp_id}"
            f"/{self.lp_slug}/courses/{course_id}/{slug}/lessons"
        )
