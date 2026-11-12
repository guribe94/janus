"""The bd (beads) wrapper: one place that reads units, saves their record and adds comments (section 7, Step 1).

A unit's record is the JSON in its issue's notes. It carries its version (v); unit_record() reads one and migrate_unit()
brings an older version up to UNIT_V, so a renamed or added field is one migration step here, never a .get() guess at
every reader (section 7, seam 6)."""
from orchkit.context import sh, Err
import json

UNIT_V = 2


def migrate_unit(m):
    """A unit record at UNIT_V. v1 (no v): launch wrote only the current run id (run), and resumes added the list (runs); v2 lists
    every run id in runs, the current one last. A newer record than this orch knows is returned as it is."""
    if m.get("v", 1) < 2:
        runs = list(dict.fromkeys([*m.get("runs", []), *([m["run"]] if m.get("run") else [])]))
        m = {**m, "v": 2, **({"runs": runs} if runs else {})}
    return m


def unit_record(i):
    """The record of a unit issue (its notes; an old one its description), migrated; {} when it cannot be read."""
    try: m = json.loads(i.get("notes") or i.get("description") or "{}")
    except Exception: m = {}
    return migrate_unit(m) if isinstance(m, dict) and m else {}


class Beads:
    def __init__(self, cfg): self.cfg = cfg
    def run(self, *a):
        rc, o, e = sh(["bd", *a, "--json"], cwd=self.cfg.beads)
        if rc: raise Err(f"bd {a[0]}: {(e or o).strip()[:120]}")
        try: return json.loads(o) if o.strip() else []
        except Exception: raise Err(f"bd {a[0]}: bad json")
    def issues(self, label, status="open"):
        r = self.run("list", "--label", label, "--status", status)
        return r.get("issues", r) if isinstance(r, dict) else r
    def units(self):
        """Open units: launch marks each in_progress (so `bd ready` never lists a running unit); older ones are still open."""
        out = []
        for i in self.issues("unit", "open,in_progress"):
            t = i.get("title", "")
            if not t.startswith("unit:"): continue
            out.append({**unit_record(i), "id": i["id"], "name": t[5:]})
        return out
    def save(self, u):
        """The unit's record (beads notes). What check computes on the fly (state, verdict, keys starting with _) is never saved."""
        meta = {k: v for k, v in u.items() if k not in ("id", "name", "state", "verdict", "status") and not k.startswith("_")}
        self.run("update", u["id"], "--notes", json.dumps(meta))
    def create(self, title, label, desc=""): 
        r = self.run("create", title, "-l", label, "-d", desc)
        return (r[0] if isinstance(r, list) else r)["id"]
    def comment(self, i, text): sh(["bd", "comments", "add", i, text], cwd=self.cfg.beads)

__all__ = ["UNIT_V", "migrate_unit", "unit_record", "Beads"]
