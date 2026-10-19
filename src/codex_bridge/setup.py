import asyncio
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from .config import CONFIG, DATA, STATE, load_controller, load_gateway, runtime_dir, validate_token
from .security import Rejected, authorized, identifier, private_dir, read_private
from .telegram import BotAPI, TelegramError
from .shell import configure_shell

SUPPORTED_CODEX = "codex-cli 0.160.1"


def write_new(path: Path, text: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def doctor() -> bool:
    ok = True
    for tool in ("codex", "bwrap", "ffmpeg", "ffprobe", "prlimit"):
        found = shutil.which(tool)
        print(f"{tool}: {'found' if found else 'missing'}")
        ok &= bool(found)
    if shutil.which("codex"):
        version = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=15)
        print("Codex:", version.stdout.strip())
        if version.stdout.strip() != SUPPORTED_CODEX:
            print("The protocol is tested only with 0.160.1; verify other versions separately.")
            ok = False
        login = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
        chatgpt = login.returncode == 0 and "ChatGPT" in login.stdout + login.stderr
        print("ChatGPT authentication:", "available" if chatgpt else "not confirmed")
        ok &= chatgpt
    print("tmux:", "found" if shutil.which("tmux") else "missing; /clear unavailable")
    whisper = shutil.which("whisper-cli") or (DATA / "bin/whisper-cli").is_file()
    print("whisper.cpp:", "found" if whisper else "missing; text tasks are available")
    print("GPU: Whisper uses CPU by default; CUDA is not enabled automatically.")
    osinfo = Path("/etc/os-release").read_text()
    print("Arch/CachyOS:", "yes" if "arch" in osinfo.lower() or "cachyos" in osinfo.lower() else "another OS; separate verification required")
    return ok


async def verify_telegram(config: dict):
    api = BotAPI(config["token"])
    try:
        try:
            me = await api.call("getMe", {})
        except TelegramError as error:
            raise Rejected(f"Telegram getMe: token verification failed (code {error.code}). Check the new token entered from BotFather.") from None
        if not me.get("is_bot"):
            raise Rejected("The token does not belong to a Telegram bot")
        for attempt in range(2):
            try:
                chat = await api.call("getChat", {"chat_id": config["user_id"]})
                break
            except TelegramError as error:
                if error.code != 400:
                    raise Rejected(f"Telegram getChat: private chat verification failed (code {error.code}).") from None
                if attempt:
                    raise Rejected("Telegram getChat: private chat is still unavailable. Check the numeric User ID and send /start to the bot associated with this token.") from None
                print(f"Token accepted. Private chat is unavailable for the configured User ID {config['user_id']}.")
                username = me.get("username", "")
                print(f"Open @{username} in Telegram and send /start from your account." if username else "Open the bot associated with the token in Telegram and send /start from your account.")
                input("After sending /start, press Enter to check again: ")
        if chat.get("type") != "private" or chat.get("id") != config["user_id"]:
            raise Rejected("User ID does not identify a private chat")
        print("Telegram: connection and private chat verified")
    finally:
        await api.session.close()


async def pair(token: str) -> int:
    api = BotAPI(token)
    try:
        me = await api.call("getMe", {})
        challenge = "/pair_" + secrets.token_hex(12)
        print(f"Open @{me['username']} and send this code: {challenge}")
        deadline = asyncio.get_running_loop().time() + 180
        offset = 0
        while asyncio.get_running_loop().time() < deadline:
            updates = await api.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                m = u.get("message", {})
                uid = m.get("from", {}).get("id")
                if m.get("text") == challenge and authorized(m, uid):
                    print("Verified numeric User ID:", uid)
                    if input("Is this your ID? Save it [y/N]: ").lower() != "y":
                        raise Rejected("Setup cancelled")
                    return uid
        raise Rejected("Local pairing timed out")
    finally:
        await api.session.close()


def download_model() -> Path:
    target_dir = DATA / "models"
    private_dir(target_dir)
    target = target_dir / "ggml-small.bin"
    if target.exists():
        return target
    print("Multilingual small: approximately 466 MB. Benchmark speed and quality using your own recording.")
    if input("Download the model from the official whisper.cpp repository on Hugging Face [y/N]: ").lower() != "y":
        raise Rejected("Model not downloaded")
    import hashlib
    info_url = "https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main?recursive=false&expand=false"
    info = json.loads(urllib.request.urlopen(info_url, timeout=30).read())
    entry = next(x for x in info if x.get("path") == "ggml-small.bin")
    expected = entry["lfs"]["oid"]
    temporary = target.with_suffix(".part")
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin", timeout=60) as response:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                count = 0
                while chunk := response.read(1024**2):
                    count += len(chunk)
                    if count > 600 * 1024**2:
                        raise Rejected("Model exceeds the expected size")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != expected:
            raise Rejected("Model SHA256 does not match repository metadata")
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def controller_text(projects: list[dict], uid: int, mirror: bool = False) -> str:
    result = f"user_id = {uid}\nmirror_results = {str(mirror).lower()}\n"
    for p in projects:
        result += "\n[[projects]]\n"+"\n".join(f"{k} = {json.dumps(p[k], ensure_ascii=False)}" for k in ("id", "name", "path"))+"\n"
    return result


