# century_bot.py  (Python 3.12+, pip install playwright openai && playwright install chromium)
import ast
import base64
import csv
import difflib
import glob
import hashlib
import html
import json
import os
import random
import re
import shutil
import sys
import threading
import time

import httpx
from openai import OpenAI
from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeout

# --- UTF-8 for Windows console ---
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- Config ---
API_KEY = os.getenv("LLM_API_KEY", "").strip()
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
START_URL = "https://app.century.tech"
STATE_FILE = "century_state.json"
MAX_STEPS = 200
MAX_IDLE = 6          # cycles in a row without progress before stopping
MATCH_THRESHOLD = 0.85

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# --- Selectors (adjust to the real DOM if needed) ---
SELECTORS = {
    "options": (
        "[role='radio'], [role='checkbox'], [role='option'], "
        "label:has(input[type='radio']), label:has(input[type='checkbox'])"
    ),
    "text_inputs": (
        "[data-testid='exact-answer-input'], input[type='text'], "
        "input:not([type]), input[type='number'], textarea"
    ),
    "content": "main",
}
CHECK_RE = r"^\s*(check( answer)?|submit( answer)?|answer)\s*$"
NEXT_RE = r"^\s*(next( question)?|continue|done|finish)\s*$"
RESULTS_RE = r"^\s*(view|see|show)( my| your)? (quiz )?results\s*$"
START_RE = r"^\s*(start|continue|resume|begin|go|let.?s go)\b"
CLICKABLE = "a, button, [role='button'], [role='link'], [role='listitem'], [class*='card' i]"

# key -> (display name, regex that matches the subject on the Century dashboard)
SUBJECTS = {
    "1": ("English", r"\benglish\b|\bspag\b|\breading\b"),
    "2": ("Maths", r"\bmaths?\b|\bmathematics\b"),
    "3": ("Science", r"\bscience\b|\bbiology\b|\bchemistry\b|\bphysics\b"),
}

# Per-subject lesson filters: (must match one of these, must NOT match any of these)
LESSON_FILTERS = {
    "English": (r"\breading\b|\bspag\b|\bspelling\b|\bpunctuation\b|\bgrammar\b",
                r"landmarks?|legends?"),
}


def lesson_allowed(name: str, text: str, strict: bool = True) -> bool:
    """strict=True: text must look like an allowed lesson. strict=False: only the deny list applies."""
    allow, deny = LESSON_FILTERS.get(name, (None, None))
    if deny and re.search(deny, text, re.I):
        return False
    if strict and allow and not re.search(allow, text, re.I):
        return False
    return True

SYSTEM_PROMPT = (
    "You answer quiz questions. Output STRICT plain text only: "
    "no markdown, no quotes, no square brackets, no bullets, no numbering, no explanations. "
    "If options are given, copy the correct option's text EXACTLY as written. "
    "If several options are correct, put each on its own line. "
    "For a free-text question, output only the answer itself. "
    "For a numeric answer, output only plain digits: no thousands separators, no spaces, "
    "no units, no percent or currency signs (example: 345678 or 2.5). "
    "If the task has an image, graph, table or diagram, read it carefully from the screenshot. "
    "If there are several blank fields, output exactly one answer per line, in the order the "
    "fields appear on the page. For ordering or matching tasks, output the items in the correct "
    "order, one per line."
)


# ---------- Human-like behaviour ----------
VERBOSE = False          # technical log lines (menu: L)


def dbg(*args, **kw) -> None:
    if VERBOSE:
        print(*args, **kw)


HUMAN = True            # False = instant clicks/typing
SPEED = 1.0             # 1.0 normal, 0.7 faster, 1.5 slower (set at start-up prompt)


def pause(page: Page, lo: float, hi: float) -> None:
    """Random pause in seconds (no-op when HUMAN is False)."""
    if HUMAN:
        page.wait_for_timeout(int(random.uniform(lo, hi) * 1000 * SPEED))


def reading_pause(page: Page, text: str) -> None:
    """Time a person would take to read the question and think about it."""
    if not HUMAN:
        return
    words = len(text.split())
    secs = min(words / random.uniform(3.5, 5.5), 12) + random.uniform(2.0, 6.0)
    if random.random() < 0.12:
        secs += random.uniform(5, 14)       # occasional longer think
    secs = max(3.0, min(secs, 30.0))
    dbg(f"  (reading/thinking for {secs:.1f}s)")
    page.wait_for_timeout(int(secs * 1000 * SPEED))


def human_click(el) -> None:
    """Moves the mouse along a path to a random point inside the element, then clicks."""
    if not HUMAN:
        el.click()
        return
    page = el.page
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        box = el.bounding_box()
        if box and box["width"] > 4 and box["height"] > 4:
            x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.30, 0.70)
            page.mouse.move(x, y, steps=random.randint(12, 28))
            page.wait_for_timeout(random.randint(80, 260))
            page.mouse.click(x, y, delay=random.randint(40, 130))
            return
    except Exception:
        pass
    el.click()


def human_type(inp, value: str, allow_typos: bool = True) -> None:
    """Types key by key with uneven timing and the odd typo that gets corrected."""
    page = inp.page
    human_click(inp)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(random.randint(150, 400))
    for ch in value:
        if allow_typos and len(value) >= 4 and ch.isalpha() and random.random() < 0.03:
            page.keyboard.type(random.choice("asdfghjklqwertyuiopzxcvbnm"))
            page.wait_for_timeout(random.randint(120, 380))
            page.keyboard.press("Backspace")
            page.wait_for_timeout(random.randint(80, 220))
        page.keyboard.type(ch)
        d = random.randint(60, 190)
        if ch in " ,.":
            d += random.randint(40, 160)
        page.wait_for_timeout(int(d * SPEED))
    page.wait_for_timeout(random.randint(200, 600))
    try:
        if inp.input_value() != value:      # safety net: never leave a wrong value
            inp.fill(value)
    except Exception:
        pass


# ---------- Text utilities ----------
def normalize(s: str) -> str:
    s = s.casefold().replace("\u00a0", " ")
    s = re.sub(r"[^\w\s.,%/+\-−=<>]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def clean_llm_output(raw: str) -> list[str]:
    """Turns the LLM reply into a list of clean answer strings."""
    raw = raw.strip()
    raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "")

    # Case: ['30'] or ["a", "b"]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            val = ast.literal_eval(raw)
            if isinstance(val, (list, tuple)):
                return [str(v).strip() for v in val if str(v).strip()]
        except Exception:
            raw = raw[1:-1]

    answers = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)", "", line)   # list markers
        line = re.sub(r"(\*\*|__|`)", "", line)                    # markdown
        line = line.strip().strip("'\"“”‘’«»").strip()
        line = re.sub(r"^(answer|ответ)\s*:\s*", "", line, flags=re.I)
        if line:
            answers.append(line)
    return answers


