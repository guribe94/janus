"""The message board contract: board.sh runs this file (one stdlib-only file, Python 3.8+, so it also runs on a remote host
where only board.sh and this file are shipped, next to each other in <root>/bin). Plain files, no service.

argv[1] is the board.sh path the caller used (rule names it; sync ships it), the rest is the board command."""
import fcntl, glob, json, os, re, socket, subprocess, sys, time

USAGE = """Message board for the orchestrator and every agent on every machine (shipped with orch; plain files, no service).
The board lives on the coordinating machine in $BOARD_DIR (orch sets it; default <this script>/../board), marked by .master:
  log.md               append-only, one line per post: YYYY-MM-DD HH:MM <agent> <kind>: <text>
  agents/<name>.md     the agent's card: task, brief, machine, state, step, blockers, report, check, updated
  to/<name>.md         the orchestrator's directions to that agent, newest first
  questions/<id>.md    each question (id q<N>): it stays "needs answer" until the orchestrator answers it
  .hosts               other hosts, one per line: <name> <ssh target> <root> (orch writes it from orch.toml)
  .notify              a command run in the background on a new question or block (orch: wakes the orchestrator at once)
On another host the same script (<root>/bin/board.sh) writes posts to <root>/board-outbox/outbox.log; `sync` on the
coordinating machine merges each outbox in order and pushes a read-only copy of the board to <root>/board/ on each host,
where `read`, `summary` and the peers.md / index.md files orch writes there can be read.
Usage:
  board.sh post <agent> <kind> <text>   kind: status blocked question waiting learned done (step: kept, but steps belong in the
                                        report). waiting <job ids>: parked on jobs, never SILENT or GONE. learned <fact>: a
                                        proposal for the session memory (orch check lists it until remembered or rejected).
                                        A launcher sets the card's fixed fields on its first post: BOARD_MACHINE, BOARD_BRIEF,
                                        BOARD_REPORT, BOARD_CHECK ("pid <n>", "herdr <agent>", "pueue <host>:<task>", "tab <label>").
  board.sh direct <agent|all> <text>    orchestrator to an agent (or every open card); answers that agent's open questions
  board.sh answer <question id> <text>  orchestrator: answer one question (a direction to its agent)
  board.sh read <agent>                 the agent's directions
  board.sh questions                    open questions: <id> <agent> <asked> <text> (tab-separated)
  board.sh summary                      every open card grouped: needs answer, blocked, working, waiting, done; flags SILENT
                                        (> BOARD_SILENT_MIN, default 30 min, without a post) and GONE (process ended without done)
  board.sh digest [--since 1h]          for humans: landed, started, asked, answered, failed in that window
  board.sh close <agent>                archive a card and its directions; its open questions are withdrawn
  board.sh rule <agent> [host]          the board rule a launcher adds to an agent's prompt (one place)
  board.sh sync                         pull every host's outbox, then push the board copy to every host
Every write on the coordinating machine takes one flock (.lock)."""

KINDS = ("status", "step", "blocked", "question", "waiting", "learned", "done")
FIELDS = ("task", "brief", "machine", "state", "step", "blockers", "report", "check", "updated")
GROUP = {"question": 1, "blocked": 2, "working": 3, "waiting": 4, "done": 5}
SSH = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
RSYNC_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=8"


class Die(Exception): pass


def now(): return time.strftime("%Y-%m-%d %H:%M")


def epoch_of(t):
    try: return int(time.mktime(time.strptime(t, "%Y-%m-%d %H:%M")))
    except (ValueError, OverflowError): return 0


def okname(a):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a or ""): raise Die(f"bad agent name '{a}'")


def oneline(words): return re.sub(r"[\n\r\t]", " ", " ".join(words))


def read(p):
    try:
        with open(p, errors="replace") as f: return f.read()
    except OSError: return ""


def write(p, text):
    """Write by temp file + rename (a reader never sees half a card)."""
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w") as f: f.write(text)
    os.replace(tmp, p)


def field(p, name):
    m = re.search(rf"^- {re.escape(name)}: ?(.*)$", read(p), re.M)
    return m.group(1) if m else ""


def run(cmd, timeout=60):
    try: r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired): return 1, ""
    return r.returncode, r.stdout


class Locked:
    """flock(2) on a lock file: the lock flock(1) and every older board.sh take, so old and new writers exclude each other."""
    def __init__(self, path, wait=True): self.path, self.wait = path, wait
    def __enter__(self):
        self.f = open(self.path, "a")
        try: fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except OSError: self.f.close(); raise
        return self
    def __exit__(self, *e): self.f.close()


