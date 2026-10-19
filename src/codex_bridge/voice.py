import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path

from .security import Rejected


async def subprocess_checked(args: list[str], timeout: float, memory: int = 4 * 1024**3) -> bytes:
    process = await asyncio.create_subprocess_exec("prlimit", f"--as={memory}", "--cpu=300", "--", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode != 0 or len(output) > 256 * 1024:
            raise Rejected("Local audio processing failed")
        return output
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


def media_sandbox(work: Path, executable: str, model: Path | None = None) -> list[str]:
    command = ["bwrap", "--die-with-parent", "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(work), "/work", "--chdir", "/work"]
    executable_path = Path(executable).resolve(strict=True)
    if not executable_path.is_relative_to(Path("/usr")):
        command += ["--ro-bind", str(executable_path), "/media-tool"]
        target = "/media-tool"
    else:
        target = str(executable_path)
    if model:
        command += ["--ro-bind", str(model.resolve(strict=True)), "/model.bin"]
    return command + ["--", target]


class Whisper:
    def __init__(self, config: dict):
        self.config = config
        self.busy = False

    async def transcribe(self, audio: bytes, duration: int) -> str:
        if self.busy:
            raise Rejected("Whisper is busy. Retry the voice message after processing finishes")
        executable = self.config.get("whisper", "")
        model = Path(self.config.get("whisper_model", "/nonexistent"))
        if not executable or not model.is_file() or model.name.endswith(".en.bin"):
            raise Rejected("Configure local whisper.cpp and a multilingual model")
        if not 0 < duration <= 120 or not 0 < len(audio) <= 8 * 1024**2 or audio[:4] != b"OggS" or b"OpusHead" not in audio[:4096]:
            raise Rejected("Invalid OGG/Opus or audio limit exceeded")
        self.busy = True
        try:
            with tempfile.TemporaryDirectory(prefix="ctb-voice-") as tmp:
                root = Path(tmp)
                (root / "voice.ogg").write_bytes(audio)
                probe = await subprocess_checked(media_sandbox(root, "/usr/bin/ffprobe") + ["-v", "error", "-protocol_whitelist", "file,pipe", "-show_format", "-show_streams", "-of", "json", "/work/voice.ogg"], 15, 1024**3)
                metadata = json.loads(probe)
                streams = metadata.get("streams", [])
                if metadata["format"]["format_name"] != "ogg" or not 0 < float(metadata["format"]["duration"]) <= 120.5 or len(streams) != 1 or streams[0]["codec_name"] != "opus":
                    raise Rejected("Audio format or duration not allowed")
                await subprocess_checked(media_sandbox(root, "/usr/bin/ffmpeg") + ["-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", "/work/voice.ogg", "-t", "120", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "/work/voice.wav"], 30, 1024**3)
                await subprocess_checked(media_sandbox(root, executable, model) + ["-m", "/model.bin", "-f", "/work/voice.wav", "-l", "auto", "-ng", "-t", "4", "-otxt", "-of", "/work/transcript"], 240)
                result = (root / "transcript.txt").read_text().strip()
                if not result or len(result.encode()) > 64 * 1024:
                    raise Rejected("Whisper did not return valid text")
                return result
        finally:
            self.busy = False
