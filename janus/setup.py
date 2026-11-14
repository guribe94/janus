"""janus init, doctor, queue, register and install-host: a shareable project setup, new machines, and the one registered
orchestrator session."""
import argparse, concurrent.futures as cf, json, os, shlex, sys, time
from pathlib import Path
from janus.actions.jobs import facts_line, remote_wt
from janus.actions.landing import SECRETS, cmd_commit_state, push_cmd
from janus.actions.launch import new_tab, wait_shell
from janus.board import board_dir, board_script
from janus.context import (CLAIM_WINDOW, CONF, old_layout, Err, JANUS_MARK, JANUS_SELF, SSH, atomic_write, clean_env, herdr, hrun, now, pending_age, qp,
    record_session_proc, say, session_alive, sh, slug, wake_clear)
from janus.hooks import session_start_text
from janus.ledger import ledger_read, read_secret, spend
from janus.platform import pane_tool, watcher_loaded, watcher_service
from janus.probe import discover, probe, pueue
from janus.units import ws_id

def cmd_register(cfg, a):
    """The only way to change the registered orchestrator session (with the watcher's pending claim for a new one it started)."""
    if os.environ.get("JANUS_ROLE") == "agent": raise Err("refused: an agent (JANUS_ROLE=agent) cannot register as the orchestrator (issue 2)")
    sid = a.session or next((os.environ[k] for k in ("JANUS_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID") if os.environ.get(k)), None)
    kind = "codex" if os.environ.get("CODEX_THREAD_ID") and not os.environ.get("CLAUDECODE") else \
        "opencode" if os.environ.get("OPENCODE_SESSION_ID") and not os.environ.get("CLAUDECODE") else "claude"
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
    """A new project: write a commented starter .janus/janus.toml (from example.janus.toml next to janus.py), create the briefs,
    out and board folders under .janus, and say what is left. Never overwrites a config."""
    root = Path(root).resolve(); t = root / CONF
    if t.exists(): raise Err(f"{t} exists")
    ex = JANUS_SELF.parent / "example.janus.toml"
    if not ex.is_file(): print(f"janus: {ex} not found (install copies it next to janus.py)", file=sys.stderr); return 2
    name = a.name or slug(root.name) or "project"
    host = (os.uname().nodename.split(".")[0].lower() or "local").replace(" ", "-")
    text = ex.read_text().replace('name = "myproject"', f'name = "{name}"').replace('workspace = "myproject"', f'workspace = "{name}"')
    text = text.replace("[hosts.laptop]", f"[hosts.{host}]").replace('hosts = ["laptop"]', f'hosts = ["{host}"]')
    if a.dry_run: print(text); return 0
    atomic_write(t, text)
    for d in ("briefs", "out", "board"): (root / ".janus" / d).mkdir(parents=True, exist_ok=True)
    say(f"wrote {t} (this machine is [hosts.{host}], named after its host name — rename it if you like); created .janus/briefs, out, board")
    say("next: edit .janus/janus.toml (hosts, models, the API key's secret store), run `bd init` here if there is no .beads/, then `janus doctor`")
    return 0

def cmd_init(cfg, a):
    """In a project that has its config already: nothing to write."""
    say(f"{cfg.root / CONF} exists; run janus doctor to check the setup")

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
    miss = [ev for ev in ("SessionStart", "PreToolUse", "Stop") if not any("janus hook " + ev in h.get("command", "") for e in hk.get(ev, []) for h in e.get("hooks", []))]
    r(not miss, "Claude Code hooks " + ("installed" if not miss else "missing: " + ", ".join(miss)), "run ./install")
    r(watcher_loaded() or None, f"watcher {watcher_service()[1]} ({watcher_service()[0]}) loaded", "run ./install")
    if cfg is None:
        old = old_layout()
        r(False, f"no {CONF} in {Path.cwd()} or a parent" + (f" (the old layout is at {old}/janus.toml)" if old else ""),
          f"janus migrate {old} (dry run), then janus migrate {old} --apply" if old else "janus init"); return 1
    say(f"-- project {cfg.name} ({cfg.root})")
    pl = Path.home() / ".config/janus/projects"
    try: entries = [Path(x) for x in pl.read_text().split()]   # the file the watcher actually reads (install)
    except OSError: entries = []
    listed = any(p.resolve() == cfg.root.resolve() for p in entries)   # equivalent paths match, and the message follows the fact
    r(listed or None, f"project {'listed in' if listed else 'not listed in'} {pl}" + ("" if listed else " (the watcher will not visit it)"),
      f"run ./install --project {cfg.root}")
    if not cfg.hosts: r(False, f"no [hosts.*] in {CONF}", "add [hosts.<this machine>] local = true")
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
    if cfg.f("migration/PENDING.md").is_file(): r(None, "migration is not final: the bd database and its unit records still name the old root", f"review {cfg.f('migration/PENDING.md')}")
    for w in model_host_warnings(cfg): r(None, w, "drop [models.<m>] hosts to allow every host, or add the missing ones")
    for k, d in (("briefs", "briefs"), ("out", "out")):
        r(cfg.p(k, d).is_dir() or None, f"paths.{k} {cfg.p(k, d)}", f"mkdir {cfg.p(k, d)}")
    b = board_script(cfg)
    r(os.access(b, os.X_OK) or None, f"message board {b}", "optional: agents post progress through it (paths.board)")
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

def launcher_cmd(b, root):
    """The shell line that writes janus's launcher on a host: only where there is none or it is exactly this launcher; an installed
    janus link and any other file stay (install-host must not overwrite what `install --release-only` linked)."""
    return (f"mkdir -p {qp(b)} && L={qp(b)}/janus && W=$(printf '#!/bin/sh\\nexec python3 %s/janus/janus.py \"$@\"\\n' {qp(root)}) && "
           "if [ -L \"$L\" ]; then echo \"note: $L is an installed janus link: kept\"; elif [ ! -e \"$L\" ] || [ \"$(cat \"$L\")\" = \"$W\" ]; then "
           "printf '%s\\n' \"$W\" > \"$L\" && chmod +x \"$L\"; else echo \"note: $L is not janus's launcher: kept\"; fi")

def cmd_install_host(cfg, a):
    """janus install-host <host>: discover it, then run install_steps there, copy janus to <root>/janus (read-only commands) and
    install the heartbeat watchdog when [heartbeat] watchdog is this host."""
    n = a.host
    if n not in cfg.hosts: raise Err(f"unknown host {n} (add [hosts.{n}] to .janus/janus.toml first: ssh, root)")
    h = cfg.hosts[n]; f = discover(n, h)
    if not f: raise Err(f"{n} is unreachable (ssh {h.get('ssh', '')} must work with keys, BatchMode)")
    if not cfg.run.dry: cfg.jput("hosts-facts.json", {**cfg.jget("hosts-facts.json"), n: f})
    say(facts_line(n, f))   # the host's capability profile, as janus hosts shows it
    steps = install_steps(cfg, n, h, f); rc = 0
    if not h.get("local"):
        root = h["root"]; dst = f"{h['ssh']}:{root.replace('$HOME/', '')}/janus/"
        steps.append(("janus copy", ["rsync", "-a", "--delete", "--exclude", "__pycache__", "-e", " ".join(SSH), str(JANUS_SELF), str(JANUS_SELF.parent / "janus"), dst]))
        b = h.get("bin", "$HOME/.local/bin")   # row 64: `janus` resolves in a job's shell there (job scripts put this folder on PATH)
        steps.append(("janus launcher", launcher_cmd(b, root)))
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
