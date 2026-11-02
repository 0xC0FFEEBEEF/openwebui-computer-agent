"""sandbox-agent: the control plane of the AI sandbox desktops.

Runs as the unprivileged `agent` user. Every Open WebUI chat gets its own SESSION: its own X desktop
(Xvnc), its own Chrome (own profile, so logins stay with that chat), its own plan/goal and viewer.
Display :1 is the systemd-managed "default" desktop (XFCE); further desktops (:2, :3, ...) are started
on demand with Openbox, parked after SESSION_IDLE seconds without agent activity, and re-adopted if this
service restarts (they are not in its kill scope).

A request names its session with ?s=<chat id> (no s = the default desktop). The default desktop also
belongs to whichever chat's goal it holds, so a chat that started there keeps it.

  /view?t=VIEW&s=           live viewer page (noVNC core) that Open WebUI embeds in the chat
  /websockify?t=VIEW&s=     websocket -> that session's Xvnc (localhost only)
  /api/events?t=VIEW&s=     server-sent events: pointer moves, clicks, typing, captions, plan
  /api/control?t=VIEW&s=    POST {"user": true|false} - user takes / hands back control
  /api/observe?s=           (Bearer CONTROL) screenshot with numbered elements + element list
  /api/act?s=               (Bearer CONTROL) one action (click, type, key, scroll, navigate, drag...)
  /api/caption?s=           (Bearer CONTROL) text shown in the viewer
  /api/todo?s=              (Bearer CONTROL) the plan + goal
  /api/sessions             (Bearer CONTROL) list sessions

Pointer motion is done with xdotool, so the desktop really moves; the viewer also draws an animated
pointer from /api/events because VNC clients only ever show the *local* cursor position.
"""

import asyncio
import base64
import io
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import numpy as np
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageGrab
from scipy import ndimage

HERE = Path(__file__).resolve().parent
CONTROL_TOKEN = os.environ["CONTROL_TOKEN"]
VIEW_TOKEN = os.environ["VIEW_TOKEN"]
NOVNC_DIR = os.environ.get("NOVNC_DIR", "/usr/share/novnc")
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "2"))          # extra desktops besides :1
SESSION_IDLE = int(os.environ.get("SESSION_IDLE", "1800"))       # freeze after this long without use (lossless)
GEOMETRY = os.environ.get("SESSION_GEOMETRY", "1280x800")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"
STATE_DIR = Path.home() / ".local" / "state" / "sandbox-agent"
ELEMENTS_JS = (HERE / "elements.js").read_text()
# Readable text from the visible part of the page onward: small vision models read facts far
# better from text than from a screenshot shrunk to their input size.
TEXT_JS = """(() => {
  const w = document.createTreeWalker(document.querySelector('main, [role=main], article') || document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const out = []; let n = 0;
  while (w.nextNode() && n < 6000) {
    const t = w.currentNode.textContent.replace(/\\s+/g, ' ').trim();
    const el = w.currentNode.parentElement;
    if (!t || !el || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.width === 0) continue;
    out.push(t); n += t.length + 1;
  }
  return out.join(' ').slice(0, 6000);
})()"""
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)

app = FastAPI(title="sandbox-agent", docs_url=None, redoc_url=None, openapi_url=None)
# The viewer runs in Open WebUI's sandboxed iframe (opaque origin "null"), so every
# request it makes, including the noVNC module imports, is cross-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/novnc", StaticFiles(directory=NOVNC_DIR, follow_symlink=True), name="novnc")


def safe_sid(sid: str | None) -> str:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:64]
    return sid or "default"


