import asyncio
import json
import hashlib
import os
import signal
import socket
import stat
import shutil
import time
import uuid
from pathlib import Path

from .config import ControllerConfig
from .ipc import serve
from .references import REFERENCE_TOOL, delete_export, publish, publish_generated
from .rpc import CodexRPC, RPCError
from .security import Rejected, authorized_update, private_dir, safe_text
from .storage import Store
from .terminal import Terminal

APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}
HELP = "/projects /project /sessions /status /queue /cancel /pause /resume /new /clear /codex_status\nText messages become tasks. Voice requires confirmation. Use complex Codex commands locally."
LABELS = {"idle": "⚪ Idle", "stopped": "🔴 Not started", "queued": "📥 Queued", "dispatching": "🟡 Dispatching to Codex", "running": "🟢 Running", "waiting_approval": "⚠️ Awaiting approval", "waiting_user": "❓ Awaiting your reply", "waiting_reference": "🎨 Awaiting review", "unknown": "⚠️ Needs recovery", "completed": "✅ Completed", "failed": "❌ Error", "cancelled": "⏹ Cancelled", "interrupted": "⏹ Interrupted"}
ACTIVITY_LABELS = {"reasoning": "Reasoning", "agentMessage": "Writing a reply", "commandExecution": "Command execution", "fileChange": "File changes", "webSearch": "Web search", "mcpToolCall": "Tool call", "dynamicToolCall": "Tool call", "imageGeneration": "Image generation", "contextCompaction": "Context compaction"}


async def probe_codex(*args: str) -> str:
    process = await asyncio.create_subprocess_exec("codex", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, error = await asyncio.wait_for(process.communicate(), 15)
        if process.returncode:
            raise Rejected("Local Codex check failed")
        return (out+error).decode(errors="replace").strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def text_input(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "text_elements": []}]


def question_replies(item: dict) -> set[str] | None:
    content = [x for x in item.get("content", []) if x.get("type") == "text"]
    if len(content) != 1:
        return None
    text = content[0]["text"].strip()
    if text.startswith("# Context from my IDE setup:\n"):
        text = text.rsplit("\n## My request for Codex:\n", 1)[-1].strip()
    opening, closing = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
    if not text.startswith(opening) or not text.endswith(closing):
        return None
    try:
        replies = json.loads(text[len(opening):-len(closing)])
        if isinstance(replies, dict):
            replies = [replies]
        if not isinstance(replies, list) or not replies:
            return None
        return {r["questionItemId"] for r in replies if isinstance(r, dict)
                and all(isinstance(r.get(k), str) for k in ("questionItemId", "question", "answer"))}
    except (ValueError, TypeError):
        return None


