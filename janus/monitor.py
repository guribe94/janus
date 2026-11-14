"""janus monitor: a read-only terminal dashboard (stdlib curses) over the machines, the work and the board.

Two halves that only meet in Store/derive: COLLECTION (a background thread reads beads, the hosts' probes, herdr, local reports and
the board's files; each source keeps its last successful data and the age and error of its latest failure) and RENDERING
(derive turns the sources into rows; render turns rows into lines; the curses loop paints them and reads keys, and never waits
for collection). Read-only by construction: every collection builds a fresh Cfg whose Run is ro, never asks who the orchestrator is,
calls bd with --readonly, reads board files directly (never board.sh: read/summary would touch .seen files or the notify hook) and
calls none of check, status, sync, handoff, writer lock, ack or wake code. Nothing here writes a project file. The one outside
effect is the probe's own scratch under /tmp on each host, which `janus capacity` already makes. Standard library only."""
import contextlib, dataclasses, datetime, json, os, queue, re, signal, sys, threading, time
from pathlib import Path
from janus.backlog import LOG_LABELS, LOG_RX
from janus.beads import unit_record
from janus.board import board_dir
from janus.context import CONF, Cfg, Err, Run, sh
from janus.actions.launch import model_tool
from janus.platform import herdr, ps_table
from janus.probe import probe
from janus.units import api_out, report_path, unit_state, verdict as unit_verdict, waiting_line_jobs
try: import curses
except ImportError: curses = None   # janus itself must still import where the curses module is missing; monitor then says so

TABS = ("Active", "Backlog", "Finished", "Board")
MIN_INTERVAL = 1.0      # seconds: every refresh is an ssh round trip per host
FINISHED_KEEP = 40      # closed units shown (newest first)
BOARD_TAIL = 400        # board log lines read
LOG_BYTES = 256 * 1024  # of the log's tail

# ---------------------------------------------------------------- text hygiene: nothing outside is shown raw

_SECRET = re.compile(r"""(?ix)\b(bearer\s+)\S+
    | ([\w.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|credential|authorization)[\w.-]*)(["']?\s*[=:]\s*["']?)(?:bearer\s+)?(?!\*\*\*)[^\s"',;]+
    | \b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,} | \bgh[pousr]_[A-Za-z0-9]{16,} | \bAKIA[0-9A-Z]{12,} | \bxox[abprs]-[A-Za-z0-9-]{8,}""")

def scrub(s):
    """The text with anything shaped like a credential (key=value, bearer, well-known token forms) masked."""
    return _SECRET.sub(lambda m: m.group(1) + "***" if m.group(1) else m.group(2) + m.group(3) + "***" if m.group(2) else "***", s)

def clean(x, n=None):
    """One line of printable text, scrubbed, at most n characters; terminal escapes from outside never reach the screen."""
    s = scrub(" ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", "" if x is None else str(x)).split()))
    return s[:n - 1] + "~" if n and len(s) > n else s

def fmt_dur(s):
    if s is None: return "?"
    s = max(0, int(s))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m" if s < 86400 else f"{s // 86400}d{s % 86400 // 3600:02d}h"

def epoch(x):
    """An epoch number, or an ISO timestamp as one; None when it is neither (unknown stays unknown)."""
    if isinstance(x, (int, float)) and not isinstance(x, bool): return float(x)
    try: return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError): return None

def num(x):
    try: return int(float(x))
    except (ValueError, TypeError): return None

def err_text(e): return clean(e if str(e) else type(e).__name__, 160)

# ---------------------------------------------------------------- collection

@dataclasses.dataclass(frozen=True)
class Source:
    """What is known of one source: its last successful data (never mutated afterwards) and when; its latest failure, if it failed since."""
    data: object = None
    ok_at: float | None = None
    err: str | None = None
    err_at: float | None = None

class Store:
    """The sources the collection thread fills and the screen reads (a lock; Source values are immutable)."""
    def __init__(self):
        self.lock = threading.Lock(); self.src = {}; self.version = 0
        self.running = False; self.started = self.finished = None; self.runs = 0
    def ok(self, name, data, at):
        with self.lock: self.src[name] = Source(data, at); self.version += 1
    def fail(self, name, err, at):
        with self.lock: self.src[name] = dataclasses.replace(self.src.get(name, Source()), err=err, err_at=at); self.version += 1
    def begin(self, at):
        with self.lock: self.running, self.started = True, at; self.version += 1
    def end(self, at):
        with self.lock: self.running, self.finished = False, at; self.runs += 1; self.version += 1
    def view(self):
        with self.lock: return dict(self.src), self.running, self.finished, self.version

def fresh_cfg(root):
    """A new Cfg for one collection (nothing carried between refreshes), read-only whoever the caller is. Cfg would create a
    missing state folder: the monitor refuses to instead."""
    if not (Path(root) / CONF).is_file(): raise Err(f"no {CONF} in {root}: run janus init (the monitor never creates it)")
    return Cfg(Path(root), Run(ro=True))

def bd_json(cfg, *args, timeout=20):
    """bd with --readonly (bd refuses any write); the issues of its JSON."""
    rc, o, e = sh(["bd", "--readonly", *args, "--json"], cwd=cfg.beads, timeout=timeout)
    if rc: raise Err(f"bd {args[0]}: {clean(e or o, 120) or 'rc ' + str(rc)}")
    try: r = json.loads(o) if o.strip() else []
    except ValueError: raise Err(f"bd {args[0]}: bad json")
    r = r.get("issues", r) if isinstance(r, dict) else r
    if not isinstance(r, list): raise Err(f"bd {args[0]}: unexpected answer")
    return [i for i in r if isinstance(i, dict)]

def unit_of(i):
    t = str(i.get("title", ""))
    return {**unit_record(i), "id": i.get("id"), "name": clean(t[5:]), "status": i.get("status"), "closed_at": i.get("closed_at"), "created_at": i.get("created_at")} \
        if t.startswith("unit:") and t[5:] else None

def collect_units(cfg):
    return [u for u in map(unit_of, bd_json(cfg, "list", "--label", "unit", "--status", "open,in_progress", "-n", "0")) if u]

