"""janus init, doctor, queue, register and install-host: a shareable project setup, new machines, and the one registered
orchestrator session."""
import argparse, concurrent.futures as cf, json, os, re, shlex, sys, time, tomllib
from pathlib import Path
from types import SimpleNamespace
from janus.actions.jobs import facts_line, remote_wt
from janus.actions.landing import SECRETS, cmd_commit_state, push_cmd
from janus.actions.launch import new_tab, wait_shell
from janus.beads import Beads
from janus.board import board_dir, board_script
from janus.context import (CLAIM_WINDOW, Err, JANUS_MARK, JANUS_SELF, SSH, Run, atomic_write, clean_env, herdr, hrun, now, pending_age, qp,
    record_session_proc, say, session_alive, sh, slug, ts, wake_clear)
from janus.hooks import session_start_text
from janus.ledger import ledger_read, read_secret, spend
from janus.platform import pane_tool, run, watcher_loaded, watcher_service
from janus.probe import discover, probe, pueue
from janus.project import label_owner, project_id, tmux_name
from janus.units import all_units, ws_id

def cmd_register(cfg, a):
    """The only way to change the registered orchestrator session (with the watcher's pending claim for a new one it started)."""
    if os.environ.get("JANUS_ROLE") == "agent": raise Err("refused: an agent (JANUS_ROLE=agent) cannot register as the orchestrator (issue 2)")
    sid = a.session or next((os.environ[k] for k in ("JANUS_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID") if os.environ.get(k)), None)
    kind = "codex" if os.environ.get("CODEX_THREAD_ID") and not os.environ.get("CLAUDECODE") else "claude"
    old = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    if old and sid and old != sid and session_alive(cfg): say(f"note: replacing registered session {old}, whose process is still alive")
    if cfg.run.dry: say(f"+ register {sid or 'pending'} ({kind})"); return
    cfg.f("session").write_text(sid or f"pending:{int(now())}"); cfg.f("session_kind").write_text(kind + "\n")
    if sid: record_session_proc(cfg)
    # the caller's own pane only (HERDR_PANE_ID): `herdr pane current` is the focused pane, which is often another (issue 2)
    pid = os.environ.get("HERDR_PANE_ID")
    if pid: cfg.f("pane").write_text(pid)
    else: say("note: no HERDR_PANE_ID, so no pane recorded (the watcher then judges by the session's process and transcript)")
    cfg.touch("last_cycle"); cfg.touch("last_ack")
    say("registered " + (sid or "(pending: the next hook event from this session claims it)") + f" ({kind})")
    if cfg.c.get("orchestrator", {}).get("kind", "claude") != kind:
        say(f"warning: janus.toml [orchestrator] kind is {cfg.c.get('orchestrator', {}).get('kind')}, but this session is {kind}: set it to {kind}")
    ws = ws_id(cfg, create=True)
    if ws:   # plain shell loops: `watch` is not installed on the Mac (M10)
        have = {t.get("label") for t in (herdr("tab", "list", "--workspace", ws) or {}).get("tabs", [])}
        for lab, cmd in (("status", "while :; do clear; janus status; sleep 60; done"),
                         ("board", f"while :; do clear; BOARD_DIR={shlex.quote(str(board_dir(cfg)))} {shlex.quote(str(board_script(cfg)))} summary; sleep 60; done"),
                         ("watcher", f"tail -f {shlex.quote(str(cfg.f('watch.log')))}")):
            if lab in have: continue
            r = herdr("tab", "create", "--workspace", ws, "--label", lab, "--cwd", str(cfg.root), "--no-focus")
            if r: herdr("pane", "run", r["root_pane"]["pane_id"], cmd)

# janus resume (imp-resume): continue the orchestration in a Claude or Codex session with nothing lost.
#
# The owner's ask (bd janus-resume): `janus resume [--runtime claude|codex] [--here]` — safe takeover of the registered
# orchestrator, the state committed and pushed (pulled first when this machine is behind), running agents and jobs untouched,
# the new session given the handoff, the standing rules and the decisions index, and the runtime's hooks checked.
TAKEOVER_MSG = "janus resume is taking over: stop now, do not start new work, and end your turn."
CODEX_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")

def _orch_state(pane):
    """The registered pane's herdr agent status ("working", "idle", ...), or None when herdr does not answer or the pane has no
    agent. The watcher judges the orchestrator the same way (janus/watch.py)."""
    ag = herdr("agent", "get", pane) if pane else None
    return ((ag or {}).get("agent") or {}).get("agent_status")

def state_repo(cfg):
    """The git repo that holds the state: the project root when the state folder is inside a repo (its usual place, committed by
    janus commit-state), else the state folder itself when that is a repo of its own. None when no repo holds it."""
    rc, o, _ = sh(["git", "-C", str(cfg.state), "rev-parse", "--show-toplevel"])
    if rc == 0 and Path(o.strip()).resolve() == cfg.state.resolve(): return cfg.state
    if sh(["git", "-C", str(cfg.root), "rev-parse", "--is-inside-work-tree"])[0] == 0: return cfg.root
    return None

def sync_state(cfg):
    """Nothing lost (item 3): pull the state first when this machine is behind its remote, then commit and push it, so another
    machine that resumes next sees the same state. Refuses on diverged histories (a hand merge, never a silent one). Running
    agents and jobs are untouched (they belong to their hosts' queues); a parked unit keeps its jobs."""
    repo = state_repo(cfg)
    if repo is None: say("state: no git repo holds the state folder: nothing to pull or push"); return
    if not sh(["git", "-C", str(repo), "remote"])[1].split(): say("state: no git remote: nothing to pull or push"); return
    if cfg.run.dry: say(f"+ [{repo}] git fetch; git pull --ff-only when behind; commit and push the state"); return
    rc, o, e = sh(["git", "-C", str(repo), "fetch", "--quiet"], timeout=120)
    if rc: say(f"state: fetch failed ({(e or o).strip()[:80]}); syncing locally")
    elif sh(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "@{u}"])[0] == 0:
        behind = sh(["git", "-C", str(repo), "rev-list", "--count", "HEAD..@{u}"])[1].strip()
        if behind and behind != "0":
            rc, o, e = sh(["git", "-C", str(repo), "pull", "--ff-only", "--quiet"], timeout=180)
            if rc: raise Err(f"state is {behind} commit(s) behind its remote and cannot fast-forward (diverged): resolve it by hand, then janus resume")
            say(f"state pulled from its remote ({behind} commit(s))")
    if repo == cfg.root: cmd_commit_state(cfg, argparse.Namespace(message="janus: state"))
    else:   # the state folder is a repo of its own: the same steps, staged under it (a secret is never committed)
        sh(["git", "-C", str(repo), "add", "-A"], timeout=120)
        paths = [x for x in sh(["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z"])[1].split("\0") if x]
        hit = [sp for sp in paths for r in SECRETS if (cfg.state / sp).is_file() and r.search((cfg.state / sp).read_bytes())]
        if hit: sh(["git", "-C", str(repo), "reset", "-q"]); raise Err("refused (unstaged again), possible secrets in the state: " + ", ".join(hit))
        if not paths: say("state: nothing to commit"); return
        for cmd in (["git", "-C", str(repo), "commit", "-m", "janus: state"], push_cmd(repo, cfg)):
            rc, o, e = sh(cmd, cwd=str(repo), timeout=120)
            if rc: raise Err(f"{' '.join(cmd[:3])} failed: {(e or o).strip()[-160:]}")
        say(f"state committed and pushed ({len(paths)} path(s))")

