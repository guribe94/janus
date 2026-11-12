"""The janus command line: argparse, and the dispatch table from subcommand to command function (the only file with
subcommand wiring)."""
import argparse, os, sys, tomllib
from pathlib import Path
from janus.actions.jobs import LANES
from janus.context import Cfg, Err, Run, find_root, say
from janus.decisions import cmd_answer, cmd_ask, cmd_decide, cmd_decisions
from janus.hooks import cmd_hook
from janus.memory import MEM_SECTIONS
from janus.setup import doctor, init_project
from janus.actions.backlog import cmd_backlog, cmd_fill
from janus.actions.jobs import cmd_busy, cmd_cancel, cmd_run, cmd_thrash
from janus.actions.landing import cmd_commit_state, cmd_gate, cmd_gate_exec, cmd_gate_judge, cmd_gate_scope, cmd_land
from janus.actions.launch import cmd_api_run, cmd_launch
from janus.actions.lifecycle import cmd_board_close, cmd_close, cmd_collect, cmd_park, cmd_resume, cmd_stop
from janus.actions.swarm import cmd_forget, cmd_index, cmd_introduce, cmd_learned, cmd_ledger, cmd_memory, cmd_peers, cmd_remember, cmd_report, cmd_transcript
from janus.actions.tidy import cmd_tidy
from janus.check import cmd_ack, cmd_capacity, cmd_check, cmd_status
from janus.quality import cmd_quality
from janus.setup import cmd_doctor, cmd_init, cmd_queue, cmd_register, migrate
from janus.soak import cmd_soak
from janus.watch import cmd_watch

