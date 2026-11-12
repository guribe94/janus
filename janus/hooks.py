"""The Claude Code / Codex hooks (one format): SessionStart, UserPromptSubmit, PreToolUse and Stop.

The PreToolUse shell rules parse the command (heredocs are data, substitutions are judged, wrappers are stripped); extend the
parser, never replace it (section 9)."""
import json, os, re, shlex, sys
from pathlib import Path
from janus.beads import Beads
from janus.reset import stop_reset
from janus.context import (CLAIM_WINDOW, Cfg, Err, atomic_write, JANUS_MARK, find_root, handoff_path, handoff_stale, note_event, now, pending_age, record_session_proc,
    session_alive, session_root, sh, ts)
from janus.decisions import session_text as decisions_text
from janus.memory import memory_text
from janus.units import main_checkout, own_tree

# PreToolUse shell rules (audit H2, M5). The command is parsed, not pattern-matched: heredoc bodies are data unless fed to a
# shell or ssh; $( ), backticks and <( ) are judged as commands of their own; the rest is split on ; && || | & ( ) and newlines,
# and every segment is judged by its command word after wrappers (sudo, env, timeout, nice, nohup, xargs, command, exec, ...)
# and an absolute path are stripped. bash/sh/zsh -c, eval and ssh <host> <cmd> are judged recursively. Nothing passes because
# it starts with janus, bd, pueue, herdr or board.sh: those tools have their own allow-lists below.

BD_OK = set("list show ready create update close reopen comment comments note label tag assign q query search find-duplicates duplicates count "
            "status statuses types stale history diff lint dep graph children blocked epic remember recall memories forget prime info where "
            "context help version heartbeat defer undefer priority promote set-state state unclaim reclaim todo kv".split())

PUEUE_OK = {"status", "log", "follow", "wait", "help", "group", "completions"}   # read-only, here or over ssh (group: listing only)

HERDR_NO = {("pane", "close"), ("pane", "run"), ("pane", "send-text"), ("pane", "send-keys"), ("agent", "send-keys"), ("tab", "close"),
            ("workspace", "close"), ("worktree", "remove"), ("server", "stop")}

WRAP = {"sudo": "-u -g -h -p -C", "doas": "-u", "env": "-u -C -S", "timeout": "-s -k --signal --kill-after", "nice": "-n", "nohup": "",
        "command": "", "exec": "-a", "builtin": "", "time": "", "caffeinate": "-t -w", "stdbuf": "-i -o -e", "setsid": "", "ionice": "-c -n -p",
        "xargs": "-n -P -I -L -s -E -d -a --max-args --max-procs --replace --delimiter --arg-file", "chronic": "", "unbuffer": ""}

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}

INTERP = re.compile(r"^(python[\d.]*|perl|ruby|node|osascript)$")

INTERP_BAD = re.compile(r"rmtree|os\.(remove|unlink|rmdir|kill|killpg)|\.unlink\(|unlink\s*\(|\bsignal\.SIG|subprocess.*\b(rm|kill|pkill)\b|system\s*\(.*\b(rm|kill)\b|do shell script.*\b(rm|kill)\b")

DESTRUCT = {"kill": "kill by hand: use janus stop", "pkill": "kill by hand: use janus stop", "killall": "kill by hand: use janus stop",
            "truncate": "truncating files", "shred": "shred", "dd": "dd", "reboot": "reboot", "shutdown": "shutdown", "halt": "halt",
            "diskutil": "diskutil", "srm": "srm"}

QUOTED_TEXT = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")

def unquoted(text):
    """text without its quoted strings that hold whitespace (messages and prose: data); a quoted path stays."""
    return QUOTED_TEXT.sub(lambda m: m.group(0) if not re.search(r"\s", m.group(0)) else "''", text)

