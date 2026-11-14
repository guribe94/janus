"""The janus command line: argparse, and the dispatch table from subcommand to command function (the only file with
subcommand wiring)."""
import argparse, contextlib, os, sys, tomllib
from pathlib import Path
from janus.actions.jobs import LANES
from janus.context import CONF, Cfg, Err, Run, find_root, old_layout, projects, say, session_root
from janus.board import cmd_answer
from janus.hooks import cmd_hook
from janus.memory import MEM_SECTIONS
from janus.migrate import migrate
from janus.setup import doctor, init_project
from janus.actions.backlog import cmd_backlog, cmd_fill
from janus.actions.jobs import cmd_busy, cmd_cancel, cmd_hosts, cmd_run, cmd_thrash
from janus.actions.landing import cmd_commit_state, cmd_land
from janus.actions.launch import cmd_api_run, cmd_launch
from janus.actions.lifecycle import cmd_board_close, cmd_close, cmd_collect, cmd_park, cmd_resume as cmd_resume_unit, cmd_stop
from janus.actions.swarm import cmd_forget, cmd_index, cmd_ledger, cmd_memory, cmd_remember, cmd_report, cmd_transcript
from janus.actions.tidy import cmd_tidy
from janus.check import cmd_capacity, cmd_check, cmd_status
from janus.monitor import cmd_monitor, interval_arg
from janus.setup import cmd_doctor, cmd_init, cmd_install_host, cmd_queue, cmd_register, cmd_resume_orch
from janus.watch import cmd_watch

LOCKS_LATE = {"collect"}   # writers that take the lock themselves, once their remote reads are done
WRITERS = {"launch", "park", "resume", "stop", "collect", "close", "fill", "tidy", "thrash", "cancel", "ledger", "backlog",
           "remember", "forget", "memory", "index", "register", "board-close", "report"}

def cmd_resume(cfg, a):
    """janus resume: a parked unit when one is named, else the orchestration itself (janus/setup.cmd_resume_orch): continue it
    in a Claude or Codex session with nothing lost."""
    if a.unit is None: return cmd_resume_orch(cfg, a)
    if a.runtime or a.here: raise Err("--runtime and --here resume the orchestration: give no unit")
    return cmd_resume_unit(cfg, a)