def collect_closed(cfg):
    us = [u for u in map(unit_of, bd_json(cfg, "list", "--label", "unit", "--status", "closed", "-n", "0")) if u]
    best = {}
    for u in sorted(us, key=lambda u: epoch(u.get("closed_at")) or 0, reverse=True): best.setdefault(u["name"], u)   # a name relaunched: its latest closing
    return list(best.values())[:FINISHED_KEEP]

def collect_backlog(cfg):
    keep = lambda i: {k: i.get(k) for k in ("id", "title", "labels", "status", "external_ref", "created_at", "description", "priority")}
    return {"ready": [keep(i) for i in bd_json(cfg, "ready", "-n", "0")],
            "items": [keep(i) for i in bd_json(cfg, "list", "--label", "backlog", "--status", "open,in_progress", "-n", "0")]}

def collect_reports(cfg, units):
    """{unit: what its local report says}: verdict (report.json first, then the prose; PENDING/WAITING are none), the jobs it waits
    on, the api run's stop reason. A unit whose report cannot be judged is marked, never read as 'no verdict'."""
    out = {}
    for u in units:
        try:
            p = report_path(cfg, u)
            out[u["name"]] = {"verdict": clean(unit_verdict(cfg, u)) or None, "waiting": [clean(j) for j in waiting_line_jobs(cfg, u)],
                              "path": clean(p), "limit": clean(api_out(cfg, u).get("subtype")) or None}
        except Exception as e: out[u["name"]] = {"err": err_text(e)}
    return out

def collect_host(cfg, name):
    p = probe(name, cfg.hosts[name], ro=True, cfg=cfg)
    if not p.get("ok"): raise Err(p.get("err") or "unreachable")
    return p

def collect_herdr():
    r = herdr("agent", "list")
    if r is None: raise Err("herdr did not answer")
    return {(a.get("name") or a.get("pane_id")): a for a in r.get("agents", []) if isinstance(a, dict)}

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d) (\S+) (?:direction: @(\S+) |(\w+)(?: -> (\S+))?: )(.*)$")
QREF = re.compile(r"^\[(q\d+)(?:,[^\]]*)?\]\s*")
POST_KINDS = {"question": "QUESTION", "blocked": "BLOCKER", "direction": "ANSWER"}   # the rest of the post kinds are updates

def read_fields(p):
    try: text = Path(p).read_text(errors="replace")
    except OSError: return None
    return {m.group(1): m.group(2) for m in re.finditer(r"^- (\w+): ?(.*)$", text, re.M)}

def collect_board(cfg):
    """The board's messages (its log's tail: key = byte offset, stable while the log only grows) and its questions' states, read from
    the files; nothing is run, nothing marked read."""
    bd = board_dir(cfg)
    if not bd.is_dir(): raise Err(f"no board folder {bd.name}")
    msgs, off = [], 0
    try:
        with open(bd / "log.md", "rb") as f:
            size = f.seek(0, 2); off = max(0, size - LOG_BYTES); f.seek(off); chunk = f.read()
    except FileNotFoundError: chunk = b""
    except OSError as e: raise Err(f"board log: {err_text(e)}")
    lines = chunk.split(b"\n"); pos = off
    if off: pos += len(lines.pop(0)) + 1   # a partial first line
    for raw in lines:
        at, pos = pos, pos + len(raw) + 1
        m = LINE.match(raw.decode(errors="replace").strip())
        if not m: continue
        t, who, target, kind, to, text = m.groups()
        kind = "direction" if target else kind
        q = QREF.match(text); msgs.append({"key": f"L{at}", "t": t, "who": clean(who, 40), "to": clean(target or to, 40) or None, "raw": kind, "kind": POST_KINDS.get(kind, "UPDATE"),
                                           "q": q.group(1) if q else None, "text": clean(text, 1000)})
    qs = {}
    for f in sorted((bd / "questions").glob("q[0-9]*.md")) if (bd / "questions").is_dir() else []:
        d = read_fields(f)
        if d: qs[f.stem] = {k: clean(d.get(k), 1000) for k in ("agent", "asked", "state", "text", "answered", "answer")}
    cards = {}   # each agent's own card: its latest step and when it last posted (the "latest progress" of a task)
    for f in sorted((bd / "agents").glob("*.md")) if (bd / "agents").is_dir() else []:
        d = read_fields(f)
        if d: cards[f.stem] = {k: clean(d.get(k), 200) for k in ("state", "step", "updated", "machine", "tool", "model")}
    return {"msgs": msgs[-BOARD_TAIL:], "questions": qs, "cards": cards}

class Collector:
    """One background thread: a collection at start, then one every interval after the last finished, and one at once on request().
    Never two at a time. Hosts are probed in parallel, each a source of its own, so a slow or dead host delays only itself."""
    def __init__(self, root, store, interval, clock=time.time):
        self.root, self.store, self.interval, self.clock = Path(root), store, interval, clock
        self.wake, self.stopping = threading.Event(), threading.Event(); self.thread = None
    def start(self):
        self.thread = threading.Thread(target=self.loop, name="janus-monitor-collect", daemon=True); self.thread.start()
    def request(self): self.wake.set()
    def stop(self):
        self.stopping.set(); self.wake.set()
        if self.thread: self.thread.join(1)
    def loop(self):
        due = 0
        while not self.stopping.is_set():
            if not self.wake.wait(max(0.0, min(0.2, due - self.clock()))) and self.clock() < due: continue
            if self.stopping.is_set(): break
            self.wake.clear()
            try: self.collect_once()
            except Exception as e: self.store.fail("collection", err_text(e), self.clock())   # a bug here must not end the monitor
            due = self.clock() + self.interval
    def step(self, name, fn, *a):
        """Run one source: its data on success (None on failure, the source then keeps its last data and says why)."""
        try: data = fn(*a)
        except Exception as e: self.store.fail(name, err_text(e), self.clock()); return None
        self.store.ok(name, data, self.clock()); return data
    def collect_once(self):
        st = self.store; st.begin(self.clock())
        try:
            try: cfg = fresh_cfg(self.root)
            except Exception as e: st.fail("config", err_text(e), self.clock()); return
            st.ok("config", {"name": clean(cfg.name), "hosts": list(cfg.hosts), "tools": {m: clean(model_tool(c)) for m, c in cfg.models.items()}}, self.clock())
            done = queue.Queue()
            def host(n):
                try: p = collect_host(cfg, n); done.put((n, p, None, self.clock()))
                except Exception as e: done.put((n, None, err_text(e), self.clock()))
            for n in cfg.hosts: threading.Thread(target=host, args=(n,), daemon=True).start()
            prev = st.view()[0]
            units = self.step("units", collect_units, cfg)
            closed = self.step("closed", collect_closed, cfg)
            if units is not None: self.step("reports-open", collect_reports, cfg, units)
            if closed is not None: self.step("reports-closed", collect_reports, cfg, closed)
            self.step("backlog", collect_backlog, cfg)
            self.step("board", collect_board, cfg)
            known = units if units is not None else (prev.get("units", Source()).data or [])
            if any(u.get("via") == "herdr" for u in known): self.step("herdr", collect_herdr)
            for _ in cfg.hosts:
                while True:
                    if self.stopping.is_set(): return
                    try: n, p, e, at = done.get(timeout=0.1); break
                    except queue.Empty: continue
                if e is None: st.ok(f"host:{n}", p, at)
                else: st.fail(f"host:{n}", e, at)
        finally: st.end(self.clock())

