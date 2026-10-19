import fcntl
import os
import re
import stat
from pathlib import Path


class Rejected(ValueError):
    """Safe error message: no credentials, user text, or arbitrary file paths."""


def private_dir(path: Path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise Rejected("Private directory must belong to you and have permissions 0700")


def read_private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1 or s.st_size > 128 * 1024:
            raise Rejected("Config must be a regular file with permissions 0600")
        return os.read(fd, 128 * 1024 + 1)
    finally:
        os.close(fd)


def authorized(message: dict, uid: int) -> bool:
    return (type(uid) is int and message.get("from", {}).get("id") == uid
            and message.get("chat", {}).get("type") == "private"
            and message.get("chat", {}).get("id") == uid)


def authorized_update(update: dict, uid: int) -> bool:
    if "callback_query" in update:
        c = update["callback_query"]
        return c.get("from", {}).get("id") == uid and authorized({**c.get("message", {}), "from": c.get("from", {})}, uid)
    return authorized(update.get("message", {}), uid)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", value):
        raise Rejected("Invalid identifier")
    return value


def safe_text(value: str, limit: int = 1800) -> str:
    value = re.sub(r"-----BEGIN [\s\S]*?-----END [^-]+-----", "[REDACTED]", str(value))
    value = re.sub(r"\b\d{7,12}:[A-Za-z0-9_-]{25,}\b|\b(?:sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    value = re.sub(r"(?im)(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value[:limit]


def lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Rejected("Another instance is already running") from None
    return fd