def takeover(cfg):
    """Safe takeover (item 1): a registered orchestrator that is alive is asked, through its pane, to stop, and waited for a
    bounded time ([orchestrator] takeover_wait_s, default 60). Never two at once: when it is still working after the wait, or
    its process is alive but its pane cannot be reached, refuse and start nothing. The registration switch that follows makes
    the old session inert (its hooks then match no project), and the whole resume holds the state writer lock (imp/autopilot)."""
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    if not pane:
        if session_alive(cfg): say("takeover: a registered session is alive but no pane was recorded, so it cannot be asked to stop")
        return
    pl = herdr("pane", "list")
    if pl is None: raise Err("takeover: herdr did not answer, so the registered orchestrator's state cannot be confirmed; nothing started")
    present = pane in {p.get("pane_id") for p in pl.get("panes", [])}
    st = _orch_state(pane) if present else None
    # A session was just started (session = pending:<ts>) and has not claimed the registration yet: never start a second one
    # beside it (the panel's second-orchestrator hole; the claim window is CLAIM_WINDOW, context.py). After it, the pending claim is
    # stale and a fresh start is correct.
    sess = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    age = pending_age(sess)
    if present and age is not None and age < CLAIM_WINDOW:
        raise Err(f"takeover: a session was started {int(age)} s ago (pending, pane {pane}) and has not "
                  "registered yet: nothing started (two orchestrators must not run)")
    if not present and session_alive(cfg) is not True:
        say(f"takeover: the registered orchestrator is gone (pane {pane} absent, its process ended)"); return
    if session_alive(cfg) is True and st is None:
        raise Err(f"takeover: the registered session's process is still running, but its pane {pane} cannot be reached to ask it to stop: "
                  "stop it, then janus resume")
    if present and st is not None:
        say(f"takeover: asking the registered orchestrator (pane {pane}) to stop")
        if not cfg.run.dry: herdr("agent", "prompt", pane, JANUS_MARK + TAKEOVER_MSG)
    if st == "working":
        wait = float(cfg.c.get("orchestrator", {}).get("takeover_wait_s", 60)); step = min(5.0, max(0.05, wait)); t0 = now()
        while now() - t0 < wait:
            time.sleep(step)
            if _orch_state(pane) != "working": st = None; break
        if st: raise Err(f"takeover: the registered orchestrator (pane {pane}) is still working after {wait:g} s: nothing started "
                         "(two orchestrators must not run); stop it, then janus resume")
    say("takeover: the registered orchestrator has ended its turn")

def start_orchestrator(cfg, kind):
    """Start a new orchestrator session in the requested runtime (item 2), like the watcher's restart path: its own tab in the
    project's herdr workspace, else a tmux session (platform.pane_tool). The registration is left to be claimed by the session's
    first hook event, or by the `janus register` it is told to run. Its SessionStart gives it the handoff, the standing rules and
    the decisions index, and the prompt makes its first act a fresh `janus check` (item 4)."""
    if pane_tool() is None: raise Err("neither herdr nor tmux is installed, so no new session can be started: run janus register in a Claude or Codex session and resume with --here")
    oc = cfg.c.get("orchestrator", {}); args = oc.get("args", []) if oc.get("kind", "claude") == kind else []
    prompt = JANUS_MARK + "You are the orchestrator. Run janus register, then " + wake_clear(cfg, "a new session")
    if cfg.run.dry:
        say(f"+ start a {kind} orchestrator in " + ("a herdr tab" if pane_tool() == "herdr" else "tmux") + f" and prompt it: {wake_clear(cfg, 'a new session')}"); return
    atomic_write(cfg.f("session"), f"pending:{int(now())}"); atomic_write(cfg.f("session_kind"), kind + "\n")
    if pane_tool() == "herdr":
        pane, tab = new_tab(cfg, "orchestrator", cfg.root); wait_shell(pane, "orchestrator")
        rc, o, e = sh(["herdr", "agent", "start", "orchestrator", "--kind", kind, "--pane", pane, "--timeout", "90000", "--", *args], timeout=100)
        if rc: raise Err(f"herdr agent start failed: {(e or o).strip()[:160]}")
        atomic_write(cfg.f("pane"), pane)
        herdr("agent", "prompt", "orchestrator", prompt)
        say(f"started a {kind} orchestrator in a new herdr tab (pane {pane}); its first hook event claims the registration")
    else:
        sess = "janus-orchestrator"
        rc, o, e = sh(["tmux", "new", "-d", "-s", sess, "-c", str(cfg.root), shlex.join([kind, *args])], timeout=30)
        if rc: raise Err("tmux new failed: " + (e or o).strip()[:120])
        atomic_write(cfg.f("pane"), "")   # no herdr pane: the watcher judges this session by its process and transcript
        time.sleep(2)
        if sh(["tmux", "has-session", "-t", sess])[0]: raise Err(f"the {kind} session ended at once; nothing is running")
        sh(["tmux", "send-keys", "-t", sess, "-l", prompt]); sh(["tmux", "send-keys", "-t", sess, "Enter"])
        say(f"started a {kind} orchestrator in tmux session {sess}; its first hook event claims the registration")

def register_here(cfg):
    """--here: make this Claude or Codex session the registered orchestrator (janus register) and hand it the context a
    SessionStart gives (item 4), so an in-place resume loses nothing."""
    if cfg.run.dry: say("+ register this session as the orchestrator and print the handoff, the standing rules and the decisions index"); return
    cmd_register(cfg, argparse.Namespace(session=None))
    say(session_start_text(cfg))

def codex_hooks_check(cfg):
    """Item 5: under Codex the janus hooks must be installed and trusted. janus reads Codex's hook file to confirm they are
    installed; Codex keeps hook trust in a private store janus does not read, so it names the one step (/hooks). Inert when
    Codex's hook file does not exist."""
    p = Path.home() / ".codex/hooks.json"
    if not p.exists(): return
    try: hk = json.loads(p.read_text()).get("hooks", {})
    except Exception: hk = {}
    miss = [ev for ev in CODEX_HOOK_EVENTS if not any("janus hook " + ev in h.get("command", "") for e in hk.get(ev, []) for h in e.get("hooks", []))]
    if miss: say(f"Codex: janus hooks are missing ({', '.join(miss)}): run ./install, then trust them once with /hooks")
    else: say("Codex: janus hooks installed; trust them once with /hooks (Codex keeps its own trust store, which janus does not read)")

def cmd_resume_orch(cfg, a):
    """janus resume [--runtime claude|codex] [--here]: continue the orchestration in a new session with nothing lost. The state
    is committed and pushed first (pulled first when this machine is behind), the registered orchestration is taken over safely,
    then this session is registered (--here) or a new one is started in the requested runtime and given the context a
    SessionStart gives. Under Codex the hook check names the one step (item 5)."""
    if os.environ.get("JANUS_ROLE") == "agent": raise Err("refused: an agent cannot resume the orchestration (issue 2)")
    here = "codex" if os.environ.get("CODEX_THREAD_ID") and not os.environ.get("CLAUDECODE") else "claude"
    kind = here if a.here else (a.runtime or (cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else "")
                                or cfg.c.get("orchestrator", {}).get("kind", "claude"))
    say(f"janus resume: runtime {kind}, " + ("this session (--here)" if a.here else "a new session"))
    sync_state(cfg)   # item 3 first: the state is saved (and pulled) before anything changes
    takeover(cfg)     # item 1: never two orchestrators
    if a.here: register_here(cfg)
    else: start_orchestrator(cfg, kind)
    if kind == "codex": codex_hooks_check(cfg)   # item 5