def kill_children():
    """Quit with a probe still waiting on a slow ssh: end every process this one started (ssh, sh, bd), so nothing lingers."""
    me = os.getpid(); t = ps_table(); kids, grow = set(), True
    while grow:
        grow = False
        for pid, row in t.items():
            if pid != me and pid not in kids and (row[0] == me or row[0] in kids): kids.add(pid); grow = True
    for pid in kids:
        with contextlib.suppress(OSError): os.kill(pid, signal.SIGTERM)
    return len(kids)

@contextlib.contextmanager
def detached_stdin():
    """The monitor reads keys from the terminal: a child (ssh forwards its stdin) must never read them. Every command this process
    runs gets a closed pipe for stdin instead of the terminal (this process only; restored on exit)."""
    import janus.context as C, janus.platform as P
    real = P.run
    def run(cmd, cwd=None, inp=None, timeout=25, env=None): return real(cmd, cwd=cwd, inp="" if inp is None else inp, timeout=timeout, env=env)
    saved = (P.run, C.run); P.run = C.run = run
    try: yield
    finally: P.run, C.run = saved

# ---------------------------------------------------------------- rows: what the sources mean

@dataclasses.dataclass
class Row:
    key: str
    cells: tuple
    style: str = ""
    detail: list = dataclasses.field(default_factory=list)
    stale: bool = False

@dataclasses.dataclass
class Model:
    project: str = "?"
    machines: list = dataclasses.field(default_factory=list)   # Row: host, state, run/queued, agents, load, ram, note
    rows: dict = dataclasses.field(default_factory=lambda: {t: [] for t in TABS})
    counts: dict = dataclasses.field(default_factory=dict)      # tab -> number, None while unknown
    empty: dict = dataclasses.field(default_factory=dict)       # tab -> why there is nothing to list
    banners: dict = dataclasses.field(default_factory=dict)     # tab -> [source problems]
    problems: int = 0

def source_state(s, now, limit):
    """none (not asked yet) | failed (never succeeded) | ok | stale (failed since its last success, or older than limit)."""
    if s.ok_at is None: return "failed" if s.err else "none"
    return "stale" if s.err or now - s.ok_at > limit else "ok"

def source_note(label, s, now, limit):
    k = source_state(s, now, limit)
    if k == "ok": return None
    if k == "none": return f"{label}: loading"
    if k == "failed": return f"{label}: UNAVAILABLE, no data yet ({s.err})"
    return f"{label}: STALE, last good data {fmt_dur(now - s.ok_at)} old" + (f" ({s.err})" if s.err else " (no answer yet)")

WORK_COLS = (("NAME", 24, 9, True), ("STATE", 22, 8, False), ("HOST", 10, 4, False), ("MODEL", 14, 3, False), ("ELAPSED", 8, 6, False))
BOARD_COLS = (("NEW", 3, 7, False), ("TIME", 16, 5, False), ("KIND", 9, 8, False), ("AGENT", 16, 6, False), ("MESSAGE", 40, 9, True))
HOST_COLS = (("HOST", 12, 9, False), ("STATE", 11, 8, False), ("RUN/QUEUED", 10, 7, False), ("AGENTS", 6, 6, False), ("LOAD", 6, 4, False), ("RAM FREE", 8, 5, False), ("NOTE", 30, 3, True))
NOT_READ = {"err": "report not read yet"}   # a unit's report before its first read: unknown, never 'no verdict'
PRIO = {"blocked": 0, "failed": 1, "working": 2, "idle": 3, "queued": 4, "parked": 5, "starting": 6, "closing": 6, "unknown": 7}

def state_style(st):
    w = st.split(":")[0]
    return "bad" if w in ("failed", "blocked") or st == "ended:BLOCKED" or st == "ended:FAILED" else "ok" if w in ("working", "finished") else "dim" if w == "closed" else "warn"