def add_project(config_path: Path, pid: str, name: str, project: Path):
    identifier(pid)
    existing = tomllib.loads(read_private(config_path).decode())
    projects = existing["projects"]
    if any(p["id"] == pid for p in projects):
        raise Rejected("Project ID already exists")
    projects.append({"id": pid, "name": name, "path": str(project.resolve(strict=True))})
    temp = config_path.with_name("controller.new.toml")
    try:
        write_new(temp, controller_text(projects, existing["user_id"], existing.get("mirror_results", False)))
        load_controller(temp)
        temp.replace(config_path)
    finally:
        temp.unlink(missing_ok=True)


def install_units():
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    for name, role, extra in (("codex-telegram-controller", "controller", ""), ("codex-telegram-bridge", "gateway", "Wants=codex-telegram-controller.service\nAfter=codex-telegram-controller.service\n")):
        unit = units / (name+".service")
        if unit.exists():
            print(f"Unit {name} already exists; keeping it")
            continue
        executable = json.dumps(sys.executable.replace("%", "%%"))
        runtime = "RuntimeDirectory=codex-telegram-bridge\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=restart\n" if role == "controller" else ""
        text = f"[Unit]\nDescription=Codex Telegram {role}\n{extra}StartLimitIntervalSec=120\nStartLimitBurst=5\n\n[Service]\nType=simple\nExecStart={executable} -m codex_bridge {role}\nEnvironment=PATH=%h/.local/bin:/usr/bin\nUMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=20\nKillMode=control-group\nNoNewPrivileges=yes\nLimitCORE=0\n{runtime}MemoryMax={'8G' if role == 'controller' else '6G'}\nTasksMax=256\n\n[Install]\nWantedBy=default.target\n"
        write_new(unit, text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


async def wizard():
    if not doctor():
        raise Rejected("Install missing dependencies and verify Codex; configuration was not changed")
    for path in (CONFIG, STATE, STATE / "gateway", DATA, DATA / "outbox", runtime_dir(), runtime_dir() / "gateway"):
        private_dir(path)
    if (CONFIG / "controller.toml").exists() and (CONFIG / "gateway.toml").exists():
        config = load_gateway(CONFIG / "gateway.toml")
        load_controller(CONFIG / "controller.toml")
        await verify_telegram(config)
        install_units()
        configure_shell()
        print("Existing setup verified; secrets and projects preserved")
        return
    existing = None
    if (CONFIG / "controller.toml").exists():
        load_controller(CONFIG / "controller.toml")
        existing = tomllib.loads(read_private(CONFIG / "controller.toml").decode())
    elif (CONFIG / "gateway.toml").exists():
        raise Rejected("controller.toml is missing; existing gateway.toml was not overwritten")
    token = getpass.getpass("Enter your Telegram Bot Token locally (input hidden): ").strip()
    validate_token(token)
    if existing:
        uid = existing["user_id"]
        print("Using your previously configured numeric User ID")
    else:
        raw_id = input("Your numeric Telegram User ID (Enter for one-time pairing): ").strip()
        uid = int(raw_id) if raw_id else await pair(token)
    config = {"token": token, "user_id": uid}
    if type(uid) is not int or uid <= 0:
        raise Rejected("A positive numeric Telegram User ID is required")
    await verify_telegram(config)
    projects = existing["projects"] if existing else []
    mirror = existing.get("mirror_results", False) if existing else input("Send Codex final replies and generated images to Telegram [y/N]: ").strip().lower() == "y"
    while not existing:
        pid = identifier(input("Project ID (ASCII, for example tools): ").strip())
        name = input("Project name: ").strip()
        path = str(Path(input("Local project directory: ").strip()).expanduser().resolve(strict=True))
        projects.append({"id": pid, "name": name, "path": path})
        if input("Add another project [y/N]: ").lower() != "y":
            break
    default_whisper = DATA / "bin/whisper-cli"
    whisper = str(default_whisper) if default_whisper.is_file() else input("Path to whisper-cli (Enter to disable voice for now): ").strip()
    if whisper:
        executable = str(Path(whisper).expanduser().resolve(strict=True))
        default_model = DATA / "models/ggml-small.bin"
        model_path = str(default_model) if default_model.is_file() else input("Path to a multilingual ggml model (Enter for small): ").strip()
        model = Path(model_path).expanduser().resolve(strict=True) if model_path else download_model()
        if model.name.endswith(".en.bin"):
            raise Rejected("A multilingual model is required; do not use .en")
        config.update(whisper=executable, whisper_model=str(model))
    candidate = CONFIG / "controller.new.toml"
    try:
        if not existing:
            write_new(candidate, controller_text(projects, uid, mirror))
            load_controller(candidate)
            candidate.rename(CONFIG / "controller.toml")
        write_new(CONFIG / "gateway.toml", "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in config.items())+"\n")
    finally:
        candidate.unlink(missing_ok=True)
    install_units()
    configure_shell()
    print("Setup complete. Start: systemctl --user enable --now codex-telegram-bridge")
    print("After your first message: codex-tg PROJECT_ID. Live check: codex-bridge smoke PROJECT_ID")
