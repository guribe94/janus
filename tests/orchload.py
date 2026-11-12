"""Load the orch under test (ORCH_PY, default the orch.py next to tests/) for the test modules.

orch.py is a shim over the orchkit package next to it, so the loaded module gets every orchkit module's names (tests call
orch.<function> wherever it lives), and a test that patches `orch.X` must reach the caller wherever it lives (board.py calls
herdr, cap.py calls sh, ...): every setattr on it is broadcast to each loaded orchkit module holding the name."""
import importlib, importlib.util, os, pkgutil, sys, types
from pathlib import Path

ORCH_PY = Path(os.environ.get("ORCH_PY") or Path(__file__).resolve().parent.parent / "orch.py")


class _Broadcast(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for m in list(sys.modules.values()):
            if getattr(m, "__name__", "").startswith("orchkit.") and hasattr(m, name): setattr(m, name, value)


def load():
    sys.path.insert(0, str(ORCH_PY.parent))   # the orchkit package next to the orch.py under test (a release copy too), never cwd's
    spec = importlib.util.spec_from_file_location("orch", ORCH_PY); orch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orch)
    import orchkit
    for m in pkgutil.walk_packages(orchkit.__path__, "orchkit."):
        for k, v in vars(importlib.import_module(m.name)).items():
            if not k.startswith("__") and not isinstance(v, types.ModuleType): setattr(orch, k, v)
    orch.__class__ = _Broadcast
    return orch
