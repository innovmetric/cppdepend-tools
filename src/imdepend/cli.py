"""The small public CLI shipped in the first release."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="imdepend",
        description="CppDepend tools. Installation commands will arrive in a later release.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('im-cppdepend-tools')}",
    )
    if not arguments:
        parser.error("specify --help or --version")
    parser.parse_args(arguments)
    return 0
