import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from baton import __version__
from baton.auditor import run_audit_command, run_audit_fix
from baton.autostate import run_stop_command
from baton.config import ROUTE_TIERS, ConfigError, find_config_path, load_config
from baton.doctor import run_doctor
from baton.git import main_root
from baton.limits import LimitStateError, LimitStore, parse_duration
from baton.next import DEFAULT_AGENT_TIMEOUT, DEFAULT_VERIFY_TIMEOUT, run_next_command
from baton.planner import run_plan_command
from baton.probe import DEFAULT_PROMPT, DEFAULT_TIMEOUT, run_probe_command, run_recheck_command
from baton.roadmapper import run_roadmap_command
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
    subparsers.add_parser("stats", help="summarize recorded agent attempts and usage")
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
        "--resume",
        action="store_true",
        help="continue the open step from the partial diff in the working tree",
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
    pr = subparsers.add_parser("pr", help="publish a completed milestone pull request")
    pr.add_argument("--path", default=".", metavar="DIR")
    pr.add_argument("--merge", action="store_true", help="wait for CI and merge reviewed revision")
    auto = subparsers.add_parser(
        "auto", help="work through the whole roadmap: plan, steps, PR, merge, next milestone"
    )
    auto.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    auto.add_argument(
        "--roadmap",
        metavar="PATH",
        help="roadmap file inside the repository (default: docs/ROADMAP.md)",
    )
    auto.add_argument("--max-steps", type=int, metavar="N", help="stop after N implemented steps")
    auto.add_argument("--max-hours", type=float, metavar="H", help="stop after H hours")
    auto.add_argument(
        "--no-merge", action="store_true", help="stop after publishing the milestone PR"
    )
    auto.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT,
        metavar="S",
        help="seconds before an agent is killed (default: %(default)s)",
    )
    auto.add_argument(
        "--verify-timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        metavar="S",
        help="seconds before a Verify command is killed (default: %(default)s)",
    )
    stop = subparsers.add_parser("stop", help="ask baton auto to stop at the next safe point")
    stop.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    plan = subparsers.add_parser(
        "plan", help="plan the next open roadmap milestone headless into .agent-plan.md"
    )
    plan.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    plan.add_argument(
        "--milestone", metavar="ID", help="plan this milestone (e.g. M3) instead of the next one"
    )
    plan.add_argument(
        "--roadmap",
        metavar="PATH",
        help="roadmap file inside the repository (default: docs/ROADMAP.md)",
    )
    plan.add_argument("--agent", metavar="NAME", help="use a named agent from the config")
    plan.add_argument(
        "--tier", choices=ROUTE_TIERS, help="override the milestone size used for routing"
    )
    plan.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT,
        metavar="S",
        help="seconds before the agent is killed (default: %(default)s)",
    )
    plan.add_argument(
        "--dry-run", action="store_true", help="print the command and prompt, change nothing"
    )
    roadmap = subparsers.add_parser(
        "roadmap", help="turn an idea into docs/ROADMAP.md with a headless planner"
    )
    roadmap.add_argument("idea", nargs="?", help="what to build (or use --from FILE)")
    roadmap.add_argument("--from", dest="from_file", metavar="FILE", help="read the idea from FILE")
    roadmap.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    roadmap.add_argument("--agent", metavar="NAME", help="use a named agent from the config")
    roadmap.add_argument(
        "--tier", choices=ROUTE_TIERS, help="route tier of plan_roadmap (default: L)"
    )
    roadmap.add_argument(
        "--yes", action="store_true", help="take the suggested answers instead of asking"
    )
    roadmap.add_argument(
        "--commit", action="store_true", help="commit docs/ROADMAP.md as 'docs: add roadmap'"
    )
    roadmap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT,
        metavar="S",
        help="seconds before the agent is killed (default: %(default)s)",
    )
    roadmap.add_argument(
        "--dry-run", action="store_true", help="print the command and prompt, change nothing"
    )
    audit = subparsers.add_parser(
        "audit", help="review every tracked file batch by batch into docs/AUDIT.md"
    )
    audit.add_argument(
        "--path", default=".", metavar="DIR", help="repository root (default: current directory)"
    )
    audit.add_argument("--agent", metavar="NAME", help="use a named agent from the config")
    audit.add_argument("--tier", choices=ROUTE_TIERS, help="route tier of review (default: M)")
    audit.add_argument(
        "--max-batches", type=int, metavar="N", help="stop after N batches; rerun to continue"
    )
    audit.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="review up to N batches at the same time (default: %(default)s)",
    )
    audit.add_argument(
        "--commit", action="store_true", help="commit docs/AUDIT.md as 'docs: add code audit'"
    )
    audit.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT,
        metavar="S",
        help="seconds before the agent is killed (default: %(default)s)",
    )
    audit.add_argument(
        "--dry-run",
        action="store_true",
        help="print counts, command and first prompt, change nothing",
    )
    audit.add_argument(
        "--fix",
        action="store_true",
        help="work through the committed docs/AUDIT.md milestones like `baton auto`",
    )
    audit.add_argument("--max-steps", type=int, metavar="N", help="with --fix: stop after N steps")
    audit.add_argument(
        "--max-hours", type=float, metavar="H", help="with --fix: stop after H hours"
    )
    audit.add_argument(
        "--no-merge", action="store_true", help="with --fix: stop after publishing the milestone PR"
    )
    audit.add_argument(
        "--verify-timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        metavar="S",
        help="with --fix: seconds before a Verify command is killed (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "stats":
        from baton.runs import print_stats

        try:
            print_stats(sys.stdout)
        except LimitStateError as exc:
            print(str(exc), file=sys.stderr)
            return 1
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
        return run_status(main_root(Path(args.path)), config_path=args.config)
    if args.command == "next":
        return run_next_command(
            main_root(Path(args.path)),
            config_path=args.config,
            agent_name=args.agent,
            tier=args.tier,
            agent_timeout=args.timeout,
            verify_timeout=args.verify_timeout,
            dry_run=args.dry_run,
            commit=args.commit,
            push=args.push,
            worktree=args.worktree,
            resume=args.resume,
        )
    if args.command == "pr":
        from baton.pr import run_pr

        return run_pr(main_root(Path(args.path)), merge=args.merge)
    if args.command == "auto":
        from baton.auto import run_auto_command

        return run_auto_command(
            main_root(Path(args.path)),
            config_path=args.config,
            max_steps=args.max_steps,
            max_hours=args.max_hours,
            merge=False if args.no_merge else None,
            roadmap=args.roadmap,
            agent_timeout=args.timeout,
            verify_timeout=args.verify_timeout,
        )
    if args.command == "stop":
        return run_stop_command(main_root(Path(args.path)))
    if args.command == "plan":
        return run_plan_command(
            main_root(Path(args.path)),
            config_path=args.config,
            milestone_id=args.milestone,
            roadmap=args.roadmap,
            agent_name=args.agent,
            tier=args.tier,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    if args.command == "roadmap":
        return run_roadmap_command(
            main_root(Path(args.path)),
            idea=args.idea,
            from_file=args.from_file,
            config_path=args.config,
            agent_name=args.agent,
            tier=args.tier,
            timeout=args.timeout,
            yes=args.yes,
            commit=args.commit,
            dry_run=args.dry_run,
        )
    if args.command == "audit":
        if args.jobs < 1:
            parser.error("--jobs must be at least 1")
        if args.fix:
            scan_only = {
                "--agent": args.agent,
                "--tier": args.tier,
                "--max-batches": args.max_batches,
                "--jobs": None if args.jobs == 1 else args.jobs,
                "--commit": args.commit,
                "--dry-run": args.dry_run,
            }
            used = [flag for flag, value in scan_only.items() if value not in (None, False)]
            if used:
                parser.error(f"--fix cannot be combined with {', '.join(used)}")
            return run_audit_fix(
                main_root(Path(args.path)),
                config_path=args.config,
                max_steps=args.max_steps,
                max_hours=args.max_hours,
                merge=False if args.no_merge else None,
                agent_timeout=args.timeout,
                verify_timeout=args.verify_timeout,
            )
        fix_only = {
            "--max-steps": args.max_steps,
            "--max-hours": args.max_hours,
            "--no-merge": args.no_merge,
        }
        used = [flag for flag, value in fix_only.items() if value not in (None, False)]
        if used:
            parser.error(f"{', '.join(used)} only apply with --fix")
        return run_audit_command(
            main_root(Path(args.path)),
            config_path=args.config,
            agent_name=args.agent,
            tier=args.tier,
            timeout=args.timeout,
            max_batches=args.max_batches,
            jobs=args.jobs,
            commit=args.commit,
            dry_run=args.dry_run,
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
