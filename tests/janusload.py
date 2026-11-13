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

# Hermetic admission (a Mac's live memory pressure refused ./install: the job scripts the suite runs read cap_measure's
# kern.memorystatus_vm_pressure_level and vm.loadavg, so a Mac at warn paused every admission test). A `sysctl` ahead on PATH
# answers those two keys with a calm machine (JANUS_TEST_PRESSURE overrides the level) and passes every other key to the real one.
# The production script and its thresholds are untouched; test_janus.Capacity proves that a warn level still pauses.
import atexit, shutil, tempfile
_STUB = Path(tempfile.mkdtemp(prefix="janus-calm-"))
(_STUB / "sysctl").write_text(f"""#!/bin/sh
case "$*" in
  *kern.memorystatus_vm_pressure_level*) echo "${{JANUS_TEST_PRESSURE:-1}}";;
  *vm.loadavg*) echo "{{ 0.50 0.50 0.50 }}";;
  *) PATH=${{PATH#{_STUB}:}}; exec sysctl "$@";;
esac
""")
(_STUB / "sysctl").chmod(0o755); os.environ["PATH"] = f"{_STUB}:{os.environ.get('PATH', '')}"
atexit.register(shutil.rmtree, _STUB, True)

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