def to_number_string(ans: str) -> str:
    """Makes a string safe for <input type=number>: '345,678' -> '345678', '1.234,5' -> '1234.5'."""
    s = ans.strip().replace("\u00a0", " ").replace("−", "-")
    m = re.search(r"-?\d[\d\s,.']*", s)
    if not m:
        return ans.strip()
    num = m.group(0).strip().replace(" ", "").replace("'", "").rstrip(".,")

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):      # European: 1.234,5
            num = num.replace(".", "").replace(",", ".")
        else:                                     # US: 1,234.5
            num = num.replace(",", "")
    elif "," in num:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", num):   # 345,678 -> thousands
            num = num.replace(",", "")
        else:                                           # 2,5 -> decimal
            num = num.replace(",", ".")
    elif num.count(".") > 1:                              # 1.234.567 -> thousands
        num = num.replace(".", "")
    return num


def best_match(answer: str, options: list[str]) -> int | None:
    """Index of best option: exact -> whole words -> fuzzy."""
    a = normalize(answer)
    norm = [normalize(o) for o in options]
    for i, o in enumerate(norm):
        if o == a:
            return i
    for i, o in enumerate(norm):
        if o and a and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", o) and len(a) >= 0.8 * len(o):
            return i
    scores = [difflib.SequenceMatcher(None, a, o).ratio() for o in norm]
    if scores:
        i = max(range(len(scores)), key=scores.__getitem__)
        if scores[i] >= MATCH_THRESHOLD:
            return i
    return None


# ---------- LLM ----------
def llm_create(**kw):
    """chat.completions.create with automatic retry on rate limits and server hiccups."""
    delay = 3.0
    for attempt in range(5):
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            msg = str(e)
            retryable = re.search(r"Error code: (429|5\d\d)|rate.?limit|timed? ?out|connection error",
                                  msg, re.I)
            if attempt == 4 or not retryable:
                raise
            m = re.search(r"try again in ([\d.]+)\s*(ms|s)\b", msg, re.I)
            wait = (float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)) + 1 if m else delay
            wait = min(max(wait, 2.0), 60.0)
            print(f"  ! API busy ({msg.splitlines()[0][:70]}) - retrying in {wait:.0f}s")
            time.sleep(wait)
            delay = min(delay * 2, 30.0)


VISION_MODEL = os.getenv("LLM_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def ask_llm(question_text: str, options: list[str], n_fields: int = 0,
            image: bytes | None = None, hint: str = "") -> list[str]:
    user = f"Question/page text:\n{question_text}\n"
    if options:
        user += "\nOptions:\n" + "\n".join(options)
    if n_fields > 1:
        user += (f"\nThere are {n_fields} answer fields. Output exactly {n_fields} lines, "
                 "one answer per field, in the order the fields appear on the page.")
    if hint:
        user += "\n" + hint
    model, content = MODEL, user
    if image:
        b64 = base64.b64encode(image).decode()
        content = [
            {"type": "text", "text": user + "\nThe attached screenshot shows the full task, "
                                            "including any images, graphs or diagrams. Use it."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        model = VISION_MODEL
    resp = llm_create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    return clean_llm_output(resp.choices[0].message.content or "")


def discover_vision_model() -> str | None:
    """Asks Groq which models it currently serves and picks one that accepts images."""
    try:
        ids = [m.id for m in client.models.list().data]
    except Exception:
        return None
    for pat in (r"llama-4-maverick", r"llama-4-scout", r"vision|[-/]vl\b|qwen.*vl"):
        for i in ids:
            if re.search(pat, i, re.I):
                return i
    return None


VISUALS_JS = r"""() => {
  const m = document.querySelector('main') || document.body;
  return Array.from(m.querySelectorAll('img, canvas, svg')).filter(e => {
    const r = e.getBoundingClientRect();
    return r.width >= 120 && r.height >= 80 && r.bottom > 0 && r.top < innerHeight;
  }).length;
}"""


def page_has_visuals(page: Page) -> bool:
    try:
        return int(page.evaluate(VISUALS_JS)) > 0
    except Exception:
        return False


def take_shot(page: Page) -> bytes:
    try:
        return page.locator(SELECTORS["content"]).first.screenshot(type="jpeg", quality=70, timeout=5000)
    except Exception:
        return page.screenshot(type="jpeg", quality=70)


def ask_smart(page: Page, qtext: str, texts: list[str], n_fields: int, use_vision: bool,
              hint: str = ""):
    """Text model by default; screenshot + vision model when the task has visuals."""
    global VISION_MODEL
    if use_vision:
        for attempt in (1, 2):
            try:
                answers = ask_llm(qtext, texts, n_fields, take_shot(page), hint)
                if answers:
                    return answers, True
                break
            except Exception as e:
                msg = str(e).splitlines()[0] if str(e) else repr(e)
                dbg(f"  ! vision model failed: {msg[:120]}")
                if attempt == 1 and re.search(r"404|not exist|not_found|decommission", msg, re.I):
                    found = discover_vision_model()
                    if found and found != VISION_MODEL:
                        dbg(f"  switching vision model to {found}")
                        VISION_MODEL = found
                        continue
                dbg("  using the text model instead")
                break
    return ask_llm(qtext, texts, n_fields, None, hint), False


# ---------- Telegram: stats + remote control from your phone ----------
TG_TOKEN = os.getenv("TG_TOKEN", "")      # paste the token from @BotFather between the quotes
TG_CHAT_FILE = "century_telegram.json"    # remembers which chat is yours after pairing


class Stats:
    def __init__(self):
        self.started = None
        self.lessons_done = 0
        self.lesson_total = 0
        self.lesson_no = 0
        self.lesson_q = 0
        self.lesson_started = None
        self.questions = 0
        self.by_subject: dict = {}        # name -> {"lessons": n, "questions": n}
        self.manual = 0
        self.errors = 0
        self.current = "idle"
        self.stop = False
        self.paused = False
        self.want_shot = False


STATS = Stats()


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def stats_text() -> str:
    s = STATS
    run_t = fmt_dur(time.time() - s.started) if s.started else "-"
    per_q = f" (~{fmt_dur((time.time() - s.started) / s.questions)} per question)" \
        if s.started and s.questions else ""
    subj = " | ".join(f"{n}: {v['lessons']} lessons, {v['questions']} q"
                      for n, v in s.by_subject.items()) or "-"
    state = "paused" if s.paused else ("stopping after this lesson" if s.stop else "running")
    return (
        "<b>Farming stats</b>\n"
        f"Status: {state}\n"
        f"Running for: {run_t}\n"
        f"Lessons done: {s.lessons_done} / {s.lesson_total}\n"
        f"Questions answered: {s.questions}{per_q}\n"
        f"By subject: {html.escape(subj)}\n"
        f"Needed you: {s.manual}   Errors: {s.errors}\n"
        f"Now: {html.escape(s.current)}"
    )


class Tg:
    """Tiny Telegram client (uses httpx, which the openai package already installs)."""

    def __init__(self, token: str):
        self.token = token.strip()
        self.enabled = bool(self.token)
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.chat_id = None
        self.pair_code = f"{random.randint(0, 9999):04d}"
        self._quit = False
        if self.enabled:
            env_id = os.getenv("TG_CHAT_ID", "").strip()
            if env_id.lstrip("-").isdigit():
                self.chat_id = int(env_id)
            elif os.path.exists(TG_CHAT_FILE):
                try:
                    with open(TG_CHAT_FILE, encoding="utf-8") as f:
                        self.chat_id = json.load(f).get("chat_id")
                except Exception:
                    pass

    # --- low level ---
    def call(self, method: str, timeout: float = 35, **data):
        try:
            r = httpx.post(f"{self.api}/{method}", json=data, timeout=timeout)
            return r.json()
        except Exception:
            return None

    def send(self, text: str) -> None:
        if self.enabled and self.chat_id:
            self.call("sendMessage", chat_id=self.chat_id, text=text,
                      parse_mode="HTML", disable_web_page_preview=True)

    def send_photo(self, png: bytes, caption: str = "") -> None:
        if not (self.enabled and self.chat_id):
            return
        try:
            httpx.post(f"{self.api}/sendPhoto", data={"chat_id": self.chat_id, "caption": caption},
                       files={"photo": ("screen.png", png, "image/png")}, timeout=30)
        except Exception:
            pass

    def reply(self, chat: int, text: str) -> None:
        self.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML")

    # --- commands from the phone ---
    def handle(self, chat: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()

        if self.chat_id is None:                       # not paired yet
            if cmd == "/start" and arg.strip() == self.pair_code:
                self.chat_id = chat
                try:
                    with open(TG_CHAT_FILE, "w", encoding="utf-8") as f:
                        json.dump({"chat_id": chat}, f)
                except Exception:
                    pass
                self.reply(chat, "Connected. Send /help to see the commands.")
                print("  Telegram: paired with your phone.")
            else:
                self.reply(chat, "Send /start CODE  (the 4-digit code is shown in the bot window).")
            return
        if chat != self.chat_id:                       # ignore strangers
            return

        if cmd in ("/stats", "/status", "/start"):
            self.reply(chat, stats_text())
        elif cmd == "/pause":
            STATS.paused = True
            self.reply(chat, "Paused. Send /resume to continue.")
        elif cmd == "/resume":
            STATS.paused = False
            self.reply(chat, "Resumed.")
        elif cmd == "/stop":
            STATS.stop = True
            STATS.paused = False
            self.reply(chat, "Stopping. The bot ends after the current step.")
        elif cmd == "/history":
            self.reply(chat, history_text())
        elif cmd == "/shot":
            STATS.want_shot = True
            self.reply(chat, "Taking a screenshot...")
        else:
            self.reply(chat, "/stats - numbers so far\n/pause - pause\n/resume - continue\n"
                             "/shot - screenshot of the browser\n/stop - stop the bot\n/history - last lessons")

    def poll(self) -> None:
        offset = 0
        while not self._quit:
            res = self.call("getUpdates", timeout=40, offset=offset, allowed_updates=["message"])
            # note: the "timeout" above is Telegram's long-poll seconds; httpx uses its own timeout
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = (msg.get("chat") or {}).get("id")
                if text and chat is not None:
                    try:
                        self.handle(chat, text)
                    except Exception:
                        pass

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self.poll, daemon=True).start()

    def stop(self) -> None:
        self._quit = True


TG = Tg("")      # replaced in main() once the menu choice is known


HISTORY_FILE = "century_history.csv"


def append_history(name: str, n: int, questions: int, secs: float) -> None:
    new = not os.path.exists(HISTORY_FILE)
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "subject", "lesson_no", "questions", "seconds"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), name, n, questions, int(secs)])
    except Exception:
        pass