class Controller:
    def __init__(self, config: ControllerConfig, user_id: int, store: Store | None = None):
        self.config, self.user_id = config, user_id
        self.store = store or Store(config.state / "metadata.db")
        self.epoch = uuid.uuid4().hex
        self.mutex = asyncio.Lock()
        self.requests: dict[str, dict] = {}
        self.progress: dict[str, tuple[str, float]] = {}
        self.activity: dict[str, tuple[str, str, str, float]] = {}
        self.answers: dict[str, tuple[str, str, str]] = {}
        self.stop = asyncio.Event()
        self.starting: set[str] = set()
        self.terminal = Terminal(config.runtime)
        self.rpc = CodexRPC(config.runtime / "codex.sock", self.on_event)

    def notify(self, pid: str, text: str, buttons=None, image=None, final=False):
        p = self.store.project(pid)
        body = {"project": pid, "text": f"📂 {safe_text(p['name'], 80)}\n{safe_text(text, 3800)}"}
        if final:
            body["final"] = True
        if buttons:
            body["buttons"] = buttons
        if image:
            body["image"] = image
        self.store.notice(body)

    def result(self, pid: str, label: str, answer: str = ""):
        text = safe_text(answer, 16001)
        if len(text) > 16000:
            text = text[:16000] + "\n[Reply shortened; the full text is available in Codex.]"
        chunks = [text[i:i+3400] for i in range(0, len(text), 3400)] or [""]
        for index, chunk in enumerate(chunks):
            heading = label if index == 0 else f"Codex reply — continued {index+1}/{len(chunks)}"
            self.notify(pid, heading + ("\n\n"+chunk if chunk else ""), final=True)

    async def generated_image(self, p: dict, turn: str, item: dict):
        if not self.config.mirror_results or item.get("status") != "completed":
            return
        key = "image:" + hashlib.sha256(json.dumps([p["thread"], turn, item["id"]]).encode()).hexdigest()
        if self.store.setting(key):
            return
        try:
            filename = await asyncio.to_thread(publish_generated, self.config.outbox, p["id"], uuid.uuid4().hex, item.get("result"))
        except (Rejected, OSError):
            self.notify(p["id"], "❌ Generated image not sent: data is missing or failed format/size validation.", final=True)
        else:
            self.notify(p["id"], "🎨 Image from Codex", image=filename, final=True)
        self.store.setting(key, "notified")

    def request(self, p: dict, kind: str, payload: dict, rid=None, ttl: int = 1800) -> str:
        token = uuid.uuid4().hex
        self.store.db.execute("INSERT INTO requests(token,project,thread,session,turn,rid,kind,payload,expires,epoch) VALUES(?,?,?,?,?,?,?,?,?,?)", (token, p["id"], p["thread"] or "", p["session"] or "", p["turn"] or "", json.dumps(rid), kind, json.dumps(payload), time.time()+ttl, self.epoch))
        if rid is not None:
            self.requests[token] = {"rid": rid, "kind": kind, "payload": payload}
        return token

    def async_questions(self, p: dict, turn: str, item: dict):
        key = "question:" + hashlib.sha256(json.dumps([p["thread"], turn, item["id"]]).encode()).hexdigest()
        if self.store.setting(key):
            return
        questions = item.get("questions") or []
        if not isinstance(questions, list) or not 1 <= len(questions) <= 20:
            self.notify(p["id"], "❓ Codex asked a question that cannot be displayed safely. Check the local CLI.", final=True)
        else:
            for index, q in enumerate(questions):
                raw = json.dumps(q, ensure_ascii=False)
                options = (q.get("options") or []) if isinstance(q, dict) else []
                if (not isinstance(q, dict) or not isinstance(q.get("title"), str) or not q["title"].strip()
                        or not isinstance(options, list) or len(options) > 10
                        or any(not isinstance(o, str) or not o.strip() for o in options)
                        or len(raw) > 3000 or safe_text(raw, 3000) != raw):
                    self.notify(p["id"], "❓ A sensitive or complex question needs a local reply.", final=True)
                    continue
                qid = json.dumps(["request_user_input_async", item["id"], index], separators=(",", ":"))
                token = self.request({**p, "turn": turn}, "async_question", {"questionItemId": qid, "question": q["title"], "options": options})
                buttons = [[(safe_text(o, 60), f"q:{token}:o{i}")] for i, o in enumerate(options)]
                buttons.append([("✏️ Reply with text or voice", f"q:{token}:edit")])
                labels = "\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(options)) if options else ""
                self.notify(p["id"], "❓ Codex has a question\n" + q["title"] + labels
                            + "\nCodex may continue working. Choose an option or reply to this message.", buttons, final=True)
        self.store.setting(key, "notified")

    def dismiss_async_questions(self, pid: str, turn: str, item: dict):
        replies = question_replies(item)
        rows = self.store.db.execute("SELECT token,payload FROM requests WHERE project=? AND turn=? AND kind='async_question' AND state IN ('pending','editing','sending')", (pid, turn)).fetchall()
        for row in rows:
            if replies is None or json.loads(row["payload"])["questionItemId"] in replies:
                self.store.db.execute("UPDATE requests SET state='resolved',payload='{}' WHERE token=?", (row["token"],))

    def current(self) -> dict:
        pid = self.store.setting("selected")
        if pid not in {p.id for p in self.config.projects}:
            pid = self.config.projects[0].id
            self.store.setting("selected", pid)
        return self.store.project(pid)

    def public_project(self, p: dict) -> dict:
        return {k: p[k] for k in ("id", "name", "state", "paused", "thread", "session", "turn")}

    def context(self, p: dict) -> dict:
        rows = self.store.db.execute("SELECT token,kind,state FROM requests WHERE project=? AND thread=? AND turn=? AND state IN ('pending','editing') AND expires>? AND kind IN ('question','async_question','reference','helper_reference')", (p["id"], p["thread"], p["turn"], time.time())).fetchall()
        editing = [r for r in rows if r["kind"] == "async_question" and r["state"] == "editing"]
        if len(editing) == 1:
            rows = editing
        return {"project": p["id"], "thread": p["thread"], "request": rows[0]["token"] if len(rows) == 1 else None}

    async def handle(self, op: str, data: dict, local: bool = False):
        async with self.mutex:
            if op == "events":
                events = []
                for r in self.store.db.execute("SELECT * FROM notices WHERE acked=0 ORDER BY id LIMIT 10").fetchall():
                    body = json.loads(r["body"])
                    valid = True
                    async_question = False
                    for buttons in body.get("buttons", []):
                        for _, callback in buttons:
                            parts = callback.split(":")
                            if len(parts) == 3:
                                req = self.store.db.execute("SELECT * FROM requests WHERE token=?", (parts[1],)).fetchone()
                                async_question = bool(req and req["kind"] == "async_question")
                                p = self.store.project(body["project"])
                                if not req or req["state"] not in {"pending", "editing"} or req["epoch"] != self.epoch or req["expires"] <= time.time() or (req["thread"], req["session"]) != (p["thread"] or "", p["session"] or "") or (req["kind"] in {"approval", "question", "async_question", "reference", "helper_reference"} and req["turn"] != p["turn"]):
                                    valid = False
                    if not valid:
                        if async_question:
                            body.pop("buttons", None)
                            body["final"] = True
                            body["text"] += "\nThis question is no longer active; it was dismissed locally or the turn ended."
                            self.store.db.execute("UPDATE notices SET body=? WHERE id=?", (json.dumps(body), r["id"]))
                            events.append({"id": r["id"], **body})
                            continue
                        if body.get("image"):
                            delete_export(self.config.outbox, body["image"])
                        self.store.db.execute("DELETE FROM notices WHERE id=?", (r["id"],))
                    else:
                        events.append({"id": r["id"], **body})
                return events
            if op == "ack":
                if type(data.get("id")) is not int:
                    raise Rejected("Invalid notification ID")
                row = self.store.db.execute("SELECT body FROM notices WHERE id=? AND acked=0", (data["id"],)).fetchone()
                if row:
                    body = json.loads(row[0])
                    mid = data.get("message_id")
                    if type(mid) is int:
                        for buttons in body.get("buttons", []):
                            for _, callback in buttons:
                                parts = callback.split(":")
                                if len(parts) == 3 and parts[0] in {"q", "r"}:
                                    self.store.setting(f"reply:{mid}", parts[1])
                    if body.get("image"):
                        delete_export(self.config.outbox, body["image"])
                self.store.db.execute("DELETE FROM notices WHERE id=?", (data["id"],))
                return {}
            if op in {"local_info", "reconcile", "migrate", "reference_decide", "reference_begin", "reference_poll"}:
                if not local:
                    raise Rejected("This operation is available locally only")
                p = self.store.project(data.get("project", self.current()["id"]))
                if op == "local_info":
                    if data.get("attach") and p["thread"] and shutil.which("tmux"):
                        await self.terminal.ensure(p)
                    return {**p, "endpoint": str(self.config.runtime / "codex.sock")}
                if op == "reconcile":
                    await self.reconcile(p["id"], explicit=True)
                    return self.public_project(self.store.project(p["id"]))
                if op == "reference_decide":
                    self.get_request(data["token"], {"reference", "helper_reference"})
                    await self.decide(data["token"], data["decision"], data.get("text"))
                    return {}
                if op == "reference_begin":
                    if not p["turn"] or p["state"] != "running":
                        raise Rejected("Publishing requires an active turn with no other pending request")
                    if data.get("session") and data["session"] != p["session"]:
                        raise Rejected("Publication session is stale")
                    live = await self.read_thread(p["thread"])
                    if not any(t["id"] == p["turn"] and t["status"] == "inProgress" for t in live.get("turns", [])):
                        raise Rejected("No active turn")
                    token = self.request(p, "helper_reference", {}, ttl=86400)
                    filename = await asyncio.to_thread(publish, Path(p["path"]), self.config.outbox, p["id"], token, data["path"])
                    self.store.db.execute("UPDATE requests SET payload=? WHERE token=?", (json.dumps({"image": filename}), token))
                    self.requests[token] = {"rid": None, "kind": "helper_reference", "payload": {}}
                    self.store.state(p["id"], "waiting_reference", p["turn"])
                    task = self.store.active(p["id"])
                    if task:
                        self.store.task_state(task["id"], "waiting_reference")
                    number = self.store.db.execute("SELECT COUNT(*) FROM requests WHERE project=? AND turn=? AND kind IN ('reference','helper_reference')", (p["id"], p["turn"])).fetchone()[0]
                    self.notify(p["id"], f"🎨 Reference #{number}. The publish helper blocks its calling tool until you decide.", [[("✅ Accept", f"r:{token}:accept"), ("🔄 Redo", f"r:{token}:redo"), ("✏️ Edits", f"r:{token}:edit")]], filename)
                    return {"token": token}
                if op == "reference_poll":
                    row = self.store.db.execute("SELECT * FROM requests WHERE token=? AND project=? AND kind='helper_reference'", (data["token"], p["id"])).fetchone()
                    if not row or row["epoch"] != self.epoch or row["state"] == "stale" or row["expires"] <= time.time():
                        raise Rejected("Reference wait interrupted; dependent work must not continue")
                    payload = json.loads(row["payload"])
                    if payload.get("decision"):
                        self.store.db.execute("UPDATE requests SET payload='{}',state='stale' WHERE token=?", (row["token"],))
                    return {"decision": payload.get("decision"), "edits": payload.get("edits")}
                try:
                    async with asyncio.timeout(100):
                        return await self.migrate_project(p, data)
                except TimeoutError:
                    current = self.store.project(p["id"])
                    self.store.state(p["id"], "unknown", current["turn"])
                    self.store.db.execute("UPDATE projects SET paused=1 WHERE id=?", (p["id"],))
                    raise Rejected("Handoff timed out; check the session binding locally. Queue paused") from None
                except RPCError:
                    raise Rejected("App Server did not confirm the handoff. Check /status and the binding locally; no automatic retry") from None
            if op == "context":
                return self.context(self.current())
            if op == "voice":
                token = data.get("token")
                r = self.get_request(token, {"voice_download"})
                text = self.valid_text(data.get("text"))
                payload = json.loads(r["payload"])
                payload["text"] = text
                self.store.db.execute("UPDATE requests SET kind='voice',payload=? WHERE token=? AND state='pending'", (json.dumps(payload), token))
                self.notify(r["project"], "🎤 Transcribed:\n"+text, [[("✅ Send to Codex", f"v:{token}:send"), ("✏️ Edit", f"v:{token}:edit"), ("❌ Cancel", f"v:{token}:cancel")]])
                return {}
            if op == "voice_failed":
                r = self.get_request(data.get("token"), {"voice_download"})
                self.store.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE token=?", (r["token"],))
                self.notify(r["project"], safe_text(data.get("reason", "Transcription failed")))
                return {}
            if op != "update":
                raise Rejected("Operation not allowed")
            update = data.get("update")
            if not isinstance(update, dict) or not authorized_update(update, self.user_id):
                raise Rejected("Access denied")
            uid = update.get("update_id")
            if type(uid) is not int:
                raise Rejected("Invalid Telegram update")
            if self.store.db.execute("SELECT 1 FROM updates WHERE id=?", (uid,)).fetchone():
                return {"duplicate": True}
            if "callback_query" in update:
                self.claim_update(uid)
                c = update["callback_query"]
                return await self.callback(c.get("data", ""))
            message = update["message"]
            p = self.current()
            reply_id = message.get("reply_to_message", {}).get("message_id")
            target = self.store.setting(f"reply:{reply_id}") if reply_id else None
            if "voice" in message:
                voice = message["voice"]
                if type(voice.get("duration")) is not int or not 0 < voice["duration"] <= 120 or not 0 < voice.get("file_size", 0) <= 8 * 1024**2:
                    raise Rejected("Voice message: maximum 120 seconds and 8 MiB")
                context = self.context(p)
                if target:
                    request = self.get_request(target, {"question", "async_question", "reference", "helper_reference"})
                    p = self.store.project(request["project"])
                    context = {"project": p["id"], "thread": p["thread"], "request": target}
                self.store.transaction()
                try:
                    self.claim_update(uid)
                    token = self.request(p, "voice_download", {"context": context})
                    self.store.commit()
                except BaseException:
                    self.store.rollback()
                    raise
                return {"voice": {"token": token, "file_id": voice["file_id"], "duration": voice["duration"]}}
            if "text" not in message:
                self.claim_update(uid)
                self.notify(p["id"], "Text and voice are supported. Publish project files only through the local helper.")
                return {}
            text = self.valid_text(message["text"])
            editing = self.store.db.execute("SELECT * FROM requests WHERE state='editing' AND kind='voice' AND expires>?", (time.time(),)).fetchall()
            if len(editing) > 1:
                raise Rejected("Multiple voice edits are pending. Cancel the extra voice confirmations using their buttons")
            if len(editing) == 1:
                self.get_request(editing[0]["token"], {"voice"})
                self.claim_update(uid)
                r = dict(editing[0]); payload = json.loads(r["payload"]); payload["text"] = text
                self.store.db.execute("UPDATE requests SET payload=?,state='pending' WHERE token=?", (json.dumps(payload), r["token"]))
                self.notify(r["project"], "🎤 Edited:\n"+text, [[("✅ Send", f"v:{r['token']}:send"), ("❌ Cancel", f"v:{r['token']}:cancel")]])
                return {}
            if text.startswith("/"):
                self.claim_update(uid)
                return await self.command(p, text)
            # Telegram replies are bound to the originating request even after project switching.
            if target:
                self.claim_update(uid)
                r = self.get_request(target, {"question", "async_question", "reference", "helper_reference"})
                await self.decide(target, "text", text)
            else:
                await self.route_text(self.context(p), text, uid)
            return {}

    def valid_text(self, text):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 64 * 1024 or "\x00" in text:
            raise Rejected("Request must be nonempty and no larger than 64 KiB")
        if safe_text(text, len(text)) != text:
            raise Rejected("Message appears to contain a secret or control characters; send a request without secret values")
        return text

    def claim_update(self, uid: int):
        self.store.db.execute("INSERT INTO updates VALUES(?)", (uid,))

    async def route_text(self, context: dict, text: str, update_id: int | None):
        p = self.store.project(context["project"])
        if context.get("thread") != p["thread"]:
            raise Rejected("Session changed; send the message again")
        if context.get("request"):
            if update_id is not None:
                self.claim_update(update_id)
            r = self.get_request(context["request"], {"question", "async_question", "reference", "helper_reference"})
            await self.decide(r["token"], "text", text)
            return
        if p["state"].startswith("waiting") or self.store.db.execute("SELECT 1 FROM requests WHERE project=? AND turn=? AND kind='async_question' AND state IN ('pending','editing') AND expires>?", (p["id"], p["turn"], time.time())).fetchone():
            raise Rejected("Multiple requests are pending. Reply to the appropriate Telegram message")
        self.store.transaction()
        try:
            if update_id is not None:
                self.claim_update(update_id)
            tid = self.store.enqueue(p["id"], text, update_id)
            n = self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE project=? AND state='queued'", (p["id"],)).fetchone()[0]
            self.notify(p["id"], f"📥 Task {tid[:8]} queued. Position: {n}. Status: {LABELS[p['state']]}.")
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise

    async def command(self, p: dict, text: str):
        command = text.split()[0].split("@")[0]
        if command in {"/start", "/help"}:
            self.notify(p["id"], HELP, [[("📊 Codex status", f"s:{p['id']}")]])
        elif command == "/project":
            self.notify(p["id"], f"Selected project: {p['name']}\n{LABELS[p['state']]}\nSelect another project: /projects")
        elif command == "/projects":
            projects = [self.public_project(x) for x in self.store.projects() if x["id"] in {c.id for c in self.config.projects}]
            self.notify(p["id"], "📂 Projects\n"+"\n".join(f"{x['name']} — {LABELS[x['state']]}" for x in projects), [[(x["name"], f"p:{x['id']}")] for x in projects])
        elif command == "/status":
            await self.status(p)
        elif command == "/sessions":
            rows = self.store.db.execute("SELECT thread FROM sessions WHERE project=? ORDER BY created DESC LIMIT 20", (p["id"],)).fetchall()
            self.notify(p["id"], "Session history (restore locally only):\n"+"\n".join(r[0] for r in rows))
        elif command == "/queue":
            rows = self.store.db.execute("SELECT id,state FROM tasks WHERE project=? AND finished IS NULL ORDER BY created LIMIT 25", (p["id"],)).fetchall()
            self.notify(p["id"], "Queue:\n"+"\n".join(f"{r[0][:8]} — {LABELS[r[1]]}" for r in rows))
        elif command in {"/pause", "/resume"}:
            self.store.db.execute("UPDATE projects SET paused=? WHERE id=?", (command == "/pause", p["id"]))
            self.notify(p["id"], "Queue paused" if command == "/pause" else "Queue resumed. Unknown state requires local review.")
        elif command == "/cancel":
            if not p["turn"] or p["state"] in {"idle", "stopped", "unknown"}:
                raise Rejected("No confirmed active turn")
            await self.rpc.call("turn/interrupt", {"threadId": p["thread"], "turnId": p["turn"]})
            self.notify(p["id"], "Cancellation requested. Waiting for turn/completed.")
        elif command == "/new":
            if p["state"] not in {"idle", "stopped"} or self.store.active(p["id"]):
                raise Rejected("Wait for the task to finish or cancel it")
            token = self.request(p, "new", {})
            self.notify(p["id"], "⚠️ Start a new Codex chat? The old runtime and its child processes will close via archive/unarchive; history will be kept. Disconnect the local TUI first, then reconnect to the new ID.", [[("✅ New chat", f"n:{token}:yes"), ("❌ Cancel", f"n:{token}:no")]])
        elif command == "/clear":
            # shortcut: remote /clear is unverified on 0.160.1; enable after live ID/cwd tests.
            if p["state"] not in {"idle", "stopped"} or self.store.active(p["id"]):
                raise Rejected("Wait for completion or use /cancel")
            self.notify(p["id"], "Remote /clear in Codex 0.160.1 has not reliably passed Session ID validation. The bridge does not substitute this command. Use /new after disconnecting the local TUI. Run native /clear locally, then verify and migrate the new chat.")
        elif command == "/codex_status":
            self.notify(p["id"], "Run native /status in your local Codex TUI. Telegram /status shows a safe bridge summary. Mirroring native status output is unsupported in this version.")
        else:
            self.notify(p["id"], "Command not allowed. Sandbox settings, shell commands and interactive menus are available locally only.\n"+HELP)
        return {}

    async def status(self, p: dict):
        lines = []
        if not p["thread"]:
            lines += ["Codex: 🔴 No project session", "Busy: no"]
        elif not self.rpc.connected:
            lines += ["Codex: 🔴 Disconnected", "Busy: unverified"]
        else:
            try:
                page = await self.rpc.call("thread/turns/list", {"threadId": p["thread"], "sortDirection": "desc", "itemsView": "notLoaded", "limit": 1}, timeout=5)
                thread = (await self.rpc.call("thread/read", {"threadId": p["thread"], "includeTurns": False}, timeout=5))["thread"]
                if (thread["id"], thread["sessionId"], Path(thread["cwd"]).resolve()) != (p["thread"], p["session"], Path(p["path"])):
                    raise Rejected("Live session does not match the project")
                runtime = thread["status"]
                turn = page["data"][0] if page["data"] else None
                if runtime["type"] == "active":
                    flags = runtime.get("activeFlags", [])
                    state = "waiting_approval" if "waitingOnApproval" in flags else "waiting_user" if "waitingOnUserInput" in flags else "running"
                    if state == "running" and p["state"] == "waiting_reference" and turn and turn["id"] == p["turn"]:
                        state = "waiting_reference"
                    lines += [f"Codex: {LABELS[state]}", "Busy: yes"]
                    if turn and turn["status"] == "inProgress":
                        lines.append("Current turn: " + turn["id"][:8])
                        task = self.store.active(p["id"])
                        source = "Bridge queue" if task and task["turn"] == turn["id"] else "Local / untracked Codex turn"
                        lines.append("Source: " + source)
                        if type(turn.get("startedAt")) is int:
                            lines.append(f"Elapsed: {max(0, int(time.time()-turn['startedAt']))} sec.")
                        activity = self.activity.get(p["id"])
                        if activity and activity[:2] == (p["thread"], turn["id"]):
                            lines.append(f"Latest activity: {activity[2]} ({max(0, int(time.time()-activity[3]))} sec. ago)")
                        else:
                            lines.append("Latest activity: no activity event observed yet")
                        count = self.store.db.execute("SELECT COUNT(*) FROM requests WHERE project=? AND thread=? AND turn=? AND kind='async_question' AND state IN ('pending','editing') AND expires>? AND epoch=?", (p["id"], p["thread"], turn["id"], time.time(), self.epoch)).fetchone()[0]
                        if count:
                            lines.append(f"Open async questions: {count} (Codex may continue working)")
                elif runtime["type"] == "idle":
                    lines += ["Codex: ⚪ Idle — no active task", "Busy: no"]
                    if turn and turn["status"] in {"completed", "failed", "interrupted"}:
                        lines.append("Last turn: " + LABELS[turn["status"]])
                elif runtime["type"] == "notLoaded":
                    lines += ["Codex: 🔴 Session is not loaded", "Busy: no"]
                else:
                    lines += ["Codex: ⚠️ Runtime state unknown", "Busy: unverified"]
            except (RPCError, OSError, TimeoutError, KeyError, TypeError, ValueError):
                lines = ["Codex: ⚠️ Live status unavailable", "Busy: unverified", "Queue state was not changed. Check the connection locally."]
        n = self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE project=? AND state='queued'", (p["id"],)).fetchone()[0]
        lines += [f"Bridge: {LABELS[p['state']]}", f"Queue: {n}. Paused: {'yes' if p['paused'] else 'no'}."]
        self.notify(p["id"], "\n".join(lines), [[("🔄 Refresh", f"s:{p['id']}")]], final=True)

    def get_request(self, token: str, kinds: set[str]) -> dict:
        r = self.store.db.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
        if not r or r["kind"] not in kinds or r["state"] not in {"pending", "editing"} or r["expires"] <= time.time() or r["epoch"] != self.epoch:
            raise Rejected("Button expired or already used")
        p = self.store.project(r["project"])
        if (r["thread"], r["session"]) != (p["thread"] or "", p["session"] or ""):
            self.store.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE token=?", (token,))
            raise Rejected("Button belongs to another session")
        return dict(r)

    async def callback(self, data: str):
        if not isinstance(data, str) or len(data) > 64:
            raise Rejected("Invalid button")
        parts = data.split(":")
        if len(parts) == 2 and parts[0] in {"p", "s"}:
            p = self.store.project(parts[1])
            if p["id"] not in {c.id for c in self.config.projects}:
                raise Rejected("Project not allowed")
            if parts[0] == "s":
                await self.status(p)
            else:
                self.store.setting("selected", p["id"])
                self.notify(p["id"], f"Project selected. Status: {LABELS[p['state']]}")
            return {}
        if len(parts) != 3:
            raise Rejected("Invalid button")
        prefix, token, action = parts
        if prefix == "n":
            r = self.get_request(token, {"new"})
            p = self.store.project(r["project"])
            if action not in {"yes", "no"}:
                raise Rejected("Invalid decision")
            self.store.db.execute("UPDATE requests SET state='resolved' WHERE token=?", (token,))
            if action == "yes":
                if self.store.active(p["id"]) or p["state"] not in {"idle", "stopped"}:
                    raise Rejected("Project busy")
                if p["thread"]:
                    await self.release_thread(p)
                await self.start_project(p["id"])
                self.notify(p["id"], "New chat created. Previous history kept.")
        elif prefix == "v":
            r = self.get_request(token, {"voice"})
            if action == "edit":
                self.store.db.execute("UPDATE requests SET state='editing' WHERE token=?", (token,))
                self.notify(r["project"], "Send the corrected text. It will stay bound to the original project.")
            elif action in {"send", "cancel"}:
                self.store.db.execute("UPDATE requests SET state='resolved' WHERE token=?", (token,))
                if action == "send":
                    payload = json.loads(r["payload"])
                    await self.route_text(payload["context"], payload["text"], None)
                    self.store.db.execute("UPDATE requests SET payload='{}' WHERE token=?", (token,))
                else:
                    self.store.db.execute("UPDATE requests SET payload='{}' WHERE token=?", (token,))
                    self.notify(r["project"], "Voice request cancelled")
            else:
                raise Rejected("Invalid decision")
        elif prefix in {"a", "r", "q"}:
            await self.decide(token, action)
        else:
            raise Rejected("Invalid button")
        return {}

    async def release_thread(self, p: dict, force: bool = False):
        live = await self.read_thread(p["thread"])
        if (live["id"], live["sessionId"], Path(live["cwd"]).resolve()) != (p["thread"], p["session"], Path(p["path"])):
            raise Rejected("Previous session does not match the project; close cancelled")
        active = [t for t in live.get("turns", []) if t["status"] == "inProgress"]
        if active and not force:
            raise Rejected("The old chat has an active turn. Finish it locally first")
        if force:
            self.store.state(p["id"], "unknown", p["turn"])
            await self.terminal.detach_for_replace(p, force=True)
            # Remove only this thread's native queue before interrupt can pump its next turn.
            queued = await self.rpc.call("thread/queue/list", {"threadId": p["thread"]})
            for submission in queued["data"]:
                deleted = await self.rpc.call("thread/queue/delete", {"threadId": p["thread"], "queuedSubmissionId": submission["id"]})
                if not deleted.get("deleted"):
                    raise Rejected("Old chat queue changed; handoff stopped")
            for turn in active:
                await self.rpc.call("turn/interrupt", {"threadId": p["thread"], "turnId": turn["id"]})
            try:
                async with asyncio.timeout(15):
                    while True:
                        live = await self.read_thread(p["thread"])
                        if not any(t["status"] == "inProgress" for t in live.get("turns", [])) and live["status"]["type"] != "active":
                            break
                        await asyncio.sleep(.25)
            except TimeoutError:
                raise Rejected("Codex did not confirm the old turn ended. Binding unchanged; queue blocked") from None
        else:
            await self.terminal.detach_for_replace(p)
        task = self.store.active(p["id"])
        if task:
            finished = next((t for t in live.get("turns", []) if t["id"] == task["turn"] and t["status"] in {"completed", "failed", "interrupted"}), None)
            if not finished:
                raise Rejected("Previous task outcome is unknown; review it locally first")
            self.store.task_state(task["id"], finished["status"])
        self.store.state(p["id"], "unknown")
        # Unsubscribe alone retains the writer for a 30-minute grace period.
        # Archive closes its runtime; unarchive restores history without resuming it.
        await self.rpc.call("thread/archive", {"threadId": p["thread"]})
        loaded = await self.rpc.call("thread/loaded/list", {})
        if p["thread"] in loaded["data"]:
            raise Rejected("Old runtime is still open. A new session will not be created")
        await self.rpc.call("thread/unarchive", {"threadId": p["thread"]})

    async def migrate_project(self, p: dict, data: dict):
        for flag in ("force", "old_process_stopped", "unarchive"):
            if type(data.get(flag, False)) is not bool:
                raise Rejected("Invalid session handoff flags")
        try:
            thread_id = str(uuid.UUID(data["thread"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Rejected("A valid Codex session UUID is required") from None
        force = data.get("force", False)
        self.check_project(p)
        if any(other["id"] != p["id"] and other["thread"] == thread_id for other in self.store.projects()):
            raise Rejected("Target session is already bound to another project")
        if data.get("unarchive"):
            await self.rpc.call("thread/unarchive", {"threadId": thread_id})
        try:
            target = await self.read_thread(thread_id)
        except (RPCError, TimeoutError):
            raise Rejected("Target history is unavailable. Previous session was not closed") from None
        if target["id"] != thread_id or Path(target["cwd"]).resolve() != Path(p["path"]) or target.get("parentThreadId") or target.get("ephemeral"):
            raise Rejected("Target session does not match the project")
        if any(other["id"] != p["id"] and other["session"] == target["sessionId"] for other in self.store.projects()):
            raise Rejected("Target Session ID is already bound to another project")
        loaded = (await self.rpc.call("thread/loaded/list", {}))["data"]
        if thread_id != p["thread"] and thread_id not in loaded and not data.get("old_process_stopped"):
            raise Rejected("Target chat is not loaded in the bridge App Server. Stop its original Codex/App Server runtime, then retry with --old-process-stopped. --force closes only the previous managed session")
        if thread_id == p["thread"]:
            if target["sessionId"] != p["session"]:
                raise Rejected("Session ID changed; binding not confirmed")
            if thread_id not in loaded:
                await self.reconcile(p["id"], explicit=True)
            else:
                joined = (await self.rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True}))["thread"]
                if (joined["id"], joined["sessionId"], Path(joined["cwd"]).resolve()) != (thread_id, p["session"], Path(p["path"])):
                    self.store.state(p["id"], "unknown", p["turn"])
                    raise Rejected("App Server did not confirm the current session")
            return self.public_project(self.store.project(p["id"]))
        task = self.store.active(p["id"])
        if (task or p["state"] not in {"idle", "stopped", "unknown"}) and not force:
            raise Rejected("Project busy. Finish the task or explicitly use --force")
        if task and not task["turn"]:
            raise Rejected("Previous task delivery is unknown; --force does not permit retrying or discarding it")
        before = [(t["id"], t["status"]) for t in target.get("turns", [])]
        self.store.db.execute("UPDATE projects SET paused=1 WHERE id=?", (p["id"],))
        try:
            if p["thread"]:
                await self.release_thread(p, force=force)
            resumed = (await self.rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True, "sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user"}))["thread"]
            if (resumed["id"], resumed["sessionId"], Path(resumed["cwd"]).resolve()) != (thread_id, target["sessionId"], Path(p["path"])):
                raise Rejected("App Server returned another session; binding unchanged")
            if thread_id not in (await self.rpc.call("thread/loaded/list", {}))["data"]:
                raise Rejected("Target session is not loaded in the runtime; binding unchanged")
            confirmed = await self.read_thread(thread_id)
            after = [(t["id"], t["status"]) for t in confirmed.get("turns", [])]
            if (confirmed["id"], confirmed["sessionId"], Path(confirmed["cwd"]).resolve()) != (thread_id, target["sessionId"], Path(p["path"])) or [t[0] for t in before] != [t[0] for t in after] or any(a != b and a[1] != "inProgress" for a, b in zip(before, after)):
                raise Rejected("Could not verify the target ID and preserved history")
            remaining = self.store.db.execute("SELECT 1 FROM tasks WHERE project=? AND state='queued'", (p["id"],)).fetchone()
            self.store.transaction()
            try:
                self.store.bind(p["id"], resumed)
                self.store.db.execute("UPDATE projects SET paused=? WHERE id=?", (bool(p["paused"] or remaining), p["id"]))
                self.store.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE project=? AND state IN ('pending','sending','editing')", (p["id"],))
                self.store.commit()
            except BaseException:
                self.store.rollback()
                raise
            for token in list(self.requests):
                row = self.store.db.execute("SELECT state FROM requests WHERE token=?", (token,)).fetchone()
                if row and row["state"] == "stale":
                    self.requests.pop(token, None)
            await self.reconcile(p["id"], explicit=True)
        except BaseException:
            current = self.store.project(p["id"])
            self.store.state(p["id"], "unknown", current["turn"])
            raise
        self.notify(p["id"], "Session connected through App Server; ID and history verified. Old approval requests are stale." + (" Queue preserved and paused; review it before /resume." if remaining else "") + " Imported chats may lack the dynamic publish tool.")
        return self.public_project(self.store.project(p["id"]))

    async def decide(self, token: str, decision: str, text: str | None = None):
        r = self.get_request(token, {"approval", "question", "async_question", "reference", "helper_reference"})
        p = self.store.project(r["project"])
        if r["turn"] != p["turn"] or (r["kind"] != "async_question" and token not in self.requests) or not self.rpc.connected:
            raise Rejected("Request is no longer current")
        live = await self.read_thread(p["thread"])
        if not any(t["id"] == r["turn"] and t["status"] == "inProgress" for t in live.get("turns", [])):
            self.store.db.execute("UPDATE requests SET state='stale' WHERE token=?", (token,))
            raise Rejected("Turn has already ended")
        if r["kind"] == "async_question":
            if (live.get("id"), live.get("sessionId"), live.get("cwd")) != (p["thread"], p["session"], p["path"]):
                raise Rejected("Live session does not match the question")
            payload = json.loads(r["payload"])
            origin = json.loads(payload["questionItemId"])[1]
            items = await self.read_turn_messages(p["thread"], r["turn"])
            position = next((i for i, item in enumerate(items) if item.get("type") == "agentMessage" and item.get("id") == origin and item.get("delivery") == "async"), None)
            if position is None:
                raise Rejected("Question could not be verified in the active turn")
            for item in items[position+1:]:
                if item.get("type") == "userMessage":
                    replies = question_replies(item)
                    if replies is None or payload["questionItemId"] in replies:
                        self.store.db.execute("UPDATE requests SET state='resolved',payload='{}' WHERE token=?", (token,))
                        break
            self.get_request(token, {"async_question"})
            if decision == "edit":
                self.store.db.execute("UPDATE requests SET state='pending' WHERE project=? AND kind='async_question' AND state='editing'", (p["id"],))
                self.store.db.execute("UPDATE requests SET state='editing' WHERE token=?", (token,))
                self.store.setting("selected", p["id"])
                self.notify(p["id"], "✏️ Send your answer as text or voice. It stays bound to this question.", final=True)
                return
            if decision.startswith("o") and decision[1:].isdigit():
                index = int(decision[1:])
                if index >= len(payload["options"]):
                    raise Rejected("Invalid option")
                text = payload["options"][index]
            elif decision != "text" or not text:
                raise Rejected("Send your reply as text")
            text = self.valid_text(text)
            reply = json.dumps([{k: payload[k] for k in ("questionItemId", "question")} | {"answer": text}], ensure_ascii=False)
            changed = self.store.db.execute("UPDATE requests SET state='sending' WHERE token=? AND state IN ('pending','editing')", (token,)).rowcount
            if changed != 1:
                raise Rejected("Decision already sent")
            try:
                await self.rpc.call("turn/steer", {"threadId": p["thread"], "expectedTurnId": r["turn"],
                    "input": text_input("<send_user_message_question_reply>\n" + reply + "\n</send_user_message_question_reply>")})
            except Exception:
                self.store.state(p["id"], "unknown", p["turn"])
                raise Rejected("Answer delivery could not be confirmed; check locally. No automatic retry.") from None
            self.store.db.execute("UPDATE requests SET state='resolved',payload='{}' WHERE token=?", (token,))
            self.notify(p["id"], "Answer sent to the original turn.", final=True)
            return
        pending = self.requests[token]
        payload = json.loads(r["payload"])
        if r["kind"] == "approval":
            if decision not in {"yes", "no"}:
                raise Rejected("Invalid approval")
            if payload["method"] == "item/permissions/requestApproval":
                result = {"permissions": payload["params"].get("permissions", {}) if decision == "yes" else {}, "scope": "turn"}
            else:
                result = {"decision": "accept" if decision == "yes" else "decline"}
        elif r["kind"] in {"reference", "helper_reference"}:
            if decision == "edit":
                self.store.db.execute("UPDATE requests SET state='editing' WHERE token=?", (token,))
                self.store.setting("selected", p["id"])
                self.notify(p["id"], "✏️ Send edits as text or voice; the original task is waiting.")
                return
            if decision not in {"accept", "redo", "text"} or (decision == "text" and not text):
                raise Rejected("Invalid reference decision")
            result = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"decision": decision, "edits": text}, ensure_ascii=False)}]}
            if r["kind"] == "helper_reference":
                payload.update(decision=decision, edits=text)
                self.store.db.execute("UPDATE requests SET state='resolved',payload=? WHERE token=? AND state IN ('pending','editing')", (json.dumps(payload), token))
                self.requests.pop(token, None)
                self.store.state(p["id"], "running", p["turn"])
                task = self.store.active(p["id"])
                if task:
                    self.store.task_state(task["id"], "running")
                self.notify(p["id"], "Decision sent to the waiting publish helper.")
                return
        else:
            questions = payload["params"]["questions"]
            if len(questions) != 1:
                raise Rejected("Answer multi-part questions locally")
            q = questions[0]
            if decision.startswith("o") and decision[1:].isdigit():
                index = int(decision[1:]); options = q.get("options") or []
                if index >= len(options):
                    raise Rejected("Invalid option")
                text = options[index]["label"]
            elif decision != "text" or not text:
                raise Rejected("Send your reply as text")
            result = {"answers": {q["id"]: {"answers": [text]}}}
        changed = self.store.db.execute("UPDATE requests SET state='sending' WHERE token=? AND state IN ('pending','editing')", (token,)).rowcount
        if changed != 1:
            raise Rejected("Decision already sent")
        try:
            await self.rpc.respond(pending["rid"], result)
        except Exception:
            self.store.state(p["id"], "unknown", p["turn"])
            raise Rejected("Decision delivery could not be confirmed; check locally") from None
        # Keep waiting until serverRequest/resolved or item/completed confirms consumption.
        self.notify(p["id"], "Decision sent to the original turn.")

    @staticmethod
    def check_project(p: dict):
        path = Path(p["path"])
        if path.resolve(strict=True) != path or not path.is_dir():
            raise Rejected("Project path changed")
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                exe = Path(os.readlink(entry / "exe")).name
                if exe not in {"codex", "codex-linux-sandbox"}:
                    continue
                cmd = (entry / "cmdline").read_bytes().split(b"\x00")
                if b"app-server" in cmd or b"--remote" in cmd:
                    continue
                if Path(os.readlink(entry / "cwd")) == path:
                    raise Rejected("A standalone Codex is already running in the project. Stop it before migration")
            except (OSError, PermissionError):
                continue

    async def start_project(self, pid: str):
        p = self.store.project(pid)
        self.check_project(p)
        self.starting.add(pid)
        try:
            t = (await self.rpc.call("thread/start", {"cwd": p["path"], "sandbox": "workspace-write", "approvalPolicy": "on-request", "approvalsReviewer": "user", "dynamicTools": [REFERENCE_TOOL], "developerInstructions": "For images intended for user review, call bridge_publish_reference with a project-relative path and wait for its returned decision before dependent work. Never use a paid image or speech API for the bridge. Keep approvals enabled."}))["thread"]
            if Path(t["cwd"]).resolve() != Path(p["path"]) or t.get("parentThreadId"):
                raise Rejected("Codex returned an unsuitable session")
            self.store.bind(pid, t)
        finally:
            self.starting.discard(pid)

    async def history_pages(self, method: str, params: dict):
        seen = set()
        while True:
            page = await self.rpc.call(method, params)
            cursor = page.get("nextCursor")
            if cursor is not None and cursor in seen:
                raise RPCError("History pagination did not advance")
            yield page["data"]
            if cursor is None:
                return
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    async def read_thread(self, thread_id: str, items_view: str = "notLoaded") -> dict:
        thread = {**(await self.rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"], "turns": []}
        params = {"threadId": thread_id, "sortDirection": "asc", "itemsView": items_view, "limit": 50 if items_view == "notLoaded" else 1}
        async for page in self.history_pages("thread/turns/list", params):
            thread["turns"].extend(page)
        return thread

    async def read_turn_messages(self, thread_id: str, turn_id: str) -> list[dict]:
        messages = []
        params = {"threadId": thread_id, "turnId": turn_id, "sortDirection": "asc", "limit": 1}
        async for page in self.history_pages("thread/items/list", params):
            for entry in page:
                if entry["turnId"] != turn_id:
                    raise RPCError("History item belongs to another turn")
                if entry["item"]["type"] in {"agentMessage", "userMessage"}:
                    messages.append(entry["item"])
        return messages

    async def reconcile(self, pid: str, explicit: bool = False):
        p = self.store.project(pid)
        if not p["thread"]:
            return
        task = self.store.active(pid)
        t = await self.read_thread(p["thread"], "summary" if task and not task["turn"] else "notLoaded")
        if t["id"] != p["thread"] or t["sessionId"] != p["session"] or Path(t["cwd"]).resolve() != Path(p["path"]):
            raise Rejected("Session does not match the project")
        # Rejoin before returning unknown: pending requests/completion are subscription events.
        await self.rpc.call("thread/resume", {"threadId": p["thread"], "excludeTurns": True})
        turns = t.get("turns", [])
        if task:
            matching = [x for x in turns if x["id"] == task["turn"]]
            if not matching and not task["turn"]:
                matching = [x for x in turns if any(i.get("type") == "userMessage" and i.get("clientId") == task["id"] for i in x.get("items", []))]
                if matching:
                    self.store.task_state(task["id"], "unknown", matching[-1]["id"])
                else:
                    queue = await self.rpc.call("thread/queue/list", {"threadId": p["thread"]})
                    pending = [x for x in queue["data"] if x["clientUserMessageId"] == task["id"]]
                    if len(pending) == 1:
                        deleted = await self.rpc.call("thread/queue/delete", {"threadId": p["thread"], "queuedSubmissionId": pending[0]["id"]})
                        if deleted.get("deleted"):
                            self.store.task_state(task["id"], "queued")
                            task = None
            if not matching:
                # No known turn ID after a send failure: never infer completion from silence.
                if task:
                    self.store.state(pid, "unknown")
                    return
            if matching:
                turn = matching[-1]
                if turn["status"] in {"completed", "failed", "interrupted"}:
                    self.store.task_state(task["id"], turn["status"])
                elif turn["status"] == "inProgress":
                    self.store.state(pid, "unknown", turn["id"])
                    return
        # Resume metadata can describe the pre-load state; read the actual runtime status.
        t = (await self.rpc.call("thread/read", {"threadId": p["thread"], "includeTurns": False}))["thread"]
        if t["status"]["type"] == "idle":
            self.store.state(pid, "idle")
        elif t["status"]["type"] == "active":
            turn = next((x for x in reversed(turns) if x["status"] == "inProgress"), None)
            self.store.state(pid, "unknown" if explicit or task else "running", turn["id"] if turn else None)
        else:
            self.store.state(pid, "unknown")

    async def on_event(self, event: dict):
        async with self.mutex:
            method, params = event["method"], event.get("params", {})
            if method == "bridge/disconnected":
                self.requests.clear()
                self.answers.clear()
                self.activity.clear()
                self.store.recover()
                for p in self.store.projects():
                    self.notify(p["id"], "🔴 Connection to Codex lost. Dispatch blocked until reconciliation.")
                return
            if method == "thread/started":
                t = params["thread"]
                for p in self.store.projects():
                    if p["thread"] and p["thread"] != t["id"] and t.get("cwd") == p["path"] and not t.get("parentThreadId") and not t.get("forkedFromId") and p["id"] not in self.starting:
                        self.store.state(p["id"], "unknown")
                        self.notify(p["id"], "⚠️ Another chat was created locally. Queue blocked; run migrate locally.")
                return
            thread = params.get("threadId")
            p = next((x for x in self.store.projects() if x["thread"] == thread), None)
            if p is None:
                return
            if "id" in event and any(r["rid"] == event["id"] for r in self.requests.values()):
                return
            task = self.store.active(p["id"])
            item = params.get("item", {})
            if method == "item/started" and item.get("type") in ACTIVITY_LABELS:
                self.activity[p["id"]] = (p["thread"], params["turnId"], ACTIVITY_LABELS[item["type"]], time.time())
            if method in {"item/started", "item/completed"} and item.get("type") == "userMessage":
                self.dismiss_async_questions(p["id"], params["turnId"], item)
                if task and item.get("clientId") == task["id"]:
                    self.store.task_state(task["id"], "running", params["turnId"])
                    self.store.state(p["id"], "running", params["turnId"])
                return
            if method == "turn/started":
                turn = params["turn"]
                self.answers.pop(p["id"], None)
                self.store.state(p["id"], "running", turn["id"])
                self.notify(p["id"], "🟢 Codex started a turn")
            elif method == "turn/completed":
                turn = params["turn"]
                status = turn["status"]
                if status not in {"completed", "failed", "interrupted"}:
                    self.store.state(p["id"], "unknown")
                    return
                if p["turn"] not in {None, turn["id"]}:
                    return
                completion_key = f"completion:{p['thread']}:{turn['id']}"
                if self.store.setting(completion_key):
                    return
                for message in turn.get("items", []):
                    if message.get("type") == "agentMessage" and message.get("delivery") == "async" and message.get("questions") is not None:
                        self.async_questions(p, turn["id"], message)
                completed_task = task if task and task["turn"] == turn["id"] else None
                if completed_task:
                    self.store.task_state(task["id"], status)
                self.store.state(p["id"], "idle")
                self.store.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE project=? AND turn=? AND state IN ('pending','sending','editing')", (p["id"], turn["id"]))
                for token in list(self.requests):
                    row = self.store.db.execute("SELECT state FROM requests WHERE token=?", (token,)).fetchone()
                    if row and row[0] == "stale":
                        self.requests.pop(token, None)
                label = {"completed": "✅ Task completed", "failed": "❌ Task failed", "interrupted": "⏹ Task interrupted"}[status]
                label += "\nTask: " + (completed_task["id"][:8] if completed_task else turn["id"][:8])
                if completed_task and completed_task.get("text"):
                    label += " — " + safe_text(completed_task["text"].replace("\n", " "), 180)
                if completed_task and completed_task["started"]:
                    label += f"\nDuration: {int(time.time()-completed_task['started'])} sec."
                cached = self.answers.pop(p["id"], None)
                answer = cached[2] if cached and cached[:2] == (p["thread"], turn["id"]) else ""
                if self.config.mirror_results:
                    messages = [x["text"] for x in turn.get("items", []) if x.get("type") == "agentMessage" and not (x.get("delivery") == "async" and x.get("questions") is not None) and x.get("phase") in {None, "final_answer"}]
                    answer = messages[-1] if messages else answer
                    for image in turn.get("items", []):
                        if image.get("type") == "imageGeneration":
                            await self.generated_image(p, turn["id"], image)
                self.result(p["id"], label, answer if self.config.mirror_results else "")
                self.store.setting(completion_key, "notified")
            elif method == "item/completed" and item.get("type") in {"agentMessage", "imageGeneration"}:
                if params.get("turnId") != p["turn"]:
                    return
                if item.get("delivery") == "async" and item.get("questions") is not None:
                    self.async_questions(p, p["turn"], item)
                    return
                if not self.config.mirror_results:
                    return
                if item["type"] == "agentMessage" and item.get("phase") in {None, "final_answer"}:
                    self.answers[p["id"]] = (p["thread"], p["turn"], safe_text(item["text"], 16001))
                elif item["type"] == "imageGeneration":
                    await self.generated_image(p, p["turn"], item)
            elif method in APPROVALS or method == "item/tool/requestUserInput":
                kind = "approval" if method in APPROVALS else "question"
                if params.get("turnId") != p["turn"]:
                    return
                question_json = json.dumps(params.get("questions", []))
                if kind == "question" and (len(params.get("questions", [])) != 1 or any(q.get("isSecret") for q in params.get("questions", [])) or safe_text(question_json, 64000) != question_json):
                    self.store.state(p["id"], "waiting_user", p["turn"])
                    if task:
                        self.store.task_state(task["id"], "waiting_user")
                    self.notify(p["id"], "❓ Multi-part or sensitive question: answer locally. Queue is waiting.")
                    return
                permissions = params.get("permissions") or params.get("additionalPermissions") or params.get("grantRoot")
                permissions_json = json.dumps(permissions, ensure_ascii=False) if permissions else ""
                decisions = params.get("availableDecisions")
                if kind == "approval" and ((decisions is not None and not all(x in decisions for x in ("accept", "decline"))) or len(permissions_json) > 1000 or (params.get("command") and len(params["command"]) > 1000)):
                    self.store.state(p["id"], "waiting_approval", p["turn"])
                    if task:
                        self.store.task_state(task["id"], "waiting_approval")
                    self.notify(p["id"], "⚠️ Complex approval or no allow-once decision. Approve locally; nothing was approved automatically.")
                    return
                saved = {"permissions": params.get("permissions", {})} if kind == "approval" else {"questions": params["questions"]}
                token = self.request(p, kind, {"method": method, "params": saved}, event["id"])
                state = "waiting_approval" if kind == "approval" else "waiting_user"
                self.store.state(p["id"], state, p["turn"])
                if task:
                    self.store.task_state(task["id"], state)
                if kind == "approval":
                    network = params.get("networkApprovalContext")
                    content = "Network access: "+json.dumps(network, ensure_ascii=False) if network else params.get("command") or "File changes / additional permissions"
                    if permissions_json:
                        content += "\nRequested permissions: "+permissions_json
                    self.notify(p["id"], "⚠️ Codex requests approval\n"+safe_text(content, 2050)+"\nReason: "+safe_text(params.get("reason") or "not provided", 350), [[("✅ Allow once", f"a:{token}:yes"), ("❌ Decline", f"a:{token}:no")]])
                else:
                    q = params["questions"][0]
                    buttons = [[(safe_text(o["label"], 60), f"q:{token}:o{i}")] for i, o in enumerate(q.get("options") or [])]
                    self.notify(p["id"], "❓ "+safe_text(q["question"])+"\nYou can reply to this message with text.", buttons)
            elif method == "item/tool/call":
                if params.get("tool") != "bridge_publish_reference" or params.get("turnId") != p["turn"]:
                    return
                token = self.request(p, "reference", {"method": method, "params": params}, event["id"], ttl=86400)
                try:
                    self.check_project(p)
                    args = params["arguments"]
                    if not isinstance(args, dict) or set(args) != {"path"}:
                        raise Rejected("Invalid publication")
                    filename = await asyncio.to_thread(publish, Path(p["path"]), self.config.outbox, p["id"], token, args["path"])
                except (Rejected, OSError):
                    self.store.db.execute("UPDATE requests SET state='resolved' WHERE token=?", (token,))
                    self.requests.pop(token, None)
                    await self.rpc.respond(event["id"], {"success": False, "contentItems": [{"type": "inputText", "text": "Reference rejected: export a static PNG/JPEG/WEBP inside the project, without symlinks/hardlinks, <=10 MiB."}]})
                    return
                payload = {"method": method, "params": {"callId": params["callId"]}, "image": filename}
                self.store.db.execute("UPDATE requests SET payload=? WHERE token=?", (json.dumps(payload), token))
                self.store.state(p["id"], "waiting_reference", p["turn"])
                if task:
                    self.store.task_state(task["id"], "waiting_reference")
                number = self.store.db.execute("SELECT COUNT(*) FROM requests WHERE project=? AND turn=? AND kind IN ('reference','helper_reference')", (p["id"], p["turn"])).fetchone()[0]
                self.notify(p["id"], f"🎨 Reference #{number}. The tool call is waiting for your decision.", [[("✅ Accept", f"r:{token}:accept"), ("🔄 Redo", f"r:{token}:redo"), ("✏️ Edits", f"r:{token}:edit")]], filename)
            elif method == "serverRequest/resolved":
                for token, req in list(self.requests.items()):
                    if req["rid"] == params["requestId"]:
                        self.store.db.execute("UPDATE requests SET state='resolved',payload='{}' WHERE token=?", (token,))
                        self.requests.pop(token, None)
                left = self.store.db.execute("SELECT COUNT(*) FROM requests WHERE project=? AND turn=? AND state IN ('pending','sending','editing') AND kind IN ('approval','question','reference')", (p["id"], p["turn"])).fetchone()[0]
                if not left and p["state"].startswith("waiting"):
                    self.store.state(p["id"], "running", p["turn"])
                    if task:
                        self.store.task_state(task["id"], "running")
            elif method == "item/completed" and params.get("item", {}).get("type") == "dynamicToolCall":
                call_id = params["item"]["id"]
                for token in list(self.requests):
                    r = self.store.db.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
                    if r and r["kind"] == "reference" and json.loads(r["payload"]).get("params", {}).get("callId") == call_id:
                        self.store.db.execute("UPDATE requests SET state='resolved',payload='{}' WHERE token=?", (token,))
                        self.requests.pop(token, None)
                        self.store.state(p["id"], "running", p["turn"])
                        if task:
                            self.store.task_state(task["id"], "running")
            elif method == "item/started":
                item_type = params.get("item", {}).get("type")
                labels = {"commandExecution": "🔧 Running a command", "fileChange": "📝 Editing files", "webSearch": "🔎 Searching", "mcpToolCall": "🔧 Calling a tool"}
                label = labels.get(item_type)
                prev, when = self.progress.get(p["id"], ("", 0))
                if label and label != prev and time.time() - when > 20:
                    self.notify(p["id"], label)
                    self.progress[p["id"]] = label, time.time()

    async def dispatch(self):
        while not self.stop.is_set():
            async with self.mutex:
                expired = self.store.db.execute("SELECT token,project,kind FROM requests WHERE expires<? AND state IN ('pending','editing','sending')", (time.time(),)).fetchall()
                for r in expired:
                    self.store.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE token=?", (r["token"],))
                    self.notify(r["project"], "⌛ Request "+r["kind"]+" expired. Nothing was approved automatically; answer locally if Codex is waiting.")
                if self.rpc.connected:
                    for c in self.config.projects:
                        p = self.store.project(c.id)
                        if p["state"] == "stopped" and not p["thread"] and self.store.db.execute("SELECT 1 FROM tasks WHERE project=? AND state='queued'", (p["id"],)).fetchone():
                            try:
                                await self.start_project(p["id"])
                            except Exception:
                                self.store.state(p["id"], "unknown")
                                self.notify(p["id"], "Could not create a session. Check locally.")
                            p = self.store.project(c.id)
                        task = self.store.claim(c.id)
                        if task is None:
                            continue
                        stage = "preflight"
                        try:
                            self.check_project(p)
                            # Native queue does not steer a local turn if the user wins the race.
                            stage = "queue"
                            added = await self.rpc.call("thread/queue/add", {"threadId": p["thread"], "clientUserMessageId": task["id"], "input": text_input(task["text"])})
                            stage = "receipt"
                            queued_id = added["queuedSubmission"]["id"]
                            self.store.setting(f"submission:{task['id']}", queued_id)
                            # Codex's native queue pump starts the head automatically when idle.
                            # Bind its turn only after a userMessage event echoes our clientId.
                            self.store.state(c.id, "dispatching")
                        except Exception as error:
                            code = error.details.get("code") if isinstance(error, RPCError) else None
                            diagnostic = {"stage": stage, "error": type(error).__name__, "code": code if type(code) is int else None}
                            self.store.setting(f"delivery:{task['id']}", json.dumps(diagnostic))
                            print(f"Task delivery failed: project={c.id} stage={stage} error={diagnostic['error']} code={diagnostic['code']}", flush=True)
                            self.store.task_state(task["id"], "unknown")
                            self.store.state(c.id, "unknown")
                            self.notify(c.id, "⚠️ Task delivery not confirmed. Automatic retry disabled; local review required.")
            await asyncio.sleep(.5)

    async def run(self):
        if await probe_codex("--version") != "codex-cli 0.160.1":
            raise Rejected("Protocol tested on Codex 0.160.1. Recheck compatibility after updating")
        if "ChatGPT" not in await probe_codex("login", "status"):
            raise Rejected("Existing Codex ChatGPT authentication required; no API key is used")
        for p in self.config.projects:
            self.store.add_project(p)
        self.store.recover()
        endpoint = self.config.runtime / "codex.sock"
        if endpoint.exists() or endpoint.is_symlink():
            info = endpoint.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise Rejected("Unsafe existing App Server socket")
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.settimeout(1)
                probe.connect(str(endpoint))
            except ConnectionRefusedError:
                endpoint.unlink()
            else:
                raise Rejected("App Server is already running. A competing process will not be started")
            finally:
                probe.close()
        env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "CODEX_API_KEY"}}
        self.process = await asyncio.create_subprocess_exec("codex", "app-server", "--listen", f"unix://{endpoint}", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="user"', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        ipc_server = local_server = dispatcher = None
        try:
            for _ in range(100):
                if endpoint.exists():
                    break
                if self.process.returncode is not None:
                    raise Rejected("Codex App Server stopped during startup")
                await asyncio.sleep(.1)
            os.chmod(endpoint, 0o600)
            await self.rpc.connect()
            for p in self.store.projects():
                if p["id"] in {c.id for c in self.config.projects} and p["thread"]:
                    try:
                        await self.reconcile(p["id"])
                    except Exception:
                        self.notify(p["id"], "Saved session unavailable. A new session will not be created automatically.")
            private_dir(self.config.runtime / "gateway")
            ipc_server = await serve(self.config.runtime / "gateway/gateway.sock", self.handle)
            local_server = await serve(self.config.runtime / "local.sock", lambda op, data: self.handle(op, data, local=True))
            dispatcher = asyncio.create_task(self.dispatch())
            retries = 0
            while not self.stop.is_set():
                if self.process.returncode is not None:
                    raise Rejected("Codex stopped; systemd will restart the service")
                if not self.rpc.connected:
                    await self.rpc.close()
                    try:
                        await self.rpc.connect()
                        retries = 0
                        async with self.mutex:
                            self.epoch = uuid.uuid4().hex
                            for p in self.store.projects():
                                if p["thread"]:
                                    await self.reconcile(p["id"])
                                    self.notify(p["id"], "Connection restored. Check /status; uncertain tasks remain blocked.")
                    except Exception:
                        retries += 1
                        if retries >= 6:
                            raise Rejected("Could not reconnect to Codex; restart required") from None
                        try:
                            await asyncio.wait_for(self.stop.wait(), min(2 ** retries, 30))
                        except TimeoutError:
                            pass
                await asyncio.sleep(1)
        finally:
            if dispatcher:
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)
            for s in (ipc_server, local_server):
                if s:
                    s.close()
                    await s.wait_closed()
            await self.rpc.close()
            await self.terminal.stop()
            if self.process.returncode is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 10)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            for name in ("codex.sock", "gateway.sock", "local.sock"):
                (self.config.runtime / name).unlink(missing_ok=True)
            (self.config.runtime / "gateway/gateway.sock").unlink(missing_ok=True)
            self.store.db.close()
