"""Dependency hygiene (W0.1): the runtime entry points may only import modules
that are declared in requirements.txt.

`resonant_engine.py` and `resonant_cli.py` import the heavy ASR backend lazily,
so a missing requirement used to surface only as a crash deep inside the
dictation pipeline (or as a raw traceback in the CLI). This test statically
scans every `import` in those two modules and fails when a third-party module is
not declared.

Import names do not always match the PyPI distribution name (`import whisper`
comes from `openai-whisper`, `import PIL` from `pillow`). We resolve that via
`importlib.metadata.packages_distributions()` when the package is installed and
fall back to an explicit map so the check also works in the lightweight dev venv
where torch/whisper are deliberately absent (brief §1 rule 5).
"""
import ast
import os
import sys
from importlib.metadata import packages_distributions

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINTS = ("resonant_engine.py", "resonant_cli.py")

# import name -> distribution name, for the few packages whose names differ.
IMPORT_TO_DIST = {
    "whisper": "openai-whisper",
    "PIL": "pillow",
}


def _normalise(name):
    return name.strip().lower().replace("_", "-")


def _declared_requirements(path=None):
    """Distribution names declared in requirements.txt (markers/comments stripped)."""
    path = path or os.path.join(BASE, "requirements.txt")
    declared = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
                line = line.split(sep, 1)[0]
            declared.add(_normalise(line))
    return declared


def _local_modules():
    """Project modules living next to the entry points (imported by name)."""
    return {name[:-3] for name in os.listdir(BASE) if name.endswith(".py")}


def _top_level_imports(path):
    """Every top-level module name imported anywhere in a file, including
    function-local `import` statements (that is where the ASR import lives)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_entrypoint_imports_are_all_declared():
    declared = _declared_requirements()
    local = _local_modules()
    installed = packages_distributions()

    missing = {}
    for entry in ENTRYPOINTS:
        for module in _top_level_imports(os.path.join(BASE, entry)):
            if module in sys.stdlib_module_names or module in local:
                continue
            # Candidate distributions this import could belong to.
            candidates = {_normalise(module)}
            mapped = IMPORT_TO_DIST.get(module)
            if mapped:
                candidates.add(_normalise(mapped))
            candidates.update(_normalise(d) for d in installed.get(module, []))
            if candidates & declared:
                continue
            missing.setdefault(entry, set()).add(module)

    assert not missing, (
        "Undeclared runtime imports in the entry points — add them to "
        "requirements.txt: "
        + "; ".join(f"{f}: {sorted(mods)}" for f, mods in missing.items()))


def test_asr_backend_is_declared():
    """The Whisper backend must stay in runtime requirements (not dev-only)."""
    assert "openai-whisper" in _declared_requirements()
