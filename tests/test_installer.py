"""Public installer workflows against isolated fake CppDepend ZIPs."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from imdepend import cli, profiles  # noqa: E402
from imdepend.common import ToolError, file_lock  # noqa: E402
from imdepend.packages import package_show, read_index, register, rename, sha256  # noqa: E402


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="imdepend-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "packages"
        self.profile_root = self.root / "profiles"
        self.local_appdata = self.root / "local"
        self.appdata = self.root / "roaming"
        self.package_root.mkdir()
        self.appdata.mkdir()
        (self.appdata / "CppDepend").mkdir()
        (self.appdata / "CppDepend" / "history.txt").write_text("keep this history", encoding="utf-8")
        patcher = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local_appdata), "APPDATA": str(self.appdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        version_patcher = patch.object(cli, "version", return_value="0.2.0")
        version_patcher.start()
        self.addCleanup(version_patcher.stop)
        self.run_cli("setup", "--package-root", str(self.package_root), "--profile-root", str(self.profile_root))

    def make_zip(self, name: str, *, light: bool = False, content: bytes = b"console", extra: dict[str, bytes] | None = None) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("VisualCppDepend.exe", b"gui")
            archive.writestr("CppDepend.Console.exe", content)
            if light:
                archive.writestr("CppDependLight/VisualCppDepend.exe", b"old gui")
            for entry, data in (extra or {}).items():
                archive.writestr(entry, data)
        return path

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_full_registration_profile_and_rollback(self) -> None:
        first = self.make_zip("first.zip", light=True)
        second = self.make_zip("second.zip", content=b"updated console")
        code, output, error = self.run_cli("package", "register", str(first), "--slug", "first", "--summary", "First build", "--yes", "--json")
        self.assertEqual((code, error), (0, ""))
        first_hash = json.loads(output)["data"]["sha256"]
        self.assertEqual(json.loads(output)["data"]["layout"], "light-based")
        self.assertEqual(self.run_cli("package", "register", str(first), "--slug", "duplicate", "--summary", "Same", "--yes", "--json")[0], 0)
        self.assertEqual(list(read_index(self.package_root)), ["first"])
        self.assertEqual(self.run_cli("package", "register", str(second), "--slug", "second", "--summary", "Second build", "--yes")[0], 0)
        self.assertEqual(self.run_cli("profile", "install", "good", "first")[0], 0)
        self.assertEqual(self.run_cli("profile", "install", "other", "first")[0], 0)
        self.assertEqual(profiles.executable(self.profile_root, "good", "old").relative_to(self.profile_root).as_posix(), "good/current/CppDependLight/VisualCppDepend.exe")
        self.assertEqual(profiles.executable(self.profile_root, "good", "new").name, "VisualCppDepend.exe")
        self.assertEqual(self.run_cli("profile", "install", "good", "second")[0], 0)
        self.assertEqual(profiles.profile_show(self.profile_root, "good")["previous"]["sha256"], first_hash)
        self.assertEqual(self.run_cli("profile", "rollback", "good")[0], 0)
        self.assertEqual(profiles.profile_show(self.profile_root, "good")["current"]["sha256"], first_hash)
        self.assertEqual(self.run_cli("profile", "install", "good", "first", "--json")[0], 0)
        self.assertEqual(profiles.profile_show(self.profile_root, "other")["current"]["sha256"], first_hash)
        self.assertEqual((self.appdata / "CppDepend" / "history.txt").read_text(encoding="utf-8"), "keep this history")
        self.assertEqual(self.run_cli("status", "--json")[0], 0)

    def test_rename_keeps_bytes_and_existing_profile_offline(self) -> None:
        archive = self.make_zip("typo.zip")
        registered = register(self.package_root, archive, "typo", "A build", None)
        self.assertEqual(self.run_cli("profile", "install", "good", "typo")[0], 0)
        renamed = rename(self.package_root, "typo", "fixed")
        self.assertEqual(renamed["registration_slug"], "typo")
        self.assertEqual(renamed["slug"], "fixed")
        self.assertEqual(registered["archive_filename"], renamed["archive_filename"])
        self.assertEqual(registered["manifest_filename"], renamed["manifest_filename"])
        duplicate = register(self.package_root, archive, "typo", "Same ZIP", None)
        self.assertEqual((duplicate["slug"], duplicate["already_registered"]), ("fixed", True))
        self.assertEqual(self.run_cli("package", "show", "typo", "--json")[0], 1)
        self.assertEqual(self.run_cli("package", "show", "fixed", "--json")[0], 0)
        offline = self.root / "offline-packages"
        self.package_root.rename(offline)
        self.assertEqual(self.run_cli("profile", "show", "good", "--json")[0], 0)
        self.assertEqual(self.run_cli("status", "--json")[0], 0)
        self.assertEqual(profiles.executable(self.profile_root, "good", "old").name, "VisualCppDepend.exe")
        with self.assertRaises(ToolError) as error:
            profiles.executable(self.profile_root, "good", "new")
        self.assertEqual(error.exception.code, "gui_unavailable")

    def test_unsafe_zip_and_path_preflight(self) -> None:
        unsafe = self.make_zip("unsafe.zip", extra={"../outside.txt": b"bad"})
        code, _, stderr = self.run_cli("package", "register", str(unsafe), "--slug", "unsafe", "--summary", "Bad", "--yes", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["error"]["code"], "unsafe_archive")
        self.assertEqual(read_index(self.package_root), {})
        long_path = self.make_zip("long.zip", extra={"deep/" + "x" * 230 + ".h": b"x"})
        register(self.package_root, long_path, "long", "Long path", None)
        with self.assertRaises(ToolError) as error:
            profiles.profile_install(self.profile_root, self.package_root, "good", "long")
        self.assertEqual(error.exception.code, "path_too_long")
        self.assertFalse((self.profile_root / "good" / "current").exists())

    def test_recovery_after_current_moved(self) -> None:
        old = self.make_zip("old.zip")
        new = self.make_zip("new.zip", content=b"new")
        register(self.package_root, old, "old", "Old", None)
        new_metadata = register(self.package_root, new, "new", "New", None)
        profiles.profile_install(self.profile_root, self.package_root, "good", "old")
        path = self.profile_root / "good"
        profiles._extract(path, self.package_root / new_metadata["archive_filename"], new_metadata)
        profiles._marker(path, "install", "current_moved", new_metadata["sha256"])
        (path / "current").rename(path / "previous")
        profiles.recover(path)
        self.assertEqual(profiles.generation(path / "current")["sha256"], new_metadata["sha256"])
        self.assertIsNotNone(profiles.generation(path / "previous"))
        self.assertFalse((path / "operation.toml").exists())

    def test_registration_recovers_complete_and_partial_orphans(self) -> None:
        from imdepend import packages

        source = self.make_zip("interrupted.zip")
        with patch.object(packages, "write_index", side_effect=OSError("injected interruption")):
            with self.assertRaises(OSError):
                register(self.package_root, source, "interrupted", "Recovered", None)
        self.assertEqual(read_index(self.package_root), {})
        self.assertEqual(len(list(self.package_root.glob("interrupted-*.zip"))), 1)
        self.assertEqual(len(list(self.package_root.glob("interrupted-*.toml"))), 1)
        recovered = register(self.package_root, source, "interrupted", "Recovered", None)
        self.assertEqual(recovered["slug"], "interrupted")
        self.assertEqual(len(list(self.package_root.glob("interrupted-*.zip"))), 1)

        second = self.make_zip("partial.zip", content=b"partial")
        digest = sha256(second)
        orphan = self.package_root / f"partial-{digest[:12]}.zip"
        shutil.copyfile(second, orphan)
        recovered_partial = register(self.package_root, second, "partial", "Recovered partial", None)
        self.assertEqual(recovered_partial["archive_filename"], orphan.name)
        self.assertTrue(orphan.with_suffix(".toml").is_file())

    def test_interrupted_install_before_promotion_restores_old_state(self) -> None:
        old = self.make_zip("old.zip")
        new = self.make_zip("new.zip", content=b"new")
        old_meta = register(self.package_root, old, "old", "Old", None)
        new_meta = register(self.package_root, new, "new", "New", None)
        profiles.profile_install(self.profile_root, self.package_root, "good", "old")
        path = self.profile_root / "good"
        profiles._extract(path, self.package_root / new_meta["archive_filename"], new_meta)
        profiles._marker(path, "install", "prepared", new_meta["sha256"])
        profiles.recover(path)
        self.assertEqual(profiles.generation(path / "current")["sha256"], old_meta["sha256"])
        self.assertFalse((path / "staging").exists())

    def test_inaccessible_candidate_blocks_update(self) -> None:
        from imdepend import windows

        with patch.object(windows, "cppdepend_processes", return_value=[(123, None)]):
            with self.assertRaises(ToolError) as error:
                windows.assert_profile_idle(self.profile_root / "good")
        self.assertEqual(error.exception.code, "process_path_unavailable")

    def test_all_install_promotion_phases_recover(self) -> None:
        old = register(self.package_root, self.make_zip("old.zip"), "old", "Old", None)
        middle = register(self.package_root, self.make_zip("middle.zip", content=b"middle"), "middle", "Middle", None)
        new = register(self.package_root, self.make_zip("new.zip", content=b"new"), "new", "New", None)
        for phase in ("prepared", "previous_parked", "current_moved", "promoted"):
            with self.subTest(phase=phase):
                name = f"phase-{phase.replace('_', '-')}"
                profiles.profile_install(self.profile_root, self.package_root, name, "old")
                profiles.profile_install(self.profile_root, self.package_root, name, "middle")
                path = self.profile_root / name
                profiles._extract(path, self.package_root / new["archive_filename"], new)
                if phase in ("previous_parked", "current_moved", "promoted"):
                    (path / "previous").rename(path / "retired")
                if phase in ("current_moved", "promoted"):
                    (path / "current").rename(path / "previous")
                if phase == "promoted":
                    (path / "staging").rename(path / "current")
                profiles._marker(path, "install", phase, new["sha256"])
                profiles.recover(path)
                expected_current = middle if phase in ("prepared", "previous_parked") else new
                expected_previous = old if phase in ("prepared", "previous_parked") else middle
                self.assertEqual(profiles.generation(path / "current")["sha256"], expected_current["sha256"])
                self.assertEqual(profiles.generation(path / "previous")["sha256"], expected_previous["sha256"])
                self.assertFalse((path / "operation.toml").exists())
                self.assertFalse((path / "retired").exists())

    def test_interrupted_rollback_phases_recover(self) -> None:
        old = register(self.package_root, self.make_zip("old.zip"), "old", "Old", None)
        new = register(self.package_root, self.make_zip("new.zip", content=b"new"), "new", "New", None)
        for phase in ("prepared", "current_parked", "previous_promoted", "complete"):
            with self.subTest(phase=phase):
                name = f"rollback-{phase.replace('_', '-')}"
                profiles.profile_install(self.profile_root, self.package_root, name, "old")
                profiles.profile_install(self.profile_root, self.package_root, name, "new")
                path = self.profile_root / name
                if phase in ("current_parked", "previous_promoted", "complete"):
                    (path / "current").rename(path / "staging")
                if phase in ("previous_promoted", "complete"):
                    (path / "previous").rename(path / "current")
                if phase == "complete":
                    (path / "staging").rename(path / "previous")
                profiles._marker(path, "rollback", phase, old["sha256"])
                profiles.recover(path)
                expected_current = new if phase == "prepared" else old
                expected_previous = old if phase == "prepared" else new
                self.assertEqual(profiles.generation(path / "current")["sha256"], expected_current["sha256"])
                self.assertEqual(profiles.generation(path / "previous")["sha256"], expected_previous["sha256"])
                self.assertFalse((path / "operation.toml").exists())

    def test_preview_can_cancel_registration(self) -> None:
        source = self.make_zip("cancel.zip")
        with patch("sys.stdin", io.StringIO("n\n")):
            code, output, errors = self.run_cli("package", "register", str(source), "--slug", "cancel", "--summary", "Cancel", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Register this package?", errors)
        self.assertEqual(read_index(self.package_root), {})

    def test_repository_lock_blocks_another_process_but_not_a_stale_file(self) -> None:
        lock = self.package_root / ".imdepend-package-store.lock"
        ready = self.root / "ready.txt"
        child_code = (
            "import sys,time;sys.path.insert(0,sys.argv[1]);"
            "from pathlib import Path;from imdepend.common import file_lock;"
            "p=Path(sys.argv[2]);r=Path(sys.argv[3])\n"
            "with file_lock(p):\n r.write_text('ready');time.sleep(5)"
        )
        child = subprocess.Popen([sys.executable, "-c", child_code, str(Path(__file__).resolve().parents[1] / "src"), str(lock), str(ready)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), f"lock-holder failed: {child.stderr.read() if child.poll() is not None else ''}")
            with self.assertRaises(ToolError) as error:
                with file_lock(lock, timeout=0.2):
                    pass
            self.assertEqual(error.exception.code, "repository_busy")
        finally:
            child.terminate()
            child.wait(timeout=5)
            child.stderr.close()
        with file_lock(lock, timeout=0.2):
            pass

    def test_json_errors_and_usage_codes(self) -> None:
        code, stdout, stderr = self.run_cli("package", "show", "missing", "--json")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "package_not_found")
        code, stdout, stderr = self.run_cli("package", "register", "--yes", "--json")
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_arguments")

    def test_json_management_contract(self) -> None:
        source = self.make_zip("record.zip")
        commands = [
            (("--json", "setup"), "setup", {"package_root", "profile_root", "package_root_available"}),
            (("package", "register", str(source), "--slug", "record", "--summary", "A record", "--yes", "--json"), "package.register", {"slug", "sha256", "registration_slug", "manifest_filename", "already_registered"}),
            (("package", "list", "--json"), "package.list", {"packages"}),
            (("package", "show", "record", "--json"), "package.show", {"slug", "sha256", "archive_filename"}),
            (("profile", "install", "good", "record", "--json"), "profile.install", {"name", "current", "previous", "unchanged"}),
            (("profile", "list", "--json"), "profile.list", {"profiles"}),
            (("profile", "show", "good", "--json"), "profile.show", {"name", "current", "recovery_required"}),
            (("status", "--json"), "status", {"package_root", "profile_root", "profiles"}),
        ]
        for arguments, expected_command, fields in commands:
            with self.subTest(command=expected_command):
                code, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual((code, stderr), (0, ""))
                response = json.loads(stdout)
                self.assertEqual((response["schema_version"], response["command"]), (1, expected_command))
                self.assertTrue(fields.issubset(response["data"]))

    def test_busy_process_and_console_exit_code(self) -> None:
        cmd = Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"
        archive = self.make_zip("cmd.zip", content=cmd.read_bytes())
        register(self.package_root, archive, "cmd", "Test console", None)
        profiles.profile_install(self.profile_root, self.package_root, "good", "cmd")
        self.assertEqual(profiles.run_console(self.profile_root, "good", ["/d", "/c", "exit", "7"]), 7)
        self.assertEqual(self.run_cli("console", "good", "--", "/d", "/c", "exit", "7")[0], 7)
        target = profiles.executable(self.profile_root, "good", "console")
        process = subprocess.Popen([str(target), "/d", "/c", "ping", "-n", "12", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaises(ToolError) as error:
                profiles.profile_rollback(self.profile_root, "good")
            self.assertEqual(error.exception.code, "profile_busy")
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