def main():
    ap = argparse.ArgumentParser(prog="janus"); sp = ap.add_subparsers(dest="cmd", required=True)
    def add(n, fn, *args, dry=False):   # the subcommand, the function it runs, its arguments
        p = sp.add_parser(n); p.set_defaults(fn=fn)
        for x in args: p.add_argument(*x[0], **x[1])
        if dry: p.add_argument("--dry-run", action="store_true")
        return p
    A = lambda *n, **k: (n, k)
    add("check", cmd_check, A("--brief", action="store_true"), A("--escalate", action="store_true"), A("--json", action="store_true", help="the findings as JSON (key, family, text, urgent, ackable, open_since)"))
    add("capacity", cmd_capacity); add("status", cmd_status); add("watch", cmd_watch, A("--alert", action="store_true", help="only tell the orchestrator about new board questions and blocks"))
    add("learned", cmd_learned, A("--reject", help="reject learned post <agent>:<n> (check stops listing it)"), A("--why", help="why it is rejected"))
    add("launch", cmd_launch, A("unit"), A("--kind", choices=["implement", "review", "audit", "cleanup"], required=True), A("--tier", choices=["critical", "normal"], default="normal"),
        A("--host"), A("--model"), A("--brief"), A("--cwd"), A("--budget", type=float, help="USD budget for this unit (overrides the provider's budget_per_unit)"),
        A("--test-host", help="host where the unit's builds and tests run; the agent is placed there"), A("--allow-main", action="store_true", help="allow a main checkout as cwd"), A("--of", help="unit whose implementer's vendor a review must avoid"),
        A("--report", help="a resumed unit: the report it continues (default out/<unit>/REPORT.md)"), A("--resume-of", help="a resumed unit: the unit whose report it continues"),
        A("--item", help="the backlog item this unit works on (janus backlog)"), dry=True)
    add("api-run", cmd_api_run, A("unit"), A("--run", required=True), A("--model", required=True), A("--budget", type=float, required=True), A("--brief", required=True),
        A("--cwd", required=True), A("--report"), A("--resume-session", help="continue this agent session"), A("--prompt-file", help="the prompt (default: the brief's)"))
    add("park", cmd_park, A("unit"), dry=True)
    add("resume", cmd_resume, A("unit"), A("--message", help="resume with this text instead of its jobs' results"), A("--force", action="store_true", help="resume although its jobs have not finished"), dry=True)
    add("ledger", cmd_ledger, A("action", choices=["check", "repair", "reconcile"]), A("--provider"), dry=True)
    add("doctor", cmd_doctor, A("--no-hosts", action="store_true", help="skip the ssh checks"))
    add("backlog", cmd_backlog, A("action", choices=["import", "list", "set"]), A("args", nargs="*", help="set: <item id> actor=<agent|owner|hardware|...>"), dry=True)
    add("queue", cmd_queue, A("host", nargs="?"), A("--log", type=int, help="print this task's log"))
    add("stop", cmd_stop, A("unit"), A("--force", action="store_true", help="stop although its remote copy has uncollected work"), dry=True)
    add("collect", cmd_collect, A("unit", help="a unit, or <host>:<task id> for a janus run job"), dry=True)
    add("run", cmd_run, A("--host", required=True), A("--lane", choices=LANES, default="jobs"), A("--unit"), A("--wt", required=True),
        A("--subdir", help="run from this subdirectory of the worktree"), A("--usb", action="store_true", help="a job that needs the host's USB devices (container.usb_args, usb_lock)"),
        A("--priority", type=int, default=0), A("--detach", "--no-wait", dest="no_wait", action="store_true", help="queue, print <host>:<task>, return (agents: then write WAITING <ids> and end the turn)"),
        A("--allow-unmerged", action="store_true", help="run on a tree with a merge in progress, conflict markers or far behind the landing branch"),
        A("command", nargs=argparse.REMAINDER), dry=True)
    add("gate", cmd_gate, A("name"), A("--host", help="test host for the whole gate (passed in paths.gate_host_env)"), A("--unit", help="the unit this gate checks (default $JANUS_UNIT)"), A("--priority", type=int, default=10),
        A("--wait", action="store_true", help="wait and print the RESULT line; exit 0 on PASS"),
        A("--timeout-min", type=float, help="stop the gate after this long (default [landing] gate_timeout_min; 0: none)"),
        A("--quick", action="store_true", help="a short gate: the gates-quick slot when [landing] quick_gate_min is set"),
        A("--allow-unmerged", action="store_true", help="gate a tree mid-merge, with conflict markers or far behind the landing branch"), dry=True)
    add("gate-judge", cmd_gate_judge, A("name"), A("rc", type=int))
    add("gate-exec", cmd_gate_exec, A("name"), A("--timeout-min", type=float, default=0), A("command", nargs=argparse.REMAINDER))
    add("gate-scope", cmd_gate_scope, A("unit"), A("--name", help="the gate's name (default land-<unit>)"), dry=True)
    add("thrash", cmd_thrash, dry=True)
    add("tidy", cmd_tidy, dry=True)
    add("busy", cmd_busy, A("wt", help="a worktree: exit 0 while a janus job, a unit's job or a mutation run uses it, else 1"))
    add("cancel", cmd_cancel, A("job", help="<host>:<task id> of a janus run/gate job"), A("--force", action="store_true", help="cancel although its files could not be saved"), dry=True)
    add("fill", cmd_fill, A("--max", type=int, help="launch at most this many"), dry=True)
    add("report", cmd_report, A("--brief", action="store_true", help="summary lines only"), A("--sent", action="store_true", help="record that the report was sent (clears report-due)"))
    add("land", cmd_land, A("unit"), A("--no-gate", action="store_true", help="skip the final gate (it is still refused mid-merge)"),
        A("--no-dispatch", action="store_true", help="on a conflict, only say what merge unit to dispatch"), dry=True)
    add("close", cmd_close, A("unit"), A("--force", action="store_true"), A("--reason"), dry=True)
    add("commit-state", cmd_commit_state, A("-m", "--message", default="janus: state"), dry=True)
    add("ack", cmd_ack, A("problem"), A("decision"))
    m = add("remember", cmd_remember, A("text"), A("--source", help="where it can be checked: a path, a commit sha or a job (a fact needs one)"),
            A("--key", help="its subject: a newer entry on the same subject replaces the older one"), A("--learned", help="the learned post it accepts (<agent>:<n>)"), dry=True)
    g = m.add_mutually_exclusive_group(required=True)
    for k in MEM_SECTIONS: g.add_argument(f"--{k}", action="store_true")
    add("forget", cmd_forget, A("text", help="the entry's number (janus memory), its subject, or its text"), dry=True)
    add("index", cmd_index, dry=True); add("peers", cmd_peers)
    add("introduce", cmd_introduce, A("a"), A("b"), A("--why", required=True, help="why they should know each other (it goes to both)"),
        A("--topic", help="the regex each reads in the other's transcript (default: a path named in --why)"), dry=True)
    add("quality", cmd_quality, A("action", choices=["lint", "review"], help="lint: a brief alone; review: the pre-landing review"),
        A("arg", nargs="?", help="lint: a brief path or unit name; review: the unit"), A("--override", help="review: accept the findings with this reason"), dry=True)
    add("soak", cmd_soak, A("--minutes", type=float, default=1.0, help="run this many minutes (0: one pass)"),
        A("--live", action="store_true", help="also run one watcher pass (default: read-only)"))
    add("transcript", cmd_transcript, A("unit"), A("--grep", help="only the lines matching this regex (case-insensitive)"), A("--tail", type=int, default=40, help="the last N lines (0: all)"))
    add("memory", cmd_memory, A("--keep", type=int, help="re-date entry N after review (it stays)"), dry=True)
    add("decide", cmd_decide, A("title"), A("--by", choices=["owner", "orchestrator"], required=True),
        A("--context", help="what was true and what forced the decision"), A("--options", action="append", help="one option with its trade-off (repeatable)"),
        A("--decision", help="what was decided, in one sentence"), A("--why", help="the ruling, constraint or measurement that decided it"),
        A("--consequences", help="what it makes easy, forbids or requires"), A("--supersedes", type=int, help="the record this one replaces; its status is marked"),
        A("--instruction", help="the owner instruction (beads id) this settles: it is linked and labelled"), dry=True)
    add("decisions", cmd_decisions, A("--grep", help="only records matching this regex (case-insensitive)"))
    add("ask", cmd_ask, A("question"), A("--options", action="append", help="one option for the owner (repeatable)"),
        A("--recommend", help="what you recommend"), dry=True)
    add("answer", cmd_answer, A("id", type=int), A("answer"), A("--decide", action="store_true", help="also write a decision record of the answer"), dry=True)
    add("init", cmd_init, A("--name", help="project name for a new janus.toml (default: the folder's name)"), dry=True)
    add("migrate", migrate, A("project", nargs="?", help="a project with an old orch.toml/.orch to move to janus (default: here)"))
    add("board-close", cmd_board_close, A("agent", help="a board card that is not an open unit"), dry=True)
    add("register", cmd_register, A("session", nargs="?"), dry=True)
    add("hook", cmd_hook, A("event", choices=["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]))
    a = ap.parse_args()
    if a.cmd == "hook":
        try: cmd_hook(a.event)
        except Exception as e: print(f"janus hook: {e}", file=sys.stderr)
        return 0
    if a.cmd == "migrate": return migrate(a.project)   # no janus.toml yet: the project still has orch.toml
    root = find_root(os.getcwd()) if a.cmd in ("init", "doctor") else find_root()   # never $JANUS_ROOT for setup commands
    if a.cmd == "init" and not root: return init_project(Path.cwd(), a)
    if a.cmd == "doctor" and not root: return doctor(None, a)
    if not root: print("janus: no janus.toml here or in a parent (janus init writes a starter one)", file=sys.stderr); return 2
    try: cfg = Cfg(root, Run(dry=getattr(a, "dry_run", False)))
    except (tomllib.TOMLDecodeError, OSError) as e: print(f"janus: {root / 'janus.toml'}: {e}", file=sys.stderr); return 2
    try: r = a.fn(cfg, a)
    except Err as e: print(f"janus: {e}", file=sys.stderr); return 1
    rc, lines = r if isinstance(r, tuple) else (r if isinstance(r, int) else 0, [])   # a read-only command returns its lines
    for l in lines: say(l)
    return rc
