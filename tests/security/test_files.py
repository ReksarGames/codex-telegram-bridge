import io
import os
import pytest
from PIL import Image
from conftest import activate,update
from codex_bridge.references import publish,read_export,open_beneath,normalized_png
from codex_bridge.security import Rejected,read_private

@pytest.mark.parametrize('name',['../image.png','/etc/passwd','.env','sub/../../image.png','~/.ssh/id_rsa'])
def test_traversal_and_sensitive_paths(tmp_path,name):
    with pytest.raises(Rejected): open_beneath(tmp_path,name)

def image(path): Image.new('RGB',(8,8),'blue').save(path,format='PNG')

def test_symlink_hardlink_and_directory(tmp_path):
    root=tmp_path/'root';root.mkdir();outside=tmp_path/'secret.png';image(outside)
    (root/'link.png').symlink_to(outside);os.link(outside,root/'hard.png');(root/'dir').symlink_to(tmp_path)
    for name in ['link.png','hard.png','dir/secret.png']:
        with pytest.raises(Rejected): open_beneath(root,name)
    with pytest.raises(Rejected): open_beneath(root,'dir')

def test_fifo_is_not_opened_blocking(tmp_path):
    os.mkfifo(tmp_path/'fifo.png')
    with pytest.raises(Rejected): open_beneath(tmp_path,'fifo.png')

def test_format_metadata_and_weird_filename(tmp_path):
    root=tmp_path/'root';out=tmp_path/'out';root.mkdir();out.mkdir();source=root/'x; $(touch hacked).png';image(source)
    name=publish(root,out,'project','nonce',source.name);raw=read_export(out,name)
    assert name=='project/project-nonce.png';assert raw[:8]==b'\x89PNG\r\n\x1a\n';assert not (root/'hacked').exists()
    (root/'fake.png').write_text('not a picture')
    with pytest.raises(Rejected): publish(root,out,'project','nonce2','fake.png')

@pytest.mark.parametrize('payload',[b'fake png',b'GIF89a',b'\xff\xd8\xff'])
def test_bad_format(payload):
    with pytest.raises(Rejected): normalized_png(payload)

def test_private_config(tmp_path):
    path=tmp_path/'secrets';path.write_text('fake');path.chmod(0o644)
    with pytest.raises(Rejected): read_private(path)
    path.chmod(0o600);assert read_private(path)==b'fake'
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError): read_private(link)

async def test_reference_gate_and_original_project(controller):
    c=controller;activate(c);image(c.config.projects[0].path/'ref.png')
    await c.on_event({'id':500,'method':'item/tool/call','params':{'threadId':'thread-a','turnId':'turn-1','callId':'ref-call','tool':'bridge_publish_reference','arguments':{'path':'ref.png'}}})
    token=c.store.db.execute("SELECT token FROM requests WHERE kind='reference'").fetchone()[0]
    assert c.store.project('a')['state']=='waiting_reference';assert not c.rpc.responses
    c.store.enqueue('a','next',None);assert c.store.claim('a') is None
    await c.handle('update',{'update':update(number=1,callback='p:b')})
    await c.handle('update',{'update':update(number=2,callback=f'r:{token}:accept')})
    assert c.rpc.responses[-1][0]==500;assert 'accept' in str(c.rpc.responses[-1][1])
    with pytest.raises(Rejected): await c.callback(f'r:{token}:accept')

async def test_reference_edits_voice_context(controller):
    c=controller;activate(c);image(c.config.projects[0].path/'ref.png')
    await c.on_event({'id':500,'method':'item/tool/call','params':{'threadId':'thread-a','turnId':'turn-1','callId':'ref-call','tool':'bridge_publish_reference','arguments':{'path':'ref.png'}}})
    token=c.store.db.execute("SELECT token FROM requests WHERE kind='reference'").fetchone()[0]
    await c.callback(f'r:{token}:edit');await c.handle('update',{'update':update('Сделай светлее')})
    assert 'Сделай светлее' in str(c.rpc.responses[-1]);assert c.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1

async def test_helper_is_blocked_until_review(controller):
    c=controller;activate(c);image(c.config.projects[0].path/'ref.png')
    response=await c.handle('reference_begin',{'project':'a','path':'ref.png'},local=True)
    result=await c.handle('reference_poll',{'project':'a','token':response['token']},local=True)
    assert result['decision'] is None;assert c.store.project('a')['state']=='waiting_reference'
    await c.callback(f"r:{response['token']}:redo")
    result=await c.handle('reference_poll',{'project':'a','token':response['token']},local=True)
    assert result['decision']=='redo'

def test_project_export_directory_cannot_escape(tmp_path):
    root=tmp_path/'root';out=tmp_path/'out';elsewhere=tmp_path/'elsewhere'
    root.mkdir();out.mkdir();elsewhere.mkdir();image(root/'ref.png')
    (out/'project').symlink_to(elsewhere,target_is_directory=True)
    with pytest.raises(Rejected):publish(root,out,'project','nonce','ref.png')
    assert list(elsewhere.iterdir())==[]

def test_export_collision_preserves_original(tmp_path):
    root=tmp_path/'root';out=tmp_path/'out';root.mkdir();out.mkdir();image(root/'ref.png')
    name=publish(root,out,'project','nonce','ref.png');original=read_export(out,name)
    with pytest.raises(FileExistsError):publish(root,out,'project','nonce','ref.png')
    assert read_export(out,name)==original

def test_palette_transparency_survives_safe_export():
    source=Image.new('P',(8,8));source.info['transparency']=0
    data=io.BytesIO();source.save(data,format='PNG')
    exported=Image.open(io.BytesIO(normalized_png(data.getvalue())))
    assert exported.mode=='RGBA' and exported.getpixel((0,0))[3]==0

async def test_expired_publication_is_not_delivered(controller):
    c=controller;activate(c);image(c.config.projects[0].path/'ref.png')
    await c.on_event({'id':500,'method':'item/tool/call','params':{'threadId':'thread-a','turnId':'turn-1','callId':'ref-call','tool':'bridge_publish_reference','arguments':{'path':'ref.png'}}})
    c.store.db.execute('UPDATE requests SET expires=0')
    assert await c.handle('events',{})==[]
    assert list((c.config.outbox/'a').iterdir())==[]
    assert c.store.project('a')['state']=='waiting_reference'