def init_project(root, a):
    """A new project: write a commented starter janus.toml (from example.janus.toml next to janus.py), create the state, briefs and
    out folders, and say what is left. Never overwrites a janus.toml."""
    root = Path(root).resolve(); t = root / "janus.toml"
    if t.exists(): raise Err(f"{t} exists")
    ex = JANUS_SELF.parent / "example.janus.toml"
    if not ex.is_file(): print(f"janus: {ex} not found (install copies it next to janus.py)", file=sys.stderr); return 2
    name = a.name or slug(root.name) or "project"
    host = (os.uname().nodename.split(".")[0].lower() or "local").replace(" ", "-")
    text = ex.read_text().replace('name = "myproject"', f'name = "{name}"').replace('workspace = "myproject"', f'workspace = "{name}"')
    text = text.replace("[hosts.laptop]", f"[hosts.{host}]").replace('hosts = ["laptop"]', f'hosts = ["{host}"]')
    if a.dry_run: print(text); return 0
    atomic_write(t, text)
    for d in (".janus", "briefs", "out"): (root / d).mkdir(exist_ok=True)
    say(f"wrote {t} (this machine is [hosts.{host}], named after its host name — rename it if you like); created .janus/, briefs/, out/")
    say("next: edit janus.toml (hosts, models, the API key's secret store), run `bd init` here if there is no .beads/, then `janus doctor`")
    return 0

def cmd_init(cfg, a):
    """In a project with a janus.toml: turn the old state docs in paths.state_docs into a one-line pointer to .janus/HANDOFF.md
    (fixes5 item 6); the old text is kept in .janus/history-<name> first."""
    for x in cfg.paths.get("state_docs", []):
        p = cfg.root / x
        if not p.is_file(): continue
        t = p.read_text(); ptr = f"Moved: the current state and handoff are in {cfg.state.name}/HANDOFF.md (janus check regenerates it). Old text: {cfg.state.name}/history-{p.name}\n"
        if t == ptr: say(f"{x}: already a pointer"); continue
        if cfg.run.dry: say(f"+ keep {x} in {cfg.state.name}/history-{p.name}; write the pointer"); continue
        k = cfg.f(f"history-{p.name}")
        k.write_text((k.read_text() if k.exists() else "") + f"\n\n<!-- {ts()} moved from {x} -->\n" + t)
        atomic_write(p, ptr); say(f"{x}: now a pointer (old text in {k})")
    say(f"{cfg.root / 'janus.toml'} exists; run janus doctor to check the setup")

OLD_CFG, NEW_CFG = "orch.toml", "janus.toml"   # the old names are quoted here on purpose: this is the one-time migration off them
OLD_STATE, NEW_STATE = ".orch", ".janus"

# Item 1 (finding 1): the old names a config value can carry. Only the old file names and the launchd label are rewritten, and
# only as a whole path component -- never a bare `orch` token inside a path, host, keychain name or word (`root = "/home/x/orch"`,
# `research.orchard`, `torch.py`, `me@orch`, the keychain name `orch` and `ORCH_API` all stay). Finding 10: a kit folder
# (`/opt/orchkit`) is NOT rewritten (migrate renames no kit folder), and the state folder `.orch` only when it is this project's
# own (the whole value, a path relative to the root, or under <root>): a remote or another project's `.orch` stays.
OLD_VALUE = [("orch.py", "janus.py"), ("orch.toml", "janus.toml"), ("com.orch.watch", "com.janus.watch")]

def old_value(v, root=None):
    """One config value with the old names replaced (item 1, finding 1, finding 10): each only as a whole path component (an
    `orch.py`), so a bare `orch` token, `research.orchard` and `scripts/torch.py` stay. The state folder `.orch` is replaced only
    where migrate really renames it -- the whole value, a leading `.orch/` (relative to the project root), or `<root>/.orch` --
    so `/opt/orchkit`, `/home/dev/orch/.orch` and another project's `.orch` are left as they are."""
    for a, b in OLD_VALUE:
        if a == "com.orch.watch": v = v.replace(a, b); continue   # a specific launchd label, not a bare token
        v = re.sub(r"(?<![\w.-])" + re.escape(a) + r"(?![\w.-])", b, v)
    v = re.sub(r'^(\s*["\']?)\.orch(?![\w.-])(?=[/\s"\']|$)', lambda m: m.group(1) + ".janus", v)
    if root is not None:
        r = re.escape(str(root).rstrip("/"))
        v = re.sub(r"(?<![\w.-])(" + r + r")/\.orch(?![\w.-])", r"\1/.janus", v)
    return v

ASSIGN = re.compile(r"^(\s*[A-Za-z0-9_.-]+\s*=\s*)(.*)$")

def value_comment(rest, q0=""):
    """(value, trailing comment, quote state, bracket depth) of a line's right-hand side: the comment starts at the first #
    outside a quoted string, and the quote state and the [/{ depth carry across the lines of a multi-line value (finding 1)."""
    q, esc, depth = q0, False, 0
    for i, ch in enumerate(rest):
        if esc: esc = False; continue
        if ch == "\\" and q == '"': esc = True; continue
        if q:
            if ch == q: q = ""
        elif ch in "\"'": q = ch
        elif ch == "#": return rest[:i], rest[i:], q, depth
        elif ch in "[{": depth += 1
        elif ch in "]}": depth -= 1
    return rest, "", q, depth

def rewrite_config(path, dry=False):
    """Rewrite every value in a migrated janus.toml that names the old state folder or an old name (item 1, finding 1); a
    multi-line value (a list or table spanning lines) is rewritten on its continuation lines too, and comments and anything that
    is not a value are left as written. Returns one line per change; writes nothing in dry mode."""
    lines, changes, q, depth, key = path.read_text().split("\n"), [], "", 0, ""
    for i, line in enumerate(lines):
        if q == "" and depth == 0:
            if line.lstrip().startswith("#"): continue   # a comment is not a value (the one place old names may stay on purpose)
            m = ASSIGN.match(line)
            if not m: continue
            head, rest = m.group(1), m.group(2); key = head.split("=", 1)[0].strip()
        else: head, rest = "", line   # a continuation line of a value that started on an earlier line
        val, com, q, d = value_comment(rest, q); depth += d
        new = old_value(val, path.parent)
        if new == val: continue
        changes.append(f"{path}: {key}: {val.strip()} -> {new.strip()}")
        lines[i] = head + new + com
    if changes and not dry: atomic_write(path, "\n".join(lines))
    return changes

# Item 14 (finding 4; finding 6): the old orch's standing loops only. The process's OWN script argument must be this project's
# <root>/bin/<loop>.sh or <root>/orch.py watch. A supervisor or waiter that merely NAMES a loop in its arguments
# (`janus.py run --host h -- bash bin/handoff-queue.sh`), a reader (`tail -f .../orch-backup.sh`, an editor), another project's
# tree (a path prefix such as /w/proj-two), and an agent's own `sh -c ...` shell whose command string names the scripts are never
# the loop. A loop started with a relative script path is told apart by the process's own cwd (finding 6).
OLD_ORCH_LOOPS = ("fleet-loops", "handoff-queue", "orch-backup", "wip-snapshot", "watch-stream")
OLD_ORCH_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
OLD_ORCH_WRAPS = ("sudo", "doas", "nohup", "setsid", "env", "command", "exec", "caffeinate")   # finding 4: a loop started through one

def _sh_words(cmd):
    """The words of a command line, honouring quotes and backslashes (a script named inside an argument stays one word)."""
    w, cur, q, esc = [], "", None, False
    for ch in cmd:
        if esc: cur += ch; esc = False; continue
        if q:
            if ch == q: q = None
            else: cur += ch
        elif ch in "'\"": q = ch
        elif ch in " \t":
            if cur: w.append(cur); cur = ""
        else: cur += ch
    if cur: w.append(cur)
    return w

def _proc_cwd(pid):
    """A process's working directory (Linux /proc, else lsof), or None when it cannot be told."""
    try: return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError: pass
    rc, o, e = sh(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=10)
    if rc in (0, 1) and o:
        for l in o.splitlines():
            if l.startswith("n"): return Path(l[1:])
    return None