def history_text(limit: int = 8) -> str:
    try:
        with open(HISTORY_FILE, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))[1:]
    except Exception:
        return "No history yet."
    if not rows:
        return "No history yet."
    lines = [f"{r[0][5:16]}  {r[1]}  {r[3]} q  {fmt_dur(float(r[4]))}" for r in rows[-limit:]]
    total_q = sum(int(r[3]) for r in rows)
    return ("<b>Last lessons</b>\n" + "\n".join(html.escape(x) for x in lines)
            + f"\n\nAll time: {len(rows)} lessons, {total_q} questions")


def progress_bar(done: int, total: int, width: int = 20) -> str:
    total = max(total, 1)
    filled = int(width * min(done, total) / total)
    return "█" * filled + "░" * (width - filled)


def status_line() -> str:
    s = STATS
    elapsed = time.time() - s.started if s.started else 0
    eta = ""
    if s.lessons_done >= 1 and s.lesson_total > s.lessons_done:
        eta = f" | ETA {fmt_dur(elapsed / s.lessons_done * (s.lesson_total - s.lessons_done))}"
    return (f"  [{progress_bar(s.lessons_done, s.lesson_total)}] {s.lessons_done}/{s.lesson_total} lessons"
            f" | {s.questions} q | {fmt_dur(elapsed)}{eta}")


def alert() -> None:
    """Sound on the PC when the bot needs you."""
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(880, 200)
            time.sleep(0.1)
    except Exception:
        print(chr(7), end="", flush=True)


def note_question() -> None:
    STATS.questions += 1
    STATS.lesson_q += 1
    print(paint(status_line(), "90"))


def record_lesson(name: str, n: int, total: int) -> None:
    secs = time.time() - (STATS.lesson_started or time.time())
    d = STATS.by_subject.setdefault(name, {"lessons": 0, "questions": 0})
    d["lessons"] += 1
    d["questions"] += STATS.lesson_q
    STATS.lessons_done += 1
    append_history(name, n, STATS.lesson_q, secs)
    print(paint(f"  Lesson {n}/{total} done: {STATS.lesson_q} questions in {fmt_dur(secs)}", "92"))
    TG.send(f"Lesson {n}/{total} done: <b>{html.escape(name)}</b>\n"
            f"{STATS.lesson_q} questions in {fmt_dur(secs)}")


# ---------- Page handling ----------
def visible_locators(page: Page, selector: str):
    loc = page.locator(selector)
    return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]


def find_button(page: Page, pattern: str):
    """Returns an ACTIVE button matching the name, or None."""
    loc = page.get_by_role("button", name=re.compile(pattern, re.I))
    for i in range(loc.count()):
        b = loc.nth(i)
        try:
            if (b.is_visible() and b.is_enabled()
                    and b.get_attribute("aria-disabled") != "true"
                    and b.get_attribute("disabled") is None):
                return b
        except Exception:
            continue
    return None


def wait_button(page: Page, pattern: str, timeout: float = 5.0):
    end = time.time() + timeout
    while time.time() < end:
        b = find_button(page, pattern)
        if b:
            return b
        page.wait_for_timeout(250)
    return None


def page_text(page: Page) -> str:
    try:
        return page.locator(SELECTORS["content"]).first.inner_text(timeout=3000)[:4000]
    except Exception:
        return page.inner_text("body")[:4000]


def is_multi(opt) -> bool:
    role = (opt.get_attribute("role") or "").lower()
    if role == "checkbox":
        return True
    return opt.locator("input[type='checkbox']").count() > 0


def fill_input(inp, ans: str) -> None:
    """Fills a text/number input; sanitizes numbers and falls back to real typing."""
    input_type = (inp.get_attribute("type") or "text").lower()
    value = to_number_string(ans) if input_type == "number" else ans
    dbg(f"  fill ({input_type}): {value!r}")
    if HUMAN:
        try:
            human_type(inp, value, allow_typos=(input_type != "number"))
            return
        except Exception as e:
            dbg(f"  ! human typing failed ({str(e).splitlines()[0]}), using fill")
    try:
        inp.fill(value)
    except Exception as e:
        dbg(f"  ! fill() failed ({str(e).splitlines()[0]}), typing instead")
        inp.click()
        inp.press("Control+A")
        inp.press("Delete")
        inp.type(value, delay=40)


