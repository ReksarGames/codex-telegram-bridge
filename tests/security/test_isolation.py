import json
import subprocess
import sys
from pathlib import Path
import pytest
from codex_bridge import isolation

@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(),reason='bubblewrap missing')
def test_actual_gateway_namespace_cannot_read_host(tmp_path,monkeypatch):
    config=tmp_path/'config';data=tmp_path/'data';state=tmp_path/'state';runtime=tmp_path/'runtime'
    for p in [config,data/'outbox',state/'gateway',runtime/'gateway']:p.mkdir(parents=True,mode=0o700)
    (config/'gateway.toml').write_text('token="fake"');(config/'gateway.toml').chmod(0o600)
    forbidden=[]
    for name in ['.ssh/id_rsa','.aws/credentials','.codex/auth.json','project/.env','config/controller.toml']:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('PRIVATE_HOST_DATA');forbidden.append(str(p))
    (data/'outbox/image.png').write_bytes(b'allowed')
    monkeypatch.setattr(isolation,'CONFIG',config);monkeypatch.setattr(isolation,'DATA',data);monkeypatch.setattr(isolation,'STATE',state);monkeypatch.setattr(isolation,'runtime_dir',lambda:runtime)
    cmd=isolation.gateway_command({});end=cmd.index('--');cmd=cmd[:end+1]+[sys.executable,'-c',f'''
from pathlib import Path
import aiohttp
from codex_bridge import security
for p in {forbidden!r}:
    try: Path(p).read_bytes()
    except (OSError,PermissionError): pass
    else: raise AssertionError('host file readable')
assert Path({str(data/'outbox/image.png')!r}).read_bytes()==b'allowed'
try: Path({str(data/'outbox/new.png')!r}).write_bytes(b'bad')
except OSError: pass
else: raise AssertionError('outbox writable')
Path({str(state/'gateway/check')!r}).write_text('STATE_OK')
print('ISOLATION_OK')
''']
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ISOLATION_OK'
    assert (state/'gateway/check').read_text()=='STATE_OK'
