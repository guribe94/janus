"""Load the orch under test (ORCH_PY, default the orch.py next to tests/) for the test modules.

orch's code lives in the orchkit package, so a test that patches `orch.X` must reach the caller wherever it now lives (board.py
calls herdr, cap.py calls sh, ...): every setattr on the loaded module is broadcast to each loaded orchkit module holding the name."""
import importlib.util, os, sys, types
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
    spec.loader.exec_module(orch); orch.__class__ = _Broadcast
    return orch
