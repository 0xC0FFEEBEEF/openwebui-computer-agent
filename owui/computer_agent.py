"""
title: Computer Agent
author: 0xC0FFEEBEEF
description: Agent mode - the model drives a real desktop (Chrome on the AI sandbox VM), shown live in the chat. It plans, sets a goal, and keeps going until a verifier agrees the goal is met. Take control at any time.
version: 0.2.0
requirements: httpx
"""

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

SYSTEM_PROMPT = """You operate a computer to complete the user's task. The screen is {w}x{h} and shows Google Chrome.
Every turn you receive a screenshot in which clickable page elements are boxed and numbered, a faint pink
grid labelled in screen pixels, the page text, and a list of the numbered elements. On drawing canvases,
objects that are drawn on the canvas (shapes, lines, text) also get numbers, in CYAN boxes ("drawn" in the list).

Reply with ONE JSON object and nothing else:
{{"thought": "<one short sentence: what you see and what you do next>", "check": <plan item number you just FINISHED, or omit>, "action": "<name>", ...arguments}}

YOUR GOAL and YOUR PLAN are shown every turn: ✓ = finished, → = the item to work on now.
Put "check": N in a reply once plan item N is really finished (you can see the result on screen).
If the plan turns out wrong, add "plan": ["new step", ...] to replace the unfinished part.
Keep going until the goal is met - there is no step limit. Only use "done" when the finished result is on screen.

Actions:
  {{"action": "navigate", "url": "https://..."}}                  open a web address in the current tab
  {{"action": "click", "id": 12}}                                click numbered element 12
  {{"action": "click_xy", "x": 640, "y": 380}}                   click a screen position (read it off the grid)
  {{"action": "double_click", "id": 12}} or {{"action": "double_click", "x": 640, "y": 380}}
  {{"action": "drag", "id": 7, "to_x": 700, "to_y": 400}}        press on element 7 (or "x","y"), move, release at to_x,to_y (or "to_id")
  {{"action": "label", "id": 118, "text": "Design"}}               write the text of a drawn shape (double-click, replace text, Escape)
  {{"action": "connect", "id": 118, "to_id": 121}}                  draw a connector line from drawn shape 118 to drawn shape 121
  {{"action": "type", "id": 5, "text": "...", "enter": true}}      click field 5, replace its text, then Enter if "enter" is true
  {{"action": "type", "text": "..."}}                            type into whatever has focus (e.g. a shape you just double-clicked)
  {{"action": "key", "keys": "Escape"}}                          Enter, Escape, Tab, Delete, ctrl+z, ctrl+a, Page_Down (comma = one after another)
  {{"action": "scroll", "direction": "down"}}                     "down" or "up"; add "id" or "x","y" to scroll a particular area
  {{"action": "hover", "x": 640, "y": 380}}                       move the pointer (reveals connection points, tooltips, menus)
  {{"action": "back"}} | {{"action": "new_tab", "url": "..."}} | {{"action": "wait", "seconds": 2}}
  {{"action": "done", "answer": "..."}}                            the goal is met: tell the user what you did / found
  {{"action": "ask_user", "question": "..."}}                      you need information, a login, or permission

Rules:
- If the user named a website, go to that website (navigate to it) and take the answer from it. Do not use a
  search engine instead.
- Otherwise, to search the web, navigate to {search}
- One action per reply. Look at the new screenshot before deciding again.
- Work on the → item. Do not jump ahead or go back to ✓ items.
- Read facts from the page text; quote them exactly and do not invent facts.
- For a question about the web: as soon as the page text contains the answer, reply "done" with it.
- Never type passwords, payment or personal details, and never buy, post, send or sign up for anything unless the user told you to. Use ask_user instead.
- If an action did not change the screen, try something different (another element, another position, scroll).
- Pop-ups and cookie banners: accept or close them.

Diagram and canvas editors (Lucidchart, draw.io, whiteboards):
- Shapes already ON the canvas have CYAN numbers. Use those numbers; use the pink grid only for empty spots.
- Add a shape: drag it from the shape library (numbered tiles on the left) onto one of the "Empty spots"
  listed each turn: {{"action": "drag", "id": <library tile>, "to_x": <spot x>, "to_y": <spot y>}}.
  Never drop a shape on top of another one.
- Name a shape: {{"action": "label", "id": <cyan number>, "text": "..."}}.
- Join two shapes: {{"action": "connect", "id": <from cyan number>, "to_id": <to cyan number>}}.
  Look at the next screenshot: a line with an arrow should now join them.
- Numbers change after every action - always use the numbers on the LATEST screenshot.
- Finish one diagram (shapes, then labels, then connectors) before starting the next.
- Faster than drawing shape by shape: write the diagram as Mermaid code and insert it with the app's
  diagram-as-code / import feature (Lucidchart: the "</>" Diagram as code tab in the left rail, choose Mermaid,
  type the code, insert). Then LOOK at the inserted diagram: every label readable, every arrow there, nothing
  overlapping. Fix what is wrong (edit the code or the shapes) before starting the next diagram.
- Do not use the app's built-in AI features (Lucid AI, "Build a diagram", "Document a process", "Generate",
  "Diagram with AI", AI chat boxes). They are blocked. Writing Mermaid code yourself is fine.

Lucidchart specifically:
- The shape library opens by CLICKING the "Shapes" tab in the narrow icon rail on the far left. Never drag
  the rail tabs. Once it is open, drag shape tiles (e.g. the "Process" rectangle) from it onto the canvas.
- If a Lucid AI panel is showing on the left, click the "Shapes" tab to replace it with the shape library.
- The "Shapes" tab is a TOGGLE: if the shape library is already open, do NOT click it again (that closes it).
- Use the shapes already in the left panel: the "Standard" group has a Text tool ("T") and a plain rectangle
  ("Block") - that rectangle is all these diagrams need. Do NOT open "More shapes" or any library browser.
- Stay in this document. Never open other documents, new documents, or other websites.
- If the canvas zoom (bottom right, e.g. "13%") is below 75%, set it to 100% so shapes are readable.
- Put each diagram in its own area: a title text above it, its shapes in one row or column below."""