def classify(u, ps, agents, rep):
    """(tab, state, note) of one unit. Queued, parked, failed, closed and finished stay distinct, and a process that ended is never
    taken as success: only a report that ends VERDICT: DONE says finished; no verdict is 'ended:no-verdict'."""
    via, name = u.get("via"), u.get("name")
    v = (rep or {}).get("verdict"); vw = (v or "").replace("VERDICT:", "").strip().split(" ")[0].upper()
    if u.get("status") == "closed": return "Finished", "closed" + (f":{vw}" if vw else ""), "closed" + ("; no verdict" if not vw else "")
    if via == "failed": return "Finished", "failed", "launch failed (no agent was started)"
    if via == "starting": return "Active", "starting", "launch has not finished"
    if u.get("closing"): return "Active", "closing", "a close is in progress"
    try: st = "unknown" if via == "herdr" and agents is None else unit_state(u, ps, agents or {})
    except Exception: st = "unknown"
    ended = st in ("done", "failed", "gone")
    pk = (u.get("parked") or {}).get("jobs") or ([] if v else ((rep or {}).get("waiting") or []) if ended or st == "idle" or via == "tmux" else [])
    if pk: return "Active", "parked", "waiting for " + ", ".join(map(str, pk[:4]))
    if st in ("working", "queued", "blocked", "idle", "unknown"): return "Active", st, "" if st != "unknown" else "the host or herdr cannot say now"
    if not ended: return "Active", st, f"scheduler status {st}"
    bad = st == "failed"
    if rep and rep.get("err"): return "Finished", "failed" if bad else "ended:unverified", f"process {st}; report unreadable ({rep['err']})"
    if bad: return "Finished", "failed", f"process failed" + (f"; the report says {v}, but a failed process is not success" if vw == "DONE" else "")
    if not vw: return "Finished", "ended:no-verdict", f"process {st}; the report states no verdict (a process ending is not success)"
    return "Finished", "finished" if vw == "DONE" else f"ended:{vw[:9]}", f"process {st}; report: {v}"

def unit_row(u, tab, state, note, now, stale, rep, tool=None, card=None):
    end = epoch(u.get("closed_at") or u.get("collected_at")) if tab == "Finished" else now
    t0 = epoch(u.get("started"))
    el = fmt_dur(end - t0) if end is not None and t0 is not None else "?"
    d = [f"Name:      {u.get('name')}", f"State:     {state}" + ("  (data is stale)" if stale else ""), f"Meaning:   {note}" if note else "",
         f"Host:      {clean(u.get('host')) or '?'}", f"Tool:      {tool or (card or {}).get('tool') or '?'}", f"Model:     {clean(u.get('model')) or '?'}", f"Via:       {clean(u.get('via')) or '?'}" + (f"  task {clean(u.get('task'))}" if u.get("task") is not None else ""),
         f"Elapsed:   {el}" + ("  (start to close)" if tab == "Finished" and el != "?" else ""),
         f"Started:   {time.strftime('%Y-%m-%d %H:%M', time.localtime(t0)) if t0 else '?'}", f"Worktree:  {clean(u.get('cwd')) or '?'}", f"Brief:     {clean(u.get('brief')) or '?'}",
         f"Report:    {(rep or {}).get('path') or '?'}", f"Verdict:   {(rep or {}).get('verdict') or ('unknown (report unreadable)' if (rep or {}).get('err') else 'none')}"]
    if card and (card.get("step") or card.get("updated")): d.append(f"Progress:  {card.get('step') or '-'}  (posted {card.get('updated') or '?'})")
    rp = (rep or {}).get("path")
    if rp and rp != "?": d.append(f"Log dir:   {os.path.dirname(rp)}" + (f"  (task log: janus queue --log {clean(u.get('host'))}:{clean(u.get('task'))})" if u.get("via") == "pueue" and u.get("task") is not None else ""))
    if (rep or {}).get("waiting"): d.append("Waiting:   " + ", ".join(rep["waiting"]))
    if (u.get("parked") or {}).get("jobs"): d.append("Parked on: " + ", ".join(map(clean, u["parked"]["jobs"])))
    if (rep or {}).get("limit"): d.append(f"Stopped:   {rep['limit']}")
    if u.get("item"): d.append(f"Item:      {clean(u['item'])}")
    return Row(u["name"], (u["name"], state + (" STALE" if stale else ""), clean(u.get("host")) or "?", clean(u.get("model")) or "?", el),
               "warn" if stale and state_style(state) in ("ok", "dim") else state_style(state), [x for x in d if x], stale)

def backlog_rows(data, units, now, stale):
    ready = {i.get("id") for i in data.get("ready", [])}; covered = {u.get("item") for u in units or [] if u.get("item")}; rows = []
    skip = {"unit", "instruction", "intent"}
    for i in data.get("items", []):
        labels = [clean(l) for l in i.get("labels") or []]
        if skip & set(labels) or labels and set(labels) & LOG_LABELS: continue
        ref = str(i.get("external_ref") or "")[8:]
        actor = next((l[6:] for l in labels if l.startswith("actor:")), "agent"); hold = next((l[5:] for l in labels if l.startswith("hold:")), None)
        state = "launched" if ref in covered else "held" if hold else "owner" if actor != "agent" else "ready" if i.get("id") in ready else "waiting"
        t0 = epoch(i.get("created_at")); title = clean(i.get("title")) or ref or str(i.get("id"))
        d = [f"Item:      {title}", f"State:     {state}", f"Id:        {clean(i.get('id'))}", f"Ref:       {ref or '-'}", f"Actor:     {actor}", f"Labels:    {', '.join(labels) or '-'}",
             f"Age:       {fmt_dur(now - t0) if t0 else '?'}", f"Meaning:   {'a unit works on it' if state == 'launched' else 'not blocked by beads: janus fill may launch it' if state == 'ready' else 'held on purpose: ' + hold if hold else 'someone else does it' if state == 'owner' else 'open, but beads does not list it ready (blocked or deferred)'}",
             "", clean(i.get("description"), 600)]
        rows.append(Row(f"i:{i.get('id')}", (title, state + (" STALE" if stale else ""), "-", "-", fmt_dur(now - t0) if t0 else "?"),
                        "warn" if stale else {"ready": "ok", "held": "dim", "owner": "dim", "launched": "ok"}.get(state, ""), d, stale))
    ids = {i.get("id") for i in data.get("items", [])}
    for i in data.get("ready", []):   # ready work that is not a checklist item: it is queued work too
        labels = set(i.get("labels") or []); title = str(i.get("title", ""))
        if i.get("id") in ids or labels & ({"unit", "instruction", "backlog", "intent"} | LOG_LABELS) or any(str(l).startswith("actor:") and l != "actor:agent" for l in labels): continue
        if title.startswith(("unit:", "janus:", "instr:", "intent:")) or LOG_RX.match(title): continue
        t0 = epoch(i.get("created_at"))
        rows.append(Row(f"i:{i.get('id')}", (clean(title) or str(i.get("id")), "ready" + (" STALE" if stale else ""), "-", "-", fmt_dur(now - t0) if t0 else "?"), "warn" if stale else "ok",
                        [f"Item:      {clean(title)}", "State:     ready", f"Id:        {clean(i.get('id'))}", "Meaning:   ready in beads; no unit works on it", "", clean(i.get("description"), 600)], stale))
    return sorted(rows, key=lambda r: (r.cells[1].split(" ")[0] != "ready", r.cells[0].lower()))

