"""Build, verify, and optionally publish an imdepend wheel.

Run with Python 3.12 from the repository: py -3.12 scripts/release.py
Add --publish only when ready to copy the tested wheel to the shared drive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SHARE_ROOT = Path(
    r"\\innovmetric.com\innovdfs\software\cppdepend\cppdepend-tools"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+ " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=REPOSITORY, env=env, check=True)


def verify_wheel(wheel: Path, package_version: str, uv: str) -> None:
    # Use a separate uv tool store so testing cannot replace the developer's
    # installed imdepend command or leave a test launcher on their PATH.
    with tempfile.TemporaryDirectory(prefix="imdepend-release-check-") as temporary:
        temporary_path = Path(temporary)
        environment = os.environ.copy()
        environment["UV_TOOL_DIR"] = str(temporary_path / "tools")
        environment["UV_TOOL_BIN_DIR"] = str(temporary_path / "bin")
        environment["LOCALAPPDATA"] = str(temporary_path / "local")

        # Install the wheel offline just as a recipient will. This detects
        # missing package files and entry-point errors that source tests miss.
        run(
            [
                uv,
                "tool",
                "install",
                "--python",
                sys.executable,
                "--no-python-downloads",
                "--offline",
                "--no-index",
                str(wheel),
            ],
            env=environment,
        )

        command = temporary_path / "bin" / (
            "imdepend.exe" if os.name == "nt" else "imdepend"
        )
        if not command.is_file():
            raise RuntimeError(f"The wheel did not install an imdepend command: {command}")

        # Check the installed entry point and its user-facing command tree.
        result = subprocess.run(
            [str(command), "--version"], capture_output=True, text=True, check=True,
            env=environment,
        )
        expected = f"imdepend {package_version}"
        if result.stdout.strip() != expected:
            raise RuntimeError(f"Unexpected --version output: {result.stdout!r}")
        result = subprocess.run(
            [str(command), "--help"], capture_output=True, text=True, check=True,
            env=environment,
        )
        if "usage: imdepend" not in result.stdout or "package" not in result.stdout or "profile" not in result.stdout:
            raise RuntimeError("The installed command did not show the expected help")

        # Status and profile inspection must work without the network share.
        result = subprocess.run(
            [str(command), "status", "--json"], capture_output=True, text=True,
            check=True, env=environment,
        )
        status = json.loads(result.stdout)
        if status["command"] != "status" or status["data"]["profiles"] != []:
            raise RuntimeError(f"Unexpected installed status result: {status}")
        result = subprocess.run(
            [str(command), "profile", "list", "--json"], capture_output=True,
            text=True, check=True, env=environment,
        )
        if json.loads(result.stdout)["data"]["profiles"] != []:
            raise RuntimeError("Fresh tool store unexpectedly has profiles")

        # Exercise the package and profile commands from the wheel itself with
        # harmless ZIP fixtures. This catches missing modules in the artifact.
        package_root = temporary_path / "packages"
        profile_root = temporary_path / "profiles"
        package_root.mkdir()
        for slug, payload in (("first", b"console-1"), ("second", b"console-2")):
            archive = temporary_path / f"{slug}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("VisualCppDepend.exe", b"gui")
                output.writestr("CppDepend.Console.exe", payload)
        subprocess.run(
            [str(command), "setup", "--package-root", str(package_root),
             "--profile-root", str(profile_root), "--json"],
            capture_output=True, text=True, check=True, env=environment,
        )
        for slug in ("first", "second"):
            archive = temporary_path / f"{slug}.zip"
            subprocess.run(
                [str(command), "package", "register", str(archive), "--slug", slug,
                 "--summary", slug, "--yes", "--json"],
                capture_output=True, text=True, check=True, env=environment,
            )
            subprocess.run(
                [str(command), "profile", "install", "smoke", slug, "--json"],
                capture_output=True, text=True, check=True, env=environment,
            )
        rolled_back = subprocess.run(
            [str(command), "profile", "rollback", "smoke", "--json"],
            capture_output=True, text=True, check=True, env=environment,
        )
        if json.loads(rolled_back.stdout)["data"]["current"]["installed_slug"] != "first":
            raise RuntimeError("Installed wheel rollback did not restore the first package")
        print(f"Verified installed wheel: {expected}", flush=True)


def publish_wheel(wheel: Path, package_version: str, digest: str, share_root: Path) -> None:
    destination_dir = share_root / package_version
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / wheel.name

    # A published version is immutable. Re-running a release can confirm the
    # existing wheel, but must never silently replace it with different bytes.
    if destination.exists():
        if sha256(destination) != digest:
            raise RuntimeError(f"A different wheel already exists: {destination}")
        print(f"Already published with matching SHA-256: {destination}", flush=True)
        return

    temporary = destination_dir / f".{wheel.name}.{uuid.uuid4().hex}.tmp"
    try:
        # Copy under a temporary name so readers cannot select a partial wheel.
        shutil.copyfile(wheel, temporary)
        if sha256(temporary) != digest:
            raise RuntimeError("The copied wheel has a different SHA-256")
        if destination.exists():
            raise RuntimeError(f"The release appeared during copy: {destination}")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)

    # Re-read the published file so success means the share has the tested bytes.
    if sha256(destination) != digest:
        raise RuntimeError(f"Published wheel failed SHA-256 verification: {destination}")
    print(f"Published {destination}", flush=True)
    print(f"SHA-256 {digest}", flush=True)


def verify_publish_source(package_version: str) -> None:
    # A published version is immutable, so it must identify a reviewed source
    # commit rather than an arbitrary local build or uncommitted edits.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPOSITORY, capture_output=True, text=True, check=True,
    )
    if status.stdout.strip():
        raise RuntimeError("Publishing requires a clean Git worktree")
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=REPOSITORY, capture_output=True, text=True, check=True,
    )
    if f"v{package_version}" not in tags.stdout.splitlines():
        raise RuntimeError(f"Publishing requires tag v{package_version} on HEAD")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish", action="store_true", help="Copy the verified wheel to the shared drive"
    )
    parser.add_argument(
        "--share-root", type=Path, default=DEFAULT_SHARE_ROOT,
        help=f"Release root (default: {DEFAULT_SHARE_ROOT})",
    )
    arguments = parser.parse_args()

    if sys.version_info[:2] != (3, 12):
        parser.error("run the release script with Python 3.12 to verify the recipient baseline")
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is not on PATH")

    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = project["project"]["version"]
    if arguments.publish:
        verify_publish_source(package_version)
    wheel_name = f"im_cppdepend_tools-{package_version}-py3-none-any.whl"
    output_dir = REPOSITORY / "dist" / package_version

    # Build both the source archive and wheel in a version-specific directory.
    # --clear removes stale artifacts from an earlier build of this version.
    run(
        [
            uv,
            "build",
            "--python",
            sys.executable,
            "--no-python-downloads",
            "--out-dir",
            str(output_dir),
            "--clear",
        ]
    )
    wheel = output_dir / wheel_name
    if not wheel.is_file():
        raise RuntimeError(f"Expected wheel was not built: {wheel}")

    verify_wheel(wheel, package_version, uv)
    digest = sha256(wheel)
    print(f"Ready: {wheel}", flush=True)
    print(f"SHA-256 {digest}", flush=True)
    if arguments.publish:
        publish_wheel(wheel, package_version, digest, arguments.share_root)
    else:
        print("Run again with --publish to copy this release to the share.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