class Board:
    def __init__(self, script):
        self.script = script
        here = os.path.dirname(os.path.abspath(script))
        self.bd = os.environ.get("BOARD_DIR") or os.path.join(os.path.dirname(here), "board")
        self.out = os.environ.get("BOARD_OUTBOX") or os.path.join(os.path.dirname(self.bd), "board-outbox")
        self.master = os.path.exists(os.path.join(self.bd, ".master"))

    def p(self, *a): return os.path.join(self.bd, *a)

    def localname(self):
        n = read(self.p(".master")).split("\n")[0].strip()
        return n or socket.gethostname().split(".")[0] or "local"

    def hosts(self):
        return [l.split() for l in read(self.p(".hosts")).splitlines() if l.strip() and not l.strip().startswith("#") and len(l.split()) >= 3]

    def notify(self):
        """The .notify command in the background, without the board's lock (it must never hold or wait for it)."""
        cmd = read(self.p(".notify")).split("\n")[0]
        if cmd.strip():
            subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)

    # ---- writes on the coordinating machine (the caller holds .lock)
    def apply(self, t, a, k, x, brief="", report="", machine="", check="", host=""):
        """One post: its log line, a question's record, the card rewritten."""
        os.makedirs(self.p("agents"), exist_ok=True); os.makedirs(self.p("to"), exist_ok=True)
        if k == "question":   # a question gets an id; it needs an answer until the orchestrator gives one
            os.makedirs(self.p("questions"), exist_ok=True)
            n = int(read(self.p(".qseq")).strip() or 0) + 1
            with open(self.p(".qseq"), "w") as f: f.write(f"{n}\n")
            q = f"q{n}"
            with open(self.p("questions", f"{q}.md"), "w") as f: f.write(f"- id: {q}\n- agent: {a}\n- asked: {t}\n- state: open\n- text: {x}\n")
            x = f"[{q}] {x}"
        with open(self.p("log.md"), "a") as f: f.write(f"{t} {a} {k}: {x}\n")
        c = self.p("agents", f"{a}.md"); card = {n: field(c, n) for n in FIELDS}
        if brief:
            card["brief"] = brief
            card["task"] = next((re.sub(r"^# *", "", l) for l in read(brief).splitlines() if l.startswith("#")), "")
            if not report:
                m = re.search(rf"[^ `'\"\n]*/out/{re.escape(a)}/[A-Za-z0-9._-]*\.md", read(brief)); card["report"] = m.group(0) if m else ""
        if report: card["report"] = report
        if machine: card["machine"] = machine
        if not card["machine"]: card["machine"] = host
        if check: card["check"] = check
        st = {"status": dict(state="working", blockers=""), "step": dict(state="working", step=x, blockers=""),
              "blocked": dict(state="blocked", blockers=x), "question": dict(state="question", blockers=x),
              "waiting": dict(state="waiting", step=f"waiting on {x}", blockers=""), "done": dict(state="done", step=x, blockers=""),
              "learned": dict(state=card["state"] or "working")}[k]
        card.update(st); card["updated"] = t
        write(c, f"# {a}\n" + "".join(f"- {n}: {card[n]}\n" for n in FIELDS))
        if k in ("question", "blocked"): self.notify()

    def direct1(self, a, x, t, answer=True, only_q=None):
        """Direct one agent: newest first in to/<agent>.md, a log line, and its open questions answered (a broadcast answers none)."""
        ids = []
        if answer:
            for q in self.question_files():
                if field(q, "agent") != a or field(q, "state") != "open": continue
                if only_q and os.path.basename(q)[:-3] != only_q: continue
                write(q, read(q).replace("- state: open\n", "- state: answered\n", 1) + f"- answered: {t}\n- answer: {x}\n")
                ids.append(os.path.basename(q)[:-3])
        if ids and not x.startswith("[q"): x = f"[{','.join(ids)}] {x}"
        f = self.p("to", f"{a}.md"); write(f, f"- {t} {x}\n" + read(f))
        with open(self.p("log.md"), "a") as lf: lf.write(f"{t} orchestrator direction: @{a} {x}\n")
        c = self.p("agents", f"{a}.md")
        if os.path.exists(c) and field(c, "state") == "question" and not any(q[1] == a for q in self.openq()):
            write(c, re.sub(r"^- blockers: .*$", "- blockers: ", read(c).replace("- state: question\n", "- state: working\n", 1), count=1, flags=re.M))

    # ---- reads
    def question_files(self):
        fs = glob.glob(self.p("questions", "q*.md"))
        return sorted((f for f in fs if re.fullmatch(r"q\d+\.md", os.path.basename(f))), key=lambda f: int(os.path.basename(f)[1:-3]))

    def openq(self):
        """[(id, agent, asked, text)] of open questions, oldest first."""
        return [(field(q, "id"), field(q, "agent"), field(q, "asked"), field(q, "text")) for q in self.question_files() if field(q, "state") == "open"]

    def gone(self, ck):
        """True when the card's process (its check field) ended."""
        if ck.startswith("pid "):
            try: os.kill(int(ck[4:]), 0); return False
            except (OSError, ValueError): return True
        if ck.startswith("herdr "):
            rc, o = run(["herdr", "agent", "get", ck[6:]])
            try: s = ((json.loads(o).get("result") or {}).get("agent") or {}).get("agent_status")
            except ValueError: s = None
            return not s or s == "done"
        if ck.startswith("pueue "):
            th, _, tid = ck[6:].partition(":")
            if th != self.localname() and th in [h[0] for h in self.hosts()]: return False   # another host's task: not judged here
            rc, o = run(["pueue", "status", "--json"])
            try: s = ((json.loads(o).get("tasks") or {}).get(tid) or {}).get("status")
            except ValueError: s = None
            s = s if isinstance(s, str) or s is None else next(iter(s), None)
            return s not in ("Running", "Queued")
        if ck.startswith("tab "):
            rc, o = run(["herdr", "tab", "list"])
            try: return not any(x.get("label") == ck[4:] for x in (json.loads(o).get("result") or {}).get("tabs", []))
            except ValueError: return True
        return False

    def cardrows(self):
        """[(group, line)] per card: 1 question, 2 blocked, 3 working, 4 waiting, 5 done."""
        n = int(time.time()); rows = []
        for c in sorted(glob.glob(self.p("agents", "*.md"))):
            a = os.path.basename(c)[:-3]; st = field(c, "state")
            age = int((n - epoch_of(field(c, "updated"))) / 60); flag = ""
            if st not in ("done", "waiting"):   # a parked agent (waiting) is never SILENT or GONE
                if age > int(os.environ.get("BOARD_SILENT_MIN") or 30): flag = f"SILENT {age}m"
                if self.master and self.gone(field(c, "check")): flag = (flag + ", " if flag else "") + "GONE (no done post)"
            b = field(c, "blockers")
            rows.append((GROUP.get(st, 3), f"{a:<24} {field(c, 'machine'):<8} {st:<9} {str(age) + 'm':<6} {field(c, 'step')[:40]:<40} "
                                          f"{b}{'; ' if b and flag else ''}{flag}"))
        return rows

    # ---- commands
    def cmd_post(self, a="", k="", *words):
        if not a or not k: raise Die("usage: post <agent> <kind> <text>")
        okname(a)
        if k not in KINDS: raise Die("kind must be one of: " + " ".join(KINDS))
        x = oneline(words); e = os.environ.get
        if self.master:
            os.makedirs(self.p("agents"), exist_ok=True); os.makedirs(self.p("to"), exist_ok=True)
            with Locked(self.p(".lock")):
                self.apply(now(), a, k, x, e("BOARD_BRIEF", ""), e("BOARD_REPORT", ""), e("BOARD_MACHINE", ""), e("BOARD_CHECK", ""), self.localname())
        else:
            os.makedirs(self.out, exist_ok=True)
            with Locked(os.path.join(self.out, ".lock")), open(os.path.join(self.out, "outbox.log"), "a") as f: f.write(f"{now()} {a} {k}: {x}\n")

    def need_master(self, what):
        if not self.master: raise Die(f"{what} runs on the coordinating machine only")

    def cmd_direct(self, a="", *words):
        self.need_master("direct"); x = oneline(words)
        if not x: raise Die("usage: direct <agent|all> <text>")
        os.makedirs(self.p("to"), exist_ok=True)
        if a != "all":
            okname(a)
            with Locked(self.p(".lock")): self.direct1(a, x, now())
            return
        t = now(); n = 0
        for c in sorted(glob.glob(self.p("agents", "*.md"))):
            if field(c, "state") == "done": continue
            with Locked(self.p(".lock")): self.direct1(os.path.basename(c)[:-3], f"(to all) {x}", t, answer=False)
            n += 1
        print(f'broadcast to {n} open card(s). If it is a standing instruction, also: orch remember --decision "{x}"')

    def cmd_answer(self, q="", *words):
        self.need_master("answer"); x = oneline(words)
        if not x: raise Die("usage: answer <question id> <text>")
        if not os.path.exists(self.p("questions", f"{q}.md")): raise Die(f"no question {q}")
        with Locked(self.p(".lock")): self.direct1(field(self.p("questions", f"{q}.md"), "agent"), x, now(), only_q=q)

    def cmd_read(self, a="", *_):
        okname(a); t = read(self.p("to", f"{a}.md"))
        sys.stdout.write(t if t else f"(no directions for {a})\n")

    def cmd_questions(self, *_):
        for q in self.openq(): print("\t".join(q))

    def cmd_summary(self, *_):
        n = int(time.time())
        print(f"{'AGENT':<24} {'MACHINE':<8} {'STATE':<9} {'AGE':<6} {'STEP':<40} BLOCKERS / FLAGS")
        qs = self.openq()
        if qs:
            print("== needs answer (board.sh answer <id> <text>)")
            for i, ag, at, tx in qs: print(f"  ? {i:<5} {ag:<20} {int((n - epoch_of(at)) / 60):>3}m  {tx}")
        last = None
        for g, line in sorted(self.cardrows(), key=lambda r: r[0]):
            if g != last: print("== " + {1: "question", 2: "blocked", 3: "working", 4: "waiting", 5: "done"}[g]); last = g
            print(line)

    def cmd_digest(self, *args):
        since = args[1] if len(args) > 1 and args[0] == "--since" else "1h"
        mult = {"m": 60, "h": 3600, "d": 86400}
        s = int(since[:-1]) * mult[since[-1]] if since[-1:] in mult else int(since)
        cs = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - s))
        print(f"board digest since {cs}")
        L = {k: [] for k in ("landed", "started", "done", "asked", "answered", "failed", "learned", "broadcast")}; seen = set()
        for l in read(self.p("log.md")).splitlines():
            f = l.split(" ", 4)
            if len(f) < 4 or f"{f[0]} {f[1]}" < cs: continue
            who, k, txt = f[2], f[3].rstrip(":"), f[4] if len(f) > 4 else ""
            line = f"  {f[1]} {who}: {txt[:140]}"
            if k == "done" and "DONE" in txt and "land" in txt: L["landed"].append(line)
            elif k == "done" and re.search("BLOCKED|FAIL", txt): L["failed"].append(line)
            elif k == "done": L["done"].append(line)
            elif k == "blocked": L["failed"].append(line)
            elif k == "question": L["asked"].append(line)
            elif who == "orchestrator" and k == "direction" and re.search(r"\[q[0-9]+\]", txt): L["answered"].append(line)
            elif who == "orchestrator" and k == "direction" and "(to all)" in txt:
                bk = re.sub(r"^@\S+ ", "", txt)
                if bk not in seen: seen.add(bk); L["broadcast"].append(line)
            elif k == "status" and txt.startswith("launched"): L["started"].append(line)
            elif k == "learned": L["learned"].append(line)
            elif "landed" in txt: L["landed"].append(line)
        for k, v in L.items(): print(f"{k}: {len(v)}" + "".join("\n" + x for x in v))

    def cmd_close(self, a="", *_):
        self.need_master("close"); okname(a)
        if not os.path.exists(self.p("agents", f"{a}.md")): raise Die(f"no card for {a}")
        for d in ("agents", "to"): os.makedirs(self.p(d, "archive"), exist_ok=True)
        s = time.strftime("%Y%m%d-%H%M")
        with Locked(self.p(".lock")):
            os.replace(self.p("agents", f"{a}.md"), self.p("agents", "archive", f"{a}.{s}.md"))
            if os.path.exists(self.p("to", f"{a}.md")): os.replace(self.p("to", f"{a}.md"), self.p("to", "archive", f"{a}.{s}.md"))
            for q in self.question_files():   # its open questions go with it (they would stay "needs answer" for nobody)
                t = read(q)
                if f"- agent: {a}\n" in t and "- state: open\n" in t: write(q, t.replace("- state: open\n", f"- state: withdrawn (card closed {s})\n", 1))

    def cmd_rule(self, a="", h="", *_):
        okname(a); p = self.script
        if h:
            r = next((x[2] for x in self.hosts() if x[0] == h), "")
            if r: p = f"{r}/bin/board.sh"
        # No quote characters: launchers embed this in quoted prompts and remote command lines.
        print(f"Message board: run {p} post {a} status <one line> when you start; {p} post {a} waiting <job ids> when you park on jobs; "
              f"{p} post {a} blocked <what blocks you> or {p} post {a} question <your question> when stuck; {p} post {a} learned <a fact "
              f"every agent should know, with its source> when you find one; {p} post {a} done <your VERDICT line> at the end. Post only "
              f"these state changes: your steps go in your report. Before each step run {p} read {a} and follow any new direction.")

    def merge(self, lines, host):
        """Apply outbox lines (<date> <time> <agent> <kind>: <text>) in order; bad lines are skipped."""
        for l in lines:
            f = l.split(" ", 3)
            if len(f) < 4: continue
            d, tm, a, r = f; k = r.split(":", 1)[0]; x = r.split(": ", 1)[1] if ": " in r else r
            if k not in KINDS or not re.fullmatch(r"[A-Za-z0-9._-]+", a): continue
            self.apply(f"{d} {tm}", a, k, x, host=host)

    def cmd_sync(self, *_):
        self.need_master("sync")
        try: sl = Locked(self.p(".sync.lock"), wait=False).__enter__()   # one sync at a time
        except OSError: return
        try: self.sync()
        finally: sl.__exit__()

    def sync(self):
        ob = os.path.join(self.out, "outbox.log")
        if os.path.getsize(ob) if os.path.exists(ob) else 0:   # posts made here before this folder became the coordinating board
            mine = os.path.join(self.out, f"local.{os.getpid()}"); os.replace(ob, mine)
            with Locked(self.p(".lock")): self.merge(read(mine).splitlines(), self.localname())
            os.remove(mine)
        here = os.path.dirname(os.path.abspath(self.script))
        for h, ssh, root in (x[:3] for x in self.hosts()):
            # Pull: under the host's outbox lock, rename outbox.log to a numbered sending file, then merge every sending file in
            # name order. A merged name is recorded before the file is deleted there, so nothing is merged twice or lost.
            rc, files = run([*SSH, ssh, f"o={root}/board-outbox; mkdir -p $o; cd $o && {{ command -v flock >/dev/null && flock .lock sh -c "
                             f"'[ -s outbox.log ] && mv outbox.log sending.$(date +%s%N); true' || {{ [ -s outbox.log ] && mv outbox.log "
                             f"sending.$(date +%s)$$; true; }}; }} && ls sending.* 2>/dev/null | sort"])
            if rc: continue
            done = set(read(self.p(f".merged-{h}")).split())
            for f in files.split():
                if f not in done:
                    rc, body = run([*SSH, ssh, f"cat {root}/board-outbox/{f}"])
                    if rc: continue
                    with Locked(self.p(".lock")):
                        self.merge(body.splitlines(), h)
                        with open(self.p(f".merged-{h}"), "a") as mf: mf.write(f + "\n")
                run([*SSH, ssh, f"rm -f {root}/board-outbox/{f}"])
            # Push the read-only copy and the board (board.sh and this file); lock, merge records, hosts and notify stay here.
            run([*SSH, ssh, f"mkdir -p {root}/bin {root}/board"])
            ex = [x for e in (".lock", ".sync.lock", ".merged-*", ".master", ".notify", ".hosts", ".qseq", "*.tmp.*") for x in ("--exclude", e)]
            rc, _ = run(["rsync", "-a", "--delete", "--chmod=Fa-w", *ex, "-e", RSYNC_SSH, self.bd + "/", f"{ssh}:{root}/board/"], timeout=300)
            if rc == 0: run([*SSH, ssh, f"find {root}/board -type f -perm -u+w -exec chmod a-w {{}} +"])
            run(["rsync", "-a", "-e", RSYNC_SSH, self.script, f"{ssh}:{root}/bin/board.sh"])
            run(["rsync", "-a", "-e", RSYNC_SSH, os.path.abspath(__file__), f"{ssh}:{root}/bin/msgboard.py"])


def main(argv):
    script, cmd, args = argv[1], (argv[2] if len(argv) > 2 else ""), argv[3:]
    b = Board(script)
    fn = getattr(b, "cmd_" + cmd, None) if re.fullmatch(r"[a-z]+", cmd or "") else None
    if not fn: print(USAGE); return 2
    try: fn(*args)
    except Die as e: print(f"board.sh: {e}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__": sys.exit(main(sys.argv))
