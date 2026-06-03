import asyncio
import random

LZW_DECODE = """
function lzwDecode(data) {
    const d = data.d;
    let dict = {};
    for (let i = 0; i < 256; i++) dict[i] = String.fromCharCode(i);
    let dictSize = 256, result = '', prev = String.fromCharCode(d[0]);
    result += prev;
    for (let i = 1; i < d.length; i++) {
        const code = d[i];
        let entry = dict[code] !== undefined ? dict[code] : (code === dictSize ? prev + prev[0] : null);
        if (!entry) break;
        result += entry;
        dict[dictSize++] = prev + entry[0];
        prev = entry;
    }
    return result;
}
"""

LZW_ENCODE = """
function lzwEncode(str) {
    let dict = {};
    for (let i = 0; i < 256; i++) dict[String.fromCharCode(i)] = i;
    let dictSize = 256, result = [], w = '';
    for (let c of str) {
        const wc = w + c;
        if (dict[wc] !== undefined) { w = wc; }
        else { result.push(dict[w]); dict[wc] = dictSize++; w = c; }
    }
    if (w !== '') result.push(dict[w]);
    return result;
}
"""

class SCORMSimulator:
    def __init__(self, page, config: dict, state=None, email: str = "", speed: float = 1):
        self.page    = page
        self.context = page.context
        self.config  = config
        self.sim     = config["simulation"]
        self.state   = state
        self.email   = email
        self.speed   = speed

    def _random_minutes(self) -> int:
        return random.randint(self.sim["min_minutes_per_lesson"], self.sim["max_minutes_per_lesson"])

    def _format_scorm_time(self, minutes: int) -> str:
        h = minutes // 60
        m = minutes % 60
        s = random.randint(0, 59)
        return f"PT{h:02d}H{m:02d}M{s:02d}S"

    def _real_wait(self, seconds: float) -> float:
        return seconds / self.speed

    def _log(self, msg: str, level: str = "info"):
        if self.state:
            self.state.log(msg, level, self.email)

    async def _dismiss_modals(self):
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except:
            pass
        try:
            cancel = self.page.locator("button:has-text('Cancel')").first
            if await cancel.is_visible():
                await cancel.click(force=True)
                await asyncio.sleep(1)
        except:
            pass

    async def _open_module(self, module_url: str) -> bool:
        self._log(f"Going to module: {module_url}")
        await self.page.goto(module_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        url = self.page.url
        if "signin" in url or "login" in url:
            self._log("Session expired!", "error")
            return False

        # Wait for Angular to fully load
        for _ in range(15):
            title = await self.page.title()
            if title and "INCO Learning" not in title and len(title) > 5:
                break
            await asyncio.sleep(2)

        self._log(f"Module page: {await self.page.title()}")
        await self._dismiss_modals()

        # Click 1: module listing page buttons
        click1_selectors = [
            "button.lmn-button-theme-accent",
            "button:has-text('Resume training')",
            "button:has-text('Start learning now')",
            "button:has-text('Start learning')",
            "button:has-text('Retake the course')",
            "button:has-text('Retake')",
        ]
        for sel in click1_selectors:
            try:
                btn = await self.page.wait_for_selector(sel, timeout=4000)
                if btn:
                    text = await btn.inner_text()
                    self._log(f"Click 1: '{text.strip()}'")
                    await btn.click()
                    break
            except:
                continue

        await asyncio.sleep(5)
        self._log(f"After click 1: {self.page.url[:80]}")

        # Check if SCORM already loaded after Click 1
        scorm_already_loaded = any(
            any(k in f.url for k in ["indexAPI.html", "scormdriver", "dcbstatic"])
            for f in self.page.frames
        )
        if scorm_already_loaded:
            self._log("SCORM loaded after Click 1 — done!")
            return True

        # Click 2: lesson player idle message button
        self._log("Clicking lesson player button...")
        await self._dismiss_modals()

        clicked = await self.page.evaluate("""
            () => {
                const panel = document.querySelector(
                    'dcb-course-lesson-player-idle-message button.lmn-button-theme-accent'
                );
                if (panel) {
                    panel.click();
                    return panel.innerText?.trim();
                }
                const btns = document.querySelectorAll('button.lmn-button-theme-accent');
                const skip = ['Next lesson','Previous','Green Pathways','English','Cancel','Renew','Terminate'];
                for (const btn of btns) {
                    const t = (btn.innerText||'').trim();
                    if (t && !skip.some(s => t.includes(s))) {
                        btn.click();
                        return t;
                    }
                }
                return null;
            }
        """)
        self._log(f"Lesson player btn clicked: '{clicked}'")
        await asyncio.sleep(8)
        self._log(f"URL after lesson click: {self.page.url[:150]}")
        return True

    async def _get_scorm_frame(self):
        self._log("Scanning for SCORM iframe...")
        for attempt in range(20):
            await asyncio.sleep(3)
            for frame in self.page.frames:
                url = frame.url
                if "indexAPI.html" in url or "scormcontent" in url:
                    self._log(f"✅ SCORM frame (indexAPI): {url[:70]}")
                    return frame
            for frame in self.page.frames:
                url = frame.url
                if "launcher.html" in url or ("dcbstatic" in url and "scorm" in url):
                    self._log(f"Found launcher frame: {url[:70]}")
                    return frame
            if attempt < 4:
                urls = [f.url[:70] for f in self.page.frames
                        if f.url and "blank" not in f.url and "about" not in f.url]
                self._log(f"Attempt {attempt+1} frames: {urls}")
        return None

    async def _wait_for_api(self, frame) -> bool:
        self._log("Waiting for SCORM2004_objAPI...")
        for attempt in range(40):
            try:
                ready = await frame.evaluate(
                    "typeof SCORM2004_objAPI !== 'undefined' && SCORM2004_objAPI !== null"
                )
                if ready:
                    self._log(f"✅ SCORM API ready after {attempt*2}s")
                    return True
            except:
                pass
            await asyncio.sleep(2)
        return False

    async def _update_suspend_data(self, frame, progress_pct: int):
        js = f"""
        (async () => {{
            {LZW_DECODE}
            {LZW_ENCODE}
            try {{
                const rawStr = SCORM2004_objAPI.GetValue("cmi.suspend_data");
                if (!rawStr) return {{ ok: false, reason: "no_data" }};
                const raw = JSON.parse(rawStr);
                const data = JSON.parse(lzwDecode(raw));
                const lessonKeys = Object.keys(data.progress.lessons);
                const lessonKey = lessonKeys[lessonKeys.length - 1];
                const items = data.progress.lessons[lessonKey].i;
                const total = Object.keys(items).length;
                const upTo = Math.floor(total * ({progress_pct} / 100));
                for (let i = 0; i < upTo; i++) items[String(i)] = {{ c: 1, i: {{}} }};
                data.progress.lessons[lessonKey].p = upTo;
                const encoded = lzwEncode(JSON.stringify(data));
                SCORM2004_objAPI.SetValue("cmi.suspend_data",
                    JSON.stringify({{ v: 2, d: encoded, cpv: raw.cpv }}));
                return {{ ok: true, lessonKey, upTo, total }};
            }} catch(e) {{ return {{ ok: false, error: e.message }}; }}
        }})()
        """
        try:
            return await frame.evaluate(js)
        except:
            return {"ok": False}

    async def _commit_lesson(self, frame, minutes, progress_pct, terminate: bool = False) -> bool:
        scorm_time = self._format_scorm_time(minutes)
        terminate_call = 'SCORM2004_objAPI.Terminate("");' if terminate else ''
        js = f"""
        (() => {{
            try {{
                SCORM2004_objAPI.SetValue("cmi.session_time",      "{scorm_time}");
                SCORM2004_objAPI.SetValue("cmi.progress_measure",  "1");
                SCORM2004_objAPI.SetValue("cmi.completion_status", "completed");
                SCORM2004_objAPI.SetValue("cmi.success_status",    "passed");
                const r = SCORM2004_objAPI.Commit("");
                {terminate_call}
                return r;
            }} catch(e) {{ return false; }}
        }})()
        """
        try:
            return await frame.evaluate(js)
        except:
            return False

    async def _click_next_lesson(self) -> bool:
        for sel in [
            "button.dcb-course-lesson-header-next-session",
            "[class*='next-session']",
            "button:has-text('Next lesson')",
        ]:
            try:
                btn = await self.page.wait_for_selector(sel, timeout=4000)
                if btn:
                    text = await btn.inner_text()
                    if "no next lesson" in text.lower():
                        return False
                    self._log(f"Clicking next: {text.strip()[:30]}")
                    await btn.click()
                    await asyncio.sleep(4)
                    return True
            except:
                continue
        return False

    async def run_module(self, module_url: str, course: dict, completed_lessons: int = 0) -> bool:
        total_lessons = course["lessons"]
        course_name   = course["name"]
        # When resuming: re-commit all remaining lessons with 5s wait (not 10-20 mins)
        # When fresh: use real 10-20 min wait per lesson
        is_resuming   = completed_lessons > 0
        if is_resuming:
            self._log(f"Resuming module — will fast-commit all lessons (no long waits)")

        try:
            ok = await self._open_module(module_url)
            if not ok:
                return False

            frame = await self._get_scorm_frame()
            if not frame:
                self._log(f"No SCORM frame for {course_name}", "error")
                return False

            if not await self._wait_for_api(frame):
                self._log(f"SCORM API timeout", "error")
                return False

            self._log(f"✅ SCORM ready! {total_lessons} lessons")

            lesson_num = 0
            while True:
                lesson_num += 1

                if self.state:
                    while self.state.is_paused():
                        await asyncio.sleep(5)
                    if self.state.is_stop_requested():
                        return False

                minutes      = self._random_minutes()
                progress_pct = min(100, round(lesson_num / total_lessons * 100))

                # Use real wait only for fresh modules
                # When resuming: use 5 seconds per lesson (fast re-commit)
                if is_resuming:
                    wait_secs = 5
                else:
                    wait_secs = self._real_wait(minutes * 60)

                if self.state:
                    self.state.update(self.email,
                        current_lesson=lesson_num,
                        total_lessons=total_lessons,
                        progress=progress_pct,
                        time_spent=f"{minutes}m",
                        status_label=f"📖 Lesson {lesson_num}/{total_lessons}"
                    )
                self._log(f"Lesson {lesson_num}/{total_lessons} time:{minutes}m")

                sd = await self._update_suspend_data(frame, progress_pct)
                self._log(f"Suspend: {sd}")

                # Detect last lesson by checking Next button
                has_next = await self._check_has_next()
                is_last  = not has_next

                result = await self._commit_lesson(frame, minutes, progress_pct, terminate=is_last)
                self._log(f"Commit {lesson_num}: {result}", "success" if result else "error")

                if is_last:
                    self._log(f"Last lesson committed + terminated — module complete!")
                    await asyncio.sleep(wait_secs)
                    break
                else:
                    await asyncio.sleep(wait_secs)
                    clicked = await self._click_next_lesson()
                    if not clicked:
                        self._log("No next lesson — terminating session")
                        await self._commit_lesson(frame, minutes, progress_pct, terminate=True)
                        break
                    self._log(f"Waiting for next lesson SCORM to load...")
                    await asyncio.sleep(8)
                    new_frame = await self._get_scorm_frame()
                    if new_frame:
                        frame = new_frame
                        if await self._wait_for_api(frame):
                            self._log(f"✅ Next lesson SCORM ready!")
                        else:
                            self._log(f"⚠️ Next lesson API timeout", "warning")
                    else:
                        self._log(f"⚠️ No new SCORM frame", "warning")

            return True

        except Exception as e:
            self._log(f"Error: {e}", "error")
            return False

    async def _check_has_next(self) -> bool:
        """Check if Next lesson button is available and enabled"""
        try:
            await asyncio.sleep(1)
            result = await self.page.evaluate("""
                () => {
                    const allBtns = document.querySelectorAll('button');
                    for (const btn of allBtns) {
                        const text = (btn.innerText || btn.textContent || '').trim();
                        if (!text.toLowerCase().includes('next lesson')) continue;
                        if (text.toLowerCase().includes('no next') ||
                            text.toLowerCase().includes('there is no')) return false;
                        if (btn.disabled) return false;
                        return true;
                    }
                    return false;
                }
            """)
            return bool(result)
        except:
            return False
