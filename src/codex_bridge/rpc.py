"""Versioned Codex JSON-RPC over a private Unix WebSocket, never TCP."""
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp


class RPCError(RuntimeError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class CodexRPC:
    def __init__(self, path: Path, on_event: Callable[[dict], Awaitable[None]]):
        self.path, self.on_event = path, on_event
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.counter = 0
        self.connected = False

    async def connect(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.path)))
        try:
            self.ws = await self.session.ws_connect("http://localhost/", max_msg_size=128 * 1024**2)
            self.connected = True
            self.reader = asyncio.create_task(self._read())
            self.consumer = asyncio.create_task(self._consume())
            await self.call("initialize", {"clientInfo": {"name": "codex_telegram_bridge", "title": "Telegram Bridge", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
            await self.ws.send_json({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def call(self, method: str, params: dict, timeout: float = 30) -> dict:
        if not self.connected:
            raise RPCError("Codex disconnected")
        self.counter += 1
        rid = f"bridge-{self.counter}"
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.ws.send_json({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)

    async def respond(self, rid: str | int, result: dict):
        if not self.connected:
            raise RPCError("Codex disconnected")
        await self.ws.send_json({"id": rid, "result": result})

    async def _read(self):
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                message = json.loads(frame.data)
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message:
                            # Protocol errors can contain file contents; never propagate raw text.
                            future.set_exception(RPCError(f"Codex RPC error {message['error'].get('code')}", message['error']))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self.events.put_nowait(message)
        except (ValueError, asyncio.QueueFull, aiohttp.ClientError):
            pass
        finally:
            self.connected = False
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RPCError("Codex disconnected"))
            await self.events.put({"method": "bridge/disconnected", "params": {}})

    async def _consume(self):
        while True:
            event = await self.events.get()
            try:
                await self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A malformed event must stop dispatch, never imply a successful turn.
                await self.on_event({"method": "bridge/disconnected", "params": {}})
                await self.ws.close()
                return

    async def close(self):
        self.connected = False
        for name in ("reader", "consumer"):
            task = getattr(self, name, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if hasattr(self, "session"):
            await self.session.close()
