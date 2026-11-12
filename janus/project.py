"""Cross-project safety: on a machine (or a host's queue, or a herdr server) shared by several janus projects, a project sees
and acts on its own items only. Each item janus creates carries the project's id: a pueue job's label ends in @<id>
(unit:<name>@<id>, gate:<name>@<id>, run:<name>@<id>, an agent run <name>@<id>), a tmux session is janus-<unit>@<id>, a herdr tab is created in the project's
workspace, a board card gets a `project` field. Orphan findings, cancels, tidy and the autopilot consider only items with this
project's id; an item with no project id (made before ids, or by hand) is listed as machine state (machine:* findings) and never
acted on; another project's items are neither shown nor touched. The id is [project] id, default the project's name."""
import re

def project_id(cfg):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(cfg.c.get("project", {}).get("id") or cfg.name)).strip("-") or "janus"

def tag(cfg, label):
    """A pueue label that carries this project's id."""
    return f"{label}@{project_id(cfg)}"

def label_owner(label):
    """(label without the id, project id or None)."""
    base, sep, pid = (label or "").rpartition("@")
    return (base, pid) if sep and base and pid else (label or "", None)

def own_label(cfg, t):
    """A pueue task's label without the id when the task is this project's, else None (another project's, or none's)."""
    base, pid = label_owner((t or {}).get("label"))
    return base if pid == project_id(cfg) else None

def untagged(t):
    """A task with no project id: machine state, listed, never acted on."""
    return label_owner((t or {}).get("label"))[1] is None

def tmux_name(cfg, name):
    """A unit's tmux session on its host: janus-<unit>@<project> (tmux names take no '.' or ':')."""
    return f"janus-{name}@{project_id(cfg).replace('.', '_')}"

def unit_tmux(u):
    """The unit's recorded tmux session (janus-<unit> for one launched before project ids)."""
    return u.get("tmux") or f"janus-{u['name']}"

def own_card(cfg, fields):
    """A board card (its fields) this project created."""
    return (fields or {}).get("project") == project_id(cfg)

__all__ = ["project_id", "tag", "label_owner", "own_label", "untagged", "tmux_name", "unit_tmux", "own_card"]
