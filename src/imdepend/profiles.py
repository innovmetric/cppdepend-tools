"""Independent local CppDepend profile generations."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from .common import SCHEMA, ToolError, atomic_write, file_lock, read_toml, toml_document, valid_name
from .packages import package_show, sha256, zip_entries
from .windows import assert_profile_idle


def profile_dir(root: Path, name: str) -> Path:
    return root / valid_name(name, "profile")


def generation(path: Path) -> dict | None:
    if not path.is_dir():
        return None
    data = read_toml(path / ".imdepend-generation.toml")
    if not data or not isinstance(data.get("sha256"), str) or data.get("layout") not in ("legacy", "light-based"):
        raise ToolError("invalid_profile", f"missing or invalid generation metadata: {path}")
    return data


def profile_show(root: Path, name: str) -> dict:
    path = profile_dir(root, name)
    if not path.is_dir():
        raise ToolError("profile_not_found", f"profile does not exist: {name}")
    current = generation(path / "current")
    previous = generation(path / "previous")
    if current is None and not (path / "operation.toml").exists():
        raise ToolError("invalid_profile", f"profile has no current installation: {name}")
    return {
        "name": name,
        "path": str(path),
        "current": current,
        "previous": previous,
        "recovery_required": (path / "operation.toml").exists(),
    }


def profile_list(root: Path) -> list[dict]:
    if not root.exists():
        return []
    return [
        profile_show(root, item.name)
        for item in sorted(root.iterdir())
        if item.is_dir() and ((item / "current").exists() or (item / "operation.toml").exists())
    ]


def _inside(profile: Path, target: Path) -> bool:
    return target.parent.resolve() == profile.resolve() and not target.is_symlink() and not target.is_junction()


def _remove(profile: Path, name: str) -> None:
    target = profile / name
    if target.exists():
        if not _inside(profile, target):
            raise ToolError("unsafe_profile", f"refusing to remove external or linked path: {target}")
        shutil.rmtree(target)


def _marker(profile: Path, operation: str, phase: str, digest: str) -> None:
    atomic_write(profile / "operation.toml", toml_document({"schema_version": SCHEMA, "operation": operation, "phase": phase, "sha256": digest}))


def recover(profile: Path) -> None:
    marker = profile / "operation.toml"
    if not marker.exists():
        return
    data = read_toml(marker)
    phases = {
        "install": {"prepared", "previous_parked", "current_moved", "promoted"},
        "rollback": {"prepared", "current_parked", "previous_promoted", "complete"},
    }
    if data.get("phase") not in phases.get(data.get("operation"), set()):
        raise ToolError("recovery_failed", f"unknown recovery phase in {marker}")
    current, previous, staging, retired = (profile / item for item in ("current", "previous", "staging", "retired"))
    if data.get("operation") == "install":
        if not current.exists():
            if staging.exists():
                if generation(staging) is None or generation(staging).get("sha256") != data.get("sha256"):
                    raise ToolError("recovery_failed", f"staged generation does not match recovery marker: {profile}")
                staging.rename(current)
            elif previous.exists():
                previous.rename(current)
            else:
                raise ToolError("recovery_failed", f"cannot recover profile: {profile}")
        else:
            _remove(profile, "staging")
        if retired.exists():
            if previous.exists():
                _remove(profile, "retired")
            else:
                retired.rename(previous)
    elif data.get("operation") == "rollback":
        if not current.exists():
            if not previous.exists():
                raise ToolError("recovery_failed", f"cannot recover rollback: {profile}")
            previous.rename(current)
        if staging.exists():
            if previous.exists():
                raise ToolError("recovery_failed", f"ambiguous rollback state: {profile}")
            staging.rename(previous)
    else:
        raise ToolError("recovery_failed", f"unknown recovery operation: {profile}")
    if generation(current) is None:
        raise ToolError("recovery_failed", f"recovered current generation is missing: {profile}")
    marker.unlink()


def _path_preflight(staging: Path, entries: list[zipfile.ZipInfo]) -> None:
    for info in entries:
        pieces = PurePosixPath(info.filename.replace("\\", "/").rstrip("/")).parts
        target = staging.joinpath(*pieces)
        if len(str(target)) >= 260:
            raise ToolError("path_too_long", f"ZIP entry would exceed the Windows path limit: {target}")
        for parent in (target if info.is_dir() else target.parent, *target.parents):
            if parent == staging.parent:
                break
            if len(str(parent)) >= 248:
                raise ToolError("path_too_long", f"ZIP directory would exceed the Windows path limit: {parent}")


def _extract(profile: Path, archive_path: Path, metadata: dict) -> None:
    _remove(profile, "staging")
    staging = profile / "staging"
    staging.mkdir()
    layout, entries = zip_entries(archive_path)
    if layout != metadata["layout"]:
        raise ToolError("invalid_archive", "archive layout differs from registered manifest")
    _path_preflight(staging, entries)
    needed = sum(info.file_size for info in entries if not info.is_dir())
    if shutil.disk_usage(staging).free < needed:
        raise ToolError("insufficient_space", f"not enough local disk space to extract {archive_path}")
    with zipfile.ZipFile(archive_path) as archive:
        for info in entries:
            target = staging.joinpath(*PurePosixPath(info.filename.replace("\\", "/")).parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    required = [staging / "VisualCppDepend.exe", staging / "CppDepend.Console.exe"]
    if layout == "light-based":
        required.append(staging / "CppDependLight" / "VisualCppDepend.exe")
    if not all(item.is_file() for item in required):
        raise ToolError("invalid_archive", "required CppDepend executables were not extracted")
    atomic_write(staging / ".imdepend-generation.toml", toml_document({"schema_version": SCHEMA, "sha256": metadata["sha256"], "installed_slug": metadata["slug"], "layout": layout}))


def profile_install(profile_root: Path, package_root: Path, name: str, slug: str) -> dict:
    path = profile_dir(profile_root, name)
    metadata = package_show(package_root, slug, verify_archive=True)
    archive = package_root / metadata["archive_filename"]
    path.mkdir(parents=True, exist_ok=True)
    with file_lock(path / ".imdepend-profile.lock"):
        assert_profile_idle(path)
        recover(path)
        current = generation(path / "current")
        if current is not None and current["sha256"] == metadata["sha256"]:
            return {**profile_show(profile_root, name), "unchanged": True}
        try:
            _extract(path, archive, metadata)
            if sha256(archive) != metadata["sha256"]:
                raise ToolError("archive_hash_mismatch", f"package archive changed during extraction: {archive}")
        except Exception:
            _remove(path, "staging")
            raise
        assert_profile_idle(path)
        _marker(path, "install", "prepared", metadata["sha256"])
        if (path / "previous").exists():
            (path / "previous").rename(path / "retired")
        _marker(path, "install", "previous_parked", metadata["sha256"])
        if (path / "current").exists():
            (path / "current").rename(path / "previous")
        _marker(path, "install", "current_moved", metadata["sha256"])
        (path / "staging").rename(path / "current")
        _marker(path, "install", "promoted", metadata["sha256"])
        _remove(path, "retired")
        (path / "operation.toml").unlink()
        return {**profile_show(profile_root, name), "unchanged": False}


def profile_rollback(profile_root: Path, name: str) -> dict:
    path = profile_dir(profile_root, name)
    if not path.is_dir():
        raise ToolError("profile_not_found", f"profile does not exist: {name}")
    with file_lock(path / ".imdepend-profile.lock"):
        assert_profile_idle(path)
        recover(path)
        current = generation(path / "current")
        previous = generation(path / "previous")
        if current is None or previous is None:
            raise ToolError("rollback_unavailable", f"profile has no previous installation: {name}")
        _marker(path, "rollback", "prepared", previous["sha256"])
        (path / "current").rename(path / "staging")
        _marker(path, "rollback", "current_parked", previous["sha256"])
        (path / "previous").rename(path / "current")
        _marker(path, "rollback", "previous_promoted", previous["sha256"])
        (path / "staging").rename(path / "previous")
        _marker(path, "rollback", "complete", previous["sha256"])
        (path / "operation.toml").unlink()
        return profile_show(profile_root, name)


def executable(profile_root: Path, name: str, mode: str) -> Path:
    path = profile_dir(profile_root, name)
    current = generation(path / "current")
    if current is None:
        raise ToolError("profile_not_found", f"profile is not installed: {name}")
    if mode == "console":
        relative = Path("CppDepend.Console.exe")
    elif mode == "new":
        if current["layout"] != "light-based":
            raise ToolError("gui_unavailable", f"new GUI is unavailable in profile {name}")
        relative = Path("VisualCppDepend.exe")
    else:
        relative = Path("CppDependLight") / "VisualCppDepend.exe" if current["layout"] == "light-based" else Path("VisualCppDepend.exe")
    target = path / "current" / relative
    if not target.is_file():
        raise ToolError("invalid_profile", f"CppDepend executable is missing: {target}")
    return target


def launch_gui(profile_root: Path, name: str, new: bool) -> dict:
    target = executable(profile_root, name, "new" if new else "old")
    child = subprocess.Popen([str(target)], cwd=target.parent)
    return {"profile": name, "executable": str(target), "pid": child.pid}


def run_console(profile_root: Path, name: str, arguments: list[str]) -> int:
    target = executable(profile_root, name, "console")
    return subprocess.run([str(target), *arguments], check=False).returncode