def message_rows(data, stale):
    msgs, qs = data["msgs"], data["questions"]; rows = []; shown = {m["q"] for m in msgs if m["q"]}
    for m in msgs:
        q = qs.get(m["q"]) if m["q"] else None; qstate = q["state"] if q else None
        label = m["kind"] if m["raw"] != "direction" or m["q"] else "DIRECTION"
        style = {"QUESTION": "warn" if qstate in (None, "open") else "dim", "BLOCKER": "bad", "ANSWER": "ok", "DIRECTION": "ok"}.get(label, "")
        d = [f"Time:      {m['t']}", f"Kind:      {label}" + (f"  ({qstate})" if qstate else ""), f"From:      {m['who']}" + (f"  to {m['to']}" if m["to"] else ""), "", m["text"]]
        if q and q.get("answer"): d += ["", f"Answer ({q.get('answered')}): {q['answer']}"]
        rows.append(Row(m["key"], ("", m["t"], label + ("*" if qstate == "open" else ""), m["who"], m["text"]), style, d, stale))
    rows.reverse()   # newest first
    for qid, q in qs.items():   # an open question older than the log window is still open
        if qid not in shown and q.get("state") == "open":
            rows.append(Row(f"Q{qid}", ("", q["asked"], "QUESTION*", q["agent"], f"[{qid}] {q['text']}"), "warn", [f"Time:      {q['asked']}", "Kind:      QUESTION  (open)", f"From:      {q['agent']}", "", q["text"]], stale))
    return rows

PRIMARY = {"Active": ("units",), "Backlog": ("backlog",), "Finished": ("units", "closed"), "Board": ("board",)}   # what a tab's count needs
EMPTY = {"Active": "Nothing is running, queued or parked", "Backlog": "Nothing is waiting to be launched", "Finished": "Nothing has finished or closed", "Board": "The board has no posts"}

def derive(view, now, interval):
    """The Model: every source read as what it means now, with its age and error beside it. Unknown stays '?' or 'unavailable'."""
    limit = 3 * interval + 60.0; S = lambda n: view.get(n) or Source(); st = lambda n: source_state(S(n), now, limit); stale = lambda n: st(n) == "stale"
    m = Model(); cfg = S("config"); hosts = (cfg.data or {}).get("hosts", [])
    if cfg.data: m.project = cfg.data["name"]
    general = [f"{n}: {S(n).err}" for n in ("config", "collection") if S(n).err]
    ps = {n: S(f"host:{n}").data for n in hosts if S(f"host:{n}").data}
    units, closed, ag = S("units").data, S("closed").data, S("herdr").data
    tools = (cfg.data or {}).get("tools", {}); cards = (S("board").data or {}).get("cards", {})
    rows = {t: [] for t in TABS}; live = {}   # live: host -> [agents known to be alive, agents whose state is unknown]
    def host_stale(u): return stale("herdr") if u.get("via") == "herdr" else stale(f"host:{u.get('host')}")
    for u in units or []:
        rep = (S("reports-open").data or {}).get(u["name"], NOT_READ)
        tab, state, note = classify(u, ps, ag, rep)
        rows[tab].append(unit_row(u, tab, state, note, now, stale("units") or stale("reports-open") or host_stale(u), rep,
                                  tools.get(u.get("model")), cards.get(u["name"])))
        if tab == "Active" and u.get("host"):
            c = live.setdefault(u["host"], [0, 0])
            if state in ("working", "idle", "blocked"): c[0] += 1
            elif state == "unknown": c[1] += 1
    names = {u["name"] for u in units or []}
    for u in closed or []:
        if u["name"] in names: continue   # reopened or relaunched: the open unit is the truth
        rep = (S("reports-closed").data or {}).get(u["name"], NOT_READ); tab, state, note = classify(u, ps, None, rep)
        rows["Finished"].append(unit_row(u, tab, state, note, now, stale("closed") or stale("reports-closed"), rep))
    rows["Active"].sort(key=lambda r: (PRIO.get(r.cells[1].split(" ")[0], 8), r.cells[0].lower()))
    rows["Finished"].sort(key=lambda r: (r.cells[1].startswith("closed"), r.cells[0].lower()))   # finished and waiting to be collected first
    if S("backlog").data is not None: rows["Backlog"] = backlog_rows(S("backlog").data, units, now, stale("backlog"))
    if S("board").data is not None: rows["Board"] = message_rows(S("board").data, stale("board"))
    m.rows = rows
    herdr_used = any(u.get("via") == "herdr" for u in units or [])
    srcs = {"Active": [("units", "units"), ("reports-open", "local reports")] + ([("herdr", "herdr")] if herdr_used else []), "Backlog": [("backlog", "beads backlog")],
            "Finished": [("units", "units"), ("closed", "closed units"), ("reports-closed", "closed reports")], "Board": [("board", "board")]}
    for t in TABS:
        known = all(S(n).data is not None for n in PRIMARY[t]); m.counts[t] = len(rows[t]) if known else None
        m.banners[t] = general + [x for x in (source_note(lbl, S(n), now, limit) for n, lbl in srcs[t]) if x]
        if not rows[t]:
            bad = [x for x in m.banners[t] if "UNAVAILABLE" in x or "unreadable" in x]
            m.empty[t] = EMPTY[t] if known else (bad[0] if bad else "Loading...")
    for n in hosts: m.machines.append(machine_row(n, S(f"host:{n}"), now, limit, None if units is None else live.get(n, [0, 0])))
    m.problems = sum(1 for s in view.values() if source_state(s, now, limit) in ("failed", "stale"))
    return m