def scan_subst(c):
    """Replace $( ), backticks, <( ) and >( ) outside single quotes by a placeholder; turn unquoted newlines into ';' and drop
    comments. Returns (text, [inner texts])."""
    out, inner, i, q = [], [], 0, None
    while i < len(c):
        ch = c[i]
        if q == "'":
            out.append(ch); q = None if ch == "'" else q; i += 1; continue
        if ch == "\\" and i + 1 < len(c): out.append(c[i:i + 2]); i += 2; continue
        if ch == "'" and q is None: q = "'"; out.append(ch); i += 1; continue
        if ch == '"': q = None if q == '"' else ('"' if q is None else q); out.append(ch); i += 1; continue
        if q is None and ch == "#" and (i == 0 or c[i - 1] in " \t\n;&|("):
            while i < len(c) and c[i] != "\n": i += 1
            continue
        if q is None and ch == "\n": out.append(" ; "); i += 1; continue
        if (ch == "$" or (ch in "<>" and q is None)) and c[i + 1:i + 2] == "(" and c[i + 2:i + 3] != "(":
            d, j, qq = 1, i + 2, None
            while j < len(c) and d:
                x = c[j]
                if qq: qq = None if x == qq else qq
                elif x in "'\"": qq = x
                elif x == "(": d += 1
                elif x == ")": d -= 1
                j += 1
            inner.append(c[i + 2:j - 1]); out.append(" __SUBST__ "); i = j; continue
        if ch == "`":
            j = c.find("`", i + 1); j = len(c) if j < 0 else j
            inner.append(c[i + 1:j]); out.append(" __SUBST__ "); i = j + 1; continue
        out.append(ch); i += 1
    return "".join(out), inner

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")

def split_heredocs(c):
    """(command text without heredoc bodies, [(line that opened it, body)])."""
    lines, out, docs, i = c.split("\n"), [], [], 0
    while i < len(lines):
        l = lines[i]; out.append(l); i += 1
        for m in HEREDOC.finditer(l):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2): body.append(lines[i]); i += 1
            i += 1; docs.append((l, "\n".join(body)))
    return "\n".join(out), docs

def words_of(text):
    lx = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>"); lx.whitespace_split = True; lx.commenters = ""
    try: return list(lx)
    except ValueError: return text.split()