PLAN_PROMPT = """You will operate a web browser to do the user's task. Before starting, write:
- "goal": one sentence describing the finished result.
- "done_when": 2 to 6 short criteria that can be checked by looking at the screen when finished.
- "plan": the steps in order. Each step must be small enough to finish in about 5-15 actions
  (e.g. for a diagram: "Waterfall: add 6 phase shapes", "Waterfall: label the shapes", "Waterfall: connect
  the shapes"). Use as many steps as the task needs (up to 30). The last step is "Answer the user".
- If the user names a website (e.g. weather.gov), the first step opens THAT website, and the answer must come
  from it - never replace it with a search engine.
To search the web the browser can open {search}

Reply with ONE JSON object and nothing else:
{{"goal": "...", "done_when": ["...", "..."], "plan": ["step 1", "step 2", ...]}}"""

VERIFY_PROMPT = """You check whether a computer task is finished. You get the task, the goal, the criteria,
the agent's final message, and the current screen. Judge ONLY from what the screen and page text show.

Reply with ONE JSON object and nothing else:
{"complete": true or false, "missing": "<what is still missing or wrong; empty if complete>"}"""

# Grammar-constrained output: replies always parse. (It does not stop a reasoning model's thinking -
# llama.cpp applies the grammar after the think block - which is what the THINKING valve is for.)
# Shape-library tiles (buttons named after the shape) in diagram editors; a click on one is turned into a drag.
SHAPE_TILES = (r"block|rectangle|rounded rectangle|square|process|decision|diamond|terminator|circle|ellipse|oval|"
               r"triangle|isosceles triangle|right triangle|pentagon|hexagon|octagon|parallelogram|trapezoid|cylinder|"
               r"database|document|data|sticky note|note|cloud|star|cross|arrow|callout|text|text block")

ACTION_SCHEMA = {"type": "json_schema", "json_schema": {"name": "action", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["thought", "action"],
    "properties": {
        "thought": {"type": "string", "maxLength": 200},
        "check": {"type": "integer"},
        "plan": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "action": {"type": "string", "enum": ["navigate", "click", "click_xy", "double_click", "drag", "label", "connect", "type", "key",
                                              "scroll", "hover", "back", "new_tab", "wait", "done", "ask_user"]},
        "id": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"},
        "to_id": {"type": "integer"}, "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
        "url": {"type": "string"}, "text": {"type": "string"}, "enter": {"type": "boolean"},
        "keys": {"type": "string"}, "direction": {"type": "string", "enum": ["up", "down"]},
        "seconds": {"type": "number"}, "answer": {"type": "string"}, "question": {"type": "string"}}}}}
PLAN_SCHEMA = {"type": "json_schema", "json_schema": {"name": "plan", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["goal", "done_when", "plan"],
    "properties": {"goal": {"type": "string"},
                   "done_when": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
                   "plan": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 30}}}}}
VERIFY_SCHEMA = {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["complete", "missing"],
    "properties": {"complete": {"type": "boolean"}, "missing": {"type": "string"}}}}}

# Controls a pinned run (working inside one document) must not use.
PINNED_DENY = (r"^\s*new\b|request shapes|upgrade|^\s*share\b|delete|trash|invite|move to|make a copy|"
               r"duplicate document|publish|close document|documents\s*$|"
               r"more shapes|browse templates|use selected shapes")
# 2026-10-04 (user): importing is allowed ("let it import whatever, then inspect visually"), and export/download
# were blocked although the task itself asked for a PDF export.
# Dialogs a pinned run keeps getting lost in; closed with Escape as soon as they appear.
TRAP_DIALOGS = r"popular libraries|browse templates|select the shapes you want"   # "insert content" = import, now allowed

ACTIONS = {"navigate", "click", "click_xy", "double_click", "drag", "label", "connect", "type", "key", "scroll", "hover", "back",
           "new_tab", "wait", "done", "ask_user", "right_click", "open_browser"}


RESUME_RE = re.compile(r"^\s*(please\s+)?(continue|resume|keep going|go on|carry on|proceed|keep working|"
                       r"finish( it| up)?|go ahead|pick (it )?(back )?up|you can continue)\b[\s\w,.!']{0,40}$", re.I)

# With a file / knowledge attached, Open WebUI (RAG_SYSTEM_CONTEXT off, the default) PREPENDS its RAG template --
# "### Task: Respond to the user query using the provided context, ..." + <context>documents</context> -- to the
# user's own message. Taken verbatim, that template became the agent's goal and a 40-step run chased "respond
# with citations" instead of the user's request (2026-10-04). Some templates put the query in <user_query>.
STUCK_REFUSALS = 8   # consecutive refused repeats that end a run as "stuck"

RAG_WRAP_RE = re.compile(r"^\s*### Task:.*?<context>(.*)</context>(.*)$", re.S)
RAG_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def split_rag(text: str) -> tuple[str, str]:
    """(the user's own words, attached-document context) from a possibly RAG-wrapped user message."""
    m = RAG_WRAP_RE.match(text or "")
    if not m:
        return text, ""
    q = RAG_QUERY_RE.search(m.group(2)) or RAG_QUERY_RE.search(text)
    return (q.group(1) if q else m.group(2)).strip(), m.group(1).strip()