def machine_row(n, s, now, limit, agents):
    k = source_state(s, now, limit); p = s.data or {}
    run, qd = num(p.get("cap_rj")), None
    if p.get("pueue") is not None:
        tasks = list(((p.get("pueue") or {}).get("tasks") or {}).values()); w = [num(p.get("wait_jobs"))]
        if run is not None: run = max(run, sum(1 for x in tasks if x.get("group") in ("jobs", "place") and "Running" in str(x.get("status"))))   # work that runs counts even when it holds no reservation
        qd = None if None in w else sum(1 for x in tasks if x.get("group") in ("jobs", "place") and "Queued" in str(x.get("status"))) + sum(w)
    mb = num(p.get("avail_mb")); load = clean(p.get("load")) or "?"
    ram = f"{mb / 1024:.1f}G" if mb else "?"
    ag = "?" if agents is None else f"{agents[0]}" + ("+?" if agents[1] else "")
    age = f"last good {fmt_dur(now - s.ok_at)} ago" if s.ok_at is not None else ""
    pause = clean(p.get("cap_pause"), 60)
    state, style, note = {"none": ("checking", "dim", "first probe running"), "failed": ("UNREACHABLE", "bad", s.err or ""),
                          "stale": ("UNREACHABLE" if s.err else "STALE", "bad" if s.err else "warn", f"{s.err}; {age}" if s.err else f"no answer for a while; {age}"),
                          "ok": ("PAUSED", "warn", "admission paused: " + pause) if pause else ("ok", "ok", "")}[k]
    return Row(f"h:{n}", (n, state, f"{'?' if run is None else run}/{'?' if qd is None else qd}", ag, load, ram, clean(note, 120)), style, [], k in ("stale",))

# ---------------------------------------------------------------- screen: rows to lines

def layout(cols, width):
    """[(column index, width)] that fit width: the least important columns go first; the flex column takes what is left."""
    use = list(range(len(cols)))
    def total(): return sum(8 if cols[i][3] else cols[i][1] for i in use) + len(use) - 1   # the flex column counts at a minimum: it never costs the others their place
    while total() > width and len(use) > 1:
        drop = [i for i in use if not cols[i][3]]
        if not drop: break
        use.remove(min(drop, key=lambda i: cols[i][2]))
    ws = {i: cols[i][1] for i in use}
    fl_ = next((i for i in use if cols[i][3]), None)
    if fl_ is not None: ws[fl_] = max(4, width - (sum(ws.values()) - ws[fl_]) - (len(use) - 1))
    while sum(ws.values()) + len(use) - 1 > width and any(w > 3 for w in ws.values()):   # still too wide: trim the widest
        i = max(ws, key=ws.get); ws[i] -= 1
    return [(i, ws[i]) for i in use]

def fit(s, n): s = str(s); return (s[:n - 1] + "~" if len(s) > n else s).ljust(n) if n > 1 else s[:n]

def cells_line(cells, lay): return " ".join(fit(cells[i], w) for i, w in lay)

@dataclasses.dataclass
class UI:
    tab: int = 0
    sel: dict = dataclasses.field(default_factory=dict)      # tab -> key of the selected row (selection follows the row, not the position)
    idx: dict = dataclasses.field(default_factory=dict)      # tab -> its last index (where to land when the row is gone)
    top: dict = dataclasses.field(default_factory=dict)      # tab -> first visible index
    detail: bool = False
    dscroll: int = 0
    dkey: str | None = None                                   # the row whose details are open: details follow it, not the selection
    dcache: list = dataclasses.field(default_factory=list)
    seen: set | None = None                                   # board message keys the user has been shown; None until the first board read
    flash: str = ""
    filt: str = ""                                            # rows must hold this text (case-insensitive); / edits it
    typing: bool = False
    help: bool = False

    def observe(self, model):
        """Board baseline: what is there when the board first loads is not new; later posts are, until they are selected."""
        if self.seen is None and model.counts.get("Board") is not None: self.seen = {r.key for r in model.rows["Board"]}
    def is_new(self, key): return self.seen is not None and key not in self.seen
    def new_count(self, model): return sum(1 for r in model.rows["Board"] if self.is_new(r.key))

    def sync(self, model, bh):
        """Keep the selection on the same row across refreshes (by key); a vanished row lands on its old position. Scroll so it stays visible."""
        t = TABS[self.tab]; rows = model.rows[t]
        if not rows: self.sel.pop(t, None); self.idx[t] = 0; self.top[t] = 0; return
        keys = [r.key for r in rows]; old = self.idx.get(t, 0); top = self.top.get(t, 0)
        if self.sel.get(t) in keys:
            i = keys.index(self.sel[t]); top += i - old if old < len(keys) and i != old and top <= old else 0   # rows inserted above: stay where the eye is
        else: i = min(old, len(keys) - 1)
        self.sel[t] = keys[i]; self.idx[t] = i
        top = min(max(top, i - bh + 1), i) if bh > 0 else 0
        self.top[t] = max(0, min(top, len(keys) - bh)) if len(keys) > bh else 0
        if t == "Board" and self.seen is not None: self.seen.add(keys[i])

    def move(self, model, d):
        t = TABS[self.tab]; rows = model.rows[t]
        if rows: self.idx[t] = max(0, min(len(rows) - 1, self.idx.get(t, 0) + d)); self.sel[t] = rows[self.idx[t]].key

    def key(self, model, k, bh):
        """One key press: 'quit', 'refresh' or None. q always quits; Esc only closes details."""
        K = curses
        if self.typing:   # the filter line: printable keys add, Backspace removes, Enter keeps it, Esc clears it
            if k in (10, 13, K.KEY_ENTER): self.typing = False
            elif k == 27: self.typing = False; self.filt = ""
            elif k in (127, 8, K.KEY_BACKSPACE): self.filt = self.filt[:-1]
            elif 32 <= k < 127: self.filt += chr(k)
            return None
        if self.help: self.help = False; return None
        if k == ord("?"): self.help = True; return None
        if k == ord("/"): self.typing = True; self.detail = False; return None
        if k in (ord("q"), ord("Q")): return "quit"
        if k in (ord("r"), ord("R")): self.flash = "refresh requested"; return "refresh"
        if k in (9, K.KEY_RIGHT, K.KEY_BTAB, K.KEY_LEFT) or k in map(ord, "1234"):
            self.tab = (self.tab + (-1 if k in (K.KEY_BTAB, K.KEY_LEFT) else 1)) % len(TABS) if k not in map(ord, "1234") else k - ord("1")
            self.detail = False; return None
        if k in (10, 13, K.KEY_ENTER):
            self.detail = not self.detail and bool(model.rows[TABS[self.tab]]); self.dscroll = 0; self.dkey = self.sel.get(TABS[self.tab]); return None
        if k == 27:
            if self.detail: self.detail = False
            else: self.filt = ""
            return None
        d = {K.KEY_DOWN: 1, ord("j"): 1, K.KEY_UP: -1, ord("k"): -1, K.KEY_NPAGE: max(1, bh - 1), K.KEY_PPAGE: -max(1, bh - 1), K.KEY_HOME: -10**9, ord("g"): -10**9, K.KEY_END: 10**9, ord("G"): 10**9}.get(k)
        if d is not None:
            if self.detail: self.dscroll = max(0, self.dscroll + d)   # clamped when drawn
            else: self.move(model, d)
        return None

