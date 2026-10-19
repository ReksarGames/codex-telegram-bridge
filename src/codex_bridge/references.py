"""Read through pinned directory descriptors; decode and re-encode before export."""
import io
import base64
import binascii
import os
import stat
import warnings
from pathlib import Path

from PIL import Image

from .security import Rejected, identifier, private_dir

MAX_IMAGE = 10 * 1024**2
Image.MAX_IMAGE_PIXELS = 24_000_000
FORMATS = {"PNG", "JPEG", "WEBP"}


def open_beneath(root: Path, relative: str) -> int:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise Rejected("Publish a regular image inside the project")
    directory = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or not 0 < s.st_size <= MAX_IMAGE:
            os.close(fd)
            raise Rejected("Image must be a regular file without hardlinks, at most 10 MiB")
        return fd
    except OSError:
        raise Rejected("Image path unavailable or unsafe") from None
    finally:
        os.close(directory)


def stable_bytes(fd: int) -> bytes:
    before = os.fstat(fd)
    raw = bytearray()
    while len(raw) <= MAX_IMAGE:
        chunk = os.read(fd, min(65536, MAX_IMAGE + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    after = os.fstat(fd)
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != before.st_size:
        raise Rejected("File changed while being read")
    return bytes(raw)


def normalized_png(raw: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                    raise Rejected("Only static PNG, JPEG and WEBP are allowed")
                image.load()
                target = io.BytesIO()
                image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB").save(target, format="PNG")
        result = target.getvalue()
        if len(result) > MAX_IMAGE:
            raise Rejected("Exported image exceeds 10 MiB")
        return result
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Rejected("Image corrupt or too large") from None


def publish(root: Path, outbox: Path, pid: str, token: str, relative: str) -> str:
    fd = open_beneath(root, relative)
    try:
        raw = stable_bytes(fd)
    finally:
        os.close(fd)
    return export_image(outbox, pid, token, raw)


def publish_generated(outbox: Path, pid: str, token: str, encoded: str) -> str:
    if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_IMAGE + 2) // 3):
        raise Rejected("Invalid generated image size")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise Rejected("Invalid generated image data") from None
    if len(raw) > MAX_IMAGE:
        raise Rejected("Generated image exceeds 10 MiB")
    return export_image(outbox, pid, token, raw)


def export_image(outbox: Path, pid: str, token: str, raw: bytes) -> str:
    result = normalized_png(raw)
    name = f"{identifier(pid)}-{identifier(token)}.png"
    private_dir(outbox / pid)
    directory = os.open(outbox / pid, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return f"{pid}/{name}"


def export_parts(name: str) -> tuple[str, str]:
    parts = name.split("/")
    if len(parts) != 2 or not parts[1].startswith(parts[0]+"-") or not parts[1].endswith(".png"):
        raise Rejected("Invalid export")
    identifier(parts[0])
    identifier(parts[1][len(parts[0])+1:-4])
    return parts[0], parts[1]


def delete_export(outbox: Path, name: str):
    pid, filename = export_parts(name)
    try:
        directory = os.open(outbox / pid, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(filename, dir_fd=directory)
        except FileNotFoundError:
            pass
    finally:
        os.close(directory)


def read_export(outbox: Path, name: str) -> bytes:
    export_parts(name)
    fd = open_beneath(outbox, name)
    try:
        raw = stable_bytes(fd)
    finally:
        os.close(fd)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG":
            raise Rejected("Invalid export")
        image.verify()
    return raw


REFERENCE_TOOL = {"type": "function", "name": "bridge_publish_reference", "description": "Publish a reference image and WAIT for the user's review before any dependent work. This call blocks until accept, redo or edits. Supply a project-relative image path. Never continue dependent work before receiving this tool's decision.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}
