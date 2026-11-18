import argparse
from collections.abc import Sequence

from baton import __version__
from baton.doctor import run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baton",
        description="Orchestrates coding agent CLIs through a commit plan.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file (default: $BATON_CONFIG, ~/.config/baton/config.toml, bundled example)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "doctor", help="show configured agents and whether their CLIs are in PATH"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return run_doctor(args.config)
    return 0
