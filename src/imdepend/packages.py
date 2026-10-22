"""Offline CppDepend package repository."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .common import SCHEMA, ToolError, atomic_write, file_lock, read_toml, toml_document, toml_string, valid_name


HASH = re.compile(r"[0-9a-f]{64}\Z")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def zip_entries(path: Path, *, verify_crc: bool = False) -> tuple[str, list[zipfile.ZipInfo]]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names: set[str] = set()
            for info in entries:
                raw = info.filename.replace("\\", "/")
                pieces = raw.rstrip("/").split("/")
                if (
                    not raw or raw.startswith("/") or raw.startswith("//")
                    or any(not piece or piece in (".", "..") or ":" in piece or piece.endswith((" ", ".")) for piece in pieces)
                    or any(piece.split(".", 1)[0].lower() in RESERVED for piece in pieces)
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ToolError("unsafe_archive", f"unsafe ZIP entry: {info.filename!r}")
                folded = raw.rstrip("/").casefold()
                if folded in names:
                    raise ToolError("unsafe_archive", f"duplicate or case-ambiguous ZIP entry: {info.filename!r}")
                names.add(folded)
            root_gui = "visualcppdepend.exe" in names
            console = "cppdepend.console.exe" in names
            light = "cppdependlight/visualcppdepend.exe" in names
            if not (root_gui and console):
                raise ToolError("invalid_archive", "ZIP must contain root VisualCppDepend.exe and CppDepend.Console.exe")
            if verify_crc:
                try:
                    damaged = archive.testzip()
                except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise ToolError("invalid_archive", f"ZIP data cannot be read: {path}: {exc}") from exc
                if damaged is not None:
                    raise ToolError("invalid_archive", f"ZIP entry failed its CRC check: {damaged}")
            return ("light-based" if light else "legacy"), entries
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ToolError("invalid_archive", f"invalid ZIP archive {path}: {exc}") from exc


def _safe_filename(value: object, suffix: str) -> str:
    if not isinstance(value, str) or Path(value).name != value or not value.endswith(suffix) or any(x in value for x in ("/", "\\", ":")):
        raise ToolError("invalid_metadata", f"invalid {suffix} filename in package metadata")
    return value


def read_index(root: Path) -> dict[str, dict[str, str]]:
    if not root.is_dir():
        raise ToolError("package_root_unavailable", f"package repository is unavailable: {root}")
    path = root / "packages.toml"
    data = read_toml(path)
    if not data:
        return {}
    result: dict[str, dict[str, str]] = {}
    hashes: set[str] = set()
    entries = data.get("packages", {})
    if not isinstance(entries, dict):
        raise ToolError("invalid_metadata", f"invalid package index: {path}")
    for slug, item in entries.items():
        try:
            valid_name(slug, "package")
        except ToolError as exc:
            raise ToolError("invalid_metadata", f"invalid slug in package index: {slug}") from exc
        if not isinstance(item, dict):
            raise ToolError("invalid_metadata", f"invalid index entry for {slug}")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not HASH.fullmatch(digest) or digest in hashes:
            raise ToolError("invalid_metadata", f"invalid or duplicate SHA-256 in index for {slug}")
        hashes.add(digest)
        result[slug] = {"sha256": digest, "manifest_filename": _safe_filename(item.get("manifest_filename"), ".toml")}
    return result


def write_index(root: Path, index: dict[str, dict[str, str]]) -> None:
    lines = [f"schema_version = {SCHEMA}"]
    for slug, item in sorted(index.items()):
        lines.extend(["", f"[packages.{toml_string(slug)}]", f"sha256 = {toml_string(item['sha256'])}", f"manifest_filename = {toml_string(item['manifest_filename'])}"])
    atomic_write(root / "packages.toml", "\n".join(lines) + "\n")


def read_manifest(root: Path, slug: str, item: dict[str, str], *, verify_archive: bool = False) -> dict:
    path = root / item["manifest_filename"]
    data = read_toml(path)
    if not data or data.get("sha256") != item["sha256"]:
        raise ToolError("invalid_metadata", f"missing or inconsistent package manifest: {path}")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise ToolError("invalid_metadata", f"missing summary in package manifest: {path}")
    if not isinstance(data.get("archive_size"), int) or data["archive_size"] < 0:
        raise ToolError("invalid_metadata", f"missing archive size in package manifest: {path}")
    registration_slug = data.get("registration_slug")
    if not isinstance(registration_slug, str):
        raise ToolError("invalid_metadata", f"missing registration slug in {path}")
    try:
        valid_name(registration_slug, "package")
    except ToolError as exc:
        raise ToolError("invalid_metadata", f"invalid registration slug in {path}") from exc
    archive = root / _safe_filename(data.get("archive_filename"), ".zip")
    if not archive.is_file():
        raise ToolError("invalid_metadata", f"missing package archive: {archive}")
    if archive.stat().st_size != data["archive_size"]:
        raise ToolError("invalid_metadata", f"package archive size differs from manifest: {archive}")
    if verify_archive and sha256(archive) != item["sha256"]:
        raise ToolError("archive_hash_mismatch", f"package archive hash differs from manifest: {archive}")
    if data.get("layout") not in ("legacy", "light-based"):
        raise ToolError("invalid_metadata", f"invalid package layout in {path}")
    return {**data, "slug": slug, "manifest_filename": path.name}


def package_list(root: Path) -> list[dict]:
    return [read_manifest(root, slug, item) for slug, item in sorted(read_index(root).items())]


def package_show(root: Path, slug: str, *, verify_archive: bool = False) -> dict:
    slug = valid_name(slug, "package")
    item = read_index(root).get(slug)
    if item is None:
        raise ToolError("package_not_found", f"package is not registered: {slug}")
    return read_manifest(root, slug, item, verify_archive=verify_archive)


def _target_names(root: Path, slug: str, digest: str) -> tuple[Path, Path]:
    for length in range(12, 65):
        base = f"{slug}-{digest[:length]}"
        archive = root / f"{base}.zip"
        manifest = root / f"{base}.toml"
        if len(str(archive)) >= 260 or len(str(manifest)) >= 260:
            raise ToolError("path_too_long", f"package filename would be too long under {root}")
        if not archive.exists() and not manifest.exists():
            return archive, manifest
    raise ToolError("package_conflict", "could not allocate a unique package filename")


def _publish_new(path: Path, content: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".imdepend-register-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temp_name, path)  # Windows rename refuses to overwrite an existing target.
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _find_orphan(root: Path, digest: str) -> tuple[Path, Path, dict] | None:
    for manifest in root.glob("*.toml"):
        if manifest.name == "packages.toml":
            continue
        try:
            data = read_toml(manifest)
        except ToolError:
            continue
        if data.get("sha256") == digest:
            archive = root / _safe_filename(data.get("archive_filename"), ".zip")
            if not archive.is_file():
                raise ToolError("orphan_conflict", f"unindexed manifest has no archive: {manifest}")
            if sha256(archive) != digest:
                raise ToolError("orphan_conflict", f"unindexed manifest has a mismatched archive: {manifest}")
            return archive, manifest, data
    return None


def _orphan_archive(root: Path, slug: str, digest: str) -> Path | None:
    matches = []
    for archive in root.glob(f"{slug}-{digest[:12]}*.zip"):
        prefix = archive.stem.removeprefix(f"{slug}-")
        if digest.startswith(prefix) and sha256(archive) == digest:
            matches.append(archive)
    if len(matches) > 1:
        raise ToolError("orphan_conflict", f"multiple unindexed archives match {slug} ({digest[:12]})")
    return matches[0] if matches else None


def _clean_stale_temps(root: Path) -> None:
    cutoff = time.time() - 3600
    for pattern in (".imdepend-register-*.tmp", ".packages.toml.*.tmp"):
        for path in root.glob(pattern):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()


def register(root: Path, source: Path, slug: str, summary: str, vendor_version: str | None, *, expected_sha256: str | None = None) -> dict:
    slug = valid_name(slug, "package")
    if not source.is_file():
        raise ToolError("archive_not_found", f"ZIP not found: {source}")
    if not summary.strip() or "\n" in summary or "\r" in summary:
        raise ToolError("invalid_arguments", "summary must be one nonempty line", exit_code=2)
    if vendor_version is not None and (not vendor_version.strip() or "\n" in vendor_version or "\r" in vendor_version):
        raise ToolError("invalid_arguments", "vendor version must be one nonempty line", exit_code=2)
    layout, _ = zip_entries(source, verify_crc=True)
    digest = sha256(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ToolError("archive_changed", f"ZIP changed since the registration preview: {source}")
    if not root.is_dir():
        raise ToolError("package_root_unavailable", f"package repository is unavailable: {root}")
    with file_lock(root / ".imdepend-package-store.lock"):
        _clean_stale_temps(root)
        index = read_index(root)
        existing = next((name for name, item in index.items() if item["sha256"] == digest), None)
        if existing is not None:
            return {**read_manifest(root, existing, index[existing]), "already_registered": True}
        if slug in index:
            raise ToolError("slug_conflict", f"slug already identifies a different package: {slug}")
        orphan = _find_orphan(root, digest)
        if orphan is None:
            archive = _orphan_archive(root, slug, digest)
            if archive is None:
                archive, manifest = _target_names(root, slug, digest)
                fd, temp_name = tempfile.mkstemp(prefix=".imdepend-register-", suffix=".tmp", dir=root)
                os.close(fd)
                try:
                    shutil.copyfile(source, temp_name)
                    if sha256(Path(temp_name)) != digest:
                        raise ToolError("archive_hash_mismatch", "source ZIP changed while copying")
                    os.rename(temp_name, archive)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            else:
                manifest = archive.with_suffix(".toml")
                if manifest.exists():
                    raise ToolError("orphan_conflict", f"unexpected manifest beside unindexed archive: {manifest}")
            fields: dict[str, str | int] = {
                "schema_version": SCHEMA,
                "sha256": digest,
                "registration_slug": slug,
                "summary": summary.strip(),
                "archive_filename": archive.name,
                "archive_size": archive.stat().st_size,
                "layout": layout,
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source_filename": source.name,
            }
            if vendor_version:
                fields["vendor_version"] = vendor_version.strip()
            _publish_new(manifest, toml_document(fields).encode("utf-8"))
        else:
            archive, manifest, fields = orphan
            if fields.get("layout") != layout or fields.get("registration_slug") != slug:
                raise ToolError("orphan_conflict", f"unindexed package files require inspection: {manifest}")
        index[slug] = {"sha256": digest, "manifest_filename": manifest.name}
        write_index(root, index)
        return {**read_manifest(root, slug, index[slug]), "already_registered": False}


def rename(root: Path, old: str, new: str, *, expected_sha256: str | None = None) -> dict:
    old = valid_name(old, "package")
    new = valid_name(new, "package")
    if old == new:
        raise ToolError("invalid_arguments", "old and new slugs are the same", exit_code=2)
    with file_lock(root / ".imdepend-package-store.lock"):
        index = read_index(root)
        if old not in index:
            raise ToolError("package_not_found", f"package is not registered: {old}")
        if expected_sha256 is not None and index[old]["sha256"] != expected_sha256:
            raise ToolError("package_changed", f"package {old} changed since the rename preview")
        if new in index:
            raise ToolError("slug_conflict", f"slug already exists: {new}")
        item = index.pop(old)
        read_manifest(root, old, item, verify_archive=True)
        index[new] = item
        write_index(root, index)
        return {**read_manifest(root, new, item), "previous_slug": old}