SITE_RE = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?:gov|com|org|net|edu|mil|io|us|co|ai|app|dev|info|"
                     r"uk|ca|au|de|fr|me|tv|news))\b(?:/[^\s,;)]*)?", re.I)


def named_sites(text: str) -> list[str]:
    """Domains the user named in the task, e.g. 'go to weather.gov' -> ['weather.gov']."""
    out = []
    for m in SITE_RE.finditer(text or ""):
        host = m.group(1).lower()
        host = host[4:] if host.startswith("www.") else host
        if host not in out:
            out.append(host)
    return out[:5]


def host_of(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/:?#]+)", url or "", re.I)
    host = (m.group(1).lower() if m else "")
    return host[4:] if host.startswith("www.") else host


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def first_json(text: str) -> Optional[dict]:
    """First balanced {...} in text that parses as a JSON object."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", strip_think(text), flags=re.M)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def parse_action(text: str) -> Optional[dict]:
    obj = first_json(text)
    return obj if obj and obj.get("action") in ACTIONS else None


def clean_list(items, limit: int, width: int = 100) -> list:
    return [" ".join(str(x).split())[:width] for x in (items or []) if str(x).strip()][:limit]


def describe(a: dict, elements: dict) -> str:
    kind = a.get("action")

    def where(prefix=""):
        i = a.get(prefix + "id")
        el = elements.get(i) if isinstance(i, int) else None
        if el and el.get("text"):
            return f'"{el["text"][:40]}"'
        if i is not None:
            return f"#{i}"
        if a.get(prefix + "x") is not None:
            return f"({a.get(prefix + 'x')}, {a.get(prefix + 'y')})"
        return ""

    if kind in ("click", "click_xy"):
        return f"Click {where()}".strip()
    if kind == "double_click":
        return f"Double-click {where()}".strip()
    if kind == "drag":
        return f"Drag {where()} → {where('to_')}"
    if kind == "connect":
        return f"Connect {where()} → {where('to_')}"
    if kind == "label":
        return f'Label {where()} "{str(a.get("text", ""))[:40]}"'.replace("  ", " ")
    if kind == "hover":
        return f"Hover {where()}".strip()
    if kind == "type":
        w = where()
        return f'Type "{str(a.get("text", ""))[:60]}"' + (f" into {w}" if w else "") + (" + Enter" if a.get("enter") else "")
    if kind in ("navigate", "new_tab"):
        return f"{'Open' if kind == 'navigate' else 'New tab'} {a.get('url', '')}"
    if kind == "key":
        return f"Press {a.get('keys', '')}"
    if kind == "scroll":
        return f"Scroll {a.get('direction', 'down')}"
    return kind.replace("_", " ").capitalize()


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


class Pipe:
    class Valves(BaseModel):
        SANDBOX_URL: str = Field("http://ai-sandbox:6080", description="sandbox-agent base URL as your BROWSER reaches it")
        SANDBOX_API_URL: str = Field("", description="Base URL Open WebUI uses (blank = same as SANDBOX_URL)")
        CONTROL_TOKEN: str = Field("", description="CONTROL_TOKEN from /etc/sandbox-agent/env on the sandbox")
        VIEW_TOKEN: str = Field("", description="VIEW_TOKEN from /etc/sandbox-agent/env on the sandbox")
        MODELS: str = Field("slerm-turing.gemma-3-4b-it-q4km",
                            description="Comma-separated Open WebUI model ids that drive the desktop (vision models)")
        MAX_STEPS: int = Field(0, description="Safety cap on actions; 0 = no limit, run until the goal is met or Stop")
        VERIFY: bool = Field(True, description="Check the goal's criteria against the screen before accepting 'done'")
        STALL_NUDGE: int = Field(25, description="Actions without finishing a plan item before the agent is told to re-plan smaller")
        SCREENSHOT_WIDTH: int = Field(1280, description="Screenshot width sent to the model (canvas work needs full size)")
        GRID: bool = Field(True, description="Draw a labelled pixel grid on screenshots (positions on canvases)")
        SEND_SCREENSHOT: bool = Field(True, description="Off for text-only models (they get the element list only)")
        PAGE_TEXT_CHARS: int = Field(1500, description="Visible page text sent per step (0 = none)")
        MAX_ELEMENTS: int = Field(120, description="Numbered elements listed in text per step")
        MATERIAL_CHARS: int = Field(4000, description="Attached/retrieved material (documents in the chat) passed to the agent")
        TEMPERATURE: float = Field(0.2)
        THINKING: bool = Field(False, description="Let reasoning models think before each step. Off = ~3x faster and no runaway "
                                                  "(bonsai: 17 s vs 51 s per reply, same answer)")
        STRUCTURED_OUTPUT: bool = Field(True, description="Constrain replies to a JSON schema (they always parse)")
        MAX_TOKENS: int = Field(3000, description="Per-reply budget; reasoning models think ~600 tokens first")
        SEARCH_URL: str = Field("https://duckduckgo.com/?q=<query>", description="How the agent is told to search")
        DENY_TARGETS: str = Field(
            r"lucid ai|build a diagram|document a process|describe the process|generate starter|generate a diagram|"
            r"diagram with ai|describe what you want|have more to add|duck\.ai|search assist|\bai\b",
            description="Regex (case-insensitive) of controls the agent may never click/type into (e.g. built-in AI)")
        HANDBACK_TIMEOUT: int = Field(1800, description="Seconds to wait while the user has control")
        COORDS: str = Field("pixels", description="How the model gives positions: 'pixels', or 'norm1000' for Qwen-VL-style "
                                                  "models that answer on a 0-1000 scale (converted here; no pixel grid)")
        MODEL_SETTINGS: str = Field('{"slerm-turing.ui-venus-2-9b": {"THINKING": false, "MAX_STEPS": 40, "COORDS": "norm1000"}, "slerm-turing.holo-3.1-9b": {"THINKING": false, "MAX_STEPS": 40, "COORDS": "norm1000"}, "slerm-turing.holo4-35b-a3b": {"THINKING": false, "MAX_STEPS": 40, "COORDS": "norm1000"}}',
                                    description="JSON {model id: {VALVE: value}} - per-model overrides of the valves above, "
                                                "e.g. thinking on and a bigger reply budget for one model only")

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": m.strip(), "name": f"Computer Agent · {m.strip().split('.')[-1]}"}
                for m in self.valves.MODELS.split(",") if m.strip()]

    def _settings_for(self, model_id: str) -> "Pipe.Valves":
        """The valves with this model's MODEL_SETTINGS applied (bad JSON or unknown keys are ignored)."""
        try:
            over = (json.loads(self.valves.MODEL_SETTINGS or "{}") or {}).get(model_id) or {}
        except (ValueError, AttributeError):
            over = {}
        known = {k: val for k, val in over.items() if k in self.Valves.model_fields and k != "MODEL_SETTINGS"}
        return self.valves.model_copy(update=known) if known else self.valves

    @staticmethod
    def _system(v, obs) -> str:
        """SYSTEM_PROMPT, stated on the model's own coordinate scale."""
        if v.COORDS != "norm1000":
            return SYSTEM_PROMPT.format(w=obs["screen"]["w"], h=obs["screen"]["h"], search=v.SEARCH_URL)
        return (SYSTEM_PROMPT.format(w=1000, h=1000, search=v.SEARCH_URL)
                + "\n\nPOSITIONS: every x,y you give or read (click_xy, drag to_x/to_y, hover, empty spots) is on a "
                  "0-1000 scale of the screenshot: x=0 left edge, x=1000 right edge, y=0 top, y=1000 bottom. "
                  "There is no pixel grid on the screenshot; use your own sense of position.")

    @staticmethod
    def _spots(v, obs) -> list:
        spots = obs.get("free_spots") or []
        if v.COORDS != "norm1000":
            return spots
        W, H = obs["screen"]["w"], obs["screen"]["h"]
        return [(round(x * 1000 / W), round(y * 1000 / H)) for x, y in spots]

    @staticmethod
    def _tile_click_to_drag(action: dict, elements: dict, obs: dict, history: list) -> dict:
        """A click on a diagram shape-library tile drops the shape in the middle of the view (Lucid), on top of the
        last one - every model tried it (2026-09-30: ~20 of Holo4's 40 steps). With empty spots known, turn it into
        what was meant: drag that tile onto the first empty spot."""
        if action.get("action") != "click" or not obs.get("free_spots") or not isinstance(action.get("id"), int):
            return action
        el = elements.get(action["id"])
        if not el or el.get("tag") != "button" or el.get("role") or not re.fullmatch(SHAPE_TILES, (el.get("text") or "").strip(), re.I):
            return action
        x, y = obs["free_spots"][0]
        history.append(f'(clicking the "{el["text"]}" tile would drop it on top of the last shape, so it was DRAGGED to '
                       f"the first empty spot instead; to add another, do the same again)")
        return {**action, "action": "drag", "to_x": int(x), "to_y": int(y)}

    @staticmethod
    def _to_pixels(v, action: dict, obs: dict) -> dict:
        """A norm1000 model's positions -> screen pixels (element numbers are left alone)."""
        if v.COORDS != "norm1000":
            return action
        W, H = obs["screen"]["w"], obs["screen"]["h"]
        out = dict(action)
        for kx, ky in (("x", "y"), ("to_x", "to_y")):
            if isinstance(out.get(kx), (int, float)) and isinstance(out.get(ky), (int, float)):
                out[kx] = max(0, min(W - 1, round(out[kx] * W / 1000)))
                out[ky] = max(0, min(H - 1, round(out[ky] * H / 1000)))
        return out

    async def pipe(self, body: dict, __user__: dict, __request__, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                   __task__: Optional[str] = None, __metadata__: Optional[dict] = None):
        model_id = body["model"].split(".", 1)[1] if "." in body["model"] else body["model"]
        # Title / tag / follow-up generation: answer them with the model directly, no desktop.
        if __task__:
            return await self._complete(__request__, __user__, {"model": model_id, "messages": body["messages"],
                                                                 "stream": False})
        v = self._settings_for(model_id)
        api = (v.SANDBOX_API_URL or v.SANDBOX_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {v.CONTROL_TOKEN}"}

        async def status(text: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        messages = body.get("messages", [])

        def said(m: dict) -> str:  # what the user actually typed, without Open WebUI's RAG wrapper
            return split_rag(self._text(m))[0] if m.get("role") == "user" else self._text(m)

        task = said(messages[-1]) if messages else ""
        chat_id = (__metadata__ or {}).get("chat_id") or ""
        resume_asked = bool(RESUME_RE.match(task or ""))
        if resume_asked:  # "continue": the real task is the last substantive thing the user asked for
            for m in reversed(messages[:-1]):
                if m.get("role") == "user" and not RESUME_RE.match(said(m)):
                    task = said(m)
                    break
        earlier = [f"{m['role']}: {said(m)[:600]}" for m in messages[:-1][-6:]
                   if m.get("role") in ("user", "assistant")]
        # Attached documents reach a pipe either as a system message (RAG_SYSTEM_CONTEXT on) or wrapped around
        # the user's message (the default); split_rag() recovers the latter.
        docs = [split_rag(self._text(m))[1] for m in messages if m.get("role") == "user"]
        material = "\n\n".join([self._text(m) for m in messages if m.get("role") == "system"]
                               + [d for d in docs if d])[:v.MATERIAL_CHARS]
        # Websites the user named ("go to weather.gov") are REQUIRED: "done" is refused until one of them
        # has actually been open on screen (a model answered from a Google search instead, 2026-09-29).
        required_sites = named_sites(task)
        visited: set[str] = set()

        def gen(**extra) -> dict:
            return {"model": model_id, "stream": False, "temperature": v.TEMPERATURE, "max_tokens": v.MAX_TOKENS,
                    **({} if v.THINKING else {"chat_template_kwargs": {"enable_thinking": False}}), **extra}

        # Each chat drives its own desktop session on the sandbox (its own display, Chrome and plan).
        session = chat_id or "default"
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5), params={"s": session}) as http:
            try:
                (await http.get(f"{api}/health")).raise_for_status()
            except httpx.HTTPError as e:
                return f"The sandbox desktop at {api} is not reachable ({e.__class__.__name__}). Is VM 129 (AI-Sandbox) running?"

            if __event_emitter__:
                await __event_emitter__({"type": "embeds", "data": {"embeds": [viewer_embed(v.SANDBOX_URL, v.VIEW_TOKEN, session)]}})

            async def caption(text: str, busy: bool = True):
                try:
                    await http.post(f"{api}/api/caption", headers=headers, json={"text": text, "busy": busy})
                except httpx.HTTPError:
                    pass

            async def set_todo(**b) -> list:
                try:
                    r = await http.post(f"{api}/api/todo", headers=headers, json=b)
                    return r.json().get("todo", [])
                except (httpx.HTTPError, ValueError):
                    return []

            # ---------------------------------------------------------------- plan + goal
            # "continue" in the chat picks up the unfinished goal, plan and progress kept on the sandbox.
            current = {}
            if resume_asked:
                try:
                    current = (await http.post(f"{api}/api/todo", headers=headers, json={})).json()
                except (httpx.HTTPError, ValueError):
                    current = {}
            g = current.get("goal") or {}
            history_seed: list[str] = []
            pin = ""
            if resume_asked and g.get("text") and not g.get("met") and current.get("todo") and g.get("chat", "") == chat_id:
                goal, criteria = g["text"], g.get("criteria") or [g["text"]]
                pin = g.get("pin") or ""
                task = g.get("task") or task
                todo = current["todo"]
                done_n = sum(t["status"] == "done" for t in todo)
                await status(f"Resuming: {done_n}/{len(todo)} plan items done")
                await caption("Resuming where I left off…")
                history_seed.append(f"(RESUMING an unfinished task: {done_n} of {len(todo)} plan items are done. "
                                    "Look at the screen first - the user may have changed things.)")
            else:
                await status("Planning…")
                await caption("Planning…")
                spec = {}
                try:
                    spec = first_json(await self._complete(__request__, __user__, gen(
                        **({"response_format": PLAN_SCHEMA} if v.STRUCTURED_OUTPUT else {}),
                        # Planning a big task with thinking on can use more than MAX_TOKENS before the JSON starts.
                        **({"max_tokens": max(v.MAX_TOKENS, 8192)} if v.THINKING else {}),
                        messages=[{"role": "system", "content": PLAN_PROMPT.format(search=v.SEARCH_URL)},
                                  {"role": "user", "content": self._task_block(task, earlier, material)}]))) or {}
                except Exception:
                    spec = {}
                goal = " ".join(str(spec.get("goal") or task).split())[:200]
                criteria = clean_list(spec.get("done_when"), 6, 160) or [goal]
                plan = clean_list(spec.get("plan"), 30) or [task[:100], "Answer the user"]
                todo = await set_todo(items=plan, goal={"text": goal, "met": False, "criteria": criteria, "task": task,
                                                        "chat": chat_id})

            history: list[str] = history_seed
            log: list[str] = []
            answer = None
            last_sig, repeats, recent = None, 0, []
            bad_replies = errors = rejections = since_check = 0
            step = 0
            try:
                while True:
                    step += 1
                    if v.MAX_STEPS and step > v.MAX_STEPS:
                        answer = f"I stopped at the safety cap of {v.MAX_STEPS} actions. The desktop is left as it is."
                        break
                    try:
                        obs = await self._observe(http, api, headers, status, history,
                                                  grid=v.GRID and v.COORDS != "norm1000")
                    except (httpx.HTTPError, ValueError) as e:
                        # The sandbox service can be briefly away (a restart, an upgrade): wait, don't quit.
                        if not await self._wait_sandbox(http, api, status, step):
                            answer = f"The sandbox desktop stopped answering for 15 minutes ({e.__class__.__name__}), so I stopped."
                            break
                        history.append("(the desktop was unreachable for a while - look again before acting)")
                        continue
                    if obs is None:
                        answer = "You kept control of the desktop for a long time, so I paused here. Send a message to continue."
                        break
                    elements = {e["id"]: e for e in obs["elements"]}
                    visited.add(host_of(obs.get("url") or ""))
                    if pin and pin.split("?")[0] not in (obs.get("url") or ""):
                        # Pinned runs never leave their document: steer back instead of asking the model.
                        await status(f"Step {step}: back to the document")
                        try:
                            await http.post(f"{api}/api/act", headers=headers, json={"action": "navigate", "url": pin, "settle": 6})
                        except httpx.HTTPError:
                            pass
                        history.append("(you had left the document - you were brought back. Stay in this document.)")
                        log.append(f"{step}. (left the document — brought back)")
                        continue
                    if pin and re.search(TRAP_DIALOGS, obs.get("text") or "", re.I):
                        try:
                            await http.post(f"{api}/api/act", headers=headers, json={"action": "key", "keys": "Escape", "settle": 1.5})
                        except httpx.HTTPError:
                            pass
                        history.append("(a shape-library browser dialog was open and has been closed. Do not open it again: "
                                       "drag the rectangle ('Block') straight from the 'Standard' group in the left panel.)")
                        log.append(f"{step}. (closed the library browser dialog)")
                        continue
                    await status(f"Step {step}: looking at the screen…")
                    await caption(f"Step {step}: thinking…")

                    if since_check and since_check % v.STALL_NUDGE == 0:
                        history.append(f"(NOTE: {since_check} actions since you last finished a plan item. The → item is "
                                       "too big or your approach is not working: replace the rest of the plan with smaller "
                                       "steps using \"plan\", and try a different approach.)")

                    action, raw = None, ""
                    try:
                        for attempt in range(3):
                            prompt = self._prompt(v, task, earlier, material, history, obs, repeats >= 2,
                                                  retry=raw if attempt else "", todo=todo, goal=goal, criteria=criteria,
                                                  required=required_sites, visited=visited)
                            raw = await self._complete(__request__, __user__, gen(
                                **({"response_format": ACTION_SCHEMA} if v.STRUCTURED_OUTPUT else {}),
                                messages=[{"role": "system", "content": self._system(v, obs)}, prompt]))
                            action = parse_action(raw)
                            if action:
                                break
                        errors = 0
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception as e:  # model/network hiccup: back off and carry on
                        errors += 1
                        log.append(f"{step}. (model error: {e.__class__.__name__})")
                        if errors >= 10:
                            answer = f"The model kept failing ({e.__class__.__name__}: {str(e)[:200]}), so I stopped."
                            break
                        await status(f"Step {step}: model error, retrying…")
                        await asyncio.sleep(min(60, 5 * errors))
                        continue
                    if not action:
                        bad_replies += 1
                        log.append(f"{step}. (unusable reply)")
                        history.append("(your last reply was not a usable JSON action - reply with exactly one JSON object)")
                        if bad_replies >= 20:
                            answer = f"The model gave 20 unusable replies in a row. Its last reply was:\n\n> {raw[:500]}"
                            break
                        continue
                    bad_replies = 0
                    action = self._to_pixels(v, action, obs)
                    action = self._tile_click_to_drag(action, elements, obs, history)

                    thought = str(action.get("thought", "")).strip()
                    label = describe(action, elements)
                    if isinstance(action.get("plan"), list) and action["plan"]:
                        kept = [t["text"] for t in todo if t["status"] == "done"]
                        todo = await set_todo(items=[{"text": t, "status": "done"} for t in kept] +
                                              clean_list(action["plan"], 30 - len(kept)))
                        history.append("(you revised the plan)")
                        since_check = 0
                    checks = action.get("check")
                    checks = [int(c) for c in (checks if isinstance(checks, list) else [checks]) if str(c).isdigit()]
                    if checks:
                        todo = await set_todo(check=checks)
                        since_check = 0

                    if action["action"] == "done" and required_sites and not any(
                            h == site or h.endswith("." + site) for h in visited for site in required_sites):
                        site = required_sites[0]
                        history.append(f"(NOT DONE: the user asked you to use {', '.join(required_sites)} and you have not "
                                       f"opened it. Next reply: {{\"action\": \"navigate\", \"url\": \"https://{site}\"}} "
                                       "and take the answer from that site.)")
                        log.append(f"{step}. Said done — refused: {site} was never opened")
                        continue
                    if action["action"] == "done":
                        claimed = str(action.get("answer") or thought or "Done.")
                        if v.VERIFY:
                            await status(f"Step {step}: checking the goal…")
                            await caption("Checking the goal against the screen…")
                            verdict = await self._verify(__request__, __user__, gen, http, api, headers,
                                                         task, goal, criteria, claimed)
                            if not verdict["complete"]:
                                rejections += 1
                                missing = verdict["missing"] or "the criteria are not visibly met"
                                log.append(f"{step}. Said done — goal check: not yet ({missing[:160]})")
                                if rejections < 3:
                                    history.append(f"(GOAL CHECK FAILED - not finished yet: {missing}. Keep working.)")
                                    continue
                                claimed += f"\n\n_The goal check could not confirm everything: {missing}_"
                        answer = claimed
                        todo = await set_todo(all_done=True, goal={"text": goal, "met": True})
                        log.append(f"{step}. Done")
                        break
                    rejections = 0
                    if action["action"] == "ask_user":
                        answer = str(action.get("question") or thought)
                        log.append(f"{step}. Asked you")
                        break

                    sig = json.dumps({k: action.get(k) for k in
                                      ("action", "id", "url", "text", "keys", "x", "y", "to_id", "to_x", "to_y", "direction")})
                    if (sig == last_sig and repeats >= 1) or recent[-8:].count(sig) >= 2:
                        # Models loop on one action; refuse the third identical one.
                        history.append(f"{label} -> REFUSED: you already did exactly this at least twice recently and "
                                       "it did not help. Do something different.")
                        log.append(f"{step}. {label} — refused (repeat)")
                        repeats += 1
                        since_check += 1
                        # Stuck, not slow: with no step cap (user, 2026-10-04: "it shouldn't stop a task till it's
                        # done") the only stop besides "done" and the Stop button is a model that keeps trying the
                        # same refused action (holo4 did 8 in a row and would have looped forever).
                        if repeats >= STUCK_REFUSALS:
                            answer = (f"I'm stuck: I tried the same action {repeats} times in a row and it was refused "
                                      "each time. Stopping here so you can step in; say continue to resume.")
                            break
                        continue
                    blocked = self._blocked(v, action, elements, pin)
                    if blocked:
                        history.append(f"{label} -> REFUSED: {blocked}")
                        log.append(f"{step}. {label} — refused ({blocked[:60]})")
                        since_check += 1
                        continue
                    if action["action"] == "type" and not str(action.get("text", "")).strip():
                        history.append(f'{label} -> REFUSED: "type" needs the text to type in "text"')
                        log.append(f"{step}. {label} — refused (no text)")
                        continue

                    await status(f"Step {step}: {label}")
                    await caption(f"{label}" + (f" — {thought}" if thought else ""))
                    try:
                        r = await http.post(f"{api}/api/act", headers=headers, json=action)
                    except httpx.HTTPError as e:
                        if not await self._wait_sandbox(http, api, status, step):
                            answer = f"The sandbox desktop stopped answering for 15 minutes ({e.__class__.__name__}), so I stopped."
                            break
                        history.append(f"{label} -> UNKNOWN: the desktop was briefly unreachable - check the screen")
                        continue
                    ok = r.status_code == 200
                    try:
                        err = "" if ok else str(r.json().get("error", r.text))[:200]
                    except ValueError:
                        err = f"HTTP {r.status_code}: {r.text[:150]}"
                    if r.status_code == 409:  # the user grabbed control mid-step
                        history.append(f"{label} -> not done, the user took control")
                        continue
                    hint = ""
                    el = elements.get(action.get("id")) if isinstance(action.get("id"), int) else None
                    if ok and action["action"] == "click" and el and (
                            el["tag"].startswith(("input", "textarea")) or el["role"] in ("combobox", "searchbox", "textbox")):
                        hint = (f' (that is a text field: next reply {{"action": "type", "id": {el["id"]}, '
                                f'"text": "...", "enter": true}} - do not click it again)')
                    history.append(f"{label}" + (" -> ok" + hint if ok else f" -> FAILED: {err}"))
                    log.append(f"{step}. {label}" + ("" if ok else f" — failed: {err}") +
                               (f"  \n   _{thought}_" if thought else ""))
                    repeats = repeats + 1 if sig == last_sig else 0
                    recent.append(sig)
                    last_sig = sig
                    since_check += 1
            except asyncio.CancelledError:
                await caption("Stopped.", busy=False)
                raise
            except Exception as e:  # keep the chat readable whatever broke
                answer = f"The agent hit an error: {e.__class__.__name__}: {e}"
            await caption("Finished — take control to look around." if answer else "", busy=False)
            await status(f"Finished after {step} steps", done=True)

        shown = log[-150:]
        steps = ("…\n" if len(log) > len(shown) else "") + ("\n".join(shown) or "(no actions)")
        checklist = "\n".join(f"- [{'x' if t['status'] == 'done' else ' '}] {t['text']}" for t in todo)
        goal_md = f"\n\n**Goal:** {goal}" if goal else ""
        plan_md = f"\n\n**Plan**\n{checklist}" if checklist else ""
        return f"{answer}{goal_md}{plan_md}\n\n<details>\n<summary>{step} steps</summary>\n\n{steps}\n</details>"

    @staticmethod
    def _blocked(v, action: dict, elements: dict, pin: str = "") -> str:
        """Hard rules the model cannot talk its way past."""
        kind = action.get("action")
        if pin:
            if kind in ("navigate", "new_tab", "back"):
                return "this task stays in the current document - do not open other pages or tabs."
            for key in ("id", "to_id"):
                el = elements.get(action.get(key)) if isinstance(action.get(key), int) else None
                if el and re.search(PINNED_DENY, el.get("text") or "", re.I):
                    return f'"{el["text"][:40]}" is off-limits in this document (no new documents, sharing, deleting or requests).'

        for key in ("id", "to_id"):
            el = elements.get(action.get(key)) if isinstance(action.get(key), int) else None
            if el and el.get("text") and v.DENY_TARGETS and re.search(v.DENY_TARGETS, el["text"], re.I):
                return f'"{el["text"][:40]}" is a built-in AI feature, which is not allowed. Build it yourself with shapes.'
        src = elements.get(action.get("id")) if isinstance(action.get("id"), int) else None
        # Tabs, menus, links and form fields are clicked, never dragged (agents dragged Lucid's rail tabs). Plain
        # buttons stay draggable: Lucid's shape tiles are role-less <button>s ("Block"), and dragging one onto an
        # empty spot IS how a shape gets placed (this guard refused 30 correct drags on 2026-09-30).
        if kind == "drag" and src and (src["tag"].startswith(("input", "select")) or
                                       src.get("role") in ("tab", "menuitem", "link")):
            return (f'"{src.get("text") or src["tag"]}" is a tab/menu - CLICK it to open it, do not drag it. '
                    "Drag shape tiles from the opened library instead.")
        # Only prompt-sized text: a Mermaid diagram for an ML project may well contain the word "AI".
        if kind == "type" and v.DENY_TARGETS and re.search(v.DENY_TARGETS, str(action.get("text", "")), re.I) and \
                action.get("id") is None and len(str(action.get("text", ""))) < 300:
            return "typing into an AI prompt is not allowed."
        return ""

    async def _wait_sandbox(self, http, api, status, step, limit: int = 900) -> bool:
        waited = 0
        while waited < limit:
            await status(f"Step {step}: the desktop is not answering, waiting… ({waited // 60} min)")
            await asyncio.sleep(15)
            waited += 15
            try:
                if (await http.get(f"{api}/health")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        return False

    async def _verify(self, request, user, gen, http, api, headers, task, goal, criteria, claimed) -> dict:
        """Independent look at the screen: are the goal's criteria visibly met?"""
        try:
            r = await http.post(f"{api}/api/observe", headers=headers,
                                json={"marks": False, "max_width": self.valves.SCREENSHOT_WIDTH})
            r.raise_for_status()
            obs = r.json()
            text = (f"TASK: {task}\n\nGOAL: {goal}\n\nDONE WHEN:\n" + "\n".join(f"- {c}" for c in criteria) +
                    f"\n\nTHE AGENT SAYS: {claimed[:800]}\n\nCurrent page: {obs.get('title')} — {obs.get('url')}\n\n"
                    f"Page text:\n{(obs.get('text') or '')[:self.valves.PAGE_TEXT_CHARS]}\n\nYour JSON verdict:")
            content = [{"type": "text", "text": text}]
            if self.valves.SEND_SCREENSHOT:
                content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + obs["screenshot"]}})
            out = first_json(await self._complete(request, user, gen(
                **({"response_format": VERIFY_SCHEMA} if self.valves.STRUCTURED_OUTPUT else {}),
                messages=[{"role": "system", "content": VERIFY_PROMPT}, {"role": "user", "content": content}]))) or {}
            return {"complete": bool(out.get("complete")), "missing": str(out.get("missing") or "")[:300]}
        except Exception:
            return {"complete": True, "missing": ""}  # never block completion on a verifier failure

    async def _observe(self, http, api, headers, status, history, grid=None):
        """Screenshot + elements; waits here while the user has control."""
        waited = 0.0
        while True:
            r = await http.post(f"{api}/api/observe", headers=headers,
                                json={"marks": True, "grid": self.valves.GRID if grid is None else grid,
                                      "max_width": self.valves.SCREENSHOT_WIDTH})
            r.raise_for_status()
            obs = r.json()
            if not obs.get("user_control"):
                if waited:
                    history.append("(the user had control for a while and may have changed the screen)")
                return obs
            if waited == 0:
                await status("You have control — press “Hand back to agent” to continue")
            if waited > self.valves.HANDBACK_TIMEOUT:
                return None
            await asyncio.sleep(2)
            waited += 2

    @staticmethod
    def _task_block(task, earlier, material) -> str:
        out = f"TASK: {task}"
        if earlier:
            out += "\n\nEarlier in this conversation:\n" + "\n".join(earlier)
        if material:
            out += "\n\nMaterial the user provided (attached documents):\n" + material
        return out

    def _prompt(self, v, task, earlier, material, history, obs, repeating, retry="", todo=None, goal="", criteria=None,
                required=(), visited=frozenset()):
        els = "\n".join(f'[{e["id"]}] {e["tag"]}{("/" + e["role"]) if e["role"] else ""} "{e["text"]}"'
                        for e in obs["elements"][:v.MAX_ELEMENTS]) or "(no numbered elements)"
        parts = [self._task_block(task, earlier, material)]
        if goal:
            parts.append(f"YOUR GOAL: {goal}\nDone when:\n" + "\n".join(f"- {c}" for c in criteria or []))
        if required:
            parts.append("THE USER NAMED THESE WEBSITES - use them, not a search engine: " + ", ".join(
                f"{site} ({'visited' if any(h == site or h.endswith('.' + site) for h in visited) else 'NOT visited yet'})"
                for site in required))
        if todo:
            mark = {"done": "✓", "active": "→", "pending": " "}
            parts.append("YOUR PLAN:\n" + "\n".join(f"{mark[t['status']]} {i}. {t['text']}"
                                                      for i, t in enumerate(todo, 1)))
        parts.append("Recent steps:\n" + ("\n".join(f"- {h}" for h in history[-15:]) or "(none yet)"))
        tabs = len(obs.get("tabs") or [])
        parts.append(f"Current page: {obs.get('title') or '(no title)'} — {obs.get('url') or 'no page'}"
                     + (f" ({tabs} tabs open)" if tabs > 1 else ""))
        if obs.get("text") and v.PAGE_TEXT_CHARS:
            parts.append("Text on the page (from the top of the visible area down):\n" + obs["text"][:v.PAGE_TEXT_CHARS])
        parts.append("Numbered elements on screen:\n" + els)
        if obs.get("free_spots"):
            parts.append("Empty spots on the canvas where a NEW shape fits (drop shapes here, first one first): "
                         + ", ".join(f"({x}, {y})" for x, y in self._spots(v, obs)))
        if repeating:
            parts.append("WARNING: you repeated the same action several times and nothing changed. Do something different.")
        if retry:
            parts.append(f"Your previous reply could not be used:\n{retry[:300]}\nReply with exactly one JSON object.")
        parts.append("Your JSON action:")
        content = [{"type": "text", "text": "\n\n".join(parts)}]
        if v.SEND_SCREENSHOT:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + obs["screenshot"]}})
        return {"role": "user", "content": content}

    async def _complete(self, request, user: dict, form: dict) -> str:
        from open_webui.models.users import Users
        from open_webui.utils.chat import generate_chat_completion

        res = await generate_chat_completion(request, form, Users.get_user_by_id(user["id"]), bypass_filter=True)
        if hasattr(res, "body"):  # a Response object: error or unexpected stream
            res = json.loads(res.body.decode() or "{}")
        try:
            msg = res["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"model returned no message: {str(res)[:300]}")
        # A thinking model can leave content empty and put the JSON in reasoning_content (the 2026-10-04 Qwen3.6
        # plan came back unusable and fell back to "task + Answer the user"); use it rather than lose the reply.
        return msg.get("content") or msg.get("reasoning_content") or ""

    @staticmethod
    def _text(m: dict) -> str:
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        return re.sub(r"<details.*?</details>", "", c, flags=re.S).strip()