HELP = ["janus monitor: read-only (it writes nothing and runs nothing that does)", "",
        "Tab / Left / Right / 1-4   switch tabs: Active, Backlog, Finished, Board", "Up / Down / j / k / PgUp / PgDn / g / G   select and scroll",
        "Enter   details of the selected row (Esc or Enter: back)", "/       filter the rows of every tab by text (Enter keeps it, Esc clears it)",
        "r       refresh now", "q       quit", "", "Unknown stays '?': a host that does not answer is UNREACHABLE or STALE with the age of its last good data;",
        "a process that ended is not 'finished' until its report says VERDICT: DONE. Any key closes this help."]

def filtered(model, text):
    """The model with every tab's rows reduced to those whose cells or details hold text (counts and machines stay whole)."""
    if not text: return model
    t = text.lower(); m = dataclasses.replace(model, rows={k: [r for r in v if t in " ".join(map(str, r.cells)).lower() or t in " ".join(r.detail).lower()] for k, v in model.rows.items()})
    for k, v in m.rows.items():
        if not v and model.rows[k]: m.empty = {**m.empty, k: f"No row matches the filter {text!r} (/ then Esc clears it)"}
    return m

def wrap(lines, w):
    out = []
    for l in lines:
        l = str(l)
        if not l: out.append(""); continue
        ind = " " * min(w // 2, len(l) - len(l.lstrip()) + (11 if re.match(r"^[A-Z][A-Za-z ()]*: ", l) else 0))
        while len(l) > w: cut = l.rfind(" ", len(ind), w) if l.rfind(" ", len(ind), w) > len(ind) else w; out.append(l[:cut]); l = ind + l[cut:].lstrip()
        out.append(l)
    return out

def render(model, ui, h, w, now, status):
    """The screen as lines of (text, style) segments, h lines of at most w columns. Pure: no curses here."""
    if h < 3 or w < 24: return [[("too small (q quits)"[:max(w, 1)], "warn")]][:max(h, 1)]
    if ui.help: return [[(l[:w], "bold" if i == 0 else "")] for i, l in enumerate(wrap(HELP, w))][:h]
    L = []
    tab = TABS[ui.tab]
    L.append([("janus monitor", "bold"), (f" {model.project}  ", ""), ("READ-ONLY", "info"), (f"  {status}"[:max(0, w - 30)], "dim")])
    banners = model.banners.get(tab, [])[:2]
    foot = 1; tabbar = 1; colhead = 1
    spare = h - (1 + foot + tabbar + colhead + len(banners)) - 3   # keep three body lines
    nh = len(model.machines)
    if spare >= nh + 1 and nh: mach = "full"
    elif spare >= 1 and nh: mach = "summary"
    else: mach = None
    if mach == "full":
        lay = layout(HOST_COLS, w); L.append([(cells_line([c[0] for c in HOST_COLS], lay), "bold")])
        for r in model.machines: L.append([(cells_line(r.cells, lay), r.style)])
    elif mach == "summary":
        c = {}
        for r in model.machines: c[r.cells[1]] = c.get(r.cells[1], 0) + 1
        L.append([("HOSTS ", "bold"), (", ".join(f"{k} {v}" for k, v in sorted(c.items())), "bad" if "UNREACHABLE" in c else "warn" if set(c) - {"ok"} else "ok")])
    tab_at = len(L); L.append([])   # the tab bar is filled in below, once the selection has been settled (it counts the unread)
    for b in banners: L.append([(("! " + b)[:w], "warn")])
    bh = h - len(L) - colhead - foot
    cols = BOARD_COLS if tab == "Board" else WORK_COLS; rows = model.rows[tab]
    ui.sync(model, max(bh, 1))
    sel = ui.sel.get(tab); i = ui.idx.get(tab, 0); new = ui.new_count(model)
    tabs = []
    for ti, t in enumerate(TABS):
        n = model.counts.get(t); lab = f"{t} ({'?' if n is None else n}{f', {new} new' if t == 'Board' and new else ''})"
        tabs.append((f"[{lab}]" if ti == ui.tab else f" {lab} ", "rev bold" if ti == ui.tab else "new" if t == "Board" and new else ""))
    L[tab_at] = tabs
    if ui.detail and not rows and ui.dcache: ui.detail = False
    if ui.detail and rows:
        row = next((r for r in rows if r.key == ui.dkey), None)
        if row: ui.dcache = row.detail
        lines = wrap((["(this item is no longer listed; last known details)"] if not row else []) + ui.dcache, w - 1)
        ui.dscroll = min(ui.dscroll, max(0, len(lines) - max(bh, 1)))
        L.append([(f"Details {[r.key for r in rows].index(ui.dkey) + 1 if row else '-'}/{len(rows)}  (Enter or Esc: back, Up/Down PgUp/PgDn: scroll)"[:w], "bold")])
        for l in lines[ui.dscroll:ui.dscroll + max(bh, 0)]: L.append([(l, "")])
    else:
        lay = layout(cols, w - 2)
        L.append([("  " + cells_line([c[0] for c in cols], lay), "bold")])
        if not rows: L.append([(model.empty.get(tab, "")[:w], "dim")])
        top = ui.top.get(tab, 0)
        for j, r in enumerate(rows[top:top + max(bh, 0)], top):
            cells = list(r.cells)
            if tab == "Board" and ui.is_new(r.key): cells[0] = "NEW"
            style = "new bold" if tab == "Board" and ui.is_new(r.key) else r.style
            L.append([("> " if r.key == sel else "  ", "bold"), (cells_line(cells, lay), style)])
            if r.key == sel: L[-1] = [(t, s + " rev") for t, s in L[-1]]
    L = L[:h - 1]
    while len(L) < h - 1: L.append([])
    pos = f" {i + 1}/{len(rows)}" if rows else ""
    foot = f"/{ui.filt}_" if ui.typing else (f"filter: {ui.filt}  " if ui.filt else "") + (ui.flash or "Tab tabs  Up/Down select  Enter details  / filter  ? help  r refresh  q quit")
    L.append([(foot[:max(0, w - len(pos) - 1)], "dim"), (pos, "dim")])
    return [[(t[:w], s) for t, s in line] for line in L][:h]

def status_line(store_view, model, now, interval):
    _, running, finished, _v = store_view
    s = "refreshing..." if running else "updated " + (fmt_dur(now - finished) + " ago" if finished else "never")
    return f"{s}  every {interval:g}s" + (f"  {model.problems} source problem(s)" if model.problems else "")

# ---------------------------------------------------------------- the terminal

def make_attrs():
    a = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "rev": curses.A_REVERSE}
    if curses.has_colors() and not os.environ.get("NO_COLOR"):
        try:
            curses.start_color()
            with contextlib.suppress(curses.error): curses.use_default_colors()
            for n, (name, fg) in enumerate((("ok", curses.COLOR_GREEN), ("warn", curses.COLOR_YELLOW), ("bad", curses.COLOR_RED), ("info", curses.COLOR_CYAN), ("new", curses.COLOR_MAGENTA)), 1):
                curses.init_pair(n, fg, -1); a[name] = curses.color_pair(n) | (curses.A_BOLD if name in ("bad", "new") else 0)
            return a
        except curses.error: pass
    return {**a, "ok": 0, "info": 0, "warn": curses.A_BOLD, "bad": curses.A_BOLD | curses.A_UNDERLINE, "new": curses.A_BOLD | curses.A_UNDERLINE}   # monochrome: weight and underline, the labels carry the meaning

