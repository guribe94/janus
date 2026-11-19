"""janus on / off (janus/switch.py): janus only in the projects switched on. Each test names the failure it catches. Standard
library only; HOME-level paths point into a temporary folder."""
import json, tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import load

janus = load()
from janus import switch


class Switch(unittest.TestCase):
    def setUp(self):
        self.h = Path(tempfile.mkdtemp(prefix="janus-switch-")); self.proj = self.h / "proj"; self.proj.mkdir()
        for k, v in {"PROJECTS": self.h / "projects", "CODEX_HOOKS": self.h / "codex-hooks.json", "CUR": self.h / "current",
                     "CODEX_SKILLS": (self.h / "agents-skills/janus", self.h / "codex-skills/janus")}.items():
            p = mock.patch.object(switch, k, v); p.start(); self.addCleanup(p.stop)
        self.said = []; self.say = self.said.append

    def test_on_and_off_change_only_janus_entries(self):
        """Catches: janus on overwriting a project's other hooks or settings, janus off leaving janus behind, either touching a
        hook of another tool."""
        st = self.proj / ".claude/settings.local.json"; st.parent.mkdir()
        mine = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-stop"}]}]}}
        st.write_text(json.dumps(mine))
        switch.set_project(self.proj, True, self.say)
        data = json.loads(st.read_text()); cmds = [h["command"] for ev in data["hooks"].values() for e in ev for h in e["hooks"]]
        self.assertEqual(data["permissions"], mine["permissions"]); self.assertIn("my-own-stop", cmds)
        self.assertEqual(sorted(c.split()[-1] for c in cmds if "janus hook" in c), ["PreToolUse", "SessionStart", "Stop"])
        self.assertEqual((self.proj / ".claude/skills/janus").readlink(), self.h / "current/skill")
        self.assertEqual(switch.on_projects(), [str(self.proj.resolve())]); self.assertTrue(switch.CODEX_HOOKS.exists())
        switch.set_project(self.proj, True, self.say); self.assertEqual(len(json.loads(st.read_text())["hooks"]["Stop"]), 2)   # on twice: once
        switch.set_project(self.proj, False, self.say)
        self.assertEqual(json.loads(st.read_text()), mine); self.assertFalse((self.proj / ".claude/skills/janus").is_symlink())
        self.assertEqual(switch.on_projects(), []); self.assertNotIn("janus hook", switch.CODEX_HOOKS.read_text())   # no project on: Codex off too

    def test_codex_stays_on_while_another_project_is_on(self):
        """Catches: switching one project off removing the Codex hooks another project's agents still need."""
        other = self.h / "other"; other.mkdir()
        switch.set_project(self.proj, True, self.say); switch.set_project(other, True, self.say); switch.set_project(self.proj, False, self.say)
        self.assertIn("janus hook", switch.CODEX_HOOKS.read_text()); self.assertEqual(switch.on_projects(), [str(other.resolve())])

    def test_a_git_projects_local_janus_files_stay_out_of_git(self):
        """Catches: janus on leaving .claude/settings.local.json and the skill link as untracked files git offers to commit."""
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        switch.set_project(self.proj, True, self.say); switch.set_project(self.proj, True, self.say)
        st = subprocess.run(["git", "-C", str(self.proj), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True).stdout
        self.assertEqual(st, ""); ex = (self.proj / ".git/info/exclude").read_text()
        self.assertEqual(ex.count(".claude/settings.local.json"), 1)                     # on twice: listed once

    def test_a_folder_where_the_skill_link_goes_is_never_removed(self):
        """Catches: janus off deleting a real skills folder someone put at that path."""
        d = self.proj / ".claude/skills/janus"; d.mkdir(parents=True); (d / "SKILL.md").write_text("mine\n")
        switch.set_project(self.proj, True, self.say); switch.set_project(self.proj, False, self.say)
        self.assertEqual((d / "SKILL.md").read_text(), "mine\n")

    def test_a_launched_claude_agent_gets_the_hooks_for_its_own_session(self):
        """Catches: an agent in a worktree (a project of its own) running without janus's Stop and guard hooks once the global
        hooks are gone."""
        a = janus.agent_args({"kind": "claude", "args": ["--dangerously-skip-permissions"]})
        self.assertEqual(a[:2], ["--dangerously-skip-permissions", "--settings"])
        h = json.loads(a[2])["hooks"]; self.assertEqual(sorted(h), ["PreToolUse", "SessionStart", "Stop"])
        self.assertIn("janus hook Stop", h["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(janus.agent_args({"kind": "codex", "args": ["-x"]}), ["-x"])   # Codex: its global hooks while a project is on


if __name__ == "__main__":
    unittest.main()