def main():
    ap = argparse.ArgumentParser(prog="janus"); ap.add_argument("--project", metavar="PATH", help="the project root (a worker outside it names it; default: found from the cwd, or $JANUS_ROOT)"); sp = ap.add_subparsers(dest="cmd", required=True)
    def add(n, fn, *args, dry=False):   # the subcommand, the function it runs, its arguments
        p = sp.add_parser(n); p.set_defaults(fn=fn)
        for x in args: p.add_argument(*x[0], **x[1])
        if dry: p.add_argument("--dry-run", action="store_true")
        return p
    A = lambda *n, **k: (n, k)
    add("check", cmd_check, A("--brief", action="store_true"), A("--json", action="store_true", help="the findings as JSON (key, family, text, urgent)"))
    add("hosts", cmd_hosts, A("--refresh", action="store_true", help="discover every host again now"), A("--json", action="store_true"))
    add("monitor", cmd_monitor, A("--interval", type=interval_arg, default=5.0, metavar="SECONDS", help="seconds between refreshes, counted from the end of the last one (default 5, at least 1)"))
    add("capacity", cmd_capacity); add("status", cmd_status); add("watch", cmd_watch, A("--alert", action="store_true", help="only tell the orchestrator about new board questions and blocks"),
        A("--reset", action="store_true", help="the Stop hook's context reset: the tool's new-session command, verified, then the wake prompt"))
    add("launch", cmd_launch, A("unit"), A("--kind", choices=["implement", "review", "audit", "cleanup"], required=True), A("--tier", choices=["critical", "normal"], default="normal"),
        A("--host"), A("--model"), A("--brief"), A("--cwd"), A("--new-worktree", metavar="REPO", help="make a fresh worktree of this repo (a new branch named after the unit) for the unit, under .janus/worktrees"), A("--from", dest="from_ref", metavar="REF", help="--new-worktree: the branch or commit to start from (default main)"), A("--budget", type=float, help="USD budget for this unit (overrides the provider's budget_per_unit)"),
        A("--test-host", help="host where the unit's builds and tests run; the agent is placed there"), A("--allow-main", action="store_true", help="allow a main checkout as cwd"), A("--of", help="unit whose implementer's vendor a review must avoid"),
        A("--report", help="a resumed unit: the report it continues (default out/<unit>/REPORT.md)"), A("--resume-of", help="a resumed unit: the unit whose report it continues"),
        A("--item", help="the backlog item this unit works on (janus backlog)"), dry=True)
    add("api-run", cmd_api_run, A("unit"), A("--run", required=True), A("--model", required=True), A("--budget", type=float, required=True), A("--brief", required=True),
        A("--cwd", required=True), A("--report"), A("--resume-session", help="continue this agent session"), A("--prompt-file", help="the prompt (default: the brief's)"))
    add("park", cmd_park, A("unit"), dry=True)
    add("resume", cmd_resume, A("unit", nargs="?", help="a parked unit to resume; with no unit: continue the orchestration in a new session"),
        A("--runtime", choices=["claude", "codex"], help="the runtime for the new orchestrator session (default: the registered one)"),
        A("--here", action="store_true", help="register this session as the orchestrator, instead of starting a new one"),
        A("--message", help="resume a unit with this text instead of its jobs' results"), A("--force", action="store_true", help="resume although its jobs have not finished"),
        A("--budget", type=float, help="a new budget for this run (same tree), instead of the unit's remaining budget"), dry=True)
    add("ledger", cmd_ledger, A("action", choices=["check", "repair", "reconcile", "recost"]), A("--provider"), dry=True)
    add("doctor", cmd_doctor, A("--no-hosts", action="store_true", help="skip the ssh checks"))
    add("backlog", cmd_backlog, A("action", choices=["import", "list", "set"]), A("args", nargs="*", help="set: <item id> actor=<agent|owner|hardware|...>"), dry=True)
    add("install-host", cmd_install_host, A("host", help="a host of janus.toml: folders, pueued as a user service, a janus copy, the heartbeat watchdog"), dry=True)
    add("queue", cmd_queue, A("host", nargs="?"), A("--log", metavar="[HOST:]ID", help="print one task's output: <host>:<id> (ids collide between hosts); the job's run log when pueue has none"))
    add("stop", cmd_stop, A("unit"), A("--force", action="store_true", help="stop although its remote copy has uncollected work"), dry=True)
    add("collect", cmd_collect, A("unit", help="a unit, or <host>:<task id> for a janus run job"), dry=True)
    add("run", cmd_run, A("--host", help="run there (pinned)"), A("--needs", action="append", help="requirements instead of a host: os, arch, features, devices, mem (linux,x86_64,device=usb,mem=4G); placed when a host that meets them has room"),
        A("--place-wait", action="store_true", help=argparse.SUPPRESS), A("--broad", metavar="REASON", help="a whole-suite run: why it must be whole (it runs at low priority)"), A("--lane", choices=[x for x in LANES if x != "place"], default="jobs"), A("--unit"), A("--wt", required=True),
        A("--subdir", help="run from this subdirectory of the worktree"), A("--usb", action="store_true", help="a job that needs the host's USB devices (container.usb_args, usb_lock)"),
        A("--priority", type=int, default=0), A("--detach", "--no-wait", dest="no_wait", action="store_true", help="queue, print <host>:<task>, return (agents: then write WAITING <ids> and end the turn)"),
        A("--allow-unmerged", action="store_true", help="run on a tree with a merge in progress or conflict markers"),
        A("--inputs", nargs="+", action="extend", metavar="PATH", help="the paths (relative to the worktree) the result depends on: a job whose command, host kind, toolchain and inputs already passed is not run again (CACHED PASS, exit 0); end the list with --"),
        A("--no-cache", action="store_true", help="run although the same inputs already passed"),
        A("command", nargs=argparse.REMAINDER), dry=True)
    add("thrash", cmd_thrash, dry=True)
    add("tidy", cmd_tidy, A("action", nargs="?", choices=["hold", "unhold", "holds"], help="hold/unhold one item so tidy never removes it, or list the holds"),
        A("target", nargs="?", help="hold/unhold: the item to keep (a worktree path, a host:name, a test root)"),
        A("--why", help="hold: why it is kept"), dry=True)
    add("busy", cmd_busy, A("wt", help="a worktree: exit 0 while a janus job, a unit's job or a mutation run uses it, else 1"))
    add("cancel", cmd_cancel, A("job", help="<host>:<task id> of a janus run job"), A("--force", action="store_true", help="cancel although its files could not be saved"), dry=True)
    add("fill", cmd_fill, A("--max", type=int, help="launch at most this many on each host"), dry=True)
    add("report", cmd_report, A("--brief", action="store_true", help="summary lines only"))
    add("land", cmd_land, A("unit"), A("--host", help="land on this host (default: the unit's own host when its tree is there, else here)"), A("--push", action="store_true", help="also push the landing commit, never forced (default: [landing] push)"), dry=True)
    add("close", cmd_close, A("unit"), A("--force", action="store_true"), A("--reason"),
        A("--duplicate-of", metavar="UNIT", help="close a stopped relaunch that duplicated this open unit: no verdict needed, never --force"), dry=True)
    add("commit-state", cmd_commit_state, A("-m", "--message", default="janus: state"), dry=True)
    m = add("remember", cmd_remember, A("text"), A("--source", help="where it can be checked: a path, a commit sha or a job (a fact needs one)"),
            A("--key", help="its subject: a newer entry on the same subject replaces the older one"), dry=True)
    g = m.add_mutually_exclusive_group(required=True)
    for k in MEM_SECTIONS: g.add_argument(f"--{k}", action="store_true")
    add("forget", cmd_forget, A("text", help="the entry's number (janus memory), its subject, or its text"), dry=True)
    add("index", cmd_index, dry=True)
    add("transcript", cmd_transcript, A("unit"), A("--grep", help="only the lines matching this regex (case-insensitive)"), A("--tail", type=int, default=40, help="the last N lines (0: all)"))
    add("memory", cmd_memory)
    add("init", cmd_init, A("--name", help="project name for a new .janus/janus.toml (default: the folder's name)"), dry=True)
    add("migrate", migrate, A("source", nargs="?", help="the old orchestration root (the folder holding janus.toml; default: found from here)"), A("--to", metavar="TARGET", help="the workspace root that will own .janus (default: the source itself)"), A("--apply", action="store_true", help="do it (default: a dry run that says what would move and what refuses it)"))
    add("answer", cmd_answer, A("id", help="q<N>: a board question"), A("answer"))
    add("board-close", cmd_board_close, A("agent", help="a board card that is not an open unit"), dry=True)
    add("register", cmd_register, A("session", nargs="?"), dry=True)
    add("hook", cmd_hook, A("event", choices=["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]))
    a = ap.parse_args(sys.argv[1:])
    if a.cmd == "hook":
        try: cmd_hook(a.event)
        except Exception as e: print(f"janus hook: {e}", file=sys.stderr)
        return 0
    if a.cmd == "migrate": return migrate(a.source, a.to, apply=a.apply)   # the config is still at the top: no root to find yet
    root = find_root(os.getcwd() if a.cmd in ("init", "doctor") else None, a.project)   # the one resolver; never $JANUS_ROOT for setup commands
    if a.cmd == "init" and not root: return init_project(Path.cwd(), a)
    if a.cmd == "doctor" and not root: return doctor(None, a)
    if not root:   # say WHERE the root is (the session's registered project, else the registered ones); never fail silently
        sid = next((os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "JANUS_SESSION") if os.environ.get(k)), "")
        mine = session_root(sid) if sid else None
        pr = projects(); old = old_layout()
        hint = (f"; this project still has the old layout at {old}: janus migrate {old} (dry run first)" if old else
                f"; this session's janus root is {mine}" if mine else
                f"; registered janus project(s): {', '.join(map(str, pr))}" if pr else
                "; no janus project is registered here (janus init, or --project <path>)")
        print(f"janus: no {CONF} here or in a parent{hint}", file=sys.stderr); return 2
    try: cfg = Cfg(root, Run(dry=getattr(a, "dry_run", False)))
    except (tomllib.TOMLDecodeError, OSError) as e: print(f"janus: {root / CONF}: {e}", file=sys.stderr); return 2
    # the orchestrator's state-changing commands hold the writer lock (context.Cfg.writer); long ones (land, run, api-run)
    # and agents' calls do not, so a job never blocks the watcher; collect takes the lock itself, after its ssh and rsync
    try:
        with cfg.command_writer() if a.cmd in WRITERS and a.cmd not in LOCKS_LATE else contextlib.nullcontext(): r = a.fn(cfg, a)
    except Err as e: print(f"janus: {e}", file=sys.stderr); return 1
    rc, lines = r if isinstance(r, tuple) else (r if isinstance(r, int) else 0, [])   # a read-only command returns its lines
    for l in lines: say(l)
    return rc
