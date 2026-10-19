from aiohttp import ClientSession, web

from codex_bridge.rpc import CodexRPC


async def test_rpc_receives_over_16_mib_and_keeps_connection(tmp_path,monkeypatch):
    limits = []
    original = ClientSession.ws_connect
    def connect(session,*args,**kwargs):
        limits.append(kwargs['max_msg_size'])
        return original(session,*args,**kwargs)
    monkeypatch.setattr(ClientSession,'ws_connect',connect)
    payload = 'x'*(17*1024**2)
    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for frame in socket:
            message = frame.json()
            if 'id' not in message: continue
            result = {'payload':payload} if message['method'] == 'large/result' else {'ok':True}
            await socket.send_json({'id':message['id'],'result':result})
        return socket
    app = web.Application()
    app.router.add_get('/',handler)
    runner = web.AppRunner(app)
    await runner.setup()
    path = tmp_path/'codex.sock'
    await web.UnixSite(runner,str(path)).start()
    async def ignore(event): pass
    rpc = CodexRPC(path,ignore)
    try:
        await rpc.connect()
        result = await rpc.call('large/result',{})
        assert len(result['payload']) == len(payload)
        assert limits == [128*1024**2]
        assert await rpc.call('small/result',{}) == {'ok':True}
        assert rpc.connected and not rpc.pending
    finally:
        await rpc.close()
        await runner.cleanup()
