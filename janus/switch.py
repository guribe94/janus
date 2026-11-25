"""janus on / off: janus only where it is switched on. Claude Code reads a project's own hooks and skills
(<project>/.claude/settings.local.json, <project>/.claude/skills), so a project that is on carries them there and nowhere else.
Codex loads project hooks only for a folder trusted one by one, and an agent's worktree is a folder of its own, so its hooks and
skill are global while any project is on. The watcher acts on the registered projects (PROJECTS) only. Standard library only:
./install imports this module before any release exists."""
import json, time
from pathlib import Path

HOME = Path.home()
JANUS = HOME / ".local/bin/janus"; CUR = HOME / ".local/share/janus/current"; PROJECTS = HOME / ".config/janus/projects"
EVENTS = {"SessionStart": None, "PreToolUse": "Bash|Edit|Write|MultiEdit|NotebookEdit|apply_patch", "Stop": None}
OLD_EVENTS = ("UserPromptSubmit",)   # janus's entries of events it no longer uses: removed, never added
CLAUDE_GLOBAL = HOME / ".claude/settings.json"; CODEX_HOOKS = HOME / ".codex/hooks.json"
LOCAL_FILES = ("/.claude/settings.local.json", "/.claude/skills/janus")   # what janus on writes in a project
CLAUDE_SKILL = HOME / ".claude/skills/janus"; CODEX_SKILLS = (HOME / ".agents/skills/janus", HOME / ".codex/skills/janus")

def hooks_json():
    """The janus hooks, in the settings format Claude Code and Codex share: {"hooks": {event: [entry]}}."""
    return {"hooks": {ev: [{**({"matcher": m} if m else {}), "hooks": [{"type": "command", "command": f"{JANUS} hook {ev}", "timeout": 60}]}]
                      for ev, m in EVENTS.items()}}

def is_janus(e): return any("janus hook " in h.get("command", "") for h in e.get("hooks", []))

def set_hooks(path, on, say=print, dry=False):
    """Put janus's hooks into the settings file at path (on) or take every janus entry out of it (off); every other key and
    hook is kept, and the file is backed up before it changes. True when it changed."""
    path = Path(path).expanduser()
    try: data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError: say(f"{path}: not JSON, left as it is"); return False
    old = json.dumps(data, sort_keys=True); hooks = data.setdefault("hooks", {})
    for ev in (*EVENTS, *OLD_EVENTS):
        cur = hooks.get(ev, []); want = hooks_json()["hooks"][ev] if on and ev in EVENTS else []
        at = next((i for i, e in enumerate(cur) if is_janus(e)), None)
        # janus's entry stays where it is (Codex keys a hook's trust by event:index: moving it to the end re-asked review of
        # every hook after another tool wrote the file, and each codex agent stopped at 'N hooks need review'); a new one goes last
        keep = [e for e in cur if not is_janus(e)]
        if want: keep[at if at is not None else len(keep):at if at is not None else len(keep)] = want
        if keep: hooks[ev] = keep
        else: hooks.pop(ev, None)
    if not hooks: data.pop("hooks")
    if json.dumps(data, sort_keys=True) == old: return False
    say(f"{'add' if on else 'remove'} janus hooks: {path}")
    if dry: return True
    if path.exists(): path.with_name(path.name + f".bak-janus-{int(time.time())}").write_text(path.read_text())
    if data or path.exists():
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(data, indent=2) + "\n")
    return True

def set_link(link, on, say=print, dry=False):
    """The skill link at link: to the current release's skill (on), or removed (off; only a link, never a folder)."""
    link = Path(link); target = CUR / "skill"
    if on:
        if link.is_symlink() and Path(link.readlink()) == target: return False
        if link.exists() and not link.is_symlink(): say(f"{link} is not a link: left as it is"); return False
        say(f"link {link} -> {target}")
        if not dry: link.parent.mkdir(parents=True, exist_ok=True); link.unlink(missing_ok=True); link.symlink_to(target)
        return True
    if not link.is_symlink(): return False
    say(f"remove link {link}")
    if not dry: link.unlink()
    return True

def on_projects():
    """The projects that are on (registered for the watcher), in order."""
    return [p for p in (PROJECTS.read_text().split() if PROJECTS.exists() else []) if p]

def set_project(root, on, say=print, dry=False):
    """Janus in project root: its own Claude Code hooks and skill link, its place on the watcher's list, and the Codex hooks and
    skill while any project is on. Off removes exactly those; agents and their work are untouched."""
    root = Path(root).resolve(); have = on_projects(); now = [p for p in have if p != str(root)] + ([str(root)] if on else [])
    set_hooks(root / ".claude/settings.local.json", on, say, dry); set_link(root / ".claude/skills/janus", on, say, dry)
    if on: git_exclude(root, say, dry)
    if now != have:
        say(f"{'add' if on else 'remove'} {root} {'to' if on else 'from'} {PROJECTS}")
        if not dry: PROJECTS.parent.mkdir(parents=True, exist_ok=True); PROJECTS.write_text("".join(p + "\n" for p in now))
    codex(bool(now), say, dry)
    return now

def git_exclude(root, say=print, dry=False):
    """In a git project, keep janus's local files out of git: .git/info/exclude (the clone's own, never committed) lists them."""
    ex = root / ".git/info/exclude"
    if not (root / ".git").is_dir(): return
    have = ex.read_text().splitlines() if ex.exists() else []; add = [x for x in LOCAL_FILES if x not in have]
    if not add: return
    say(f"exclude from git: {', '.join(add)} ({ex})")
    if not dry: ex.parent.mkdir(parents=True, exist_ok=True); ex.write_text("".join(l + "\n" for l in [*have, *add]))

def codex(on, say=print, dry=False):
    """Codex's janus hooks and skill: global while any project is on (it reads no per-project hooks in an agent's worktree)."""
    set_hooks(CODEX_HOOKS, on, say, dry)
    for x in CODEX_SKILLS[:1] if on else CODEX_SKILLS: set_link(x, on, say, dry)

__all__ = ["hooks_json", "set_hooks", "set_link", "on_projects", "set_project", "codex", "EVENTS", "PROJECTS", "CLAUDE_GLOBAL", "CLAUDE_SKILL"]
