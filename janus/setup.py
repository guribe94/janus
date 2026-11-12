"""janus init, doctor, queue, register and install-host: a shareable project setup, new machines, and the one registered
orchestrator session."""
import concurrent.futures as cf, json, os, re, shlex, sys
from pathlib import Path
from janus.actions.jobs import remote_wt
from janus.board import board_dir, board_script
from janus.context import (Err, JANUS_SELF, SSH, atomic_write, clean_env, herdr, hrun, now, projects, qp, record_session_proc, say,
    session_alive, sh, slug, ts)
from janus.ledger import ledger_read, read_secret, spend
from janus.platform import run, watcher_loaded, watcher_service
from janus.probe import discover, probe, pueue
from janus.units import ws_id

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
    say(f"wrote {t} (this machine is [hosts.{host}]); created .janus/, briefs/, out/")
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

def migrate(project=None):
    """One-time, idempotent move from orch to janus: the project's orch.toml -> janus.toml and .orch/ -> .janus/, the hook
    commands a previous install wrote, and the watcher. Safe to run twice; prints what it changed. Stops and says so if both
    the old and the new file/folder exist (nothing is merged by hand here)."""
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
    if old_cfg.is_file(): os.replace(old_cfg, new_cfg); did.append(f"{root}: {OLD_CFG} -> {NEW_CFG}")
    if old_state.is_dir(): os.replace(old_state, new_state); did.append(f"{root}: {OLD_STATE}/ -> {NEW_STATE}/")
    did += migrate_hooks() + migrate_watcher()
    for x in did: say(x)
    say("janus migrate: " + ("done" if did else "nothing to do (already migrated)"))
    return 0

def migrate_hooks():
    """Point the hook commands a previous install wrote (…/orch hook <event>, or an older …/orch.py hook <event>) at janus.
    A rewritten file is backed up first; returns one line per file changed."""
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
            p.with_name(p.name + ".bak-orch").write_text(p.read_text()); atomic_write(p, json.dumps(data, indent=2) + "\n")
            out.append(f"{p}: {n} hook command(s) -> janus")
    return out

def migrate_watcher():
    """Replace the orch watcher with the janus one: macOS boots out com.orch.watch and installs the shipped
    com.janus.watch.plist; Linux renames the systemd units. Returns one line per change."""
    out = []
    if sys.platform == "darwin":
        old = Path.home() / "Library/LaunchAgents/com.orch.watch.plist"
        new = Path.home() / "Library/LaunchAgents/com.janus.watch.plist"
        if old.exists():
            run(["launchctl", "bootout", f"gui/{os.getuid()}/com.orch.watch"]); old.unlink(); out.append("removed the com.orch.watch watcher")
        src = JANUS_SELF.parent / "launchd" / "com.janus.watch.plist"
        if src.is_file() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True); (Path.home() / ".config/janus").mkdir(parents=True, exist_ok=True)
            new.write_text(src.read_text().replace("__HOME__", str(Path.home())))
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(new)]); out.append("installed the com.janus.watch watcher")
    else:
        u = Path.home() / ".config/systemd/user"
        units = [("orch-watch.service", "janus-watch.service"), ("orch-watch.timer", "janus-watch.timer")]
        if any((u / a).exists() for a, _ in units):
            run(["systemctl", "--user", "disable", "--now", "orch-watch.timer"])
            for a, b in units:
                if (u / a).exists() and not (u / b).exists(): (u / b).write_text((u / a).read_text().replace("orch", "janus")); (u / a).unlink()
            run(["systemctl", "--user", "daemon-reload"]); run(["systemctl", "--user", "enable", "--now", "janus-watch.timer"])
            out.append("renamed the systemd watcher units to janus-watch")
    return out

def cmd_doctor(cfg, a): return doctor(cfg, a)

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
    r(cfg.root in projects() or None, f"project listed in {pl} (the watcher visits it)", f"run ./install --project {cfg.root}")
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
    for n, h in cfg.hosts.items():
        if a.host and n != a.host: continue
        rc, o, e = hrun(h, f"{pueue(h)} " + (f"log {int(a.log)}" if a.log is not None else "status"), timeout=30)
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
    say(f"{n}: {f['os']} {f['arch']}, {f['cores']} cores, {(f['mem_mb'] or 0) / 1024:.1f} GB; tools {' '.join(f['tools'])}")
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
