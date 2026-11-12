"""load dlus.py for the fuzz targets (state in a throwaway folder)"""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-fuzz-"))


def load():
    sys.argv = sys.argv[:1] + [a for a in sys.argv[1:] if a.startswith("-")]  # keep libFuzzer flags only
    spec = importlib.util.spec_from_file_location("dlus", os.path.join(ROOT, "dlus.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
