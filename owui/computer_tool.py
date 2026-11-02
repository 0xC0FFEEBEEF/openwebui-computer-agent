"""
title: Computer
author: 0xC0FFEEBEEF
description: Toggle on to let the model use a real desktop (Chrome on the AI sandbox VM), shown live in the chat. Works with any tool-calling model, vision or not. Can also draw in diagram editors (Lucidchart, draw.io). Needs Function Calling = Native on the model.
version: 0.2.0
requirements: httpx
"""

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

# Messages that already have the live viewer embedded (one embed per reply).
_embedded: set[str] = set()


VIEWER_WRAPPER = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{{margin:0;background:transparent;font:13px ui-sans-serif,system-ui,sans-serif;color:#c9d3e6}}
#ph{{position:relative;height:360px;border:1px solid #262626;border-radius:14px;overflow:hidden;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(120% 90% at 30% 20%,#1d2533 0%,#111419 55%,#0b0c0f 100%)}}
#ph .glow{{position:absolute;width:60%;aspect-ratio:1;border-radius:50%;filter:blur(60px);opacity:.35;
  background:conic-gradient(from 0deg,#4f8cff,#7a5cff,#2ec5b6,#4f8cff);animation:d 14s linear infinite}}
@keyframes d{{from{{transform:rotate(0) translate(10%) rotate(0)}}to{{transform:rotate(360deg) translate(10%) rotate(-360deg)}}}}
#ph .win{{position:relative;width:52%;aspect-ratio:16/10;border-radius:10px;background:#161a21;border:1px solid #2a3140;
  box-shadow:0 20px 60px rgba(0,0,0,.5);overflow:hidden;animation:w .9s cubic-bezier(.2,.8,.2,1) both}}
@keyframes w{{from{{transform:translateY(14px) scale(.96);opacity:0}}to{{transform:none;opacity:1}}}}
#ph .tb{{height:12%;background:#1d2230;border-bottom:1px solid #2a3140}}
#ph .ln{{height:7%;margin:6% 7% 0;border-radius:4px;background:linear-gradient(90deg,#232a38 0%,#33405a 50%,#232a38 100%);
  background-size:200% 100%;animation:s 1.6s ease-in-out infinite}}