def option_texts(opts) -> list[str]:
    """Visible text of each option; falls back to image alt / aria-label."""
    texts = []
    for o in opts:
        t = re.sub(r"\s+", " ", o.inner_text()).strip()
        if not t:
            try:
                t = (o.locator("img").first.get_attribute("alt", timeout=500)
                     or o.get_attribute("aria-label") or "").strip()
            except Exception:
                t = ""
        texts.append(t)
    return texts


def click_options(page: Page, opts, texts: list[str], answers: list[str],
                  numbered: bool = False) -> int:
    """Clicks the option(s) matching the answers. Returns how many were clicked."""
    multi = any(is_multi(o) for o in opts)
    targets = answers if multi else answers[:1]
    picked = set()
    for ans in targets:
        if numbered:                                  # image options: answer is the option number
            m = re.search(r"\d+", ans)
            idx = int(m.group()) - 1 if m and 0 <= int(m.group()) - 1 < len(opts) else None
        else:
            idx = best_match(ans, texts)
        if idx is None:
            dbg(f"  ! no option found for: {ans!r}")
            continue
        if idx in picked:
            continue
        picked.add(idx)
        human_click(opts[idx])
        page.wait_for_timeout(150)
        pause(page, 0.3, 1.0)
    return len(picked)


def solve_selects(page: Page, selects, qtext: str) -> bool:
    """Handles <select> dropdowns (fill-in-the-blank with a choice list)."""
    total = len(selects)
    for k, sel in enumerate(selects, 1):
        try:
            raw = [t.strip() for t in sel.locator("option").all_inner_texts()]
        except Exception:
            return False
        items = [(t, re.sub(r"\s+", " ", t).strip()) for t in raw]
        items = [(o, n) for o, n in items
                 if n and not re.match(r"^(select|choose|please|pick|\.\.\.|--)", n, re.I)]
        if not items:
            continue
        answers = ask_llm(qtext + f"\n(Choose the correct entry for dropdown {k} of {total}.)",
                          [n for _, n in items])
        dbg(f"  LLM (dropdown {k}): {answers}")
        idx = best_match(answers[0], [n for _, n in items]) if answers else None
        if idx is None:
            dbg("  ! no dropdown entry matched")
            return False
        sel.select_option(label=items[idx][0])
        pause(page, 0.5, 1.5)
    return True


def solve_current(page: Page) -> bool:
    """Answers the current question and clicks Check/Submit. True if submitted."""
    opts = visible_locators(page, SELECTORS["options"])
    inputs = [i for i in visible_locators(page, SELECTORS["text_inputs"]) if i.is_enabled()]
    selects = [s for s in visible_locators(page, "select") if s.is_enabled()]
    qtext = page_text(page)

    if not (opts or inputs or selects):
        if not solve_dragdrop(page, qtext):          # matching / ordering tasks
            return False
    else:
        texts = option_texts(opts)
        numbered = bool(opts) and any(not t for t in texts)      # picture-only options
        llm_opts = ([f"Option {i + 1}: {t or '[picture only - see screenshot]'}"
                     for i, t in enumerate(texts)] if numbered else texts)
        hint = ("Some options are pictures. Answer with the option number only (for example 2); "
                "if several are correct, give one number per line.") if numbered else ""

        if opts or inputs:
            use_vision = numbered or page_has_visuals(page)
            if use_vision:
                dbg("  (image/diagram on this task - using the vision model)")
            answers, used_vision = ask_smart(page, qtext, llm_opts,
                                             0 if opts else len(inputs), use_vision, hint)
            dbg(f"  LLM: {answers}")
            if not answers:
                return False
            reading_pause(page, qtext)

            if opts:
                n = click_options(page, opts, texts, answers, numbered)
                if n == 0 and not used_vision:
                    dbg("  (no option matched - retrying with the vision model)")
                    answers, used_vision = ask_smart(page, qtext, llm_opts, 0, True, hint)
                    dbg(f"  LLM (vision): {answers}")
                    n = click_options(page, opts, texts, answers, numbered) if answers else 0
                if n == 0:
                    return False
            else:
                for inp, ans in zip(inputs, answers):
                    fill_input(inp, ans)
                if len(inputs) > len(answers):
                    dbg("  ! more fields than answers")
        else:
            reading_pause(page, qtext)

        if selects and not solve_selects(page, selects, qtext):
            return False

    btn = wait_button(page, CHECK_RE)
    if not btn:
        print("  ! Check/Submit button is not active")
        return False
    pause(page, 0.6, 1.8)
    human_click(btn)
    page.wait_for_timeout(800)
    pause(page, 1.5, 4.0)          # looking at the feedback
    note_question()
    return True


DRAGGABLE_SEL = (
    "[draggable='true'], [data-rbd-draggable-id], [data-dnd-draggable], "
    "[aria-roledescription='sortable'], [aria-roledescription='draggable'], "
    "[class*='draggable' i], [data-testid*='drag' i]"
)
DROPZONE_SEL = (
    "[data-rbd-droppable-id], [data-dnd-droppable], [class*='dropzone' i], "
    "[class*='drop-zone' i], [class*='droppable' i], [data-testid*='drop' i]"
)


def node_text(el) -> str:
    try:
        return re.sub(r"\s+", " ", el.inner_text(timeout=800)).strip()
    except Exception:
        return ""


def label_of(el) -> str:
    t = node_text(el)
    if t:
        return t
    try:
        raw = el.evaluate("e => (e.parentElement && e.parentElement.innerText) || ''")
        return re.sub(r"\s+", " ", raw or "").strip()[:160]
    except Exception:
        return ""


