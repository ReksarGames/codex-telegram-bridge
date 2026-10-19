"""Official Bot API only, outgoing HTTPS and long polling. Never log URL/errors."""
import asyncio
import io
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import aiohttp

from . import ipc
from .references import read_export
from .security import Rejected, authorized_update, safe_text
from .voice import Whisper

BOT_COMMANDS = [
    {"command": command, "description": description}
    for command, description in (
        ("start", "Main menu and help"),
        ("projects", "Select a project"),
        ("project", "Show the selected project"),
        ("status", "Live Codex activity and queue"),
        ("queue", "Show queued tasks"),
        ("sessions", "Project session history"),
        ("pause", "Pause dispatching queued tasks"),
        ("resume", "Resume the task queue"),
        ("cancel", "Request cancellation of the active task"),
        ("new", "Start a new chat after confirmation"),
        ("clear", "Clear chat: currently available locally"),
        ("codex_status", "How to view native Codex status locally"),
        ("help", "Commands and available features"),
    )
]


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class BotAPI:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45), trust_env=False)
        self.send_lock = asyncio.Lock()
        self.last_send = 0.0

    async def call(self, method: str, params: dict, photo: bytes | None = None):
        for attempt in range(6):
            try:
                if photo is None:
                    response = await self.session.post(self.base + method, json=params, allow_redirects=False)
                else:
                    form = aiohttp.FormData()
                    for key, value in params.items():
                        form.add_field(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value))
                    form.add_field("photo", io.BytesIO(photo), filename="reference.png", content_type="image/png")
                    response = await self.session.post(self.base + method, data=form, allow_redirects=False)
                async with response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024**2:
                            raise TelegramError("Telegram: oversized response")
                    body = json.loads(raw)
                    if body.get("ok"):
                        return body["result"]
                    code = body.get("error_code", response.status)
                    if code == 429:
                        delay = min(60, max(1, int(body.get("parameters", {}).get("retry_after", 1))))
                    elif code >= 500:
                        delay = min(30, 2**attempt)
                    else:
                        raise TelegramError(f"Telegram API error {code}", code)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                delay = min(30, 2**attempt)
            if attempt < 5:
                await asyncio.sleep(delay)
        raise TelegramError("Telegram: retry limit")

    async def send(self, chat: int, body: dict, photo=None, edit_id=None):
        async with self.send_lock:
            now = asyncio.get_running_loop().time()
            await asyncio.sleep(max(0, self.last_send + 1.1 - now))
            params = {"chat_id": chat, "disable_web_page_preview": True}
            if body.get("buttons"):
                params["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in body["buttons"]]}
            if photo:
                params.pop("disable_web_page_preview", None)
                params["caption"] = body["text"][:1024]
                method = "sendPhoto"
            else:
                params["text"] = body["text"][:4000]
                method = "sendMessage"
                if edit_id:
                    method = "editMessageText"
                    params["message_id"] = edit_id
            try:
                return await self.call(method, params, photo)
            finally:
                self.last_send = asyncio.get_running_loop().time()

    async def download_voice(self, file_id: str) -> bytes:
        info = await self.call("getFile", {"file_id": file_id})
        path = info.get("file_path", "")
        if not re.fullmatch(r"voice/[A-Za-z0-9_-]+\.(oga|ogg|opus)", path) or info.get("file_size", 0) > 8 * 1024**2:
            raise Rejected("Telegram returned an invalid file")
        async with self.session.get(self.file_base+path, allow_redirects=False) as response:
            if response.status != 200:
                raise TelegramError("Telegram voice download failed")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > 8 * 1024**2:
                    raise Rejected("Voice file too large")
            return bytes(data)


class Gateway:
    def __init__(self, config: dict, socket: Path, outbox: Path, state: Path, api=None):
        self.config, self.socket, self.outbox = config, socket, outbox
        self.api = api or BotAPI(config["token"])
        self.whisper = Whisper(config)
        self.state = sqlite3.connect(state / "gateway.db")
        self.state.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.jobs: set[asyncio.Task] = set()
        self.stop = asyncio.Event()

    def setting(self, key: str, value=None):
        if value is not None:
            self.state.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.state.commit()
            return value
        r = self.state.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    async def process_update(self, update: dict):
        if not authorized_update(update, self.config["user_id"]):
            return
        try:
            result = await ipc.call(self.socket, "update", {"update": update})
        except Rejected as error:
            await self.api.send(self.config["user_id"], {"text": "📂 Bridge controls\n"+safe_text(str(error))})
            result = {}
        callback = update.get("callback_query")
        if callback:
            try:
                await self.api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Processed; the result will appear in chat"})
            except TelegramError:
                pass
        if result.get("voice"):
            if self.jobs:
                await ipc.call(self.socket, "voice_failed", {"token": result["voice"]["token"], "reason": "Whisper is busy; retry voice after processing finishes"})
            else:
                task = asyncio.create_task(self.voice(result["voice"]))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)

    async def voice(self, job: dict):
        try:
            audio = await self.api.download_voice(job["file_id"])
            text = await self.whisper.transcribe(audio, job["duration"])
            await ipc.call(self.socket, "voice", {"token": job["token"], "text": text})
        except (Rejected, TelegramError, OSError, TimeoutError, ValueError, KeyError):
            try:
                await ipc.call(self.socket, "voice_failed", {"token": job["token"], "reason": "Local voice transcription failed. Check whisper.cpp, model and limits; send text or retry voice."})
            except (Rejected, OSError):
                pass

    async def polling(self):
        attempt = 0
        while not self.stop.is_set():
            try:
                updates = await self.api.call("getUpdates", {"offset": int(self.setting("offset") or 0), "timeout": 25, "allowed_updates": ["message", "callback_query"]})
                for update in updates:
                    await self.process_update(update)
                    self.setting("offset", update["update_id"] + 1)
                attempt = 0
            except (OSError, TelegramError, TimeoutError) as error:
                if isinstance(error, TelegramError) and error.code in {401, 403, 409}:
                    raise Rejected(f"Telegram polling stopped: code {error.code}. Check the token, bot access and absence of another polling process") from None
                await asyncio.sleep(min(30, 2**min(attempt, 5)))
                attempt += 1

    async def delivery(self):
        while not self.stop.is_set():
            try:
                for body in await ipc.call(self.socket, "events"):
                    photo = None
                    if body.get("image"):
                        try:
                            photo = await asyncio.to_thread(read_export, self.outbox, body["image"])
                        except (Rejected, OSError, ValueError):
                            body = {**body, "text": body["text"].splitlines()[0]+"\n❌ Image unavailable or failed validation. For a pending reference, check locally or use /cancel.", "buttons": []}
                    key = "status:" + body.get("project", "bridge")
                    edit = self.setting(key) if not body.get("buttons") and not photo and not body.get("final") else None
                    digest = hashlib.sha256(body["text"].encode()).hexdigest()
                    if edit and self.setting(key+":digest") == digest:
                        await ipc.call(self.socket, "ack", {"id": body["id"], "message_id": int(edit)})
                        continue
                    try:
                        sent = await self.api.send(self.config["user_id"], body, photo, int(edit) if edit else None)
                    except TelegramError:
                        if not edit:
                            raise
                        self.setting(key, 0)
                        sent = await self.api.send(self.config["user_id"], body, photo)
                    message_id = sent.get("message_id") if isinstance(sent, dict) else None
                    if message_id and not body.get("buttons") and not photo and not body.get("final"):
                        self.setting(key, message_id)
                        self.setting(key+":digest", digest)
                    await ipc.call(self.socket, "ack", {"id": body["id"], "message_id": message_id})
            except FileNotFoundError:
                # A recreated host directory leaves the old bind mount without its socket.
                raise Rejected("Gateway lost the Controller socket; isolation restart required") from None
            except (OSError, TelegramError, Rejected, TimeoutError):
                await asyncio.sleep(2)
            await asyncio.sleep(.5)

    async def run(self):
        workers = []
        try:
            # Menu registration must not delay outgoing notifications at startup.
            workers = [asyncio.create_task(self.menu()), asyncio.create_task(self.polling()), asyncio.create_task(self.delivery()), asyncio.create_task(self.stop.wait())]
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            if self.stop.is_set():
                return
            for task in done:
                task.result()
                raise Rejected("Gateway worker stopped; restart required")
        finally:
            for task in workers + list(self.jobs):
                task.cancel()
            await asyncio.gather(*workers, *self.jobs, return_exceptions=True)
            await self.api.session.close()
            self.state.close()

    async def menu(self):
        await self.setup_menu()
        await self.stop.wait()

    async def setup_menu(self):
        await self.api.call("setMyCommands", {"commands": BOT_COMMANDS, "scope": {"type": "chat", "chat_id": self.config["user_id"]}})
        await self.api.call("setChatMenuButton", {"chat_id": self.config["user_id"], "menu_button": {"type": "commands"}})
