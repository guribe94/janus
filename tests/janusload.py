"""Load the janus under test (JANUS_PY, default the janus.py next to tests/) for the test modules.

janus.py is a shim over the janus package next to it, so the loaded module gets every janus module's names (tests call
janus.<function> wherever it lives), and a test that patches `janus.X` must reach the caller wherever it lives (board.py calls
herdr, cap.py calls sh, ...): every setattr on it is broadcast to each loaded janus module holding the name."""
import importlib, importlib.util, os, pkgutil, sys, types
from pathlib import Path

# The tests commit in scratch repositories: give git an identity where the machine has none.
for _k, _v in (("NAME", "t"), ("EMAIL", "t@t")):
    for _who in ("AUTHOR", "COMMITTER"):
        os.environ.setdefault(f"GIT_{_who}_{_k}", _v)

JANUS_PY = Path(os.environ.get("JANUS_PY") or Path(__file__).resolve().parent.parent / "janus.py")


class _Broadcast(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for m in list(sys.modules.values()):
            if getattr(m, "__name__", "").startswith("janus.") and hasattr(m, name): setattr(m, name, value)


def load():
    sys.path.insert(0, str(JANUS_PY.parent))   # the janus package next to the janus.py under test (a release copy too), never cwd's
    # The shim module must not be registered as "janus": that name is the package, and the shim imports it.
    spec = importlib.util.spec_from_file_location("janus_shim", JANUS_PY); shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    import janus as pkg
    for m in pkgutil.walk_packages(pkg.__path__, "janus."):
        for k, v in vars(importlib.import_module(m.name)).items():
            if not k.startswith("__") and not isinstance(v, types.ModuleType): setattr(shim, k, v)
    shim.__class__ = _Broadcast
    return shim
