import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .security import Rejected, identifier, read_private

CONFIG = Path.home() / ".config/codex-telegram-bridge"
STATE = Path.home() / ".local/state/codex-telegram-bridge"
DATA = Path.home() / ".local/share/codex-telegram-bridge"


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path


@dataclass(frozen=True)
class ControllerConfig:
    projects: tuple[Project, ...]
    state: Path
    outbox: Path
    runtime: Path
    mirror_results: bool = False


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "codex-telegram-bridge"


def load_controller(path: Path) -> ControllerConfig:
    j = tomllib.loads(read_private(path).decode())
    if type(j.get("mirror_results", False)) is not bool:
        raise Rejected("mirror_results must be a boolean")
    projects = []
    for p in j.get("projects", []):
        raw = Path(p["path"])
        actual = raw.resolve(strict=True)
        if not raw.is_absolute() or raw != actual or not actual.is_dir() or actual == Path.home() or actual == Path("/"):
            raise Rejected("Only canonical absolute project directories are allowed")
        if any(p["id"] == x.id or actual == x.path or actual in x.path.parents or x.path in actual.parents for x in projects):
            raise Rejected("Project IDs and directories must be unique and non-overlapping")
        name = p["name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise Rejected("Invalid project name")
        projects.append(Project(identifier(p["id"]), name, actual))
    if not projects:
        raise Rejected("Add at least one project locally")
    return ControllerConfig(tuple(projects), STATE, DATA / "outbox", runtime_dir(), bool(j.get("mirror_results", False)))


def load_gateway(path: Path) -> dict:
    j = tomllib.loads(read_private(path).decode())
    if type(j.get("user_id")) is not int or j["user_id"] <= 0:
        raise Rejected("A positive numeric Telegram User ID is required")
    validate_token(j.get("token", ""))
    return j


def validate_token(token: str):
    if not isinstance(token, str) or not re.fullmatch(r"\d{7,12}:[A-Za-z0-9_-]{30,}", token):
        raise Rejected("Invalid Telegram token format")