def strip_wrappers(w):
    """Drop keywords, assignments and wrapper commands in front of the real command word."""
    while w:
        b = os.path.basename(w[0])
        if w[0] in ("do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "time", "done", "fi", "esac"): w = w[1:]; continue
        if re.match(r"^[A-Za-z_]\w*=", w[0]): w = w[1:]; continue
        if b in WRAP:
            val = set(WRAP[b].split()); w = w[1:]
            while w and (w[0].startswith("-") or (b == "env" and "=" in w[0]) or (b == "timeout" and re.match(r"^[\d.]+[smhd]?$", w[0]))):
                o = w.pop(0)
                if o in val and w: w.pop(0)
                if b == "timeout" and re.match(r"^[\d.]+[smhd]?$", o): break
            continue
        break
    return w

def segments(text):
    """Split parsed words into command segments, with the redirection targets of each."""
    segs, cur, redir, w = [], [], [], words_of(text)
    i = 0
    while i < len(w):
        t = w[i]
        if t in (";", "&&", "||", "|", "&", "(", ")", ";;", "|&", "&;"):
            if cur or redir: segs.append((cur, redir))
            cur, redir = [], []; i += 1; continue
        if re.fullmatch(r"\d*(>|>>|>\||&>|&>>)", t) or (t.endswith(">") and set(t) <= set("0123456789&>|")):
            if i + 1 < len(w) and not w[i + 1] in ("&",):
                tgt = w[i + 1]
                if tgt == "&" and i + 2 < len(w): i += 3; continue   # 2>&1 style
                redir.append(tgt); i += 2; continue
            i += 1; continue
        if t in (">&", "<&", "<", "<<", "<<<", "<<-", "<>"): i += 2; continue
        if re.fullmatch(r"\d*>&\d*-?|\d*<&\d*-?", t): i += 1; continue
        cur.append(t); i += 1
    if cur or redir: segs.append((cur, redir))
    return segs

def write_ok(cfg, cwd, tgt, ok_dirs):
    """A redirection target the orchestrator may write: outside the project and the worktrees, or inside an allowed path."""
    if tgt.startswith("/dev/") or tgt in ("__SUBST__", "-") or tgt.startswith("$"): return True
    p = Path(os.path.expanduser(tgt)); p = (p if p.is_absolute() else Path(cwd) / p).resolve()
    guarded = [cfg.root.resolve(), cfg.p("worktrees", "worktrees").resolve()]
    if not any(p == g or g in p.parents for g in guarded): return True
    return any(p == o or o in p.parents for o in ok_dirs)

def judge_words(cfg, w, cwd, ok_dirs, remote, depth):
    w = strip_wrappers(w)
    if not w: return None
    b = os.path.basename(w[0]); args = w[1:]
    if b in SHELLS:
        for i, x in enumerate(args):
            if x == "-c" or (x.startswith("-") and not x.startswith("--") and "c" in x[1:]):
                return judge(cfg, args[i + 1] if i + 1 < len(args) else "", cwd, ok_dirs, remote, depth + 1)
        return None
    if b == "eval": return judge(cfg, " ".join(args), cwd, ok_dirs, remote, depth + 1)
    if b in ("ssh", "mosh"):
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if re.fullmatch(r"-[bcDEeFIiJLlmOoPpQRSWw]", args[i]) else 1
        rest = args[i + 1:]
        return judge(cfg, " ".join(rest), cwd, ok_dirs, True, depth + 1) if rest else None
    if b in DESTRUCT: return DESTRUCT[b]
    if b in ("mkfs",) or b.startswith("mkfs."): return "mkfs"
    if b == "rm" and any(x in ("-r", "-R", "--recursive") or (re.fullmatch(r"-[A-Za-z]+", x) and re.search(r"[rR]", x)) for x in args if x != "--"):
        return "rm -r by hand"
    if b == "sed" and any(x.startswith("--in-place") or re.fullmatch(r"-[A-Za-z]*i\S*", x) for x in args): return "in-place edit"
    if b == "perl" and any(re.fullmatch(r"-\w*i\S*", x) for x in args): return "in-place edit"
    if INTERP.match(b):
        code = " ".join(args[i + 1] for i, x in enumerate(args[:-1]) if x in ("-c", "-e", "-E"))
        if INTERP_BAD.search(code): return f"destructive {b} one-liner"
    if b == "find" and any(x in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for x in args):
        if "-delete" in args: return "find -delete"
        k = next(i for i, x in enumerate(args) if x in ("-exec", "-execdir", "-ok", "-okdir"))
        if k + 1 < len(args) and os.path.basename(args[k + 1]) in ("rm", "mv", "unlink", "shred", "truncate", "rmdir"): return f"find -exec {os.path.basename(args[k + 1])}"
        why = judge_words(cfg, [x for x in args[k + 1:] if x not in (";", "+", "{}")], cwd, ok_dirs, remote, depth + 1)
        if why: return "find -exec: " + why
    if b == "rsync" and any(x.startswith(("--delete", "--remove-source-files")) for x in args): return "rsync --delete by hand"
    if b == "git":
        i = 0
        while i < len(args) and args[i].startswith("-"): i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        sub, ga = (args[i], args[i + 1:]) if i < len(args) else ("", [])
        if sub == "reset" and any(x in ("--hard", "--merge", "--keep") for x in ga): return "git reset --hard"
        if sub == "clean": return "git clean"
        if sub == "push" and any(x in ("-f", "--force", "--mirror", "--delete", "-d", "--prune") or x.startswith(("--force", "+")) or (x.startswith(":") and len(x) > 1) for x in ga): return "git push --force/--delete"
        if sub in ("checkout", "switch") and any(x in ("--", ".", "-f", "--force", "--discard-changes") for x in ga): return f"git {sub} that discards changes"
        if sub == "restore": return "git restore"
        if sub == "branch" and any(x in ("-D", "--delete", "-d", "-f", "--force") for x in ga): return "git branch delete"
        if sub == "stash" and ga[:1] and ga[0] in ("drop", "clear"): return "git stash drop"
        if sub == "worktree" and ga[:1] and ga[0] in ("remove", "prune"): return "git worktree remove"
        if sub == "sparse-checkout": return "sparse-checkout"
        if sub in ("filter-branch", "filter-repo", "update-ref", "gc", "prune"): return f"git {sub}"
        if sub == "reflog" and ga[:1] and ga[0] in ("expire", "delete"): return "git reflog expire"
    if b == "pueue":
        i = 0
        while i < len(args) and args[i].startswith("-"): i += 2 if args[i] in ("-c", "--config", "-p", "--profile", "--color") else 1
        sub = args[i] if i < len(args) else "status"
        if sub not in PUEUE_OK or (sub == "group" and len(args) > i + 1 and not args[i + 1].startswith("-")):
            return f"pueue {sub} by hand: use janus run/stop/close (pueue limits are janus's)"
    if b == "bd":
        sub = next((x for x in args if not x.startswith("-")), "")
        if sub and sub not in BD_OK: return f"bd {sub} by hand"
    if b == "herdr":
        a2 = [x for x in args if not x.startswith("-")][:2]
        if tuple(a2) in HERDR_NO: return f"herdr {' '.join(a2)} by hand: janus launch/stop/close manage panes"
    if b == "board.sh" and args[:1] == ["close"]: return "board.sh close by hand: janus close does it"
    if b == "janus" and args[:1] == ["close"] and "--force" in args and not force_close_ok(cfg, args): return "janus close --force (owner: never on shared repos; allowed on a unit's own non-shared worktree)"
    if b == "janus" and args[:1] == ["api-run"]: return "janus api-run by hand: janus launch queues it (budget, ledger, lanes)"
    if b == "launchctl" and args[:1] and args[0] in ("bootout", "remove", "unload", "kill", "disable"): return f"launchctl {args[0]}"
    if b == "systemctl" and any(x in ("stop", "restart", "kill", "disable", "mask", "reboot", "poweroff") for x in args): return "systemctl stop/restart"
    if b in ("podman", "docker") and args[:1] and args[0] in ("rm", "kill", "stop", "rmi", "prune", "system", "volume", "restart"): return f"{b} {args[0]} by hand"
    if b == "tmux" and any(x.startswith("kill-") for x in args): return "tmux kill by hand: use janus stop"
    return None

GIT_MUT = {"add", "commit", "push", "reset", "checkout", "switch", "clean", "stash", "merge", "rebase", "pull", "am", "apply", "cherry-pick", "revert", "rm", "mv"}

def git_sub(args):
    i = 0
    while i < len(args) and args[i].startswith("-"): i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return args[i] if i < len(args) else ""

MUTATORS = {"rm", "mv", "cp", "ln", "rsync", "touch", "chmod", "chown", "install", "rmdir", "unlink", "tee"}

def force_close_ok(cfg, args):
    """janus close <unit> --force on the unit's own worktree (not the project root, no other open unit's cwd, not a main checkout
    with linked worktrees) is the orchestrator's to run (janus-fixes10 item 5)."""
    names = [x for x in args[1:] if not x.startswith("-")]
    if "--reason" in args:
        i = args.index("--reason"); names = [x for x in names if x != (args[i + 1] if i + 1 < len(args) else None)]
    if len(names) != 1: return False
    try:
        units = Beads(cfg).units(); u = next((x for x in units if x["name"] == names[0]), None)
        return bool(u and u.get("cwd") and own_tree(cfg, u, units) and not main_checkout(Path(u["cwd"])))
    except Exception: return False

JANUS_READ = {"ack", "check", "status", "queue", "peers", "transcript", "memory", "report", "capacity", "busy", "doctor", "learned", "thrash"}

# Why a refusal names the command that does the job instead (F13): a model shown the wrong alternative retries variants.
DENY_ALT = (
    (r"\bpueue\b", "janus cancel <host>:<task> (see them with janus queue)"),
    (r"herdr ", "janus launch/stop/close (they manage panes)"),
    (r"kill by hand|tmux kill|launchctl |systemctl stop|docker |podman ", "janus stop <unit>"),
    (r"\bgit\b|rm -r|find -delete|find -exec|rsync --delete|sparse-checkout|truncat|^dd\b", "janus tidy --dry-run, then janus tidy"),
    (r"editing files by hand|in-place edit|writing .* by hand|destructive ", "brief an agent (janus launch)"),
    (r"janus api-run", "janus launch"),
    (r"board\.sh close", "janus board-close <agent> (an open unit: janus close <unit>)"),
    (r"janus close --force", "janus close <unit> (--force only on the unit's own worktree)"),
)

def deny_alt(why):
    """The janus command that does what a refused hand operation was trying to do (F13)."""
    for rx, alt in DENY_ALT:
        if re.search(rx, why): return alt
    return "use the janus command check names, or brief an agent"

def judge(cfg, c, cwd, ok_dirs, remote=False, depth=0):
    """Why the shell command c is refused, or None."""
    if depth > 6: return "command nested too deeply to judge"
    text, docs = split_heredocs(c)
    for line, body in docs:   # a heredoc fed to a shell or ssh is commands; any other is data
        lw = strip_wrappers(words_of(scan_subst(line.split("<<")[0])[0]))
        fed = [x for x in (strip_wrappers(w) for w, _ in segments(scan_subst(line)[0])) if x]   # `cat <<EOF | bash` feeds a shell too
        if not (lw and os.path.basename(lw[0]) in SHELLS | {"ssh", "eval"} and "-c" not in lw):
            lw = next((x for x in fed[1:] if os.path.basename(x[0]) in SHELLS | {"ssh", "eval"} and "-c" not in x), lw)
        if lw and (os.path.basename(lw[0]) in SHELLS or os.path.basename(lw[0]) in ("ssh", "eval")) and not any(x == "-c" for x in lw):
            why = judge(cfg, body, cwd, ok_dirs, remote or os.path.basename(lw[0]) == "ssh", depth + 1)
            if why: return why
        elif lw and INTERP.match(os.path.basename(lw[0])) and INTERP_BAD.search(body): return f"destructive {os.path.basename(lw[0])} script"
    text, inner = scan_subst(text)
    for x in inner:
        why = judge(cfg, x, cwd, ok_dirs, remote, depth + 1)
        if why: return why
    # A loop is judged from command words only (janus-fixes7): `for`/`while`/`until`/`select` or xargs/parallel in command
    # position, over worktrees named outside quoted text; a commit message or an echo that says "while" or "worktrees" is data.
    # Only the commands inside the loop count (janus-fixes10 item 5): a `git merge` before or after a loop is not in it; a loop
    # of `janus ack` / janus read commands is allowed; file and git changes, and other janus commands, in it are refused.
    segs = segments(text)
    wt = re.search(r"\bworktrees?\b", unquoted(text)); depth_ = 0; inloop = []
    for w, _ in segs:
        sw = strip_wrappers(list(w))
        if w and w[0] in ("for", "while", "until", "select"): depth_ += 1; inloop.append(False); continue   # header (the list or condition)
        if w and w[0] == "done": depth_ = max(0, depth_ - 1); inloop.append(False); continue
        xa = any(os.path.basename(x) in ("xargs", "parallel") for x in w[:3])   # strip_wrappers drops xargs itself
        inloop.append(depth_ > 0 or xa)
    for k, (w, redir) in enumerate(segs):
        if w and w[0] == "cd" and len(w) > 1 and not remote: cwd = str((Path(cwd) / os.path.expanduser(w[1])).resolve())
        if w and w[0] in ("for", "case", "select", "function", "in"): continue
        why = judge_words(cfg, w, cwd, ok_dirs, remote, depth)
        if why: return why
        if not remote:
            for t in redir:
                if not write_ok(cfg, cwd, t, ok_dirs): return f"writing {t} by hand"
        sw = strip_wrappers(w)
        if sw and os.path.basename(sw[0]) in ("xargs", "parallel"):   # the command it runs per item
            r_, i = sw[1:], 0
            while i < len(r_) and r_[i].startswith("-"): i += 2 if r_[i] in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-j") else 1
            sw = strip_wrappers(r_[i:])
        if wt and inloop[k] and sw and (os.path.basename(sw[0]) in MUTATORS or (sw[0] == "git" and git_sub(sw[1:]) in GIT_MUT)
                                         or (os.path.basename(sw[0]) == "janus" and len(sw) > 1 and sw[1] not in JANUS_READ)):
            return "loop that changes worktrees"
    return None

def edit_ok_dirs(cfg):
    """Paths the orchestrator may write: its state dir, briefs, janus.toml, its own plan files (~/.claude/plans: plan mode writes
    there), and the project's status checklist and old state docs (paths.checklist, paths.state_docs) (audit M7)."""
    ok = [cfg.state, cfg.p("briefs", "briefs"), cfg.root / "janus.toml", Path.home() / ".claude/plans"]   # its own plan files (plan mode)
    if cfg.paths.get("checklist"): ok.append(cfg.p("checklist"))
    ok += [cfg.root / x for x in cfg.paths.get("state_docs", [])]
    return [p.resolve() for p in ok]

def patch_paths(ti):
    """Target paths of an apply_patch call (Codex): '*** Add|Update|Delete File: <path>' and '*** Move to: <path>'."""
    txt = "\n".join(v for v in (ti.values() if isinstance(ti, dict) else [ti]) if isinstance(v, str))
    return re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", txt, re.M)

def pretool_why(cfg, d):
    """Why a PreToolUse call of the orchestrator is refused, or None."""
    tool, ti = d.get("tool_name", ""), d.get("tool_input") or {}
    cwd = d.get("cwd") or str(cfg.root); ok = edit_ok_dirs(cfg)
    inside = lambda fp: any(fp == o or o in fp.parents for o in ok)
    if tool == "apply_patch" or (isinstance(ti, dict) and any(isinstance(v, str) and "*** Begin Patch" in v for v in ti.values())):
        ps = patch_paths(ti)
        bad = [p for p in ps if not inside((Path(p) if Path(p).is_absolute() else Path(cwd) / p).resolve())]
        return "editing files by hand" if bad or not ps else None
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = Path(ti.get("file_path") or ti.get("notebook_path") or "/x")
        return None if inside((fp if fp.is_absolute() else Path(cwd) / fp).resolve()) else "editing files by hand"
    c = ti.get("command") if isinstance(ti, dict) else None
    if c is None and isinstance(ti, dict): c = ti.get("cmd")
    if c is None: return None
    c = shlex.join(c) if isinstance(c, list) and not (len(c) >= 3 and c[0] in SHELLS and c[1] in ("-c", "-lc")) else (c[2] if isinstance(c, list) else c)
    return judge(cfg, c, cwd, ok)

def deny_log(cfg, d, why):
    """.janus/denied.log: every refused tool call, so a wrong refusal can be found and fixed (janus-fixes7)."""
    ti = d.get("tool_input") or {}
    c = ti.get("command") or ti.get("cmd") or ti.get("file_path") or "" if isinstance(ti, dict) else ti
    try:
        with cfg.f("denied.log").open("a") as f: f.write(f"{ts()} {d.get('tool_name', '')} ({why}): {json.dumps(c)[:400]}\n")
    except OSError: pass

def out(o): print(json.dumps(o))

def ctx(ev, text): out({"hookSpecificOutput": {"hookEventName": ev, "additionalContext": text}})

def block(reason): out({"decision": "block", "reason": reason})

AGENT_VERDICT = re.compile(r"^[\s*>-]*VERDICT:\s*(DONE|BLOCKED|FAILED)\b")
AGENT_WAIT = re.compile(r"^[\s*>-]*WAITING\b\**:?\s*\S")   # the PARK_RULE line (units.WAIT_RX, one shape)

def agent_hook(ev, d):
    """The Stop hook of a unit agent. It allows a stop that is (a) finished — a VERDICT line and the real exit codes, line 1 no
    longer PENDING — or (b) parked (PARK_RULE): the last 'WAITING <job ids>' line is newer than any verdict line. Anything else
    blocks with the text that names all three verdicts (F1, F6)."""
    rp = os.environ.get("JANUS_REPORT")
    if ev != "Stop" or not rp: return
    try: t = Path(rp).read_text()
    except OSError: t = ""
    lines = t.splitlines()
    verdict = [i for i, l in enumerate(lines) if AGENT_VERDICT.match(l)]
    waiting = [i for i, l in enumerate(lines) if AGENT_WAIT.match(l)]
    # finished: a VERDICT line and the real exit codes (janus reads the last VERDICT line; line 1 stays PENDING until then)
    if verdict and re.search(r"exit(\s+code)?s?\b", t, re.I): return
    # parked (PARK_RULE): the last 'WAITING <job ids>' line is newer than any verdict line
    if waiting and (not verdict or waiting[-1] > verdict[-1]): return
    # The runtime re-fires Stop while the hook blocks it (stop_hook_active); after it repeats twice, stop blocking so a misjudged
    # report can never loop forever. The count lives beside the report (one file per unit, collect brings it along harmlessly).
    f = Path(rp + ".stop")
    try: c = (int(f.read_text().strip() or 0) + 1) if d.get("stop_hook_active") else 0
    except (OSError, ValueError): c = 0
    try: f.write_text(str(c))
    except OSError: pass
    if c < 2:
        block(f"[janus] Not finished. Before you stop, {rp} must either (a) end with 'VERDICT: DONE', 'VERDICT: BLOCKED' or "
              f"'VERDICT: FAILED' plus the real exit codes of what you ran, or (b) hold a line 'WAITING <job ids>' for jobs you "
              f"submitted detached. Write the true one; never write DONE to get past this check.")

def capped(text, n, note):
    """A part of the SessionStart context, cut to n chars with the note that says how to see the rest (F2: each part is capped
    on its own, so a long handoff or bd prime can never push the instruction or a later part out)."""
    return text if len(text) <= n else text[:n] + f"\n({note})"

def session_start_text(cfg):
    """What a session gets at start and on `janus resume --here` (item 4): the instruction first, then the live state, the
    session memory, the decisions index and the owner rules (bd prime), each labelled and capped on its own (F2). The same text
    either way, so a resumed session loses nothing (imp-resume reuses this from janus/setup.py)."""
    # The instruction comes first, the one place the model weights most, and every part is labelled and capped on its own
    # (F2): a long handoff or bd prime can no longer push the instruction past the cut.
    head = (f"[janus] You are the orchestrator of {cfg.name} (janus skill). First act: run janus check, act on every line, "
            f"janus ack <key> \"what you did\", then update the Orchestrator notes. Below: (1) HANDOFF.md: Live state as of "
            f"the last check, then your notes; (2) session MEMORY.md; (3) decision titles; (4) owner rules (bd prime).")
    parts = [head]
    hp = handoff_path(cfg)
    if hp.exists(): parts += ["## 1 HANDOFF.md", capped(hp.read_text(), 16000, "cut: read .janus/HANDOFF.md")]
    mt = memory_text(cfg)
    if mt: parts += ["## 2 MEMORY.md", capped(mt, 8000, "cut: janus memory")]
    dt = decisions_text(cfg)
    if dt: parts += ["## 3 Decisions", capped(dt, 6000, "cut: janus decisions")]
    rc, o, e = sh(["bd", "prime"], cwd=cfg.beads, timeout=20)
    parts += ["## 4 Owner rules (bd prime)", capped(o if rc == 0 else f"(bd prime unavailable: {e.strip()[:80]})", 6000, "cut: run bd prime")]
    return "\n\n".join(parts)

def cmd_hook(ev):
    try: d = json.loads(sys.stdin.read() or "{}")
    except Exception: d = {}
    if os.environ.get("JANUS_ROLE") == "agent": return agent_hook(ev, d)
    sid = d.get("session_id")
    if not sid: return
    # The project is the one whose registered session this is, wherever the session's shell is (H3). Only a pending
    # registration (janus register without a session id, or the watcher's new orchestrator) is claimed from the cwd, within
    # CLAIM_WINDOW (10 min). The watcher records the pane it started the new orchestrator in (pending:<ts>:<pane>, panel-eng F5): only a hook
    # in that pane claims it. No pane recorded (janus register): the old behaviour. No variable (JANUS_ROLE) takes a
    # registration over (H4).
    root = session_root(sid)
    if not root:
        root = find_root(d.get("cwd"))
        if not root: return
        cfg = Cfg(root); reg = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
        parts = reg.split(":", 2); pane = parts[2] if len(parts) > 2 else ""; age = pending_age(reg)
        if age is None or age >= CLAIM_WINDOW: return
        if pane and os.environ.get("HERDR_PANE_ID") != pane: return
        atomic_write(cfg.f("session"), sid); atomic_write(cfg.f("session_kind"), hook_kind(d) + "\n")
        record_session_proc(cfg)
    else: cfg = Cfg(root)
    if session_alive(cfg) is not True: record_session_proc(cfg)
    if d.get("transcript_path"): atomic_write(cfg.f("transcript"), d["transcript_path"])
    if ev == "SessionStart":   # cached state only: the hook has a 60 s limit, and the check runs as the session's first act (M14)
        ctx(ev, session_start_text(cfg))
    elif ev == "UserPromptSubmit":
        pr = (d.get("prompt") or "").strip()
        janus_own = pr.startswith(JANUS_MARK) or pr.startswith("/")   # every prompt janus sends carries the mark (F15: no WAKE prefix list to keep in sync)
        if pr and not janus_own and "<task-notification>" not in pr and "[SYSTEM NOTIFICATION" not in pr:
            try:
                i = Beads(cfg).create("instr: " + re.sub(r"\s+", " ", pr)[:70], "instruction", pr[:4000]); note_event(cfg, "instruction")
                ctx(ev, f"[janus] Filed as instruction {i}. Do it, then: close it; or, if it is a standing rule, bd remember "
                    f"\"<rule>\" --key <subject> (reuse an existing key to replace it: bd memories <word>); if agents must follow "
                    f"it, janus remember it instead; if it settles a choice, janus decide ... --instruction {i}.")
            except Err: pass
    elif ev == "PreToolUse":
        why = pretool_why(cfg, d)
        if why: deny_log(cfg, d, why)
        if why: out({"hookSpecificOutput": {"hookEventName": ev, "permissionDecision": "deny",
                     "permissionDecisionReason": f"[janus] Refused: {why}. The orchestrator does not operate by hand. Instead: "
                         f"{deny_alt(why)}. If this refusal is wrong, write it in the notes (denied.log has it) and carry on "
                         f"another way; do not retry variants of the command."}})
    elif ev == "Stop":
        n = cfg.jget("stops.json"); c = n.get(sid, 0) + 1 if d.get("stop_hook_active") else 0; n[sid] = c; cfg.jput("stops.json", n)
        if c >= 3: return
        op = cfg.jget("open.json"); late = [k for k, t in op.items() if now() - t > cfg.deadline]
        stale = handoff_stale(cfg)   # stale only after a real event (launch, close, landing, instruction, decision)
        # a Stop that passes is the safe boundary for the context reset (janus/reset.py); a blocked one logs why it waits
        stop_reset(cfg, ", ".join(([f"late findings {', '.join(late[:3])}"] if late else []) + (["notes stale"] if stale else [])) or None)
        if late or stale:
            pieces = []
            if late: pieces.append(f"{len(late)} finding(s) past deadline ({', '.join(late[:6])}): act on each, then janus ack <key> \"what you did\"")
            if stale: pieces.append(" ".join(stale).rstrip("."))
            block("[janus] Not yet: " + ". ".join(pieces) + ".")

def hook_kind(d):
    tp = d.get("transcript_path") or ""
    return "codex" if ("codex" in tp or os.environ.get("CODEX_THREAD_ID")) else "claude"