def process_cmdlines():
    """(pid, cmdline) of this user's processes - to find a session's leftovers after a restart."""
    me = os.getuid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != me:
                continue
            yield int(p.name), (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue


# ================================================================ sessions


class PageLog:
    """What one tab's page did since it last loaded: uncaught errors, console errors, the latest console
    messages, failed requests, and network activity (for "wait until the page has settled")."""
    KEEP = 40

    def __init__(self, url: str = ""):
        self.url, self.since = url, time.time()
        self.errors: list[dict] = []                     # uncaught exceptions / promise rejections
        self.console_errors: list[dict] = []             # console.error/assert + browser-reported errors
        self.recent: list[dict] = []                     # the latest console messages of any kind
        self.failed: list[dict] = []                     # HTTP >= 400, or the request never completed
        self.requests = 0
        self.by_type: dict[str, int] = {}                # requests per resource type (Image, Script, Fetch...)
        self.inflight: dict[str, tuple[str, str]] = {}   # request id -> (url, type)
        self.last_net = time.time()

    @staticmethod
    def _add(lst: list, item: dict) -> None:
        if len(lst) < PageLog.KEEP:
            lst.append(item)

    def event(self, method: str, p: dict) -> None:
        now = time.time()
        if method.startswith("Network."):
            self.last_net = now
        if method == "Network.requestWillBeSent":
            rtype = p.get("type") or "Other"
            if p.get("requestId") not in self.inflight:
                self.requests += 1
                self.by_type[rtype] = self.by_type.get(rtype, 0) + 1
            if len(self.inflight) < 2000:
                self.inflight[p.get("requestId")] = (p.get("request", {}).get("url", ""), rtype)
        elif method == "Network.responseReceived":
            r = p.get("response") or {}
            if int(r.get("status") or 0) >= 400:
                self._add(self.failed, {"url": r.get("url", "")[:300], "status": r.get("status"),
                                        "type": p.get("type") or "Other", "error": r.get("statusText", "")[:80]})
        elif method == "Network.loadingFinished":
            self.inflight.pop(p.get("requestId"), None)
        elif method == "Network.loadingFailed":
            url, rtype = self.inflight.pop(p.get("requestId"), ("", p.get("type") or "Other"))
            if not p.get("canceled"):                      # the page itself aborted it (a map dropping tiles)
                why = p.get("blockedReason") or (p.get("corsErrorStatus") or {}).get("corsError") or ""
                self._add(self.failed, {"url": url[:300], "status": None, "type": p.get("type") or rtype,
                                        "error": (p.get("errorText", "") + (" (%s)" % why if why else ""))[:120]})
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            text = (d.get("exception") or {}).get("description") or d.get("text") or "error"
            where = "%s:%s" % (d.get("url", ""), d.get("lineNumber", 0) + 1) if d.get("url") else ""
            self._add(self.errors, {"text": text[:500], "where": where[:200]})
        elif method == "Runtime.consoleAPICalled":
            kind = p.get("type", "log")
            text = " ".join(str(a.get("value") if "value" in a else a.get("description") or a.get("type", ""))
                            for a in p.get("args") or [])[:300]
            item = {"type": kind, "text": text}
            self.recent = (self.recent + [item])[-self.KEEP:]
            if kind in ("error", "assert"):
                self._add(self.console_errors, item)
        elif method == "Log.entryAdded":
            e = p.get("entry") or {}
            # network: already a failed request; violation/intervention/deprecation: performance advice, not bugs
            if e.get("level") == "error" and e.get("source") not in ("network", "violation", "intervention",
                                                                       "deprecation", "recommendation"):
                self._add(self.console_errors, {"type": "browser", "text": (e.get("text") or "")[:300],
                                                "where": (e.get("url") or "")[:200]})

    def settled(self, quiet: float = 1.5) -> bool:
        return not self.inflight and time.time() - self.last_net >= quiet

    def as_dict(self, full: bool) -> dict:
        out = {"url": self.url, "age_s": round(time.time() - self.since, 1), "requests": self.requests,
               "by_type": self.by_type, "pending": len(self.inflight),
               "counts": {"errors": len(self.errors), "console_errors": len(self.console_errors),
                          "failed": len(self.failed)}}
        if full:
            out.update(errors=self.errors, console_errors=self.console_errors, failed=self.failed, recent=self.recent)
        else:
            out["first"] = ([e["text"].splitlines()[0][:160] for e in self.errors[:2]] +
                            [c["text"][:160] for c in self.console_errors[:1]] +
                            ["%s %s" % (f.get("status") or f.get("error"), f["url"][:120]) for f in self.failed[:2]])
        return out


class Session:
    def __init__(self, sid: str, n: int):
        self.sid, self.n = sid, n
        self.display = f":{n}"
        self.vnc_port = 5900 + n
        self.cdp_port = 9221 + n                       # :1 -> 9222, as before sessions existed
        self.cdp = f"http://127.0.0.1:{self.cdp_port}"
        self.managed = n != 1                          # :1 is run by systemd (sandbox-desktop/-session)
        self.profile = Path.home() / ".config" / ("agent-chrome" if n == 1 else f"agent-chrome-{sid}")
        self.env = {**os.environ, "DISPLAY": self.display}
        self.state = {"user_control": False, "caption": "", "busy": False, "pointer": [640, 400],
                      "todo": [], "goal": None, "browser_ready": False}
        self.restore_tabs: list[str] = []              # set when thawing a frozen session
        self.listeners: set[asyncio.Queue] = set()
        self.last_elements: dict[int, dict] = {}
        self.lock = asyncio.Lock()
        self.last_used = time.time()
        self.last_act = 0.0                            # last AGENT action (not viewers, not the user)
        self.logs: dict[str, PageLog] = {}             # tab (target id) -> what its page did since it loaded
        self.watch_task: asyncio.Task | None = None
        self.watching = False
        self.watch_ready = asyncio.Event()             # set once the listener has the open tabs
        self.todo_file = STATE_DIR / ("todo.json" if n == 1 else f"todo-{sid}.json")
        try:
            saved = json.loads(self.todo_file.read_text())
            self.state["todo"], self.state["goal"] = saved.get("todo") or [], saved.get("goal")
        except (OSError, ValueError):
            pass

    # ---- plumbing
    def publish(self, event: dict) -> None:
        for q in list(self.listeners):
            if q.qsize() < 200:
                q.put_nowait(event)

    def save_todo(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.todo_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"todo": self.state["todo"], "goal": self.state["goal"]}))
            tmp.replace(self.todo_file)
        except OSError:
            pass

    def xdo(self, *args: str) -> str:
        return subprocess.run(["xdotool", *args], env=self.env, capture_output=True, text=True, timeout=30).stdout

    def x_alive(self) -> bool:
        return Path(f"/tmp/.X11-unix/X{self.n}").exists() and subprocess.run(
            ["xdpyinfo"], env=self.env, capture_output=True).returncode == 0

    # ---- desktop lifecycle (managed sessions only)
    async def start_desktop(self) -> None:
        if not self.managed or self.x_alive():
            return
        subprocess.Popen(["Xvnc", self.display, "-geometry", GEOMETRY, "-depth", "24", "-localhost=1",
                          "-rfbport", str(self.vnc_port), "-SecurityTypes", "None", "-AlwaysShared",
                          "-AcceptSetDesktopSize=0", "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            await asyncio.sleep(0.2)
            if self.x_alive():
                break
        subprocess.run(["xsetroot", "-solid", "#3a3f47"], env=self.env, capture_output=True)
        subprocess.run(["xset", "s", "off", "-dpms"], env=self.env, capture_output=True)
        subprocess.Popen(["openbox"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        await asyncio.sleep(0.5)
        self.xdo("mousemove", *map(str, self.state["pointer"]))

    async def freeze(self) -> None:
        """End this desktop but keep everything needed to resume it on disk: the open tabs, the plan/goal,
        and the Chrome profile (logins). Chrome is closed cleanly first so its own session restore works."""
        if not self.managed:
            return
        tabs = []
        try:
            tabs = [p["url"] for p in await self.cdp_targets()
                    if p.get("url") and not p["url"].startswith(("chrome://", "devtools://", "about:blank"))]
            async with httpx.AsyncClient(timeout=3) as c:
                ws_url = (await c.get(f"{self.cdp}/json/version")).json()["webSocketDebuggerUrl"]
            async with websockets.connect(ws_url, open_timeout=3) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            for _ in range(20):                          # let Chrome write its session and exit
                await asyncio.sleep(0.25)
                if not any(f"--user-data-dir={self.profile}" in cmd for _, cmd in process_cmdlines()):
                    break
        except (httpx.HTTPError, OSError, ValueError, KeyError, websockets.WebSocketException, asyncio.TimeoutError):
            pass
        self.save_todo()
        frozen[self.sid] = {"tabs": tabs[:20], "frozen_at": int(time.time())}
        self.park()
        self.publish({"type": "state", "frozen": True, "browser_ready": False, "user_control": False,
                      "caption": "Session frozen on disk.", "pointer": self.state["pointer"],
                      "todo": self.state["todo"], "goal": self.state["goal"]})

    def park(self) -> None:
        """Stop this desktop and its Chrome processes."""
        if not self.managed:
            return
        if self.watch_task:
            self.watch_task.cancel()
            self.watch_task = None
        marks = (f"--user-data-dir={self.profile}", f"Xvnc {self.display} ")
        for pid, cmd in process_cmdlines():
            if any(m in cmd + " " for m in marks):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        # openbox exits by itself when its X server goes away
        self.state["browser_ready"] = False

    # ---- browser
    async def cdp_targets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{self.cdp}/json/list")
            return [x for x in r.json() if x.get("type") == "page"]

    def set_ready(self, ready: bool) -> None:
        if self.state["browser_ready"] != ready:
            self.state["browser_ready"] = ready
            self.publish({"type": "ready", "browser_ready": ready})

    # ---- page diagnostics (2026-10-09): what the page text and a screenshot can't show
    async def start_watch(self) -> None:
        """Start the listener if it isn't running, and give it a moment to attach to the open tabs, so the
        first page a new desktop loads is recorded from its first request."""
        if self.watch_task is None or self.watch_task.done():
            self.watch_ready.clear()
            self.watch_task = asyncio.create_task(self.watch())
        if not self.watch_ready.is_set():
            try:
                await asyncio.wait_for(self.watch_ready.wait(), 3)
                await asyncio.sleep(0.2)               # let Chrome act on the enable commands
            except asyncio.TimeoutError:
                pass

    async def watch(self) -> None:
        """One long-lived DevTools connection to this desktop's Chrome. Per tab it records uncaught errors,
        console output and failed requests since the page last loaded (`desktop errors`, `desktop verify`), and on
        a chat's own desktop it keeps the HTTP cache off, so a reload always runs the newest code. Coding agents
        kept judging stale cached pages and invisible failures (blocked map tiles, a script that never ran)."""
        while True:
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    ws_url = (await c.get(f"{self.cdp}/json/version")).json()["webSocketDebuggerUrl"]
                async with websockets.connect(ws_url, max_size=2**24, open_timeout=5, ping_interval=None) as ws:
                    self.watching = True
                    self.watch_ready.clear()
                    await self._watch(ws)
            except asyncio.CancelledError:
                self.watching = False
                raise
            except Exception:  # noqa: BLE001 - Chrome restarting or gone: try again shortly
                pass
            self.watching = False
            await asyncio.sleep(2)

    async def _watch(self, ws) -> None:
        seq = 0
        tabs: dict[str, str] = {}                      # DevTools session id -> tab (target id)

        async def send(method: str, params: dict | None = None, session: str | None = None) -> None:
            nonlocal seq
            seq += 1
            msg = {"id": seq, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))

        asked: set[str] = set()

        async def attach(target_id: str) -> None:
            if target_id not in asked:
                asked.add(target_id)
                await send("Target.attachToTarget", {"targetId": target_id, "flatten": True})

        # Discovery reports every tab already open as targetCreated, then each new one.
        await send("Target.setDiscoverTargets", {"discover": True})
        async for raw in ws:
            m = json.loads(raw)
            method, p, sid = m.get("method"), m.get("params") or {}, m.get("sessionId")
            if method == "Target.targetCreated" and p.get("targetInfo", {}).get("type") == "page":
                await attach(p["targetInfo"]["targetId"])
            elif method == "Target.attachedToTarget" and p.get("targetInfo", {}).get("type") == "page":
                tid, s = p["targetInfo"]["targetId"], p["sessionId"]
                tabs[s] = tid
                self.logs.setdefault(tid, PageLog(p["targetInfo"].get("url", "")))
                for meth in ("Runtime.enable", "Network.enable", "Log.enable"):
                    await send(meth, session=s)
                if self.managed:
                    await send("Network.setCacheDisabled", {"cacheDisabled": True}, session=s)
                self.watch_ready.set()
            elif method == "Target.detachedFromTarget":
                asked.discard(tabs.pop(p.get("sessionId"), None))
            elif method == "Target.targetDestroyed":
                self.logs.pop(p.get("targetId"), None)
                asked.discard(p.get("targetId"))
            elif sid in tabs and method:
                tid = tabs[sid]
                log = self.logs.setdefault(tid, PageLog())
                # A page's own frame id is its tab's target id: a new document there is a new page.
                if method == "Network.requestWillBeSent" and p.get("type") == "Document" and p.get("frameId") == tid:
                    log = self.logs[tid] = PageLog(p.get("request", {}).get("url", ""))
                log.event(method, p)

    async def ensure_browser(self) -> None:
        await self.start_desktop()
        try:
            if await self.cdp_targets():
                self.set_ready(True)
                await self.start_watch()
                return
        except (httpx.HTTPError, OSError, ValueError):
            pass
        self.publish({"type": "phase", "text": "Opening the browser…"})
        self.profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [CHROME, "--no-first-run", "--no-default-browser-check", "--start-maximized",
             f"--remote-debugging-port={self.cdp_port}", "--remote-debugging-address=127.0.0.1",
             f"--remote-allow-origins=http://127.0.0.1:{self.cdp_port}", f"--user-data-dir={self.profile}",
             "--password-store=basic", "--disable-features=Translate,MediaRouter",
             "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
             # If Chrome ever dies (an upgrade restarted the X server on 2026-09-29 and took the user's
             # Lucid tabs with it), bring the tabs back instead of a blank page.
             "--restore-last-session"]
            # A brand-new profile would open Google's New Tab page, whose shadow-DOM search box traps weak
            # models; start blank so they use the address bar (navigate). Existing profiles restore tabs.
            + ([] if (self.profile / "Default").exists() else ["about:blank"]),
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            await asyncio.sleep(0.25)
            try:
                pages = await self.cdp_targets()
            except (httpx.HTTPError, OSError, ValueError):
                continue
            if pages:
                await asyncio.sleep(1.5)
                await self.reopen_tabs()
                self.set_ready(True)
                await self.start_watch()
                return

    async def reopen_tabs(self) -> None:
        """After a thaw: if Chrome's own session restore did not bring the tabs back, open them."""
        want, self.restore_tabs = self.restore_tabs, []
        if not want:
            return
        try:
            have = {p.get("url") for p in await self.cdp_targets()}
            async with httpx.AsyncClient(timeout=5) as c:
                for url in want:
                    if url not in have:
                        await c.put(f"{self.cdp}/json/new?{url}")
        except (httpx.HTTPError, OSError, ValueError):
            pass

    async def active_page(self) -> dict | None:
        """The tab Chrome is showing: /json/list is most-recently-activated first."""
        try:
            pages = [p for p in await self.cdp_targets() if not p.get("url", "").startswith("devtools://")]
        except (httpx.HTTPError, OSError, ValueError):
            return None
        return pages[0] if pages else None

    def grab(self) -> Image.Image:
        # mss 10's XCB backend rejects Xvnc's visual; Pillow's own X grabber is fine.
        return ImageGrab.grab(xdisplay=self.display).convert("RGB")

    # ---- pointer / keyboard
    async def move_to(self, x: int, y: int) -> None:
        x0, y0 = self.state["pointer"]
        dist = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
        ms = int(min(900, 250 + dist * 0.6))
        self.publish({"type": "move", "x": x, "y": y, "ms": ms})
        steps = max(6, ms // 30)
        for i in range(1, steps + 1):
            f = i / steps
            f = f * f * (3 - 2 * f)  # ease in-out, same curve as the viewer
            self.xdo("mousemove", str(round(x0 + (x - x0) * f)), str(round(y0 + (y - y0) * f)))
            await asyncio.sleep(ms / 1000 / steps)
        self.state["pointer"] = [x, y]

    async def click_at(self, x: int, y: int, button: str = "1", double: bool = False) -> None:
        await self.move_to(x, y)
        self.publish({"type": "click", "x": x, "y": y})
        self.xdo("click", "--repeat", "2" if double else "1", "--delay", "80", button)

    async def drag(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Press at (x0,y0), glide to (x1,y1), release - drops shapes, draws connectors, moves things.
        Moves in small steps while held so web apps see real drag events."""
        await self.move_to(x0, y0)
        self.publish({"type": "press", "x": x0, "y": y0})
        self.xdo("mousedown", "1")
        await asyncio.sleep(0.15)
        self.xdo("mousemove", str(x0 + 6), str(y0 + 6))  # past the apps' drag threshold
        await asyncio.sleep(0.1)
        self.state["pointer"] = [x0 + 6, y0 + 6]
        await self.move_to(x1, y1)
        await asyncio.sleep(0.2)
        self.xdo("mouseup", "1")
        self.publish({"type": "release", "x": x1, "y": y1})

    async def type_text(self, text: str) -> None:
        self.publish({"type": "typing", "text": text[:200]})
        for chunk in [text[i:i + 40] for i in range(0, len(text), 40)]:
            await asyncio.to_thread(self.xdo, "type", "--delay", "18", "--", chunk)

    def point(self, a: dict, prefix: str = "") -> tuple[int, int] | None:
        """Element centre for {prefix}id, else {prefix}x/{prefix}y."""
        i = a.get(prefix + "id")
        if str(i if i is not None else "").lstrip("-").isdigit() and int(i) in self.last_elements:
            return center(self.last_elements[int(i)])
        if a.get(prefix + "x") is not None and a.get(prefix + "y") is not None:
            return int(a[prefix + "x"]), int(a[prefix + "y"])
        return None


REGISTRY_FILE = STATE_DIR / "sessions.json"
sessions: dict[str, Session] = {"default": Session("default", 1)}
frozen: dict[str, dict] = {}                      # sid -> {"tabs": [...], "frozen_at": ts}: ended, resumable
pending: dict[str, "Session"] = {}                  # viewers waiting for a session that is not running yet
creating = asyncio.Lock()


def save_registry() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        reg = {s.sid: {"n": s.n} for s in sessions.values() if s.managed}
        reg.update({sid: {"frozen": True, **info} for sid, info in frozen.items() if sid not in reg})
        REGISTRY_FILE.write_text(json.dumps(reg))
    except OSError:
        pass


def adopt_sessions() -> None:
    """After a restart of this service: take back desktops it started earlier (they keep running) and
    remember the frozen ones."""
    try:
        reg = json.loads(REGISTRY_FILE.read_text())
    except (OSError, ValueError):
        return
    for sid, info in reg.items():
        if info.get("frozen"):
            frozen[sid] = {"tabs": info.get("tabs") or [], "frozen_at": info.get("frozen_at")}
            continue
        s = Session(sid, int(info["n"]))
        if s.x_alive():
            sessions[sid] = s
        else:                                      # the desktop died with the VM: resumable from disk
            frozen[sid] = {"tabs": [], "frozen_at": int(time.time())}


def owner_of_default(sid: str) -> bool:
    goal = sessions["default"].state.get("goal") or {}
    return bool(sid) and goal.get("chat") == sid


async def get_session(sid: str | None, create: bool = True) -> Session:
    sid = safe_sid(sid)
    if sid in sessions:
        return sessions[sid]
    if owner_of_default(sid):
        return sessions["default"]
    if not create:
        raise HTTPException(404, "no such session")
    async with creating:
        if sid in sessions:
            return sessions[sid]
        managed = [s for s in sessions.values() if s.managed]
        if len(managed) >= MAX_SESSIONS:
            # make room: park the least recently used desktop nobody is driving right now
            idle = sorted((s for s in managed if time.time() - s.last_used > 600 and not s.state["user_control"]),
                          key=lambda s: s.last_used)
            if not idle:
                raise HTTPException(503, f"all {MAX_SESSIONS} extra desktops are busy - try again later")
            victim = idle[0]
            await victim.freeze()
            sessions.pop(victim.sid, None)
        used = {s.n for s in sessions.values()}
        n = next(i for i in range(2, 100) if i not in used and not Path(f"/tmp/.X11-unix/X{i}").exists())
        s = Session(sid, n)
        waiting = pending.pop(sid, None)
        if sid in frozen:                           # thaw: same profile + plan, reopen its tabs
            s.restore_tabs = frozen.pop(sid).get("tabs") or []
        if waiting:
            s.listeners |= waiting.listeners
            s.publish({"type": "state", "frozen": False, **{k: s.state[k] for k in
                       ("user_control", "caption", "pointer", "todo", "goal", "browser_ready")}})
        sessions[sid] = s
        save_registry()
        return s


def check_view(t: str | None) -> None:
    if t != VIEW_TOKEN and t != CONTROL_TOKEN:
        raise HTTPException(403, "bad token")


def check_control(request: Request) -> None:
    if request.headers.get("authorization", "") != f"Bearer {CONTROL_TOKEN}":
        raise HTTPException(403, "bad token")


# ================================================================ viewer side


@app.get("/view", response_class=HTMLResponse)
async def view(request: Request, t: str | None = None, s: str | None = None):
    check_view(t)
    # Who loads the viewer (diagnosing viewers that stay blank in some browsers).
    print(f"view s={safe_sid(s)[:12]} from={request.client.host if request.client else '?'} "
          f"ua={request.headers.get('user-agent', '')[:120]!r} referer={request.headers.get('referer', '')[:60]!r}",
          flush=True)
    return (HERE / "viewer.html").read_text()


@app.get("/health")
async def health(s: str | None = None):
    sid = safe_sid(s)
    sess = sessions.get(sid) or (sessions["default"] if (sid == "default" or owner_of_default(sid)) else None)
    return {"ok": True, "session": sess.sid if sess else sid, "running": bool(sess),
            "frozen": (not sess) and sid in frozen,
            "browser_ready": bool(sess and sess.state["browser_ready"]),
            "user_control": bool(sess and sess.state["user_control"])}


@app.post("/api/thaw")
async def thaw(t: str | None = None, s: str | None = None):
    """Resume a frozen session (the viewer's Resume button): desktop, Chrome with its tabs, plan."""
    check_view(t)
    sess = await get_session(s)
    asyncio.create_task(sess.ensure_browser())
    return {"ok": True, "session": sess.sid}


@app.websocket("/websockify")
async def websockify(ws: WebSocket, t: str | None = None, s: str | None = None):
    if t not in (VIEW_TOKEN, CONTROL_TOKEN):
        await ws.close(code=4403)
        return
    sid = safe_sid(s)
    sess = sessions.get(sid) or (sessions["default"] if (sid == "default" or owner_of_default(sid)) else None)
    if sess is None or not sess.x_alive():         # not started yet / frozen: the viewer shows its placeholder
        try:
            await ws.close(code=1013)
        except (RuntimeError, WebSocketDisconnect):
            pass
        return
    await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", sess.vnc_port)
    except OSError:
        await ws.close(code=1011)
        return

    async def up():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if sess.state["user_control"]:
                sess.last_used = time.time()   # the user driving counts as use
            writer.write(data)
            await writer.drain()

    async def down():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, ConnectionError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        writer.close()
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.get("/api/events")
async def events(request: Request, t: str | None = None, s: str | None = None):
    check_view(t)
    sid = safe_sid(s)
    sess = sessions.get(sid) or (sessions["default"] if (sid == "default" or owner_of_default(sid)) else None)
    if sess is None:                                # not started yet or frozen: a lightweight placeholder object
        sess = pending.setdefault(sid, Session(sid, 0))
    q: asyncio.Queue = asyncio.Queue()
    sess.listeners.add(q)
    q.put_nowait({"type": "state", "frozen": sid in frozen,
                  **{k: sess.state[k] for k in ("user_control", "caption", "pointer", "todo", "goal", "browser_ready")}})

    async def stream():
        try:
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sess.listeners.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/control")
async def control(request: Request, t: str | None = None, s: str | None = None):
    check_view(t)
    sess = await get_session(s)
    body = await request.json()
    sess.state["user_control"] = bool(body.get("user"))
    sess.last_used = time.time()
    sess.publish({"type": "state", "user_control": sess.state["user_control"], "caption": sess.state["caption"],
                  "pointer": sess.state["pointer"], "todo": sess.state["todo"], "goal": sess.state["goal"]})
    return {"user_control": sess.state["user_control"]}


# ================================================================ drawing surface helpers


async def cdp_send(page: dict, method: str, params: dict | None = None):
    """One DevTools command on a tab; returns its result."""
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("id") == 1:
                return msg.get("result")


async def cdp_eval(page: dict, expression: str):
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression, "returnByValue": True}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")


PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]


def draw_grid(img: Image.Image, step: int = 100) -> Image.Image:
    """Faint coordinate grid labelled in SCREEN pixels, so a model can read positions on a canvas
    (diagram shapes have no numbered boxes). Labels survive the resize sent to the model."""
    img = img.convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(step, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 0, 180, 70), width=1)
        d.rectangle([x + 1, 1, x + 32, 15], fill=(255, 0, 180, 170))
        d.text((x + 3, 1), str(x), fill="white", font=FONT)
    for y in range(step, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 0, 180, 70), width=1)
        d.rectangle([1, y + 1, 32, y + 15], fill=(255, 0, 180, 170))
        d.text((3, y + 1), str(y), fill="white", font=FONT)
    return Image.alpha_composite(img, layer).convert("RGB")


def find_drawn(img: Image.Image, surface: dict | None) -> list[dict]:
    """Objects drawn on a canvas (Lucidchart, draw.io, whiteboards), found from pixels, since they are
    not page elements. Outlined shapes = enclosed background regions; filled shapes = solid blobs once
    thin strokes are opened away (so a connector does not glue two shapes together); what is left =
    lines / arrows / free text."""
    if not surface:
        return []
    x0, y0 = max(0, surface["x"]), max(0, surface["y"])
    x1, y1 = min(img.width, surface["x"] + surface["w"]), min(img.height, surface["y"] + surface["h"])
    if x1 - x0 < 80 or y1 - y0 < 80:
        return []
    crop = img.crop((x0, y0, x1, y1))
    rgb = np.asarray(crop.convert("RGB"), dtype=np.int16)
    a = np.asarray(crop.convert("L"), dtype=np.int16)
    H, W = a.shape
    bg = int(np.bincount(a.ravel(), minlength=256).argmax())
    ink = np.abs(a - bg) > 40                      # canvas grid lines (Lucid major lines are ~225 on 255) stay below this
    # Darker major grid lines (Lucid at 100% zoom) run across most of the canvas; left in, every grid
    # cell looked like an enclosed shape (38 bogus boxes, 2026-09-29). Shape edges are much shorter.
    # Erase only their long straight RUNS: zeroing the whole column also cut the top/bottom edges of any
    # shape the grid line crossed, opening its outline so it no longer counted as enclosed.
    for c in np.nonzero(ink.mean(axis=0) > 0.45)[0]:
        lab, _ = ndimage.label(ink[:, c])
        for sl in ndimage.find_objects(lab):
            if sl and sl[0].stop - sl[0].start >= 0.6 * ink.shape[0]:
                ink[sl[0], c] = False
    for r in np.nonzero(ink.mean(axis=1) > 0.45)[0]:
        lab, _ = ndimage.label(ink[r, :])
        for sl in ndimage.find_objects(lab):
            if sl and sl[0].stop - sl[0].start >= 0.6 * ink.shape[1]:
                ink[r, sl[0]] = False
    boxes = []                                     # [x, y, w, h, role]

    def add(sl, role, pad=0):
        ys, xs = sl
        bx, by = max(0, xs.start - pad), max(0, ys.start - pad)
        boxes.append([bx, by, min(W, xs.stop + pad) - bx, min(H, ys.stop + pad) - by, role])

    # 1. outlined shapes: background regions fully enclosed by ink
    lab, _ = ndimage.label(~ndimage.binary_dilation(ink, iterations=1))
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None or i in border:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if w >= 18 and h >= 14 and w * h < 0.5 * W * H:
            add(sl, "shape", pad=3)
    # 2. filled shapes: solid blobs left after opening away strokes up to ~4 px wide
    solid = ndimage.binary_opening(ink, structure=np.ones((5, 5)))
    lab, _ = ndimage.label(solid)
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if w >= 18 and h >= 14 and w * h < 0.5 * W * H:
            add(sl, "shape")
    # 3. the rest: lines, arrows, free text
    covered = np.zeros_like(ink)
    for bx, by, w, h, _ in boxes:
        covered[max(0, by - 4):by + h + 4, max(0, bx - 4):bx + w + 4] = True
    rest = ndimage.binary_dilation(ink & ~covered, iterations=3)
    lab, _ = ndimage.label(rest)
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if max(w, h) >= 30 and min(w, h) <= 24:
            add(sl, "line")
        elif w >= 16 and 8 <= h <= 40:
            add(sl, "text")

    def area(b):
        return b[2] * b[3]

    def inside(b, o):
        return o is not b and o[0] <= b[0] + 2 and o[1] <= b[1] + 2 and \
            o[0] + o[2] >= b[0] + b[2] - 2 and o[1] + o[3] >= b[1] + b[3] - 2 and area(o) >= area(b)

    def app_ui(b):
        """The editor's own selection handles / hover arrows / rotate knobs: small and saturated blue.
        Numbering them made a model 'label' a rotate handle and spin a shape upside down (2026-09-29)."""
        bx, by, w, h, _ = b
        if max(w, h) >= 48:
            return False
        region = rgb[by:by + h, bx:bx + w][ink[by:by + h, bx:bx + w]]
        if not len(region):
            return True
        r, g, bl = region.mean(axis=0)
        return bl - max(r, g) > 40

    def big_enough(b):
        w, h, role = b[2], b[3], b[4]
        return (w >= 30 and h >= 22) if role == "shape" else (w >= 14 if role == "text" else True)

    def at_edge(b):  # the canvas's own scrollbars
        bx, by, w, h, role = b
        return role == "line" and (bx + w >= W - 14 or by + h >= H - 14 or bx <= 2 or by <= 2)

    boxes = [b for b in boxes if big_enough(b) and not app_ui(b) and not at_edge(b)]
    keep = [b for b in boxes if not any(inside(b, o) and (o[4] == "shape" or b[4] != "shape") for o in boxes)]
    keep.sort(key=lambda b: (b[1] // 20, b[0]))
    return [{"tag": "drawn", "role": role, "text": "", "x": x0 + bx, "y": y0 + by, "w": w, "h": h}
            for bx, by, w, h, role in keep[:60]]


def free_spots(surface: dict | None, drawn: list[dict], n: int = 4) -> list[list[int]]:
    """Centres of empty canvas areas big enough for a new shape (~160x80 plus a margin), preferring
    the spot continuing the row of the right-most shape, so weak models stop stacking shapes."""
    if not surface:
        return []
    sw, sh, pad = 160, 80, 50
    sx0, sy0 = surface["x"] + 20, surface["y"] + 20
    sx1, sy1 = surface["x"] + surface["w"] - 20, surface["y"] + surface["h"] - 30
    shapes = [d for d in drawn if d["role"] in ("shape", "text")]

    def free(cx, cy):
        l, t, r, b = cx - sw // 2, cy - sh // 2, cx + sw // 2, cy + sh // 2
        if l < sx0 or t < sy0 or r > sx1 or b > sy1:
            return False
        return not any(l < d["x"] + d["w"] + pad and r > d["x"] - pad and t < d["y"] + d["h"] + pad and b > d["y"] - pad
                       for d in drawn)

    spots = []
    if shapes:
        last = max((d for d in shapes if d["role"] == "shape"), key=lambda d: d["x"] + d["w"], default=None)
        if last:
            cy = last["y"] + last["h"] // 2
            for cx in range(last["x"] + last["w"] + pad + sw // 2, sx1, 20):  # continue the row
                if free(cx, cy):
                    spots.append([cx, cy])
                    break
            cx = last["x"] + last["w"] // 2
            for cy in range(last["y"] + last["h"] + pad + sh // 2, sy1, 20):  # or start below it
                if free(cx, cy):
                    spots.append([cx, cy])
                    break
    for cy in range(sy0 + sh // 2 + 40, sy1, 40):                           # then reading order
        for cx in range(sx0 + sw // 2 + 40, sx1, 40):
            if len(spots) >= n:
                return spots
            if free(cx, cy) and all(abs(cx - x) > sw + pad or abs(cy - y) > sh + pad for x, y in spots):
                spots.append([cx, cy])
    return spots[:n]


def draw_marks(img: Image.Image, elements: list[dict]) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    for el in elements:
        color = "#00a3c4" if el.get("tag") == "drawn" else PALETTE[el["id"] % len(PALETTE)]
        x, y, w, h = el["x"], el["y"], el["w"], el["h"]
        d.rectangle([x, y, x + w, y + h], outline=color, width=2)
        label = str(el["id"])
        tw = d.textlength(label, font=FONT)
        lx, ly = max(0, x - 1), max(0, y - 16)
        d.rectangle([lx, ly, lx + tw + 6, ly + 16], fill=color)
        d.text((lx + 3, ly + 1), label, fill="white", font=FONT)
    return img


def png_b64(img: Image.Image, max_w: int | None = None) -> str:
    if max_w and img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# Models say "Enter", "Esc", "PageDown", "ArrowUp"; xdotool only knows X keysym names and silently
# ignores anything else (a model pressed "Enter" eight times in a row on 2026-09-28 and nothing happened).
KEY_ALIASES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
               "backspace": "BackSpace", "del": "Delete", "delete": "Delete", "space": "space",
               "pagedown": "Page_Down", "page_down": "Page_Down", "pgdn": "Page_Down",
               "pageup": "Page_Up", "page_up": "Page_Up", "pgup": "Page_Up", "home": "Home", "end": "End",
               "up": "Up", "down": "Down", "left": "Left", "right": "Right",
               "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
               "control": "ctrl", "ctrl": "ctrl", "cmd": "super", "meta": "super", "win": "super",
               "alt": "alt", "shift": "shift"}


def xdo_keys(combo: str) -> str:
    """'Ctrl+Enter' -> 'ctrl+Return'; single letters and F-keys pass through."""
    return "+".join(KEY_ALIASES.get(p.lower(), p) for p in combo.split("+") if p)


def center(el: dict) -> tuple[int, int]:
    if el.get("tag") == "drawn":  # canvas objects: the true centre (double-click edits the label there)
        return el["x"] + el["w"] // 2, el["y"] + el["h"] // 2
    return el["x"] + el["w"] // 2, el["y"] + min(el["h"] // 2, 20)


def edge_towards(a: dict, b: dict) -> tuple[int, int]:
    """Midpoint of the side of a that faces b - where diagram apps offer a connection point."""
    ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
    bx, by = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
    dx, dy = bx - ax, by - ay
    if abs(dx) * a["h"] >= abs(dy) * a["w"]:
        return round(a["x"] + (a["w"] - 1 if dx > 0 else 1)), round(ay)
    return round(ax), round(a["y"] + (a["h"] - 1 if dy > 0 else 1))


@app.get("/api/live")
async def live(t: str | None = None, window: int = 90):
    """Which desktop an agent is driving right now (for the lab wall). View token; never starts a session."""
    check_view(t)
    now = time.time()
    act = [x for x in sessions.values() if x.last_act and now - x.last_act < max(10, min(window, 600)) and x.x_alive()]
    if not act:
        return {"active": None}
    x = max(act, key=lambda x: x.last_act)
    todo = x.state["todo"] or []
    return {"active": {"session": x.sid, "act_age_s": round(now - x.last_act, 1), "caption": x.state["caption"],
                       "busy": x.state["busy"], "user_control": x.state["user_control"], "pointer": x.state["pointer"],
                       "screen": [int(v) for v in GEOMETRY.split("x")],
                       "goal": (x.state["goal"] or {}).get("text"),
                       "plan": {"done": sum(i.get("status") == "done" for i in todo), "total": len(todo),
                                "now": next((i.get("text") for i in todo if i.get("status") == "active"), None)}}}


@app.get("/frame.jpg")
async def frame(t: str | None = None, s: str | None = None, w: int = 800, q: int = 70):
    """The current screen of a RUNNING session as a JPEG (the lab wall polls this). View token."""
    check_view(t)
    sess = sessions.get(safe_sid(s)) if s else None
    if not sess or not sess.x_alive():
        raise HTTPException(404, "no such running desktop")

    def encode() -> bytes:
        img = sess.grab()
        width = max(160, min(w, img.width))
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=max(30, min(q, 90)))
        return buf.getvalue()

    return Response(await asyncio.to_thread(encode), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ================================================================ agent side


@app.get("/api/sessions")
async def list_sessions(request: Request):
    check_control(request)
    return [{"session": s.sid, "display": s.display, "managed": s.managed, "alive": s.x_alive(),
             "idle_s": int(time.time() - s.last_used), "user_control": s.state["user_control"],
             "goal": (s.state["goal"] or {}).get("text"), "chat": (s.state["goal"] or {}).get("chat")}
            for s in sessions.values()] + [{"session": sid, "frozen": True, **info} for sid, info in frozen.items()]


@app.post("/api/observe")
async def observe(request: Request, s: str | None = None):
    check_control(request)
    sess = await get_session(s)
    sess.last_used = time.time()
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    await sess.ensure_browser()
    page = await sess.active_page()
    elements, url, title, text, surface = [], "", "", "", None
    if page:
        url, title = page.get("url", ""), page.get("title", "")
        try:
            found = await cdp_eval(page, ELEMENTS_JS) or {}
            elements, surface = found.get("elements") or [], found.get("surface")
            text = await cdp_eval(page, TEXT_JS) or ""
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
            elements = []
    img = await asyncio.to_thread(sess.grab)
    drawn, spots = [], []
    if surface:
        drawn = await asyncio.to_thread(find_drawn, img, surface)
        spots = free_spots(surface, drawn)
        elements = elements + [{"id": len(elements) + i, **d} for i, d in enumerate(drawn, 1)]
    sess.last_elements.clear()
    sess.last_elements.update({e["id"]: e for e in elements})
    marked = draw_marks(img, elements) if body.get("marks", True) else img
    if body.get("grid"):
        marked = draw_grid(marked)
    tabs = []
    try:
        tabs = [{"title": p.get("title", ""), "url": p.get("url", "")} for p in await sess.cdp_targets()]
    except (httpx.HTTPError, OSError, ValueError):
        pass
    log = sess.logs.get(page["id"]) if page else None
    return {
        "session": sess.sid,
        "url": url, "title": title, "tabs": tabs,
        "watching": sess.watching,
        "diag": log.as_dict(False) if log else None,
        "screen": {"w": img.width, "h": img.height},
        "elements": [{k: e[k] for k in ("id", "tag", "role", "text", "x", "y", "w", "h")} for e in elements],
        "text": text,
        "free_spots": spots,
        "screenshot": png_b64(marked, body.get("max_width")),
        "user_control": sess.state["user_control"],
        "todo": sess.state["todo"],
    }


@app.post("/api/diag")
async def diag(request: Request, s: str | None = None):
    """The active tab's errors, console and failed requests since its page loaded. {"settle": N} first waits up
    to N seconds (max 30) for the page to stop loading; {"full": true} returns every entry, not just counts."""
    check_control(request)
    sess = await get_session(s)
    sess.last_used = time.time()
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    await sess.ensure_browser()
    page = await sess.active_page()
    if not page:
        return {"watching": sess.watching, "url": "", "title": "", "log": None}
    deadline = time.time() + max(0.0, min(float(body.get("settle") or 0), 30.0))
    while time.time() < deadline:
        log = sess.logs.get(page["id"])
        if log and log.settled():
            break
        await asyncio.sleep(0.3)
    log = sess.logs.get(page["id"])
    return {"watching": sess.watching, "url": page.get("url", ""), "title": page.get("title", ""),
            "settled": bool(log and log.settled()), "log": log.as_dict(bool(body.get("full"))) if log else None}


@app.post("/api/act")
async def act(request: Request, s: str | None = None):
    check_control(request)
    sess = await get_session(s)
    sess.last_used = sess.last_act = time.time()
    a = await request.json()
    if sess.state["user_control"]:
        return JSONResponse({"ok": False, "error": "the user has control of the desktop"}, 409)
    kind = a.get("action")
    xdo, publish, els = sess.xdo, sess.publish, sess.last_elements
    async with sess.lock:
        el = els.get(int(a["id"])) if str(a.get("id", "")).lstrip("-").isdigit() else None
        if el is None and kind in ("click", "hover") and not ("x" in a and "y" in a):
            return JSONResponse({"ok": False, "error": f"no element {a.get('id')!r} on the last screenshot"
                                 if a.get("id") is not None else "click needs an element number: {\"action\": \"click\", \"id\": N}"}, 400)
        if el is None and a.get("id") is not None and kind in ("type", "right_click"):
            return JSONResponse({"ok": False, "error": f"no element {a.get('id')!r} on the last screenshot"}, 400)
        if kind == "click":
            x, y = center(el) if el else (int(a["x"]), int(a["y"]))
            await sess.click_at(x, y, double=bool(a.get("double")))
        elif kind == "click_xy":
            await sess.click_at(int(a["x"]), int(a["y"]), double=bool(a.get("double")))
        elif kind == "double_click":
            p = sess.point(a)
            if not p:
                return JSONResponse({"ok": False, "error": "double_click needs an element number or x,y"}, 400)
            await sess.click_at(*p, double=True)
        elif kind == "label":
            if not el:
                return JSONResponse({"ok": False, "error": "label needs the element number of a shape"}, 400)
            await sess.click_at(*center(el), double=True)
            await asyncio.sleep(0.4)
            xdo("key", "ctrl+a")
            await sess.type_text(str(a.get("text", "")))
            await asyncio.sleep(0.2)
            publish({"type": "key", "keys": "Escape"})
            xdo("key", "Escape")
        elif kind == "connect":
            dst = els.get(int(a["to_id"])) if str(a.get("to_id", "")).isdigit() else None
            if not el or not dst:
                return JSONResponse({"ok": False, "error": "connect needs two element numbers: id and to_id"}, 400)
            await sess.move_to(*center(el))           # hovering shows the shape's connection points
            await asyncio.sleep(0.5)
            sx, sy = edge_towards(el, dst)
            await sess.move_to(sx, sy)
            await asyncio.sleep(0.4)
            await sess.drag(sx, sy, *center(dst))
        elif kind == "drag":
            src, dst = sess.point(a), sess.point(a, "to_")
            if not src or not dst:
                return JSONResponse({"ok": False, "error": "drag needs a start (id or x,y) and an end (to_id or to_x,to_y)"}, 400)
            await sess.drag(*src, *dst)
        elif kind == "right_click":
            if not el and not ("x" in a and "y" in a):
                return JSONResponse({"ok": False, "error": "right_click needs an element number"}, 400)
            x, y = center(el) if el else (int(a["x"]), int(a["y"]))
            await sess.click_at(x, y, button="3")
        elif kind == "hover":
            await sess.move_to(*(center(el) if el else (int(a["x"]), int(a["y"]))))
        elif kind == "type":
            if el:
                await sess.click_at(*center(el))
                if a.get("clear", True):
                    xdo("key", "ctrl+a", "BackSpace")
            await sess.type_text(str(a.get("text", "")))
            if a.get("enter"):
                publish({"type": "key", "keys": "Enter"})
                xdo("key", "Return")
        elif kind == "key":
            keys = [xdo_keys(k) for k in str(a.get("keys", "")).replace(" ", "").split(",") if k]
            publish({"type": "key", "keys": ", ".join(keys)})
            xdo("key", "--delay", "60", *keys)
        elif kind == "scroll":
            direction = a.get("direction", "down")
            amount = max(1, min(int(a.get("amount", 5)), 20))
            if el:
                await sess.move_to(*center(el))
            elif sess.state["pointer"][1] < 120:
                await sess.move_to(640, 450)
            button = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(direction, "5")
            publish({"type": "scroll", "direction": direction})
            xdo("click", "--repeat", str(amount), "--delay", "40", button)
        elif kind in ("navigate", "new_tab"):
            await sess.ensure_browser()
            url = str(a.get("url", "")).strip()
            if url and "://" not in url and not url.startswith("about:"):
                url = "https://" + url
            wid = xdo("search", "--onlyvisible", "--class", "chrome").split()
            if wid:
                xdo("windowactivate", "--sync", wid[-1])
            xdo("key", "ctrl+t" if kind == "new_tab" else "ctrl+l")
            await asyncio.sleep(0.3)
            await sess.type_text(url)
            xdo("key", "Return")
        elif kind == "back":
            publish({"type": "key", "keys": "Alt+Left"})
            xdo("key", "alt+Left")
        elif kind == "reload":                       # a fresh load: no cached scripts, styles or data
            page = await sess.active_page()
            if not page:
                return JSONResponse({"ok": False, "error": "no page is open to reload"}, 400)
            publish({"type": "key", "keys": "Reload"})
            await cdp_send(page, "Page.reload", {"ignoreCache": True})
        elif kind == "wait":
            await asyncio.sleep(min(float(a.get("seconds", 2)), 10))
        elif kind == "open_browser":
            await sess.ensure_browser()
        else:
            return JSONResponse({"ok": False, "error": f"unknown action {kind!r}"}, 400)
        await asyncio.sleep(float(a.get("settle", 1.2)))
    return {"ok": True}


@app.post("/api/caption")
async def caption(request: Request, s: str | None = None):
    check_control(request)
    sess = await get_session(s)
    body = await request.json()
    sess.state["caption"] = str(body.get("text", ""))[:300]
    sess.state["busy"] = bool(body.get("busy", True))
    sess.publish({"type": "caption", "text": sess.state["caption"], "busy": sess.state["busy"]})
    if body.get("phase"):
        sess.publish({"type": "phase", "text": str(body["phase"])[:120]})
    return {"ok": True}


@app.post("/api/todo")
async def todo(request: Request, s: str | None = None):
    """The agent's plan. {"items": [str|{text,status}]} replaces it, {"check": n} ticks item n (1-based),
    {"clear": true} empties it. Broadcast to viewers, who animate the change."""
    check_control(request)
    sess = await get_session(s)
    st = sess.state
    body = await request.json()
    if body.get("clear"):
        st["todo"], st["goal"] = [], None
    if "goal" in body:
        g = body["goal"]
        prev = st["goal"] or {}
        st["goal"] = {"text": " ".join(str(g.get("text", prev.get("text", ""))).split())[:200],
                      "met": bool(g.get("met")),
                      "criteria": [" ".join(str(c).split())[:160] for c in (g.get("criteria") or prev.get("criteria") or [])][:6],
                      "task": str(g.get("task", prev.get("task", "")))[:4000],
                      "chat": str(g.get("chat", prev.get("chat", "")))[:80],
                      "pin": str(g.get("pin", prev.get("pin", "")))[:400]} if g else None
    if "items" in body:
        items = []
        for it in list(body["items"])[:40]:
            text, status = (it.get("text", ""), it.get("status", "pending")) if isinstance(it, dict) else (it, "pending")
            text = " ".join(str(text).split())[:120]
            if text:
                items.append({"text": text, "status": status if status in ("pending", "active", "done") else "pending"})
        st["todo"] = items
    if "check" in body:
        for n in body["check"] if isinstance(body["check"], list) else [body["check"]]:
            if str(n).isdigit() and 1 <= int(n) <= len(st["todo"]):
                st["todo"][int(n) - 1]["status"] = "done"
    if body.get("all_done"):
        for item in st["todo"]:
            item["status"] = "done"
    found = False                                  # exactly one item is active: the first one not done
    for item in st["todo"]:
        if item["status"] != "done":
            item["status"] = "pending" if found else "active"
            found = True
    sess.save_todo()
    sess.publish({"type": "todo", "items": st["todo"], "goal": st["goal"]})
    return {"todo": st["todo"], "goal": st["goal"]}


@app.post("/api/park")
async def park(request: Request, s: str | None = None):
    """Shut a chat's extra desktop down now (its plan and Chrome profile stay on disk)."""
    check_control(request)
    sid = safe_sid(s)
    sess = sessions.get(sid)
    if not sess or not sess.managed:
        return JSONResponse({"ok": False, "error": "no running extra desktop for that session"}, 404)
    await sess.freeze()
    sessions.pop(sid, None)
    save_registry()
    return {"ok": True, "frozen": frozen.get(sid)}


async def reaper():
    """Park extra desktops nobody has used for SESSION_IDLE seconds (their plan stays on disk)."""
    while True:
        await asyncio.sleep(300)
        for s in list(sessions.values()):
            if s.managed and not s.state["user_control"] and time.time() - s.last_used > SESSION_IDLE:
                await s.freeze()
                sessions.pop(s.sid, None)
                save_registry()


@app.on_event("startup")
async def startup():
    adopt_sessions()
    d = sessions["default"]
    d.xdo("mousemove", *map(str, d.state["pointer"]))
    asyncio.create_task(d.ensure_browser())
    asyncio.create_task(reaper())
