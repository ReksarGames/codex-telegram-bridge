"""Run an OGG sample through the actual Gateway and nested media namespaces."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_bridge import isolation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ogg", type=Path)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--whisper", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    config = {"whisper": str(args.whisper.resolve()), "whisper_model": str(args.model.resolve())}
    with tempfile.TemporaryDirectory(prefix="ctb-voice-check-") as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        isolation.CONFIG, isolation.DATA, isolation.STATE = root/"config", root/"data", root/"state"
        isolation.runtime_dir = lambda: root/"runtime"
        for p in [isolation.CONFIG, isolation.DATA/"outbox", isolation.STATE/"gateway", root/"runtime/gateway"]:
            p.mkdir(parents=True, mode=0o700)
        (isolation.CONFIG/"gateway.toml").write_text("# No Telegram credentials in this experiment\n")
        source = isolation.STATE/"gateway/sample.ogg"
        source.write_bytes(args.ogg.read_bytes())
        command = isolation.gateway_command(config)
        code = f"""
import asyncio,json,time
from pathlib import Path
from codex_bridge.voice import Whisper
async def main():
    start=time.monotonic()
    text=await Whisper({config!r}).transcribe(Path({str(source)!r}).read_bytes(),{args.duration})
    print(json.dumps({{'seconds':round(time.monotonic()-start,2),'text':text,'nested_isolation':True}},ensure_ascii=False))
asyncio.run(main())
"""
        command = command[:command.index("--")+1]+[sys.executable, "-c", code]
        result = subprocess.run(command, capture_output=True, text=True, timeout=290)
        if result.returncode:
            raise SystemExit("Local voice namespace check failed; inspect dependencies locally")
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