def _script_is_ours(script, rest, pid, r):
    """True when `script` (with its following words `rest`) is this project's own old loop: <root>/bin/<loop>.sh, a relative
    bin/<loop>.sh from a process whose cwd is under the root, or <root>/orch.py watch."""
    base = os.path.basename(script)
    if base == "orch.py":
        return rest[:1] == ["watch"] and Path(script).is_absolute() and Path(script).resolve() == (r / "orch.py")
    name = base[:-3] if base.endswith(".sh") else base
    if name not in OLD_ORCH_LOOPS: return False
    if Path(script).is_absolute(): return Path(script).resolve() == r / "bin" / f"{name}.sh"
    if script not in (f"bin/{name}.sh", f"./bin/{name}.sh"): return False
    cw = _proc_cwd(pid)
    return bool(cw) and (cw.resolve() == r or cw.resolve().is_relative_to(r))

def _old_orch_script(pid, w, r):
    """True when the shell words w run this project's own old loop as their own script: <root>/bin/<loop>.sh, a relative
    bin/<loop>.sh from a process whose cwd is under the root, or <root>/orch.py watch. Finding 4: a loop started through a
    wrapper (sudo/nohup/env/...) is the loop too; a job waiter or a `sh -c ...` that merely names the script is not."""
    if not w or not r: return False
    i = 0
    while i < len(w) and (os.path.basename(w[i]) in OLD_ORCH_WRAPS or re.match(r"^[A-Za-z_]\w*=", w[i])): i += 1
    if i >= len(w): return False
    if _script_is_ours(w[i], w[i + 1:], pid, r): return True   # run directly: `sudo <root>/bin/fleet-loops.sh`
    if os.path.basename(w[i]) not in OLD_ORCH_SHELLS: return False
    j = i + 1
    while j < len(w) and w[j].startswith("-"):
        if "c" in w[j][1:]: return False   # a shell shim whose command string merely names the script (an agent's own shell)
        j += 1
    return j < len(w) and _script_is_ours(w[j], w[j + 1:], pid, r)

def old_orch_procs(ps_text=None, root=None):
    """[(pid, command)] of this machine's running old-orch standing loops (item 14; finding 6): a shell whose own script is
    this project's <root>/bin/<loop>.sh or its watcher; never a job waiter or supervisor, a reader, an editor or another
    project's process. migrate lists them; janus migrate --retire stops them (the owner's yes)."""
    o = ps_text if ps_text is not None else sh(["ps", "-Aww", "-o", "pid=,command="], timeout=10)[1]
    r = Path(root).resolve() if root is not None else None
    return [(int(f[0]), f[1]) for f in (l.split(None, 1) for l in o.splitlines())
            if len(f) == 2 and f[0].isdigit() and _old_orch_script(int(f[0]), _sh_words(f[1]), r)]

def live_children(pid):
    """(pids of the process's children that are real work, pids of its children that are `sleep`). A child that is not `sleep`
    (a gate the worker runs) is work. A `sleep` child proves nothing either way: a standing loop idles in `sleep 30`, and a
    worker that waits for its gate's result polls in `sleep 5` with the gate running elsewhere, so the two look the same from
    outside (review finding F3; a rule by the loop's name, or one that calls every sleep "not work", each got one of them wrong)."""
    rc, o, e = run(["pgrep", "-P", str(pid)], timeout=10)
    if rc or not o.strip(): return [], []
    kids = [int(x) for x in o.split()]
    rc, o, e = run(["ps", "-o", "pid=,comm=", "-p", ",".join(map(str, kids))], timeout=10)
    if rc or not o.strip(): return kids, []
    rows = [(int(l.split()[0]), os.path.basename(l.split(None, 1)[1])) for l in o.splitlines() if l.strip()]
    return [k for k, c in rows if c != "sleep"], [k for k, c in rows if c == "sleep"]

def retire_old_orch(dry=False, retire=False, ps_text=None, root=None, idle=False):
    """Item 14: list the old orch's standing processes (the full command line, so the owner can tell a loop from a waiter) and,
    only with the owner's explicit yes (--retire), stop them (TERM). A process with a child that is real work (a worker whose
    gate runs under it) is listed and left alone: TERM would orphan the gate. A process whose only child is `sleep` is either an
    idle loop or a worker waiting for its gate's result, which janus cannot tell apart, so it is left alone as well and the
    owner is told how to stop it: `--retire --idle` is the owner's word that no handoff is in flight, and stops those too. The
    `kill` result is read (a refused TERM is reported, never counted as sent)."""
    procs = old_orch_procs(ps_text, root)
    if not procs: return []
    out = [f"old orch process {p} still running: {c}" for p, c in procs]
    if retire and not dry:
        n = 0
        for p, c in procs:
            work, asleep = live_children(p)
            if work:
                out.append(f"-> kept pid {p}: it has a live child ({', '.join(map(str, work))}): a mid-handoff worker, TERM would orphan its gate")
                continue
            if asleep and not idle:
                out.append(f"-> kept pid {p}: it waits in sleep, as an idle loop does and as a worker does that waits for its gate's result; janus cannot "
                           f"tell which. When no handoff is in flight, stop it: janus migrate --retire --idle (or kill -TERM {p})")
                continue
            rc, o, e = run(["kill", "-TERM", str(p)])
            if rc: out.append(f"-> could not TERM pid {p} ({(e or o).strip()[:80] or 'kill failed'}): it keeps running")
            else: n += 1
        out.append(f"-> sent TERM to {n} old orch process(es) (--retire{' --idle' if idle else ''})")
    else: out.append("-> not stopped (janus migrate --retire stops them, or stop them by hand)")
    return out

