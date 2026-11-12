"""Docs-freshness: docs/REFERENCE.md names every janus command and every finding family.

The failure caught: a command or finding family added (or renamed) in code while the reference still documents the old set,
so the orchestrator reads docs that do not describe the toolkit it runs. Design rule keep-simple; the owner rule "docs updated
with every change" (S1 the-orchestration-toolkit..., S3 commit-often-no-debt, S3 docs-useful-for-humans) had no check.

Behavioural, deterministic, standard library only: it parses janus/cli.py for the subcommands it registers and the findings
registry for its families, and asserts each appears in docs/REFERENCE.md. It fails before the doc line exists and passes after.

Run by ./check (it discovers tests/test_*.py)."""
import ast
import unittest
from pathlib import Path

from janusload import load

janus = load()

ROOT = Path(janus.__file__).resolve().parent          # janus.py's folder (the tree, or the release copy ./check tests)
CLI = ROOT / "janus" / "cli.py"
REFERENCE = ROOT / "docs" / "REFERENCE.md"

# Commands that are janus's own plumbing, not orchestrator-facing: the hook handler (installed by ./install), the two halves of
# the gate wrapper (gate-exec and gate-judge, run by the generated gate script), api-run (queued by launch), and
# commit-state/report-sent style internals are documented by their caller. Each is deliberate, so a new one cannot inherit this
# and go undocumented.
INTERNAL = {
    "hook": "installed by ./install and run by the runtime, never by hand",
    "gate-exec": "the gate wrapper's inner half, generated into the gate script",
    "gate-judge": "the gate wrapper's judging half, called by the generated gate script",
    "api-run": "queued by janus launch; never typed by the orchestrator",
}


def subcommands():
    """The subcommand names janus/cli.py registers (the first string argument of every add(...) call)."""
    tree = ast.parse(CLI.read_text())
    names = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "add" and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            names.append(n.args[0].value)
    return names


class DocsReference(unittest.TestCase):
    def test_reference_names_every_command(self):
        text = REFERENCE.read_text()
        missing = [c for c in subcommands() if c not in INTERNAL and f"janus {c}" not in text]
        self.assertEqual(missing, [], f"docs/REFERENCE.md does not name these commands: {missing}")

    def test_reference_names_every_finding_family(self):
        text = REFERENCE.read_text()
        missing = [f for f in janus.FAMILIES if f"`{f}`" not in text and f"**{f}" not in text and f"{f}:" not in text]
        self.assertEqual(missing, [], f"docs/REFERENCE.md does not name these finding families: {missing}")


if __name__ == "__main__":
    unittest.main()
