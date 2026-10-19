import time
import pytest
from conftest import activate,update
from codex_bridge.security import Rejected,safe_text

@pytest.mark.parametrize('uid,chat',[(1,'private'),(123456,'group'),(123456,'supergroup'),(123456,'channel')])
@pytest.mark.parametrize('callback',[None,'p:b'])
async def test_unauthorized(controller,uid,chat,callback):
    with pytest.raises(Rejected): await controller.handle('update',{'update':update('/new',uid=uid,chat_type=chat,callback=callback)})
    assert not controller.rpc.calls;assert not controller.store.db.execute('SELECT 1 FROM updates').fetchone()

async def approval(c):
    await c.on_event({'id':42,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-a','turnId':'turn-1','itemId':'cmd','command':'printf safe','reason':'test'}})
    return c.store.db.execute('SELECT token FROM requests ORDER BY rowid DESC LIMIT 1').fetchone()[0]

async def test_approval_once_origin_project(controller):
    c=controller;activate(c);token=await approval(c)
    await c.handle('update',{'update':update(number=1,callback='p:b')});await c.handle('update',{'update':update(number=2,callback=f'a:{token}:yes')})
    assert c.rpc.responses==[(42,{'decision':'accept'})]
    with pytest.raises(Rejected): await c.handle('update',{'update':update(number=3,callback=f'a:{token}:yes')})
    assert len(c.rpc.responses)==1

async def test_expired_forged_new_session(controller):
    c=controller;activate(c);token=await approval(c)
    with pytest.raises(Rejected): await c.callback('a:forged:yes')
    c.store.db.execute('UPDATE requests SET expires=0 WHERE token=?',(token,))
    with pytest.raises(Rejected): await c.callback(f'a:{token}:yes')
    c.store.db.execute('UPDATE requests SET expires=? WHERE token=?',(time.time()+100,token));c.store.bind('a',{'id':'different','sessionId':'different'})
    with pytest.raises(Rejected): await c.callback(f'a:{token}:yes')
    assert not c.rpc.responses

async def test_local_resolution(controller):
    c=controller;activate(c);token=await approval(c)
    await c.on_event({'method':'serverRequest/resolved','params':{'threadId':'thread-a','requestId':42}})
    with pytest.raises(Rejected): await c.callback(f'a:{token}:yes')
    assert not c.rpc.responses

async def test_failure_unconfirmed(controller):
    c=controller;activate(c);token=await approval(c);c.rpc.fail='respond'
    with pytest.raises(Rejected): await c.callback(f'a:{token}:yes')
    assert c.store.project('a')['state']=='unknown';assert not c.rpc.responses

async def test_permission_scope(controller):
    c=controller;activate(c);permissions={'network':{'enabled':True}}
    await c.on_event({'id':71,'method':'item/permissions/requestApproval','params':{'threadId':'thread-a','turnId':'turn-1','itemId':'net','permissions':permissions}})
    token=c.store.db.execute('SELECT token FROM requests').fetchone()[0];await c.callback(f'a:{token}:yes')
    assert c.rpc.responses[-1]==(71,{'permissions':permissions,'scope':'turn'})

async def test_question_not_new_task(controller):
    c=controller;activate(c)
    await c.on_event({'id':11,'method':'item/tool/requestUserInput','params':{'threadId':'thread-a','turnId':'turn-1','questions':[{'id':'choice','question':'Какой вариант?','options':[{'label':'Первый'}]}]}})
    await c.handle('update',{'update':update('Ответ кириллицей')})
    assert c.rpc.responses==[(11,{'answers':{'choice':{'answers':['Ответ кириллицей']}}})]
    assert c.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1

async def test_no_gateway_admin(controller):
    for op in ['migrate','reconcile','local_info','reference_begin','reference_poll','reference_decide']:
        with pytest.raises(Rejected): await controller.handle(op,{'project':'a'})

def test_redaction():
    fake='123456789:'+'A'*35;result=safe_text(f'token={fake}\nAPI_KEY=sk-'+('x'*30)+'\nAuthorization: BearerSensitive\x1b[31m')
    assert fake not in result and 'sk-' not in result and 'BearerSensitive' not in result and '\x1b' not in result

async def test_text_cannot_configure_sandbox(controller):
    text='use --yolo --dangerously-bypass-approvals-and-sandbox; $(touch /tmp/pwn)'
    await controller.handle('update',{'update':update(text)})
    assert not controller.rpc.calls;assert controller.store.db.execute('SELECT text FROM tasks').fetchone()[0]==text
async def test_session_only_approval_is_local_only(controller):
    from conftest import activate
    c=controller;activate(c)
    await c.on_event({'id':70,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-a','turnId':'turn-1','command':'true','availableDecisions':['acceptForSession','decline']}})
    assert c.store.project('a')['state']=='waiting_approval'
    assert c.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==0
    assert not c.rpc.responses
async def test_replayed_server_request_does_not_create_second_button(controller):
    from conftest import activate
    c=controller;activate(c)
    event={'id':71,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-a','turnId':'turn-1','command':'true'}}
    await c.on_event(event);await c.on_event(event)
    assert c.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==1
    assert c.store.db.execute('SELECT COUNT(*) FROM notices').fetchone()[0]==1


async def test_local_reference_api_cannot_approve_commands(controller):
    from conftest import activate
    c=controller;activate(c)
    await c.on_event({'id':72,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread-a','turnId':'turn-1','command':'true'}})
    token=c.store.db.execute('SELECT token FROM requests').fetchone()[0]
    with pytest.raises(Rejected):await c.handle('reference_decide',{'project':'a','token':token,'decision':'yes'},local=True)
    assert not c.rpc.responses