def port_old_scripts(root, dry=False):
    """Scripts of a migrated project that still call the retired `orch` CLI (soak 39): a shim (`exec orch "$@"` or `orch "$@"`)
    is rewritten to `janus`; any other call (`orch run`, `orch gate`, ...) is listed so the owner ports it. A gate whose steps
    call `orch` then exit 2 in 0 s (the retired CLI is gone), so migrate must never leave them silently."""
    out, listed = [], 0
    skip = {"worktrees", "node_modules", "target", "__pycache__", ".venv", ".mypy_cache", ".git", ".janus", ".orch"}
    shim_rx = re.compile(r"^(?:exec\s+)?(?:\S*/)?orch(?:\.py)?\s*(?:\"?\$@\"?)?\s*$")
    call_rx = re.compile(r"(?<![\w./-])(?:\./)?orch(?:\.py)?\s+[a-z][\w-]*\b")
    for p in sorted(root.rglob("*")):
        if not p.is_file() or (p.suffix.lower() not in (".sh", ".bash", ".py", ".mk", ".pl", ".rb") and p.name != "orch"): continue
        try: rel = p.relative_to(root)
        except ValueError: continue
        if any(part in skip for part in rel.parts): continue
        try: txt = p.read_text(errors="replace")
        except OSError: continue
        body = [l for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if len(body) == 1 and shim_rx.match(body[0].strip()):
            if not dry: atomic_write(p, re.sub(r"(?<![\w./-])(\S*/)?orch(\.py)?", lambda m: (m.group(1) or "") + "janus", txt))
            out.append(f"{rel}: a shim to the retired CLI -> janus"); continue
        for i, l in enumerate(txt.splitlines(), 1):
            if call_rx.search(l): out.append(f"{rel}:{i}: still calls the retired CLI: {l.strip()[:100]}"); listed += 1
    if listed: out.append(f"{listed} line(s) still call `orch`: port them to `janus` (a gate step that calls the retired CLI exits 2 at once, not a test result)")
    return out

def migrate(project=None, dry=False, retire=False, idle=False):
    """One-time, idempotent move from orch to janus: the project's orch.toml -> janus.toml and .orch/ -> .janus/, every config
    value that still names the old state folder or an old name (item 1), the hook commands a previous install wrote, the watcher,
    and the project's own pre-existing jobs, sessions and cards which get their project id (item 6). Safe to run twice; prints
    what it changed. `--dry-run` (item 3) prints what it would change and changes nothing. Stops and says so if both the old and
    the new file/folder exist (nothing is merged by hand here)."""
    root = Path(project or os.getcwd()).resolve()
    old_cfg, new_cfg = root / OLD_CFG, root / NEW_CFG
    if not old_cfg.is_file() and not new_cfg.is_file():
        print(f"janus: no {OLD_CFG} or {NEW_CFG} in {root}", file=sys.stderr); return 2
    if old_cfg.is_file() and new_cfg.exists():
        print(f"janus: both {OLD_CFG} and {NEW_CFG} exist in {root}; merge them by hand", file=sys.stderr); return 1
    old_state, new_state = root / OLD_STATE, root / NEW_STATE
    if old_state.is_dir() and new_state.exists():
        print(f"janus: both {OLD_STATE}/ and {NEW_STATE}/ exist in {root}; merge them by hand", file=sys.stderr); return 1
    did = []
    if old_cfg.is_file():
        did.append(f"{root}: {OLD_CFG} -> {NEW_CFG}")
        if not dry: os.replace(old_cfg, new_cfg)
    if old_state.is_dir():
        did.append(f"{root}: {OLD_STATE}/ -> {NEW_STATE}/")
        if not dry: os.replace(old_state, new_state)
    src = new_cfg if new_cfg.is_file() else old_cfg   # the renamed config (in dry mode, the file that would be renamed)
    if src.is_file(): did += rewrite_config(src, dry=dry)   # item 1
    did += migrate_hooks(dry=dry) + migrate_watcher(dry=dry)
    if new_cfg.is_file() or (dry and old_cfg.is_file()): did += adopt_items(root, dry=dry, config=src)   # item 6
    did += retire_old_orch(dry=dry, retire=retire, root=root, idle=idle)   # item 14: the old orch's standing processes (finding 4)
    did += port_old_scripts(root, dry=dry)   # soak 39: port shims / list scripts still calling the retired CLI
    for x in did: say(x)
    say("janus migrate: " + ("done" if did else "nothing to do (already migrated)") + (" [dry-run: nothing changed]" if dry else ""))
    return 0

def migrate_hooks(dry=False):
    """Point the hook commands a previous install wrote (…/orch hook <event>, or an older …/orch.py hook <event>) at janus.
    A rewritten file is backed up first; returns one line per file changed (the plan only, in dry mode)."""
    out = []
    for p in (Path.home() / ".claude/settings.json", Path.home() / ".codex/hooks.json"):
        if not p.is_file(): continue
        try: data = json.loads(p.read_text())
        except Exception: continue
        n = 0
        for lst in (data.get("hooks") or {}).values():
            for ent in lst if isinstance(lst, list) else []:
                for h in (ent.get("hooks", []) if isinstance(ent, dict) else []):
                    c = h.get("command", "")
                    if re.search(r"(^|[/\s])orch(\.py)? hook \w+$", c):
                        h["command"] = re.sub(r"orch(\.py)? hook ", r"janus\1 hook ", c); n += 1
        if n:
            out.append(f"{p}: {n} hook command(s) -> janus")
            if not dry:
                p.with_name(p.name + ".bak-orch").write_text(p.read_text()); atomic_write(p, json.dumps(data, indent=2) + "\n")
    return out

def migrate_watcher(dry=False):
    """Replace the orch watcher with the janus one: macOS boots out com.orch.watch and installs the shipped
    com.janus.watch.plist; Linux renames the systemd units. Returns one line per change (the plan only, in dry mode)."""
    out = []
    if sys.platform == "darwin":
        old = Path.home() / "Library/LaunchAgents/com.orch.watch.plist"
        new = Path.home() / "Library/LaunchAgents/com.janus.watch.plist"
        if old.exists():
            out.append("removed the com.orch.watch watcher")
            if not dry: run(["launchctl", "bootout", f"gui/{os.getuid()}/com.orch.watch"]); old.unlink()
        src = JANUS_SELF.parent / "launchd" / "com.janus.watch.plist"
        if src.is_file() and not new.exists():
            out.append("installed the com.janus.watch watcher")
            if not dry:
                new.parent.mkdir(parents=True, exist_ok=True); (Path.home() / ".config/janus").mkdir(parents=True, exist_ok=True)
                new.write_text(src.read_text().replace("__HOME__", str(Path.home())))
                run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(new)])
    else:
        u = Path.home() / ".config/systemd/user"
        units = [("orch-watch.service", "janus-watch.service"), ("orch-watch.timer", "janus-watch.timer")]
        if any((u / a).exists() for a, _ in units):
            out.append("renamed the systemd watcher units to janus-watch")
            if not dry:
                run(["systemctl", "--user", "disable", "--now", "orch-watch.timer"])
                for a, b in units:
                    if (u / a).exists() and not (u / b).exists(): (u / b).write_text((u / a).read_text().replace("orch", "janus")); (u / a).unlink()
                run(["systemctl", "--user", "daemon-reload"]); run(["systemctl", "--user", "enable", "--now", "janus-watch.timer"])
    return out

# Item 6: jobs, sessions and cards made before the project id existed carry none, so janus lists them as machine state and never
# collects, cancels or tracks them. migrate adopts the ones that are this project's — a pueue label or tmux session whose name
# matches one of the project's units (janus-<unit>, or its old orch-<unit> name on this machine), and a card of such a unit — by
# adding @<project id>, and lists every item it cannot attribute. Only this machine's items (the brief's scope).

def _adopt_view(root, config):
    """A minimal read-only view of the config for adoption, so migrate can plan it even before janus.toml exists (dry run)."""
    c = tomllib.loads(config.read_text()) if config and config.is_file() else {}
    pr = c.get("project", {})
    return SimpleNamespace(root=root, c=c, name=pr.get("name", root.name), paths=c.get("paths", {}), hosts=c.get("hosts", {}),
                           beads=root / pr.get("beads", "."), state=root / pr.get("state", ".janus"), run=Run())

def pueue_editor(path, new):
    """Write the $EDITOR pueue opens for `pueue edit` (a queued or stashed task): it sets the task's label to new, in either
    edit_mode (a TOML file per task, or a plain `label` file per task). Returns the editor command line."""
    path.write_text("# janus migrate: the $EDITOR pueue opens; set the task label to the new project id.\n"
                    "import pathlib, re, sys\n"
                    f"new = {new!r}\n"
                    "for a in sys.argv[1:]:\n"
                    "    p = pathlib.Path(a); f = (p / 'label') if p.is_dir() else p\n"
                    "    if not f.is_file(): continue\n"
                    "    if f.name == 'label': f.write_text(new + '\\n'); continue\n"
                    "    t = f.read_text()\n"
                    "    t = (re.sub(r'^label\\s*=.*$', 'label = ' + repr(new), t, count=1, flags=re.M)\n"
                    "         if re.search(r'^label\\s*=', t, re.M) else t.rstrip('\\n') + '\\nlabel = ' + repr(new) + '\\n')\n"
                    "    f.write_text(t)\n")
    return f"python3 {shlex.quote(str(path))}"

def _local_pueue(cfg):
    """The real pueue command (a list) for this machine (finding 6): the janus pueue shim (`.janus/bin/pueue`) drops EDITOR, so
    migrate calls the real binary, with this host's pueue_config when it has one."""
    import shutil
    real = shutil.which("pueue", path=clean_env()["PATH"]) or "pueue"
    h = next((x for x in cfg.hosts.values() if x.get("local")), {})
    return [real, *(["-c", str(h["pueue_config"])] if h.get("pueue_config") else [])]

