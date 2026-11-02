"""Continue an Open WebUI chat with the Computer Agent pipe from outside the browser.

Appends a user message ("continue" by default) and an assistant reply to the chat, then runs the pipe
with the chat's history and attached documents. Progress (status lines, the live desktop embed) and the
final answer are written into that reply, so the chat shows the run when opened or reloaded.

Run with Open WebUI's venv and DATA_DIR, detached:
  DATA_DIR=~/.open-webui nohup setsid ~/.venvs/openwebui/bin/python resume_chat.py CHAT_ID [MESSAGE] &
Env: PIPE_MODEL (default the bonsai pipe model), VALVE_OVERRIDES (JSON merged over the pipe's valves).
Model calls go straight to the slerm gateway (the pipe's _complete normally uses Open WebUI's routing,
which needs a live request).
"""

import asyncio
import json
import os
import sys
import time
import traceback
import types
import uuid

import httpx

from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.models.functions import Functions

CHAT_ID = sys.argv[1]
USER_TEXT = sys.argv[2] if len(sys.argv) > 2 else "continue"
PIPE_MODEL = os.environ.get("PIPE_MODEL", "computer_agent.slerm-turing.bonsai-27b")
GATEWAY = os.environ.get("GATEWAY", "http://slerm-head:8090/v1")
GATEWAY_MODEL = PIPE_MODEL.split(".", 1)[1].split(".", 1)[1]  # slerm-turing.bonsai-27b -> bonsai-27b


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def text_of(m: dict) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


async def main():
    chat = Chats.get_chat_by_id(CHAT_ID)
    hist = chat.chat["history"]
    msgs, cur = hist["messages"], hist["currentId"]
    chain, m = [], cur
    while m:
        chain.append(msgs[m])
        m = msgs[m].get("parentId")
    chain.reverse()

    material = []
    for mm in chain:
        for f in mm.get("files") or []:
            fid = f.get("id") or (f.get("file") or {}).get("id")
            fo = Files.get_file_by_id(fid) if fid else None
            content = ((fo.data or {}).get("content") if fo else "") or ""
            if content.strip():
                material.append(f"[{fo.filename}]\n{content}")
    log(f"chat has {len(chain)} messages on the current branch, {len(material)} attached documents with text")

    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    parent = dict(msgs[cur])
    parent["childrenIds"] = list(parent.get("childrenIds") or []) + [uid]
    Chats.upsert_message_to_chat_by_id_and_message_id(CHAT_ID, cur, parent)
    Chats.upsert_message_to_chat_by_id_and_message_id(CHAT_ID, uid, {
        "id": uid, "parentId": cur, "childrenIds": [aid], "role": "user", "content": USER_TEXT,
        "timestamp": now, "models": [PIPE_MODEL]})
    Chats.upsert_message_to_chat_by_id_and_message_id(CHAT_ID, aid, {
        "id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
        "model": PIPE_MODEL, "modelName": "Computer Agent · " + GATEWAY_MODEL, "modelIdx": 0,
        "timestamp": now, "done": False, "statusHistory": []})
    log("appended user message", uid, "and assistant reply", aid)

    fn = Functions.get_function_by_id("computer_agent")
    mod = types.ModuleType("computer_agent")
    exec(compile(fn.content, "computer_agent.py", "exec"), mod.__dict__)
    pipe = mod.Pipe()
    # VALVE_OVERRIDES='{"THINKING": true}' changes the pipe's valves for this run only (the DB stays as it is).
    pipe.valves = mod.Pipe.Valves(**{**(Functions.get_function_valves_by_id("computer_agent") or {}),
                                     **json.loads(os.environ.get("VALVE_OVERRIDES") or "{}")})

    async def complete(request, user, form):
        form = {**form, "model": GATEWAY_MODEL}
        async with httpx.AsyncClient(timeout=1800) as c:
            r = await c.post(f"{GATEWAY}/chat/completions", json=form)
        d = r.json()
        if "choices" not in d:
            raise RuntimeError(f"gateway: {str(d)[:300]}")
        return d["choices"][0]["message"].get("content") or ""

    pipe._complete = complete

    async def emit(ev):
        t, data = ev.get("type"), ev.get("data") or {}
        if t == "status":
            Chats.add_message_status_to_chat_by_id_and_message_id(CHAT_ID, aid, data)
            log("status:", data.get("description"))
        elif t == "embeds":
            Chats.upsert_message_to_chat_by_id_and_message_id(CHAT_ID, aid, {"embeds": data.get("embeds", [])})

    body = {"model": PIPE_MODEL, "messages":
            ([{"role": "system", "content": "\n\n".join(material)}] if material else []) +
            [{"role": mm["role"], "content": text_of(mm)} for mm in chain if mm.get("role") in ("user", "assistant")] +
            [{"role": "user", "content": USER_TEXT}]}
    try:
        result = await pipe.pipe(body, {"id": chat.user_id}, None, emit, None, {"chat_id": CHAT_ID, "message_id": aid})
    except Exception as e:
        result = f"The agent runner hit an error: {e.__class__.__name__}: {e}"
        log(traceback.format_exc())
    Chats.upsert_message_to_chat_by_id_and_message_id(CHAT_ID, aid, {"content": result, "done": True})
    log("finished:", result[:300].replace("\n", " "))


asyncio.run(main())