def paint(scr, lines, attrs):
    h, w = scr.getmaxyx(); scr.erase()
    for y, segs in enumerate(lines[:h]):
        x = 0
        for text, style in segs:
            if x >= w: break
            at = 0
            for tok in style.split(): at |= attrs.get(tok, 0)
            try: scr.addstr(y, x, text[:w - x], at)
            except (curses.error, UnicodeError): pass   # the last cell of the last line; text the locale cannot encode
            x += len(text)
    scr.refresh()

def run_ui(scr, collector, store, interval, attrs=None, clock=time.time):
    """The screen loop: draw, read a key (200 ms at most), never wait for collection. Returns when the user quits."""
    attrs = attrs if attrs is not None else make_attrs()
    with contextlib.suppress(curses.error): curses.curs_set(0)
    scr.keypad(True); scr.timeout(200)
    ui, last, dirty, model, flash_at = UI(), None, True, Model(), None
    while True:
        view = store.view()
        if ui.flash and flash_at is not None and view[3] != flash_at: ui.flash = ""   # the refresh began: the header says so
        stamp = (view[3], int(clock()), scr.getmaxyx(), ui.flash, ui.filt)
        if stamp != last or dirty:
            now = clock(); model = derive(view[0], now, interval); ui.observe(model); model = filtered(model, ui.filt)
            h, w = scr.getmaxyx(); paint(scr, render(model, ui, h, w, now, status_line(view, model, now, interval)), attrs)
            last, dirty = stamp, False
        try: k = scr.getch()
        except KeyboardInterrupt: return 0
        if k == -1: continue
        if k == curses.KEY_RESIZE:
            with contextlib.suppress(curses.error): curses.update_lines_cols()
            dirty = True; continue
        h, w = scr.getmaxyx(); ui.flash = ""
        act = ui.key(model, k, max(1, h - 8)); dirty = True
        if act == "quit": return 0
        if act == "refresh": collector.request(); flash_at = view[3]


def interval_arg(s):
    import argparse
    try: v = float(s)
    except ValueError: raise argparse.ArgumentTypeError(f"{s!r} is not a number of seconds")
    if not v >= MIN_INTERVAL: raise argparse.ArgumentTypeError(f"at least {MIN_INTERVAL:g} second(s): every refresh asks every host over ssh")
    return v

def cmd_monitor(cfg, a):
    """janus monitor [--interval SECONDS]: the read-only dashboard. Collection runs in the background; keys never wait for it."""
    if curses is None: raise Err("this Python has no curses module")
    if not (sys.stdin.isatty() and sys.stdout.isatty()): raise Err("janus monitor needs an interactive terminal")
    import locale; locale.setlocale(locale.LC_ALL, "")   # curses needs the user's locale for non-ASCII text
    os.environ.setdefault("ESCDELAY", "25")
    store = Store(); col = Collector(cfg.root, store, a.interval)
    def stop(*_): raise KeyboardInterrupt   # SIGTERM/SIGHUP end the screen the same way q does: the terminal is restored first
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        with detached_stdin():
            col.start(); curses.wrapper(lambda scr: run_ui(scr, col, store, a.interval))
    except KeyboardInterrupt: pass
    except curses.error as e: raise Err(f"the terminal cannot run the monitor ({clean(e, 80)}); is TERM set?")
    finally:
        col.stop(); kill_children()   # a probe waiting on a slow ssh must not outlive the screen
        for s, h in old.items(): signal.signal(s, h)
    return 0

__all__ = ["cmd_monitor", "interval_arg", "Collector", "Store", "Source", "derive", "render", "run_ui", "UI", "classify", "scrub", "clean", "kill_children", "fresh_cfg"]