def _under(root, path, cwds=()):
    """True when a pueue task's working directory is the project root, under it, or an open unit's own tree (finding 10: a unit
    tree outside the root, as every unit's worktree is, used to be left alone; finding 6: a job is this project's only when both
    its unit and its path are)."""
    if not path: return False
    try: p = Path(path).resolve()
    except OSError: return False
    r = Path(root).resolve()
    if p == r or r in p.parents: return True
    for c in cwds:
        if not c: continue
        try: q = Path(c).resolve()
        except OSError: continue
        if p == q or q in p.parents: return True
    return False

def _gate_unit(name, units):
    """The unit a gate job's name belongs to (finding 6): `land-<unit>`/`<unit>-...`, or a unit named as a whole token of the
    gate name (`gate:land-u1-linux` -> u1); None when it names no unit."""
    for s in (name, name[5:] if name.startswith("land-") else name):
        if s in units: return s
    for u in sorted(units, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(u)}(?!\w)", name): return u
    return None

def _task_label(pq_cmd, tid):
    """The label of task tid now, or None when it cannot be read (finding 6: verify a pueue edit took effect)."""
    rc, o, e = sh([*pq_cmd, "status", "--json"])
    if rc: return None
    try: return (json.loads(o).get("tasks", {}).get(str(tid)) or {}).get("label")
    except Exception: return None

def _follow_tmux_rename(view, name, new):
    """The open unit record follows its renamed tmux session (finding 6), so unit_tmux(u) names the new session."""
    try:
        bd = Beads(view); u = next((x for x in bd.units() if x["name"] == name), None)
        if u: u["tmux"] = new; bd.save(u)
    except Err: pass