def collect(page: Page, selector: str, max_len: int = 160, labels: bool = False):
    loc = page.locator(selector)
    out = []
    for i in range(min(loc.count(), 80)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        t = label_of(el) if labels else node_text(el)
        if len(t) <= max_len:
            out.append((t, el))
    return out


def drag_item(page: Page, src, dst) -> bool:
    """Drags src onto dst with a mouse path (works for HTML5 and most JS drag libraries)."""
    try:
        src.scroll_into_view_if_needed(timeout=3000)
        dst.scroll_into_view_if_needed(timeout=3000)
        a, b = src.bounding_box(), dst.bounding_box()
        if not a or not b:
            raise RuntimeError("no bounding box")
        ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
        bx, by = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        page.mouse.move(ax, ay, steps=random.randint(8, 16))
        page.mouse.down()
        page.wait_for_timeout(random.randint(100, 220))
        page.mouse.move(ax + random.uniform(8, 14), ay + random.uniform(8, 14), steps=4)
        page.mouse.move(bx, by, steps=random.randint(18, 32))
        page.wait_for_timeout(random.randint(120, 260))
        page.mouse.up()
        page.wait_for_timeout(400)
        return True
    except Exception as e:
        dbg(f"  ! mouse drag failed ({str(e).splitlines()[0][:80]}), trying drag_to")
        try:
            src.drag_to(dst, timeout=5000)
            return True
        except Exception as e2:
            dbg(f"  ! drag_to failed too: {str(e2).splitlines()[0][:80]}")
            return False


def parse_pairs(answers: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for line in answers:
        parts = re.split(r"\s*(?:=>|->|\u2192|\u21d2)\s*", line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


def reorder(get_items, drag, desired: list[str]) -> bool:
    """Moves items one position at a time until the list order equals `desired`."""
    for i, want in enumerate(desired):
        texts, els = get_items()
        if i >= len(els):
            break
        j = best_match(want, texts)
        if j is None:
            dbg(f"  ! item not found: {want!r}")
            return False
        if j != i and not drag(els[j], els[i]):
            return False
    return True


def solve_dragdrop(page: Page, qtext: str) -> bool:
    """Matching (items -> targets) and ordering (sortable list) tasks. Best effort."""
    items = [(t, e) for t, e in collect(page, DRAGGABLE_SEL) if t]
    if len(items) < 2:
        return False
    drops = [(t, e) for t, e in collect(page, DROPZONE_SEL, 400, labels=True) if t]
    item_texts = [t for t, _ in items]
    use_vision = page_has_visuals(page)

    if len(drops) >= 2:
        drop_texts = [t for t, _ in drops]
        extra = ("\nThis is a matching task. Items to place:\n"
                 + "\n".join(f"- {t}" for t in item_texts)
                 + "\nTargets:\n" + "\n".join(f"- {t}" for t in drop_texts)
                 + "\nOutput one line per item in the form: item => target, copying the texts exactly.")
        answers, _ = ask_smart(page, qtext + extra, [], 0, use_vision)
        dbg(f"  LLM (matching): {answers}")
        pairs = parse_pairs(answers)
        if not pairs:
            return False
        reading_pause(page, qtext)
        done = 0
        for it, tg in pairs:
            ii = best_match(it, item_texts)
            jj = best_match(tg, drop_texts)
            if ii is None or jj is None:
                dbg(f"  ! could not map pair: {it!r} => {tg!r}")
                continue
            cur_items = [(t, e) for t, e in collect(page, DRAGGABLE_SEL) if t]
            cur_drops = [(t, e) for t, e in collect(page, DROPZONE_SEL, 400, labels=True) if t]
            k = best_match(item_texts[ii], [t for t, _ in cur_items])
            if k is None or jj >= len(cur_drops):
                dbg(f"  ! item or target disappeared: {it!r}")
                continue
            dbg(f"  drag: {item_texts[ii]!r} -> {drop_texts[jj]!r}")
            if drag_item(page, cur_items[k][1], cur_drops[jj][1]):
                done += 1
            pause(page, 0.6, 1.6)
        return done > 0

    # ordering task
    extra = ("\nThis is an ordering task. Items:\n" + "\n".join(f"- {t}" for t in item_texts)
             + "\nOutput the items in the correct order, one per line, copying the texts exactly.")
    answers, _ = ask_smart(page, qtext + extra, [], 0, use_vision)
    dbg(f"  LLM (order): {answers}")
    if len(answers) < 2:
        return False
    reading_pause(page, qtext)

    def get_items():
        cur = [(t, e) for t, e in collect(page, DRAGGABLE_SEL) if t]
        return [t for t, _ in cur], [e for _, e in cur]

    ok = reorder(get_items, lambda s, d: drag_item(page, s, d), answers)
    texts, _ = get_items()
    dbg(f"  order now: {texts}")
    return ok


def has_visible_button(page: Page, pattern: str) -> bool:
    loc = page.get_by_role("button", name=re.compile(pattern, re.I))
    for i in range(min(loc.count(), 10)):
        try:
            if loc.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False


def manual_assist(page: Page, n: int) -> bool:
    """Task type the bot can't solve: save it for debugging and let the user do it. False = stop."""
    png, html = f"century_unknown_{n}.png", f"century_unknown_{n}.html"
    try:
        page.screenshot(path=png)
    except Exception:
        pass
    try:
        body = page.locator(SELECTORS["content"]).first.inner_html(timeout=3000)
        with open(html, "w", encoding="utf-8") as f:
            f.write(body[:80000])
    except Exception:
        pass
    print(paint("\n  !  The bot can't solve this task type yet.", "93"))
    print(f"     Saved {png} and {html} next to the script (send me the HTML to add support).")
    STATS.manual += 1
    STATS.current = "waiting for you at the PC (task the bot can't solve)"
    alert()
    TG.send("<b>Needs you at the PC</b>: a task the bot can't solve.")
    try:
        TG.send_photo(page.screenshot(type="png"), "Task that needs you")
    except Exception:
        pass
    ans = input(paint("     Do it yourself in the browser, then press Enter (S = stop the bot) > ", "96"))
    STATS.current = "back to work"

    return ans.strip().lower() != "s"


def signature(page: Page) -> str:
    return hashlib.md5(page_text(page).encode("utf-8", "ignore")).hexdigest()


def run(page: Page):
    submitted_sig = None
    idle = 0
    unknown_prompts = 0
    for step in range(MAX_STEPS):
        if page.is_closed():
            print("Browser page was closed - stopping.")
            break
        while STATS.paused and not STATS.stop:
            page.wait_for_timeout(1000)
        if STATS.stop:
            print("Stopped from Telegram.")
            break
        if STATS.want_shot:
            STATS.want_shot = False
            try:
                TG.send_photo(page.screenshot(type="png"), "Current screen")
            except Exception:
                pass
        progressed = False
        try:
            page.wait_for_timeout(700)
            sig = signature(page)

            if sig != submitted_sig:
                dbg(f"[{step}] new screen")
                if solve_current(page):
                    submitted_sig = signature(page)   # state after checking
                    progressed = True

            # "View results" = end of the quiz: click it and stop
            res = find_button(page, RESULTS_RE)
            if res:
                dbg("  click: View results - finished.")
                human_click(res)
                page.wait_for_timeout(1500)
                break

            # Click Next/Continue only if there's no task or it's already submitted
            has_task = bool(visible_locators(page, SELECTORS["options"])
                            or visible_locators(page, SELECTORS["text_inputs"]))
            if not has_task or submitted_sig is not None:
                nxt = find_button(page, NEXT_RE)
                if nxt and (not has_task or progressed or sig == submitted_sig):
                    dbg("  click: Next/Continue")
                    pause(page, 1.0, 3.0)
                    human_click(nxt)
                    submitted_sig = None
                    progressed = True
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except PWTimeout:
                        pass
        except Exception as e:
            if page.is_closed():
                print("Browser page was closed - stopping.")
                break
            STATS.errors += 1
            dbg(f"  ! error (page probably navigating): {str(e).splitlines()[0]}")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        idle = 0 if progressed else idle + 1
        if idle >= 2 and unknown_prompts < 8 and has_visible_button(page, CHECK_RE):
            unknown_prompts += 1
            if not manual_assist(page, unknown_prompts):
                break
            idle = 0
            submitted_sig = None
            continue
        if idle >= MAX_IDLE:
            print("No progress - stopping (lesson finished or manual help needed).")
            break


# ---------- Console UI ----------
USE_COLOR = sys.stdout.isatty() and "idlelib" not in sys.modules
if USE_COLOR and os.name == "nt":
    os.system("")      # switches on ANSI colours in the Windows console

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

BANNER = [
    ' ██████╗███████╗███╗   ██╗████████╗██╗   ██╗██████╗ ██╗   ██╗',
    '██╔════╝██╔════╝████╗  ██║╚══██╔══╝██║   ██║██╔══██╗╚██╗ ██╔╝',
    '██║     █████╗  ██╔██╗ ██║   ██║   ██║   ██║██████╔╝ ╚████╔╝',
    '██║     ██╔══╝  ██║╚██╗██║   ██║   ██║   ██║██╔══██╗  ╚██╔╝',
    '╚██████╗███████╗██║ ╚████║   ██║   ╚██████╔╝██║  ██║   ██║',
    ' ╚═════╝╚══════╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝   ╚═╝',
]
GRADIENT = ["38;5;51", "38;5;45", "38;5;39", "38;5;33", "38;5;63", "38;5;99"]

CFG = {"subjects": ["1", "2", "3"], "lessons": 3, "speed": "1", "human": True, "telegram": True, "autoclear": True, "verbose": False}
SPEEDS = {"1": ("Normal", 1.0), "2": ("Fast", 0.7), "3": ("Slow", 1.5)}


def paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if USE_COLOR else text


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def frame(title: str, rows: list[str], width: int = 62, code: str = "36") -> None:
    t = f" {title} "
    left = (width - len(t)) // 2
    print(paint("╔" + "═" * left + t + "═" * (width - left - len(t)) + "╗", code))
    for r in rows:
        pad = max(width - vlen(r) - 1, 0)
        print(paint("║", code) + " " + r + " " * pad + paint("║", code))
    print(paint("╚" + "═" * width + "╝", code))


def clear_screen() -> None:
    """Wipes the console and its scroll-back. IDLE/IDE consoles cannot be wiped, so push old text away."""
    if USE_COLOR:
        try:
            os.system("cls" if os.name == "nt" else "clear")
            print("\x1b[3J", end="", flush=True)      # scroll-back (Windows Terminal, Linux, macOS)
        except Exception:
            pass
    else:
        print("\n" * 60)


def header() -> None:
    """One-line title used after the console is cleared."""
    print(paint("  CENTURY BOT", "38;5;45") + paint("  |  auto lesson bot  |  powered by Groq", "90"))


def delete_debug_files() -> None:
    """Deletes debug output only (login, settings and history are kept)."""
    patterns = ["century_debug.txt", "century_debug.png", "century_clicks.txt",
                "century_unknown_*.png", "century_unknown_*.html"]
    files = sorted({f for p in patterns for f in glob.glob(p)})
    if not files:
        print("\n  No debug files to delete.")
        time.sleep(1.5)
        return
    print("\n  These files will be deleted:")
    for f in files:
        print(f"   - {f}")
    if input("\n  Type Y to delete them > ").strip().lower() == "y":
        n = 0
        for f in files:
            try:
                os.remove(f)
                n += 1
            except Exception:
                pass
        print(f"  Deleted {n} file(s). Login, settings and history are kept.")
        time.sleep(1.5)


def banner() -> None:
    try:
        rows = shutil.get_terminal_size((100, 40)).lines
    except Exception:
        rows = 40
    print()
    if rows < 36:                      # short window (default cmd is 30 rows): keep the menu on screen
        header()
        print()
        return
    for i, line in enumerate(BANNER):
        print(paint(line, GRADIENT[i % len(GRADIENT)]))
    print(paint("      A U T O   L E S S O N   B O T   ·   powered by Groq", "90"))
    print()


def render_menu() -> None:
    clear_screen()
    banner()
    on, off = paint("●", "92"), paint("○", "90")
    subj = "  ".join(f"{on if k in CFG['subjects'] else off} {SUBJECTS[k][0]}" for k in SUBJECTS)
    sp_name, sp_val = SPEEDS[CFG["speed"]]
    human = paint("ON ", "92") if CFG["human"] else paint("OFF", "91")
    autoclear = paint("ON ", "92") if CFG["autoclear"] else paint("OFF", "91")
    logs = paint("detailed", "93") if CFG["verbose"] else paint("clean", "92")
    if not TG_TOKEN:
        tg_state = paint("no token set (see TG_TOKEN in the file)", "90")
    else:
        tg_state = paint("ON ", "92") if CFG["telegram"] else paint("OFF", "91")
    label = lambda s: paint(s.ljust(11), "96")
    frame("SETTINGS", [
        f"{label('Subjects')} {subj}",
        f"{label('Lessons')} {CFG['lessons']}",
        f"{label('Speed')} {sp_name} ({sp_val}x)",
        f"{label('Human mode')} {human}",
        f"{label('AI model')} {MODEL}",
        f"{label('Telegram')} {tg_state}",
        f"{label('English')} Reading + SPaG only (no Landmarks & Legends)",
        f"{label('Auto-clear')} {autoclear}",
        f"{label('Logs')} {logs}",
    ])
    print()
    items = [
        ("1", "▶  Start the bot", "92"),
        ("2", "Choose subjects", "97"),
        ("3", "Number of lessons", "97"),
        ("4", "Speed", "97"),
        ("5", "Human mode on / off", "97"),
        ("6", "Record my clicks (debug)", "97"),
        ("7", "Telegram stats on / off", "97"),
        ("8", "Choose AI model (from your Groq list)", "97"),
        ("9", "Auto-clear the console between lessons", "97"),
        ("L", "Detailed logs on / off", "97"),
        ("D", "Delete debug files", "97"),
        ("0", "Quit", "91"),
    ]
    for key, text, col in items:
        print(f"   {paint('[' + key + ']', '93')} {paint(text, col)}")
    print()


def toggle_subjects() -> None:
    print("\n  Type numbers to switch subjects on/off:  1 = English   2 = Maths   3 = Science")
    raw = input("  (e.g. 1,3  or  A for all) › ").strip().lower()
    if not raw:
        return
    if raw == "a":
        CFG["subjects"] = ["1", "2", "3"]
        return
    for k in re.split(r"[,\s]+", raw):
        if k in SUBJECTS:
            if k in CFG["subjects"]:
                CFG["subjects"].remove(k)
            else:
                CFG["subjects"].append(k)
    if not CFG["subjects"]:
        CFG["subjects"] = ["1", "2", "3"]
        print("  At least one subject is needed - all three switched back on.")
        time.sleep(1.2)
    CFG["subjects"].sort()


SETTINGS_FILE = "century_settings.json"


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({**CFG, "model": MODEL}, f)
    except Exception:
        pass


def load_settings() -> None:
    global MODEL
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    subs = [k for k in data.get("subjects", []) if k in SUBJECTS]
    if subs:
        CFG["subjects"] = sorted(set(subs))
    if isinstance(data.get("lessons"), int):
        CFG["lessons"] = max(1, min(50, data["lessons"]))
    if data.get("speed") in SPEEDS:
        CFG["speed"] = data["speed"]
    for key in ("human", "telegram", "autoclear", "verbose"):
        if isinstance(data.get(key), bool):
            CFG[key] = data[key]
    if "LLM_MODEL" not in os.environ and isinstance(data.get("model"), str) and data["model"]:
        MODEL = data["model"]


def pick_model() -> None:
    """Lists the models your key can use and lets you choose one."""
    global MODEL
    print("\n  Asking Groq which models your key can use...")
    try:
        ids = sorted(m.id for m in client.models.list().data)
    except Exception as e:
        print(f"  ! could not list models: {str(e).splitlines()[0][:80]}")
        time.sleep(2)
        return
    ids = [i for i in ids if not re.search(r"whisper|tts|guard|embed|orpheus|playai|safeguard", i, re.I)]
    if not ids:
        print("  ! no usable models found")
        time.sleep(2)
        return
    for k, i in enumerate(ids, 1):
        mark = paint("  <- current", "92") if i == MODEL else ""
        print(f"   [{k}] {i}{mark}")
    raw = input("\n  Number of the model (Enter = keep) > ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(ids):
        MODEL = ids[int(raw) - 1]


def menu() -> str:
    """Interactive start menu. Returns 'start', 'record' or 'quit'."""
    global HUMAN, SPEED, VERBOSE
    load_settings()
    while True:
        save_settings()
        render_menu()
        choice = input(paint("  Select › ", "96")).strip().lower()
        if choice in ("1", "6"):
            HUMAN = CFG["human"]
            SPEED = SPEEDS[CFG["speed"]][1]
            VERBOSE = CFG["verbose"]
            return "start" if choice == "1" else "record"
        if choice == "0":
            return "quit"
        if choice == "2":
            toggle_subjects()
        elif choice == "3":
            raw = input("\n  How many lessons? (1-50) › ").strip()
            if raw.isdigit():
                CFG["lessons"] = max(1, min(50, int(raw)))
        elif choice == "4":
            raw = input("\n  1 = Normal   2 = Fast   3 = Slow › ").strip()
            if raw in SPEEDS:
                CFG["speed"] = raw
        elif choice == "5":
            CFG["human"] = not CFG["human"]
        elif choice == "7":
            CFG["telegram"] = not CFG["telegram"]
        elif choice == "8":
            pick_model()
        elif choice == "9":
            CFG["autoclear"] = not CFG["autoclear"]
        elif choice == "l":
            CFG["verbose"] = not CFG["verbose"]
        elif choice == "d":
            delete_debug_files()


def settle(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def find_candidates(page: Page, pattern: str, max_len: int = 150):
    """Visible clickable elements whose own text matches pattern -> [(text, locator)]."""
    rx = re.compile(pattern, re.I)
    loc = page.locator(CLICKABLE).filter(has_text=rx)
    out = []
    for i in range(min(loc.count(), 200)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            text = re.sub(r"\s+", " ", el.inner_text(timeout=1000)).strip()
        except Exception:
            continue
        if text and len(text) <= max_len and rx.search(text):
            out.append((text, el))
    return out


NUGGET_SEL = (
    "[data-testid*='nugget' i], [class*='nugget' i], a[href*='nugget'], "
    "a[href*='session'], a[href*='/learn/']"
)

RECORDER_JS = r"""
(() => {
  if (window.__recInstalled) return;
  window.__recInstalled = true;
  document.addEventListener('click', e => {
    const chain = [];
    let el = e.target;
    for (let i = 0; i < 4 && el && el.tagName; i++, el = el.parentElement) {
      chain.push({
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 70),
        href: el.getAttribute('href'),
        testid: el.getAttribute('data-testid'),
        role: el.getAttribute('role'),
        cls: ((el.className && el.className.toString()) || '').slice(0, 70)
      });
    }
    window.__centuryClick(JSON.stringify(chain));
  }, true);
})();
"""


def dump_clickables(page: Page) -> None:
    """Debug helper: prints and saves what the bot can see, so selectors can be fixed."""
    lines = [f"URL: {page.url}", ""]
    loc = page.locator("a, button, [role='button'], [role='link'], [data-testid]")
    for i in range(min(loc.count(), 300)):
        try:
            el = loc.nth(i)
            if not el.is_visible():
                continue
            text = re.sub(r"\s+", " ", el.inner_text(timeout=500)).strip()[:80]
            tag, href, testid, cls = el.evaluate(
                "e => [e.tagName.toLowerCase(), e.getAttribute('href'), "
                "e.getAttribute('data-testid'), (e.className || '').toString().slice(0, 70)]"
            )
            lines.append(f"<{tag}> text={text!r} href={href!r} testid={testid!r} class={cls!r}")
        except Exception:
            continue
    try:
        with open("century_debug.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        page.screenshot(path="century_debug.png")
    except Exception:
        pass
    dbg("  --- what the bot sees (first 40 lines) ---")
    for ln in lines[:42]:
        dbg("   " + ln)
    print("  Saved: century_debug.txt and century_debug.png (in the same folder as the script)")


def safe_click(el) -> bool:
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    for force in (False, True):
        try:
            if force:
                el.click(timeout=5000, force=True)
            else:
                human_click(el)
            return True
        except Exception as e:
            dbg(f"  ! click failed (force={force}): {str(e).splitlines()[0]}")
    return False


def nugget_candidates(page: Page, max_len: int = 200):
    loc = page.locator(NUGGET_SEL)
    out = []
    for i in range(min(loc.count(), 100)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            text = re.sub(r"\s+", " ", el.inner_text(timeout=1000)).strip()
        except Exception:
            continue
        if text and len(text) <= max_len:
            out.append((text, el))
    return out


def lesson_is_open(page: Page) -> bool:
    return bool(visible_locators(page, SELECTORS["options"])
                or visible_locators(page, "[data-testid='exact-answer-input']"))


def on_lesson_page(page: Page) -> bool:
    url = page.url.lower()
    return lesson_is_open(page) or "/nugget" in url or "/session" in url


START_BTN_RE = r"^\s*(start|resume|continue)\b"
MYCOURSES_RE = r"^\s*(my )?courses?\s*$"

# For a Start button: text of its card (nearest ancestor holding only this one Start button)
CONTEXT_JS = r"""
e => {
  const re = /^\s*(start|resume|continue)\b/i;
  let p = e.parentElement, best = '';
  for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
    const starts = Array.from(p.querySelectorAll('button, a, [role="button"]'))
      .filter(x => re.test((x.innerText || '').trim()));
    if (starts.length > 1) break;
    const t = (p.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length > 500) break;
    if (t) best = t;
  }
  return best;
}
"""


def start_buttons_with_context(page: Page):
    """All visible Start/Resume/Continue buttons -> [(card text, locator)]."""
    rx = re.compile(START_BTN_RE, re.I)
    out = []
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=rx)
        for i in range(min(loc.count(), 60)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                ctx = el.evaluate(CONTEXT_JS)
            except Exception:
                continue
            ctx = re.sub(r"\s+", " ", ctx or "").strip()
            out.append((ctx or "(start button)", el))
    return out


def nav_candidates(page: Page):
    rx = re.compile(MYCOURSES_RE, re.I)
    out = []
    for role in ("link", "tab", "button"):
        loc = page.get_by_role(role, name=rx)
        for i in range(min(loc.count(), 10)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    out.append(("My Courses", el))
            except Exception:
                continue
    return out


def pick_options(page: Page, stage: int, pattern: str, tried: set, name: str = ""):
    rx = re.compile(pattern, re.I)

    if stage == 1:
        starts = []
        for _ in range(4):                      # dashboard may load slowly
            starts = start_buttons_with_context(page)
            if starts:
                break
            page.wait_for_timeout(2000)
        dbg(f"  found {len(starts)} Start button(s) on the page")
        for ctx, _el in starts[:10]:
            dbg(f"    - {ctx[:100]!r}")
    else:
        starts = start_buttons_with_context(page)

    strict = stage == 1      # on the home page cards carry the course name; deeper pages may not
    matching = [(c, e) for c, e in starts
                if rx.search(c) and c not in tried and lesson_allowed(name, c, strict)]
    if matching:
        return "start button (right subject)", matching

    subj = [(t, e) for t, e in find_candidates(page, pattern)
            if t not in tried and lesson_allowed(name, t, strict=False)]
    if subj:
        return "subject", subj

    nav = [(t, e) for t, e in nav_candidates(page) if t not in tried]
    if nav and stage == 1:
        return "My Courses tab", nav

    anyst = [(c, e) for c, e in starts if c not in tried and lesson_allowed(name, c, strict=False)]
    if anyst and stage > 1:
        return "start button", anyst

    cards = [(t, e) for t, e in nugget_candidates(page)
             if t not in tried and lesson_allowed(name, t, strict=False)]
    if cards:
        return "lesson card", cards
    return "none", []


def open_random_lesson(page: Page, name: str, pattern: str) -> bool:
    """Home -> Start button of a random <subject> card -> (more clicks if needed)."""
    dbg(f"\n=== Picking a random {name} lesson ===")
    page.goto(START_URL)
    settle(page)
    dbg(f"  url: {page.url}")
    if "login" in page.url.lower():
        print("  ! Century shows the login page - log in first, then run again.")
        return False

    tried: set[str] = set()
    for stage in range(1, 6):
        if stage > 1 and on_lesson_page(page):
            break
        kind, options = pick_options(page, stage, pattern, tried, name)
        if not options:
            dbg(f"  ! nothing to click at step {stage}")
            break
        text, el = random.choice(options)
        tried.add(text)
        dbg(f"  step {stage} [{kind}]: {text[:100]!r}")
        if not safe_click(el):
            break
        settle(page)

    if on_lesson_page(page):
        dbg("  lesson is open")
        return True
    print("  ! could not open a lesson automatically")
    dump_clickables(page)
    return False


def record_clicks(page: Page, seconds: int = 90) -> None:
    """Record mode: prints and saves details of everything YOU click, so selectors can be fixed."""
    log: list = []

    def on_click(payload):
        try:
            chain = json.loads(payload)
        except Exception:
            return
        log.append(chain)
        for level, c in enumerate(chain[:3]):
            print(f"  CLICK L{level}: <{c.get('tag')}> text={c.get('text')!r} "
                  f"href={c.get('href')!r} testid={c.get('testid')!r} role={c.get('role')!r}")
        print()

    try:
        page.expose_function("__centuryClick", on_click)
    except Exception:
        pass
    page.add_init_script(RECORDER_JS)
    page.evaluate(RECORDER_JS)

    print(f"\nRECORDING for {seconds} seconds. In the browser: click a subject, then a lesson,")
    print("then the Start/Continue button. Everything you click is printed here.\n")
    end = time.time() + seconds
    while time.time() < end and not page.is_closed():
        page.wait_for_timeout(300)

    with open("century_clicks.txt", "w", encoding="utf-8") as f:
        for chain in log:
            f.write(json.dumps(chain, ensure_ascii=False) + "\n")
    print("Recording finished. Saved to century_clicks.txt - paste the CLICK lines in the chat.")


def main():
    global TG
    if not API_KEY:
        sys.exit("API key is missing.")

    action = menu()
    if action == "quit":
        print("Bye!")
        return

    clear_screen()
    header()
    TG = Tg(TG_TOKEN if CFG["telegram"] else "")
    if TG.enabled:
        TG.start()
        if TG.chat_id:
            print(paint("\n  Telegram: connected to your phone.", "92"))
        else:
            print(paint(f"\n  Telegram: open your bot in Telegram and send   /start {TG.pair_code}", "93"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx_args = {"storage_state": STATE_FILE} if os.path.exists(STATE_FILE) else {}
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()
        page.goto(START_URL)

        print(paint("\n  A browser window opened. Log in to Century there if needed.", "93"))
        input(paint("  Press Enter here when you are logged in > ", "96"))
        try:
            ctx.storage_state(path=STATE_FILE)
        except Exception:
            pass

        if action == "record":
            record_clicks(page)
            input("Press Enter to close the browser...")
            try:
                browser.close()
            except Exception:
                pass
            TG.stop()
            return

        chosen = list(CFG["subjects"])
        total = CFG["lessons"]
        names = ", ".join(SUBJECTS[k][0] for k in chosen)
        print(paint(f"\n  Starting: {names}  |  {total} lesson(s)  |  "
                    f"speed {SPEEDS[CFG['speed']][0]}  |  human mode "
                    f"{'ON' if HUMAN else 'OFF'}", "92"))

        STATS.started = time.time()
        STATS.lesson_total = total
        TG.send(f"<b>Farming started</b>\n{html.escape(names)} | {total} lessons")

        try:
            for n in range(1, total + 1):
                if STATS.stop:
                    break
                name, pattern = SUBJECTS[random.choice(chosen)]
                STATS.lesson_no, STATS.lesson_q = n, 0
                STATS.lesson_started = time.time()
                STATS.current = f"{name} lesson {n}/{total} (opening)"
                if CFG["autoclear"] and n > 1:          # fresh console for every lesson
                    clear_screen()
                    header()
                    print(paint(status_line(), "90"))
                print(paint(f"\n--------  Lesson {n}/{total}  |  {name}  --------", "96"))
                if not open_random_lesson(page, name, pattern):
                    STATS.manual += 1
                    STATS.current = "waiting for you at the PC (lesson did not open)"
                    alert()
                    TG.send("<b>Needs you at the PC</b>: the bot could not open a lesson.")
                    print("\nAuto-open failed. Open ANY lesson yourself in the browser now,")
                    ans = input("then press Enter to let the bot continue (or type S to stop): ")
                    if ans.strip().lower() == "s":
                        break
                STATS.current = f"{name} lesson {n}/{total}"
                run(page)
                record_lesson(name, n, total)
                if page.is_closed():
                    break
        except KeyboardInterrupt:
            print("Stopped by user.")
        finally:
            STATS.current = "finished"
            try:
                ctx.storage_state(path=STATE_FILE)
            except Exception:
                pass
            print()
            frame("FINISHED", [f"Lessons completed: {STATS.lessons_done} / {total}",
                               f"Questions answered: {STATS.questions}"], code="32")
            TG.send("<b>Farming finished</b>\n\n" + stats_text())
            TG.stop()
            input("\nPress Enter to close the browser...")
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
