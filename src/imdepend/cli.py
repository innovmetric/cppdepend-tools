"""Public command line for CppDepend package and profile management."""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

from .common import ToolError, absolute_path, effective_paths, save_paths
from .packages import package_list, package_show, register, rename, sha256, zip_entries
from .profiles import launch_gui, profile_install, profile_list, profile_rollback, profile_show, run_console


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise ToolError("invalid_arguments", message, exit_code=2)


def _json_option(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS, help="Print one machine-readable result")


def parser() -> Parser:
    result = Parser(
        prog="imdepend",
        description="Register CppDepend ZIPs and manage independent local installations.",
        epilog="Examples:\n  imdepend package list\n  imdepend profile install good 2026-2-fix\n  imdepend gui good\n  imdepend console good -- C:\\work\\Example.cdproj /FullAnalysis",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {version('im-cppdepend-tools')}")
    _json_option(result, root=True)
    commands = result.add_subparsers(dest="command")

    setup = commands.add_parser("setup", help="Show or configure package and profile locations")
    _json_option(setup)
    setup.add_argument("--package-root", metavar="PATH")
    setup.add_argument("--profile-root", metavar="PATH")
    _json_option(commands.add_parser("status", help="Show paths, repository availability, and profiles"))

    package = commands.add_parser("package", help="Register and inspect offline vendor ZIPs", epilog="Examples:\n  imdepend package register C:\\Downloads\\CppDepend.zip --slug 2026-2-fix --summary 'Fixed build'\n  imdepend package rename typo corrected")
    _json_option(package)
    package_commands = package.add_subparsers(dest="package_command", required=True)
    registration = package_commands.add_parser("register", help="Register a vendor ZIP by SHA-256")
    _json_option(registration)
    registration.add_argument("zip", metavar="ZIP")
    registration.add_argument("--slug")
    registration.add_argument("--summary")
    registration.add_argument("--vendor-version")
    registration.add_argument("--yes", action="store_true", help="Accept the manifest preview without prompting")
    _json_option(package_commands.add_parser("list", help="List registered packages"))
    show = package_commands.add_parser("show", help="Show one registered package")
    _json_option(show)
    show.add_argument("slug")
    rename_command = package_commands.add_parser("rename", help="Correct a package slug without an alias")
    _json_option(rename_command)
    rename_command.add_argument("old_slug")
    rename_command.add_argument("new_slug")
    rename_command.add_argument("--yes", action="store_true")

    profile = commands.add_parser("profile", help="Manage independent local installations", epilog="Examples:\n  imdepend profile install good 2026-2-fix\n  imdepend profile rollback good")
    _json_option(profile)
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    install = profile_commands.add_parser("install", help="Install or update a profile from a registered package")
    _json_option(install)
    install.add_argument("profile")
    install.add_argument("slug")
    _json_option(profile_commands.add_parser("list", help="List local profiles"))
    show_profile = profile_commands.add_parser("show", help="Show a local profile")
    _json_option(show_profile)
    show_profile.add_argument("profile")
    rollback = profile_commands.add_parser("rollback", help="Swap current and previous installations")
    _json_option(rollback)
    rollback.add_argument("profile")

    gui = commands.add_parser("gui", help="Open a profile's interactive GUI")
    gui.add_argument("profile")
    gui.add_argument("--variant", choices=("old", "new"), default="old")
    console = commands.add_parser("console", help="Run a profile's CppDepend console and wait")
    console.add_argument("profile")
    console.add_argument("arguments", nargs=argparse.REMAINDER, metavar="-- ARGUMENTS")
    return result


def _confirm(message: str) -> bool:
    print(f"{message} [y/N] ", file=sys.stderr, end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _prompt(label: str) -> str:
    print(f"{label}: ", file=sys.stderr, end="", flush=True)
    return sys.stdin.readline().strip()


def _result(args: argparse.Namespace) -> tuple[str, object]:
    package_root, profile_root = effective_paths()
    if args.command == "setup":
        package_root = absolute_path(args.package_root) if args.package_root else package_root
        profile_root = absolute_path(args.profile_root) if args.profile_root else profile_root
        if args.package_root or args.profile_root:
            save_paths(package_root, profile_root)
        return "setup", {"package_root": str(package_root), "profile_root": str(profile_root), "package_root_available": package_root.is_dir()}
    if args.command == "status":
        return "status", {"package_root": str(package_root), "package_root_available": package_root.is_dir(), "profile_root": str(profile_root), "profiles": profile_list(profile_root)}
    if args.command == "package":
        if args.package_command == "list":
            return "package.list", {"packages": package_list(package_root)}
        if args.package_command == "show":
            return "package.show", package_show(package_root, args.slug, verify_archive=True)
        if args.package_command == "rename":
            old = package_show(package_root, args.old_slug)
            if not args.yes and not _confirm(f"Rename {old['slug']} ({old['sha256'][:12]}) to {args.new_slug}; the old slug will stop working?"):
                raise ToolError("cancelled", "package rename cancelled")
            return "package.rename", rename(package_root, args.old_slug, args.new_slug, expected_sha256=old["sha256"])
        source = Path(args.zip).expanduser().resolve()
        if args.yes and (not args.slug or not args.summary):
            raise ToolError("invalid_arguments", "--yes requires --slug and --summary", exit_code=2)
        slug = args.slug or re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-")
        summary = args.summary or _prompt("Package summary")
        expected_digest = None
        if not args.yes:
            layout, _ = zip_entries(source, verify_crc=True)
            digest = sha256(source)
            expected_digest = digest
            preview = {"slug": slug, "sha256": digest, "summary": summary, "layout": layout, "source": str(source), "archive_size": source.stat().st_size, "vendor_version": args.vendor_version}
            print(json.dumps(preview, indent=2), file=sys.stderr)
            if not _confirm("Register this package?"):
                raise ToolError("cancelled", "package registration cancelled")
        return "package.register", register(package_root, source, slug, summary, args.vendor_version, expected_sha256=expected_digest)
    if args.command == "profile":
        if args.profile_command == "list":
            return "profile.list", {"profiles": profile_list(profile_root)}
        if args.profile_command == "show":
            return "profile.show", profile_show(profile_root, args.profile)
        if args.profile_command == "install":
            return "profile.install", profile_install(profile_root, package_root, args.profile, args.slug)
        return "profile.rollback", profile_rollback(profile_root, args.profile)
    raise ToolError("invalid_arguments", "unknown command", exit_code=2)


def _human(command: str, data: object) -> None:
    if command in ("package.list", "profile.list"):
        key = "packages" if command == "package.list" else "profiles"
        for item in data[key]:
            if key == "packages":
                print(f"{item['slug']} {item['sha256'][:12]} {item['layout']} - {item['summary']}")
            else:
                current = item["current"] or {}
                print(f"{item['name']} {current.get('installed_slug', '(recovering)')} {current.get('sha256', '')[:12]}")
    elif command == "status":
        print(f"Package root: {data['package_root']} ({'available' if data['package_root_available'] else 'unavailable'})")
        print(f"Profile root: {data['profile_root']}")
        for item in data["profiles"]:
            print(f"{item['name']}: {(item['current'] or {}).get('installed_slug', '(recovering)')}")
    elif command == "setup":
        print(f"Package root: {data['package_root']} ({'available' if data['package_root_available'] else 'unavailable'})")
        print(f"Profile root: {data['profile_root']}")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def _fatal(code: str, message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"schema_version": 1, "error": {"code": code, "message": message}}), file=sys.stderr)
    else:
        print(f"imdepend: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    wants_json = "--json" in arguments
    try:
        command_parser = parser()
        if not arguments:
            command_parser.print_help()
            return 0
        args = command_parser.parse_args(arguments)
        if args.command is None:
            command_parser.print_help()
            return 0
        if args.command in ("gui", "console"):
            if wants_json:
                raise ToolError("invalid_arguments", "--json is unavailable for GUI and console launches", exit_code=2)
            _, profile_root = effective_paths()
            if args.command == "gui":
                data = launch_gui(profile_root, args.profile, args.variant == "new")
                print(f"Started {data['executable']} (PID {data['pid']})")
                return 0
            vendor_args = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            return run_console(profile_root, args.profile, vendor_args)
        name, data = _result(args)
        if args.json:
            print(json.dumps({"schema_version": 1, "command": name, "data": data}, ensure_ascii=False))
        else:
            _human(name, data)
        return 0
    except ToolError as exc:
        if wants_json:
            print(json.dumps({"schema_version": 1, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"imdepend: {exc}", file=sys.stderr)
        return exc.exit_code
    except PermissionError as exc:
        return _fatal("permission_denied", str(exc), wants_json)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return _fatal("operation_failed", str(exc), wants_json)