#ph .ln:nth-child(3){{width:60%}} #ph .ln:nth-child(4){{width:75%}}
@keyframes s{{0%{{background-position:100% 0}}100%{{background-position:-100% 0}}}}
#ph .lab{{position:absolute;bottom:8%;left:0;right:0;text-align:center}}
#ph .lab small{{display:block;color:#7d889c;margin-top:4px}}
iframe{{display:none;width:100%;border:0}}
</style></head><body>
<div id="ph"><div class="glow"></div><div class="win"><div class="tb"></div><div class="ln"></div><div class="ln"></div><div class="ln"></div></div>
<div class="lab"><span id="t">Getting the desktop ready…</span><small id="u">The live view appears here in a moment.</small></div></div>
<iframe id="v" title="AI sandbox desktop"></iframe>
<script>
const VIEW = {view!r}, HEALTH = {health!r};
const v = document.getElementById('v'), ph = document.getElementById('ph');
const up = () => parent.postMessage({{type: 'iframe:height', height: document.documentElement.scrollHeight}}, '*');
new ResizeObserver(up).observe(document.body); up();
let tries = 0;
async function load() {{
  try {{ const r = await fetch(HEALTH, {{cache: 'no-store'}}); if (!r.ok) throw 0; v.src = VIEW; }}
  catch (e) {{ tries++; if (tries > 3) document.getElementById('u').textContent = 'Waiting for the sandbox to answer…';
               setTimeout(load, 3000); }}
}}
v.onload = () => {{ if (!v.src) return; ph.style.display = 'none'; v.style.display = 'block'; v.style.height = '640px'; up(); }};
addEventListener('message', (e) => {{
  if (e.source === v.contentWindow && e.data && e.data.type === 'iframe:height') {{ v.style.height = e.data.height + 'px'; up(); }}
}});
load();
</script></body></html>"""


def viewer_embed(base: str, view_token: str, session: str) -> str:
    """Embed a tiny wrapper instead of the viewer URL: it shows a placeholder at once and only loads the
    viewer once the sandbox answers, so a load that lands on a sandbox restart never leaves a blank frame."""
    base = base.rstrip("/")
    return VIEWER_WRAPPER.format(view=f"{base}/view?t={view_token}&s={session}", health=f"{base}/health?s={session}")


class Tools:
    class Valves(BaseModel):
        SANDBOX_URL: str = Field("http://ai-sandbox:6080", description="sandbox-agent base URL as your BROWSER reaches it")
        SANDBOX_API_URL: str = Field("", description="Base URL Open WebUI uses (blank = same as SANDBOX_URL)")
        CONTROL_TOKEN: str = Field("", description="CONTROL_TOKEN from /etc/sandbox-agent/env on the sandbox")
        VIEW_TOKEN: str = Field("", description="VIEW_TOKEN from /etc/sandbox-agent/env on the sandbox")
        PAGE_TEXT_CHARS: int = Field(1500, description="Page text returned per step (keeps small-context models alive)")
        MAX_ELEMENTS: int = Field(45, description="Numbered elements returned per step")
        HANDBACK_TIMEOUT: int = Field(900, description="Seconds to wait while the user has control")

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ tools

    async def computer_plan(self, steps: list[str], __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Call this FIRST for every new computer task: write your plan as a short todo list of 2-6
        concrete steps (a few words each; the last one is "Answer the user"). The user sees it live.
        Call it again to replace the plan if it turns out wrong. Then work through it in order and
        call computer_check each time a step is finished.
        :param steps: The steps, in order, e.g. ["Open github.com", "Search for open-webui", "Open the repository", "Answer the user"].
        """
        return await self._todo({"items": [str(x) for x in steps]}, __event_emitter__, __metadata__)

    async def computer_check(self, step: int, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                             __metadata__: Optional[dict] = None) -> str:
        """
        Tick off a finished step of your plan (numbered from 1). Returns the updated plan.
        :param step: The number of the plan step you just finished.
        """
        return await self._todo({"check": int(step)}, __event_emitter__, __metadata__)

    async def computer_look(self, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Look at the computer screen (a desktop with Google Chrome that the user watches live).
        Returns the page address, the page text, and the clickable elements, each with a number and
        its screen position. Use those numbers with computer_click and computer_type. Start a new task
        with computer_plan. On a drawing canvas it also lists the shapes already drawn (numbered
        "drawn") and empty spots where a new shape fits - see computer_drag, computer_label and
        computer_connect.
        Rules: never type passwords, payment or personal details, and never buy, post, send or
        sign up for anything unless the user asked you to. When the page text answers the
        user's question, stop using the computer and answer.
        """
        return await self._step(None, "Looking at the screen", __event_emitter__, __metadata__)

    async def computer_open(self, url: str, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Open a web address in the browser, e.g. "https://en.wikipedia.org/wiki/Linux".
        To search the web use "https://duckduckgo.com/?q=your+search+words".
        :param url: The web address to open.
        """
        return await self._step({"action": "navigate", "url": url}, f"Open {url}", __event_emitter__, __metadata__)

    async def computer_click(self, element: int, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                             __metadata__: Optional[dict] = None) -> str:
        """
        Click a numbered element from the latest screen (link, button, tab, checkbox...).
        To enter text into a field use computer_type instead.
        :param element: The element number shown on the latest screen.
        """
        return await self._step({"action": "click", "id": int(element)}, f"Click #{element}",
                                __event_emitter__, __metadata__)

    async def computer_type(self, text: str, element: Optional[int] = None, press_enter: bool = True,
                            __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Type text into a numbered input field (it is clicked and cleared first), e.g. a search box.
        Leave out element to type into whatever already has focus.
        :param text: The text to type.
        :param element: The number of the input field on the latest screen (optional).
        :param press_enter: Press Enter afterwards to submit (default true).
        """
        action = {"action": "type", "text": text, "enter": bool(press_enter)}
        if element is not None:
            action["id"] = int(element)
        return await self._step(action, f'Type "{text[:50]}"', __event_emitter__, __metadata__)

    async def computer_scroll(self, direction: str = "down", __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                              __metadata__: Optional[dict] = None) -> str:
        """
        Scroll the page to see more of it.
        :param direction: "down" or "up".
        """
        d = "up" if str(direction).lower().startswith("u") else "down"
        return await self._step({"action": "scroll", "direction": d}, f"Scroll {d}", __event_emitter__, __metadata__)

    async def computer_key(self, keys: str, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                           __metadata__: Optional[dict] = None) -> str:
        """
        Press keys, e.g. "Escape", "Return", "Tab", "Page_Down", "ctrl+f". Separate a sequence with commas.
        :param keys: The key or key combination to press.
        """
        return await self._step({"action": "key", "keys": keys}, f"Press {keys}", __event_emitter__, __metadata__)

    async def computer_back(self, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Go back to the previous page.
        """
        return await self._step({"action": "back"}, "Back", __event_emitter__, __metadata__)

    # ------------------------------------------------------ drawing / canvases

    async def computer_drag(self, to_x: int, to_y: int, element: Optional[int] = None, x: Optional[int] = None,
                            y: Optional[int] = None, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                            __metadata__: Optional[dict] = None) -> str:
        """
        Press, move and release the mouse: drag element (or position x,y) to position to_x,to_y.
        In a diagram editor this adds a shape: drag a tile from the shape library onto one of the
        "Empty spots" listed on the latest screen. Never drop a shape on top of another one.
        Lucidchart: open the library by CLICKING the "Shapes" tab in the icon rail on the far left first.
        :param to_x: Screen x to release at.
        :param to_y: Screen y to release at.
        :param element: The number of the element to drag (e.g. a shape library tile).
        :param x: Screen x to start from, if not dragging a numbered element.
        :param y: Screen y to start from, if not dragging a numbered element.
        """
        action = {"action": "drag", "to_x": int(to_x), "to_y": int(to_y)}
        if element is not None:
            action["id"] = int(element)
        elif x is not None and y is not None:
            action.update(x=int(x), y=int(y))
        else:
            return "FAILED: computer_drag needs element, or x and y, to start from."
        return await self._step(action, f"Drag to ({to_x}, {to_y})", __event_emitter__, __metadata__)

    async def computer_label(self, shape: int, text: str, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                             __metadata__: Optional[dict] = None) -> str:
        """
        Write the text inside a shape already drawn on the canvas (double-clicks it, replaces its
        text, then presses Escape).
        :param shape: The number of the drawn shape on the latest screen.
        :param text: The text to put in the shape.
        """
        return await self._step({"action": "label", "id": int(shape), "text": text},
                                f'Label #{shape} "{text[:40]}"', __event_emitter__, __metadata__)

    async def computer_connect(self, shape: int, to_shape: int,
                               __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                               __metadata__: Optional[dict] = None) -> str:
        """
        Draw a connector line (arrow) from one drawn shape to another. Check the next screen: the
        list of drawn objects should now include a line between them.
        :param shape: The number of the drawn shape the arrow starts at.
        :param to_shape: The number of the drawn shape the arrow points to.
        """
        return await self._step({"action": "connect", "id": int(shape), "to_id": int(to_shape)},
                                f"Connect #{shape} → #{to_shape}", __event_emitter__, __metadata__)

    async def computer_click_at(self, x: int, y: int, double: bool = False,
                                __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                                __metadata__: Optional[dict] = None) -> str:
        """
        Click a screen position instead of a numbered element (e.g. an empty spot on a canvas to
        deselect). Set double to true to double-click.
        :param x: Screen x.
        :param y: Screen y.
        :param double: Double-click instead of a single click.
        """
        return await self._step({"action": "click_xy", "x": int(x), "y": int(y), "double": bool(double)},
                                f"{'Double-click' if double else 'Click'} ({x}, {y})", __event_emitter__, __metadata__)

    async def computer_hover(self, x: int, y: int, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                             __metadata__: Optional[dict] = None) -> str:
        """
        Move the mouse pointer to a screen position without clicking (reveals menus, tooltips and a
        shape's connection points).
        :param x: Screen x.
        :param y: Screen y.
        """
        return await self._step({"action": "hover", "x": int(x), "y": int(y)}, f"Hover ({x}, {y})",
                                __event_emitter__, __metadata__)

    # --------------------------------------------------------------- internals

    async def _todo(self, body: dict, emit, metadata) -> str:
        v = self.valves
        api = (v.SANDBOX_API_URL or v.SANDBOX_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), params={"s": self._sid(metadata)}) as http:
            try:
                r = await http.post(f"{api}/api/todo", headers={"Authorization": f"Bearer {v.CONTROL_TOKEN}"}, json=body)
                r.raise_for_status()
                todo = r.json()["todo"]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                return f"ERROR: could not update the plan ({e.__class__.__name__})."
            await self._embed(emit, metadata)
        if emit:
            done = sum(t["status"] == "done" for t in todo)
            await emit({"type": "status", "data": {"description": f"📝 Plan {done}/{len(todo)}", "done": True}})
        nxt = next((i for i, t in enumerate(todo, 1) if t["status"] == "active"), None)
        return self._plan_text(todo) + (f"\nWork on step {nxt} now." if nxt else "\nAll steps done: answer the user.")

    async def _embed(self, emit, metadata):
        v = self.valves
        msg_id = (metadata or {}).get("message_id") or ""
        if emit and msg_id not in _embedded:
            _embedded.add(msg_id)
            if len(_embedded) > 500:
                _embedded.clear()
            await emit({"type": "embeds", "data": {"embeds": [
                viewer_embed(v.SANDBOX_URL, v.VIEW_TOKEN, self._sid(metadata))]}})

    @staticmethod
    def _sid(metadata) -> str:
        """Each chat drives its own desktop session on the sandbox."""
        return (metadata or {}).get("chat_id") or "default"

    @staticmethod
    def _plan_text(todo: list) -> str:
        if not todo:
            return "PLAN: (none yet - call computer_plan)"
        mark = {"done": "✓", "active": "→", "pending": " "}
        return "PLAN:\n" + "\n".join(f"{mark.get(t['status'], ' ')} {i}. {t['text']}" for i, t in enumerate(todo, 1))

    async def _step(self, action: Optional[dict], label: str, emit, metadata) -> str:
        v = self.valves
        api = (v.SANDBOX_API_URL or v.SANDBOX_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {v.CONTROL_TOKEN}"}

        async def status(text: str, done: bool = False):
            if emit:
                await emit({"type": "status", "data": {"description": text, "done": done}})

        msg_id = (metadata or {}).get("message_id") or ""
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5), params={"s": self._sid(metadata)}) as http:
            try:
                (await http.get(f"{api}/health")).raise_for_status()
            except httpx.HTTPError as e:
                return f"ERROR: the computer at {api} is not reachable ({e.__class__.__name__})."
            await self._embed(emit, metadata)

            note = ""
            if action:
                await status(f"🖥 {label}")
                await self._caption(http, api, headers, label)
                for _ in range(2):
                    r = await http.post(f"{api}/api/act", headers=headers, json=action)
                    if r.status_code == 409:  # user has control: wait, then retry once
                        if not await self._wait_handback(http, api, headers, status):
                            return "ERROR: the user kept control of the computer. Ask them what to do next."
                        note = "(The user had control for a while and may have changed the screen.)\n"
                        continue
                    break
                if r.status_code != 200:
                    try:
                        err = r.json().get("error", r.text)
                    except ValueError:
                        err = f"HTTP {r.status_code}"
                    await self._caption(http, api, headers, "", busy=False)
                    return f"FAILED: {err}. Call computer_look to see the current screen."
            else:
                await status("🖥 Looking at the screen")

            if not await self._wait_handback(http, api, headers, status):
                return "ERROR: the user kept control of the computer. Ask them what to do next."
            r = await http.post(f"{api}/api/observe", headers=headers, json={"marks": False, "max_width": 320})
            r.raise_for_status()
            obs = r.json()
            await self._caption(http, api, headers, "", busy=False)
            await status(f"🖥 {label}", done=True)
        return note + self._render(obs, action is not None)

    async def _wait_handback(self, http, api, headers, status) -> bool:
        waited = 0
        while True:
            r = await http.get(f"{api}/health")
            if not r.json().get("user_control"):
                return True
            if waited == 0:
                await status("🖥 You have control — press “Hand back to agent” to continue")
            if waited >= self.valves.HANDBACK_TIMEOUT:
                return False
            await asyncio.sleep(2)
            waited += 2

    @staticmethod
    async def _caption(http, api, headers, text: str, busy: bool = True):
        try:
            await http.post(f"{api}/api/caption", headers=headers, json={"text": text, "busy": busy})
        except httpx.HTTPError:
            pass

    @staticmethod
    def _at(e: dict) -> str:
        """Element centre, the point clicks and drags use."""
        return f'@({e["x"] + e["w"] // 2},{e["y"] + e["h"] // 2})'

    def _render(self, obs: dict, acted: bool) -> str:
        v = self.valves
        page = [e for e in obs["elements"] if e.get("tag") != "drawn"]
        drawn = [e for e in obs["elements"] if e.get("tag") == "drawn"]
        els = "\n".join(f'[{e["id"]}] {e["tag"]}{("/" + e["role"]) if e["role"] else ""} "{e["text"]}" {self._at(e)}'
                        for e in page[:v.MAX_ELEMENTS]) or "(no clickable elements)"
        more = len(page) - v.MAX_ELEMENTS
        text = (obs.get("text") or "")[:v.PAGE_TEXT_CHARS]
        scr = obs.get("screen") or {}
        out = (self._plan_text(obs.get("todo") or []) + "\n\n"
               + (("Done. " if acted else "") + "Screen now shows:\n")
               + f"Page: {obs.get('title') or '(no title)'} — {obs.get('url') or 'no page open'}"
               + (f" (screen {scr['w']}x{scr['h']})" if scr else "") + "\n\n"
               + f"Page text:\n{text or '(none)'}\n\n"
               + f"Clickable elements (number, kind, text, centre position):\n{els}"
               + (f"\n(+{more} more further down; scroll to reach them)" if more > 0 else ""))
        if drawn or obs.get("free_spots"):
            shapes = "\n".join(f'[{e["id"]}] drawn {e["role"]} {e["w"]}x{e["h"]} {self._at(e)}' for e in drawn[:60])
            out += ("\n\nDrawing canvas - objects already drawn on it (use these numbers with computer_label"
                    " and computer_connect; roles: shape, line, text):\n" + (shapes or "(nothing drawn yet)"))
            if obs.get("free_spots"):
                out += ("\nEmpty spots where a NEW shape fits (drop the next shape on the first one): "
                        + ", ".join(f"({x}, {y})" for x, y in obs["free_spots"]))
        return out
