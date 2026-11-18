import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from baton import __version__
from baton.config import ROUTE_TIERS, ConfigError, find_config_path, load_config
from baton.doctor import run_doctor
from baton.limits import LimitStateError, LimitStore, parse_duration
from baton.next import DEFAULT_AGENT_TIMEOUT, DEFAULT_VERIFY_TIMEOUT, run_next_command
from baton.probe import DEFAULT_PROMPT, DEFAULT_TIMEOUT, run_probe_command, run_recheck_command
from baton.status import run_status


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
    probe = subparsers.add_parser(
        "probe", help="run an agent CLI headless with a tiny read-only request"
    )
    probe.add_argument("agent", help="agent name from the config")
    probe.add_argument("--prompt", default=DEFAULT_PROMPT, help="prompt (default: %(default)r)")
    probe.add_argument("--model", help="override the model from the config")
    probe.add_argument("--tty", action="store_true", help="run on a pseudo-terminal")
    probe.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="S",
        help="seconds before the agent is killed (default: %(default)s)",
    )
    probe.add_argument("--record", metavar="PATH", help="save a redacted JSON recording")
    probe.add_argument("--dry-run", action="store_true", help="print the command, run nothing")
    recheck = subparsers.add_parser("recheck", help="probe one agent and update its cooldown")
    recheck.add_argument("agent", help="agent name from the config (probed even during cooldown)")
    recheck.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="S",
        help="probe timeout in seconds (default: %(default)s)",
    )
    cooldown = subparsers.add_parser(
        "cooldown", help="set a shared cooldown for a configured agent"
    )
    cooldown.add_argument("agent", help="agent name from the config")
    cooldown.add_argument("duration", help="positive duration, such as 30m or 2h (units: s, m, h)")
    status = subparsers.add_parser(
        "status", help="show plan, milestone and commit-gate state (read-only)"
    )
    status.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    nxt = subparsers.add_parser(
        "next", help="implement the first open plan step, verify it, stop for commit"
    )
    nxt.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    nxt.add_argument("--agent", metavar="NAME", help="use a named agent from the config")
    nxt.add_argument("--tier", choices=ROUTE_TIERS, help="override the step size used for routing")
    nxt.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT,
        metavar="S",
        help="seconds before the agent is killed (default: %(default)s)",
    )
    nxt.add_argument(
        "--verify-timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        metavar="S",
        help="seconds before the Verify command is killed (default: %(default)s)",
    )
    nxt.add_argument(
        "--dry-run", action="store_true", help="print the command and prompt, change nothing"
    )
    nxt.add_argument(
        "--commit",
        action="store_true",
        help="commit a verified step with the plan's message (blocked by critical findings)",
    )
    nxt.add_argument(
        "--push",
        action="store_true",
        help="implies --commit; push the milestone branch to origin after each commit",
    )
    nxt.add_argument(
        "--worktree",
        action="store_true",
        help="implies --commit; work in a dedicated git worktree of the milestone branch",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "cooldown":
        try:
            duration = parse_duration(args.duration)
            config = load_config(find_config_path(args.config))
            if args.agent not in config.agents:
                raise ValueError(
                    f"unknown agent {args.agent!r}; check your config or run baton doctor"
                )
            deadline = datetime.now(UTC) + duration
            LimitStore().set(args.agent, deadline)
        except (ValueError, OverflowError, ConfigError, LimitStateError) as exc:
            print(f"Cannot set cooldown: {exc}")
            return 1
        print(f"Agent {args.agent} is in cooldown until {deadline.isoformat()} (UTC).")
        return 0
    if args.command == "doctor":
        return run_doctor(args.config)
    if args.command == "status":
        return run_status(Path(args.path), config_path=args.config)
    if args.command == "next":
        return run_next_command(
            Path(args.path),
            config_path=args.config,
            agent_name=args.agent,
            tier=args.tier,
            agent_timeout=args.timeout,
            verify_timeout=args.verify_timeout,
            dry_run=args.dry_run,
            commit=args.commit,
            push=args.push,
            worktree=args.worktree,
        )
    if args.command == "recheck":
        return run_recheck_command(args.agent, config_path=args.config, timeout=args.timeout)
    if args.command == "probe":
        return run_probe_command(
            args.agent,
            config_path=args.config,
            prompt=args.prompt,
            model=args.model,
            tty=args.tty,
            timeout=args.timeout,
            record=args.record,
            dry_run=args.dry_run,
        )
    return 0
