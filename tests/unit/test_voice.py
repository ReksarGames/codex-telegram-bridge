import pytest
from conftest import activate,update
from codex_bridge.security import Rejected
from codex_bridge.voice import Whisper,subprocess_checked

async def test_voice_confirm_origin_context(controller):
    c=controller
    u=update(number=1);u['message'].pop('text');u['message']['voice']={'file_id':'voice-id','duration':5,'file_size':100}
    job=(await c.handle('update',{'update':u}))['voice']
    await c.callback('p:b')
    await c.handle('voice',{'token':job['token'],'text':'/cancel это только голосовой текст'})
    assert not c.store.db.execute('SELECT 1 FROM tasks').fetchone()
    await c.callback(f"v:{job['token']}:send")
    row=c.store.db.execute('SELECT project,text FROM tasks').fetchone()
    assert row['project']=='a';assert row['text'].startswith('/cancel');assert not c.rpc.calls
    with pytest.raises(Rejected): await c.callback(f"v:{job['token']}:send")

async def test_voice_reference_context_survives_switch(controller):
    c=controller;activate(c)
    from PIL import Image
    Image.new('RGB',(8,8)).save(c.config.projects[0].path/'ref.png')
    await c.on_event({'id':1,'method':'item/tool/call','params':{'threadId':'thread-a','turnId':'turn-1','callId':'ref','tool':'bridge_publish_reference','arguments':{'path':'ref.png'}}})
    token=c.store.db.execute("SELECT token FROM requests WHERE kind='reference'").fetchone()[0]
    u=update(number=1);u['message'].pop('text');u['message']['voice']={'file_id':'voice','duration':5,'file_size':100}
    job=(await c.handle('update',{'update':u}))['voice'];await c.callback('p:b')
    await c.handle('voice',{'token':job['token'],'text':'Измени цвет'})
    await c.callback(f"v:{job['token']}:send")
    assert 'Измени цвет' in str(c.rpc.responses[-1]);assert c.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1

@pytest.mark.parametrize('duration,size',[(121,100),(0,100),(5,9000000)])
async def test_voice_limits(controller,duration,size):
    u=update();u['message'].pop('text');u['message']['voice']={'file_id':'voice','duration':duration,'file_size':size}
    with pytest.raises(Rejected): await controller.handle('update',{'update':u})

async def test_missing_local_whisper_rejects():
    with pytest.raises(Rejected): await Whisper({}).transcribe(b'OggSOpusHead',1)

async def test_worker_timeout_reaps_child():
    with pytest.raises(TimeoutError): await subprocess_checked(['/usr/bin/sleep','5'],.01)
async def test_typed_voice_correction_is_not_a_privileged_command(controller):
    from conftest import update
    c=controller
    token=c.request(c.store.project('a'),'voice',{'context':c.context(c.store.project('a')),'text':'original'})
    await c.callback(f'v:{token}:edit')
    await c.handle('update',{'update':update('/pause',91)})
    assert not c.store.project('a')['paused']
    await c.callback(f'v:{token}:send')
    assert c.store.db.execute('SELECT text FROM tasks').fetchone()[0]=='/pause'


async def test_ambiguous_voice_corrections_cannot_become_a_new_task(controller):
    from conftest import update
    c=controller
    for pid in ['a','b']:
        p=c.store.project(pid)
        token=c.request(p,'voice',{'context':c.context(p),'text':'original'})
        await c.callback(f'v:{token}:edit')
    with pytest.raises(Rejected):await c.handle('update',{'update':update('correction',92)})
    assert c.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0
