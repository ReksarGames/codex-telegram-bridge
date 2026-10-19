"""Bounded schema-checked IPC; peer must have the controller's Linux UID."""
import asyncio
import json
import os
import socket
import struct
from pathlib import Path

from .security import Rejected

MAX_FRAME = 128 * 1024
OPERATIONS = {"update", "events", "ack", "context", "voice", "voice_failed", "local_info", "reconcile", "migrate", "reference_decide", "reference_begin", "reference_poll"}


def validate(request: dict):
    if not isinstance(request, dict) or request.get("op") not in OPERATIONS or set(request) - {"op", "data"}:
        raise Rejected("Invalid IPC request")
    if not isinstance(request.get("data", {}), dict):
        raise Rejected("Invalid IPC data")


async def serve(path: Path, handler):
    if path.exists() or path.is_symlink():
        path.unlink()
    async def client(reader, writer):
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid != os.getuid():
                raise Rejected("IPC: wrong user")
            raw = await asyncio.wait_for(reader.readline(), 5)
            if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                raise Rejected("IPC: message exceeds size limit")
            request = json.loads(raw)
            validate(request)
            result = await handler(request["op"], request.get("data", {}))
            response = {"ok": True, "result": result}
        except Rejected as error:
            response = {"ok": False, "error": str(error)}
        except Exception:
            response = {"ok": False, "error": "Operation did not complete; check status locally"}
        try:
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_unix_server(client, str(path), limit=MAX_FRAME + 1)
    os.chmod(path, 0o600)
    return server


async def call(path: Path, op: str, data: dict | None = None, *, timeout: float = 45):
    request = {"op": op, "data": data or {}}
    validate(request)
    raw = json.dumps(request, ensure_ascii=False).encode() + b"\n"
    if len(raw) > MAX_FRAME:
        raise Rejected("IPC: message exceeds size limit")
    reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_FRAME + 1)
    try:
        writer.write(raw)
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout))
        if not response["ok"]:
            raise Rejected(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()
