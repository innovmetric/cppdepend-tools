"""Build, verify, and optionally publish an imdepend wheel.

Run with Python 3.12 from the repository: py -3.12 scripts/release.py
Add --publish only when ready to copy the tested wheel to the shared drive.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
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

        # Check the two promised commands against the *installed* artifact.
        result = subprocess.run(
            [str(command), "--version"], capture_output=True, text=True, check=True
        )
        expected = f"imdepend {package_version}"
        if result.stdout.strip() != expected:
            raise RuntimeError(f"Unexpected --version output: {result.stdout!r}")
        result = subprocess.run(
            [str(command), "--help"], capture_output=True, text=True, check=True
        )
        if "usage: imdepend" not in result.stdout or "--version" not in result.stdout:
            raise RuntimeError("The installed command did not show the expected help")

        # The first release deliberately has no working CppDepend subcommands.
        result = subprocess.run(
            [str(command), "status"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            raise RuntimeError("An unimplemented subcommand unexpectedly succeeded")
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

    if sys.version_info < (3, 12):
        parser.error("run the release script with Python 3.12 or newer")
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is not on PATH")

    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = project["project"]["version"]
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
