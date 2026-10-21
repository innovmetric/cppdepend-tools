"""Shared paths, TOML persistence, locks, and public errors."""

from __future__ import annotations

import json
import msvcrt
import os
import re
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = 1
DEFAULT_PACKAGE_ROOT = Path(r"\\innovmetric.com\innovdfs\software\cppdepend\cppdepend-packages")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def valid_name(value: str, kind: str) -> str:
    if not NAME.fullmatch(value) or (kind == "profile" and len(value) > 32):
        suffix = " (at most 32 characters)" if kind == "profile" else ""
        raise ToolError("invalid_arguments", f"invalid {kind} name {value!r}: use lowercase letters, digits, and hyphens{suffix}", exit_code=2)
    return value


def absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolError("invalid_arguments", f"path must be absolute: {value}", exit_code=2)
    return path


def local_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise ToolError("local_appdata_missing", "LOCALAPPDATA is not set")
    return Path(value) / "imdepend"


def config_path() -> Path:
    return local_root() / "config.toml"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_document(fields: dict[str, str | int], tables: dict[str, dict[str, str]] | None = None) -> str:
    lines = [f"{key} = {toml_string(value) if isinstance(value, str) else value}" for key, value in fields.items()]
    for table, values in (tables or {}).items():
        lines.extend(["", f"[{table}]"])
        lines.extend(f"{toml_string(key)} = {toml_string(value)}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            result = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolError("invalid_metadata", f"cannot read metadata {path}: {exc}") from exc
    if result.get("schema_version") != SCHEMA:
        raise ToolError("invalid_metadata", f"unsupported schema in {path}")
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


@contextmanager
def file_lock(path: Path, *, timeout: float = 3.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ToolError("repository_busy" if "package" in path.name else "profile_busy", f"busy: {path.parent}") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def effective_paths() -> tuple[Path, Path]:
    data = read_toml(config_path())
    return (
        absolute_path(str(data.get("package_root", DEFAULT_PACKAGE_ROOT))),
        absolute_path(str(data.get("profile_root", local_root() / "profiles"))),
    )


def save_paths(package_root: Path, profile_root: Path) -> None:
    atomic_write(config_path(), toml_document({"schema_version": SCHEMA, "package_root": str(package_root), "profile_root": str(profile_root)}))