def adopt_items(root, dry=False, config=None):
    """Item 6 (finding 6): give this project's own pre-existing items their project id, and list what it cannot attribute. A
    pueue job is this project's only when its label matches one of the project's units (or a gate of one) AND its working
    directory is under this project's root; it is relabelled (…@<id>) through pueue's editor when it is queued or stashed, and
    the relabel is verified (a pueue edit that did not take is reported, never claimed). A tmux session named janus-<unit> or
    orch-<unit> is renamed (…@<id>) and the unit record follows it; a card of such a unit gets its project field. Returns one
    line per adoption and one per item left alone; changes nothing in dry mode."""
    view = _adopt_view(root, config); pid = project_id(view); au = all_units(view); units = set(au); out = []
    cwds = [m.get("cwd") for m in au.values() if m.get("cwd")]   # finding 10: a unit tree outside the root is this project's too
    pq_cmd = _local_pueue(view)
    rc, o, e = sh([*pq_cmd, "status", "--json"])
    if rc == 0 and o.strip():
        try: tasks = json.loads(o).get("tasks", {})
        except Exception: tasks = {}
        for tid, t in sorted(tasks.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            lab = t.get("label") or ""
            if label_owner(lab)[1] is not None: continue
            base = lab.split(":", 1)[1] if lab.startswith(("unit:", "run:", "gate:")) else lab
            name = _gate_unit(base, units) if lab.startswith("gate:") else base
            if name not in units:
                out.append(f"job {tid} ({lab or (t.get('command') or '')[:40]}): not one of this project's units; left alone"); continue
            if not _under(view.root, t.get("path"), cwds):
                out.append(f"job {tid} ({lab}): its path {t.get('path')} is not under {view.root} or a unit's tree; another project's, left alone"); continue
            new = f"{lab}@{pid}"; st = t.get("status"); st = st if isinstance(st, str) else next(iter(st), "")
            if st not in ("Queued", "Stashed", "Locked"):
                out.append(f"job {tid} ({lab} -> {new}): {st.lower() or 'running'}, pueue relabels only a queued or stashed task; adopt it by hand (pueue edit {tid})"); continue
            if dry:
                out.append(f"job {tid}: {lab} -> {new}"); continue
            ed = view.state / f".pueue-label-{tid}.py"; ed.parent.mkdir(parents=True, exist_ok=True); got = None
            try:
                sh([*pq_cmd, "edit", str(tid)], env={"EDITOR": pueue_editor(ed, new)}, timeout=30)
                got = _task_label(pq_cmd, tid)
            finally:
                ed.unlink(missing_ok=True)
            if got != new:
                out.append(f"job {tid} ({lab}): pueue edit did not change the label (still {got!r}); adopt it by hand (pueue edit {tid})"); continue
            out.append(f"job {tid}: {lab} -> {new}")
    elif rc: out.append(f"pueue: no queue on this machine ({(e or o).strip()[:60] or 'rc %s' % rc}); no jobs adopted")
    rc, o, e = sh(["tmux", "list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        for s in o.split():
            if label_owner(s)[1] is not None: continue
            pref = "janus-" if s.startswith("janus-") else "orch-" if s.startswith("orch-") else None
            if not pref: continue
            name = s[len(pref):]
            if name not in units: out.append(f"session {s}: not one of this project's units; left alone"); continue
            new = tmux_name(view, name); out.append(f"session {s} -> {new}")
            if not dry:
                sh(["tmux", "rename-session", "-t", s, new]); _follow_tmux_rename(view, name, new)
    elif rc: out.append(f"tmux: no server on this machine ({(e or o).strip()[:60] or 'rc %s' % rc}); no sessions adopted")
    for c in (sorted((board_dir(view) / "agents").glob("*.md")) if (board_dir(view) / "agents").is_dir() else []):
        name, txt = c.stem, c.read_text(errors="replace")
        if re.search(r"^- project: *\S", txt, re.M): continue   # already adopted
        if name not in units: out.append(f"card {name}: not one of this project's units; left alone"); continue
        out.append(f"card {name}: project {pid}")
        if not dry:
            if re.search(r"^- project:", txt, re.M): txt = re.sub(r"^- project:.*$", f"- project: {pid}", txt, count=1, flags=re.M)
            else:
                L = txt.split("\n"); L.insert(1, f"- project: {pid}"); txt = "\n".join(L)
            atomic_write(c, txt)
    return [f"adopt: {x}" for x in out]

def cmd_doctor(cfg, a): return doctor(cfg, a)

def model_host_warnings(cfg):
    """A warning per routed model whose [models.<m>] hosts is narrower than the configured hosts (decision 0004: agents run on
    every host): 'model X: runs on 1 of 3 host(s)'. A model with no hosts key may run anywhere and is not warned."""
    routed = {m for lst in cfg.route.values() for m in lst}
    out = []
    for m, c in cfg.models.items():
        hs = c.get("hosts")
        if hs and m in routed and set(hs) < set(cfg.hosts):
            out.append(f"model {m}: runs on {len(hs)} of {len(cfg.hosts)} host(s) ({', '.join(hs)}): remove [models.{m}] hosts to allow every host")
    return out

def doctor(cfg, a):
    """Check a setup end to end and say what to fix: tools, the installed release, hooks, watcher, config, secret store,
    ledger, beads and hosts. Exit 1 when anything FAILs."""
    import shutil
    res = []
    def r(ok, what, fix=""): res.append((ok, what, fix)); say(f"{'ok  ' if ok is True else 'WARN' if ok is None else 'FAIL'} {what}" + (f"  -> {fix}" if fix and ok is not True else ""))
    path = clean_env()["PATH"]; which = lambda t: shutil.which(t, path=path)
    r(sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (3.11+ for tomllib)", "install Python 3.11 or newer")
    for t, need in (("git", True), ("ssh", True), ("rsync", True), ("bd", True), ("pueue", True), ("herdr", False), ("tmux", False)):
        r(True if which(t) else (False if need else None), f"tool {t}" + (f" ({which(t)})" if which(t) else " missing"),
          {"bd": "brew install beads (or see github.com/steveyegge/beads)", "pueue": "brew install pueue; brew services start pueue"}.get(t, f"install {t}"))
    if which("pueue"): rc, o, e = sh(["pueue", "status"], clean=True); r(rc == 0, "pueued answers", "start it: brew services start pueue (macOS) or systemctl --user enable --now pueued")
    me = Path.home() / ".local/bin/janus"
    tgt = me.resolve() if me.exists() else None
    rel = tgt and ".local/share/janus/" in str(tgt)
    r(bool(rel), f"installed janus: {me} -> {tgt or 'missing'}", "run ./install from the janus repo (it copies a tested release; never a link to the working tree)")
    sk = Path.home() / ".claude/skills/janus"
    r(True if sk.exists() and ".local/share/janus/" in str(sk.resolve()) else None if sk.exists() else False,
      f"skill {sk} -> {sk.resolve() if sk.exists() else 'missing'}", "run ./install")
    try: hk = json.loads((Path.home() / ".claude/settings.json").read_text()).get("hooks", {})
    except Exception: hk = {}
    miss = [ev for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop") if not any("janus hook " + ev in h.get("command", "") for e in hk.get(ev, []) for h in e.get("hooks", []))]
    r(not miss, "Claude Code hooks " + ("installed" if not miss else "missing: " + ", ".join(miss)), "run ./install")
    r(watcher_loaded() or None, f"watcher {watcher_service()[1]} ({watcher_service()[0]}) loaded", "run ./install")
    if cfg is None:
        r(False, f"no janus.toml in {Path.cwd()} or a parent", "janus init"); return 1
    say(f"-- project {cfg.name} ({cfg.root})")
    pl = Path.home() / ".config/janus/projects"
    try: entries = [Path(x) for x in pl.read_text().split()]   # the file the watcher actually reads (install)
    except OSError: entries = []
    listed = any(p.resolve() == cfg.root.resolve() for p in entries)   # equivalent paths match, and the message follows the fact
    r(listed or None, f"project {'listed in' if listed else 'not listed in'} {pl}" + ("" if listed else " (the watcher will not visit it)"),
      f"run ./install --project {cfg.root}")
    if not cfg.hosts: r(False, "no [hosts.*] in janus.toml", "add [hosts.<this machine>] local = true")
    elif not any(h.get("local") for h in cfg.hosts.values()): r(False, "no host has local = true", "mark this machine's [hosts.<name>] local = true")
    for m, c in cfg.models.items():
        if c.get("provider") not in cfg.prov: r(False, f"model {m}: provider {c.get('provider')} has no [providers] table", "add it")
        if c.get("launcher"): r(None, f"model {m}: launcher {c['launcher']} is no longer used (janus runs API agents itself)", "remove the key")
        if c.get("kind") == "api" and not c.get("api_id"): r(False, f"model {m}: kind api needs api_id (the model id the API takes)", "set api_id")
        if c.get("kind") in ("claude", "codex", "opencode") or c.get("kind") == "api":
            t = "claude" if c.get("kind") == "api" else c["kind"]
            if not which(t): r(False if c.get("kind") == "api" else None, f"model {m}: {t} not on PATH", f"install {t}")
    for tier, lst in cfg.route.items():
        for m in lst:
            if m not in cfg.models: r(False, f"routing {tier}: unknown model {m}", "fix [routing]")
    for w in model_host_warnings(cfg): r(None, w, "drop [models.<m>] hosts to allow every host, or add the missing ones")
    for k, d in (("briefs", "briefs"), ("out", "out")):
        r(cfg.p(k, d).is_dir() or None, f"paths.{k} {cfg.p(k, d)}", f"mkdir {cfg.p(k, d)}")
    b = board_script(cfg)
    r(os.access(b, os.X_OK) or None, f"message board {b}", "optional: agents post progress through it (paths.board)")
    from janus.issue import issue_path, iset
    from janus.autopilot import unattended_state
    r(True, f"issue register {issue_path(cfg)}" + ("" if iset(cfg, "issues_file") else " (the default: [janus] issues_file is unset; janus issue writes there)"))   # row 224
    r(True, unattended_state(cfg))   # row 225: say whether the unattended loop is on, and why
    rc, o, e = sh(["bd", "list", "-n", "1", "--json"], cwd=cfg.beads)
    r(rc == 0, f"beads in {cfg.beads}", f"run bd init in {cfg.beads} ({(e or o).strip()[:80]})")
    for n, pv in cfg.prov.items():
        if not pv.get("enabled"): continue
        api = any(c.get("provider") == n and c.get("kind") == "api" for c in cfg.models.values())
        if api:
            try: var, key = read_secret(pv); r(True, f"provider {n}: API key readable from the secret store ({var}, {len(key)} chars, not shown)"); del key
            except Err as e: r(False, f"provider {n}: {e}", "macOS: security add-generic-password -s <item> -a $USER -w; elsewhere: a mode-600 key_file")
        if "ledger" in pv:
            runs, bad, notes = ledger_read(cfg, pv)
            r(not bad, f"provider {n}: ledger {pv['ledger']} " + ("valid" if not bad else f"MALFORMED ({bad[0][:80]})") + f", ${spend(runs):.2f} committed" + (f" of ${pv['cap_usd']}" if "cap_usd" in pv else ""), "janus ledger repair")
        elif api: r(None, f"provider {n}: no ledger, so no spend record or cap", "set ledger (and cap_usd)")
    if not a.no_hosts:
        with cf.ThreadPoolExecutor(8) as ex:
            ps = dict(zip(cfg.hosts, ex.map(lambda kv: probe(*kv), cfg.hosts.items())))
        for n, p in ps.items():
            if not p["ok"]: r(False, f"host {n}: unreachable ({p['err']})", f"ssh {cfg.hosts[n].get('ssh', '')} must work with keys (BatchMode)"); continue
            r(p.get("pueue") is not None or None, f"host {n}: reachable" + ("" if p.get("pueue") is not None else ", but pueue does not answer there"), "./install --hosts-only")
    fails = sum(1 for ok, *_ in res if ok is False); warns = sum(1 for ok, *_ in res if ok is None)
    say(f"doctor: {len(res)} checks, {fails} FAIL, {warns} WARN"); return 1 if fails else 0

def cmd_queue(cfg, a):
    """Read-only: each host's pueue status (or one task's log), so the orchestrator never needs ssh <host> pueue by hand."""
    L = []
    if a.log is not None:   # row 218: task ids collide between hosts, so <host>:<id> names one; a bare id asks the host argument, else every host that has it
        from janus.actions.jobs import task_output
        hn, _, tid = str(a.log).rpartition(":"); hn = hn or a.host
        if not tid.isdigit() or (hn and hn not in cfg.hosts): raise Err(f"--log {a.log}: expected <host>:<task id> (hosts: {', '.join(cfg.hosts)})")
        got = [(n, *task_output(cfg, n, tid)) for n in ([hn] if hn else cfg.hosts)]
        hit = [g for g in got if g[1] == 0] if not hn else got
        for n, rc, o in hit or got: L += [f"== {n}" + ("" if rc == 0 else f" (exit {rc}: {o.strip()[-100:]})"), o.rstrip() if rc == 0 else ""]
        if len(hit) > 1: L.append(f"note: task {tid} exists on {', '.join(n for n, _, _ in hit)}: name one with --log <host>:{tid}")
        return 0, L
    for n, h in cfg.hosts.items():
        if a.host and n != a.host: continue
        rc, o, e = hrun(h, f"{pueue(h)} status", timeout=30)
        L += [f"== {n}" + ("" if rc == 0 else f" (exit {rc}: {(e or o).strip()[-100:]})"), o.rstrip()]
    return 0, L

# janus install-host <host> (imp-sched item 4): set up a machine named in janus.toml, after discovering it (nothing assumed): its
# folders, the queue daemon (pueued) as a user service, a copy of janus for read-only commands (<root>/janus), and the heartbeat
# watchdog when [heartbeat] watchdog names this host. Idempotent; --dry-run prints the steps. No sudo.
PUEUE_URL = "https://github.com/Nukesor/pueue/releases/latest/download"
PUEUED_UNIT = "[Unit]\nDescription=pueued\n[Service]\nExecStart={bin}/pueued {cfg}\nRestart=always\n[Install]\nWantedBy=default.target\n"

def heartbeat_files():
    """The heartbeat scripts shipped next to janus.py (heartbeat/beat, heartbeat/install-watchdog), or None until they are."""
    d = JANUS_SELF.parent / "heartbeat"
    return d if (d / "beat").is_file() and (d / "install-watchdog").is_file() else None

def heartbeat_env(cfg):
    """The heartbeat settings ([heartbeat] watchdog = <host>, max_age_min, alert_cmd) as the scripts' environment, or None."""
    hb = cfg.c.get("heartbeat", {}); w = hb.get("watchdog")
    if not w: return None
    if w not in cfg.hosts or cfg.hosts[w].get("local"): raise Err(f"[heartbeat] watchdog = {w!r}: name another host of janus.toml")
    root = cfg.hosts[w].get("root", "$HOME/janus")
    return {"host": w, "file": f"{root}/heartbeat", "outbox": f"{root}/board-outbox/outbox.log", "max_age": int(hb.get("max_age_min", 15) * 60),
            "alert_cmd": hb.get("alert_cmd", "")}

def heartbeat_beat(cfg, rc=0):
    """The watcher's beat (one line per pass, copied to the watchdog host). Inert until heartbeat/ ships and [heartbeat] is set."""
    d, e = heartbeat_files(), heartbeat_env(cfg)
    if not d or not e or cfg.run.dry: return
    w = cfg.hosts[e["host"]]
    sh(["sh", str(d / "beat"), str(int(rc))], timeout=60, clean=True,
       env={"HEARTBEAT_FILE": str(cfg.f("heartbeat")), "HEARTBEAT_REMOTE": w["ssh"], "HEARTBEAT_REMOTE_PATH": e["file"].replace("$HOME/", ""),
            "HEARTBEAT_SSH_OPTS": "-o BatchMode=yes -o ConnectTimeout=5"})

def install_steps(cfg, n, h, f):
    """[(what, shell)] that set up host n, given its discovered facts f."""
    root = h.get("_rd", "").rsplit("/runs", 1)[0] if h.get("local") else h.get("root")
    if not root: raise Err(f"[hosts.{n}] has no root (the folder janus uses there)")
    dirs = [f"{root}/runs"] if h.get("local") else [f"{root}/runs", f"{root}/run", remote_wt(h, "").rstrip("/"), f"{root}/board-outbox"]
    steps = [("folders", "mkdir -p " + " ".join(qp(x) for x in dirs))]
    have = set(f["tools"])
    if f["os"] == "mac":
        if "pueued" not in have: steps.append(("pueue", "command -v brew >/dev/null && brew install pueue || { echo 'install pueue (brew install pueue)'; exit 1; }"))
        steps.append(("pueued service", "brew services start pueue >/dev/null 2>&1 || pueue status >/dev/null"))
    else:
        b = h.get("bin", "$HOME/.local/bin"); cfgf = h.get("pueue_config"); shared = h.get("pueue_dir")
        if "pueued" not in have:
            if f["arch"] not in ("x86_64", "aarch64"): raise Err(f"{n}: no pueue build for {f['arch']}: install pueue there, then run install-host again")
            steps.append(("pueue", f"mkdir -p {qp(b)} && for x in pueue pueued; do curl -fsSL -o {qp(b)}/$x.new {PUEUE_URL}/$x-{f['arch']}-unknown-linux-musl && chmod +x {qp(b)}/$x.new && mv -f {qp(b)}/$x.new {qp(b)}/$x || exit 1; done"))
        if "systemd-user" not in f["features"]: raise Err(f"{n}: no systemd user manager: start pueued there by hand (pueued -d)")
        unit = shlex.quote(PUEUED_UNIT.format(bin=b, cfg=f"--config {cfgf}" if cfgf else ""))
        if shared:   # a shared server: pueue's files in pueue_dir, the unit linked from there (nothing in a shared home)
            steps.append(("pueued service", f"mkdir -p {qp(shared)}/pueue.d/data {qp(shared)}/pueue.d/run && "
                          + (f"{{ [ -f {qp(cfgf)} ] || printf 'shared:\\n  pueue_directory: {shared}/pueue.d/data\\n  runtime_directory: {shared}/pueue.d/run\\n' > {qp(cfgf)}; }} && " if cfgf else "")   # an existing config is kept
                          + f"printf %s {unit} > {qp(shared)}/pueued.service && systemctl --user link {qp(shared)}/pueued.service && systemctl --user enable --now pueued.service"))
        else:
            steps.append(("pueued service", f"mkdir -p ~/.config/systemd/user && printf %s {unit} > ~/.config/systemd/user/pueued.service && "
                          "systemctl --user daemon-reload && systemctl --user enable --now pueued.service"))
        steps.append(("linger", 'loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes || loginctl enable-linger "$USER" 2>/dev/null || '
                      'echo "note: user services stop at logout until linger is on (loginctl enable-linger $USER, may need an admin)"'))
    return steps

def cmd_install_host(cfg, a):
    """janus install-host <host>: discover it, then run install_steps there, copy janus to <root>/janus (read-only commands) and
    install the heartbeat watchdog when [heartbeat] watchdog is this host."""
    n = a.host
    if n not in cfg.hosts: raise Err(f"unknown host {n} (add [hosts.{n}] to janus.toml first: ssh, root)")
    h = cfg.hosts[n]; f = discover(n, h)
    if not f: raise Err(f"{n} is unreachable (ssh {h.get('ssh', '')} must work with keys, BatchMode)")
    if not cfg.run.dry: cfg.jput("hosts-facts.json", {**cfg.jget("hosts-facts.json"), n: f})
    say(facts_line(n, f))   # the host's capability profile, as janus hosts shows it
    steps = install_steps(cfg, n, h, f); rc = 0
    if not h.get("local"):
        root = h["root"]; dst = f"{h['ssh']}:{root.replace('$HOME/', '')}/janus/"
        steps.append(("janus copy", ["rsync", "-a", "--delete", "--exclude", "__pycache__", "-e", " ".join(SSH), str(JANUS_SELF), str(JANUS_SELF.parent / "janus"), dst]))
        e, d = heartbeat_env(cfg), heartbeat_files()
        if e and e["host"] == n:
            if d:
                steps.append(("heartbeat files", ["rsync", "-a", "-e", " ".join(SSH), str(d), dst]))
                env = {"HEARTBEAT_FILE": e["file"], "HEARTBEAT_MAX_AGE": e["max_age"], "HEARTBEAT_OUTBOX": e["outbox"], "HEARTBEAT_AGENT": "janus-heartbeat", "HEARTBEAT_ALERT_CMD": e["alert_cmd"]}
                steps.append(("heartbeat watchdog", "".join(f"{k}={shlex.quote(str(v))} " for k, v in env.items()) + f"sh {qp(root)}/janus/heartbeat/install-watchdog"))
            else: say(f"note: [heartbeat] watchdog = {n}, but this janus has no heartbeat/ scripts yet: run install-host again once it does")
    for what, x in steps:
        if cfg.run.dry: say(f"+ [{n}] {what}: " + (shlex.join(x) if isinstance(x, list) else x)); continue
        r, o, e = sh(x, timeout=600) if isinstance(x, list) else hrun(h, x, timeout=600)
        say(f"{'ok  ' if r == 0 else 'FAIL'} {what}" + ("" if r == 0 else f": {(e or o).strip()[-160:]}") + (f" ({o.strip()[-120:]})" if r == 0 and o.strip() else ""))
        rc = rc or r
    return 1 if rc else 0
