import asyncio
import json
import pytest
from codex_bridge import ipc
from codex_bridge.security import Rejected

async def test_bounded_unix_ipc(tmp_path):
    async def handler(op,data): return {'op':op,'value':data.get('value')}
    socket=tmp_path/'bridge.sock';server=await ipc.serve(socket,handler)
    try:
        assert socket.stat().st_mode & 0o777 == 0o600
        assert await ipc.call(socket,'context',{'value':'кириллица'})=={'op':'context','value':'кириллица'}
        with pytest.raises(Rejected): await ipc.call(socket,'exec',{'command':'rm -rf /'})
        with pytest.raises(Rejected): await ipc.call(socket,'context',{'value':'x'*200000})
        reader,writer=await asyncio.open_unix_connection(str(socket));writer.write(b'{"op":"exec","data":{}}\n');await writer.drain()
        assert not json.loads(await reader.readline())['ok'];writer.close();await writer.wait_closed()
    finally: server.close();await server.wait_closed()
