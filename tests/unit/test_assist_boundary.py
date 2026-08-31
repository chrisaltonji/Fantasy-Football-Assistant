"""The import direction, enforced.

`advice/` promises in its own docstrings that "the Claude layer reads this and
adds the soft half — it never recomputes a number that appears here." That
promise is only worth the bytes if something checks it, because the failure is
silent: one convenience import and a language model is inside a bid ceiling.

Inference may depend on arithmetic. Arithmetic may never depend on inference.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "ffa"

# The deterministic core. Nothing here may reach into `assist/`.
ARITHMETIC = ("advice", "domain", "state", "reference", "ingest", "view", "config")


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("package", ARITHMETIC)
def test_the_deterministic_core_never_imports_the_inference_layer(package):
    offenders = []
    for path in sorted((SRC / package).rglob("*.py")):
        if any(m.startswith("ffa.assist") for m in imports_of(path)):
            offenders.append(path.relative_to(SRC))
    assert not offenders, (
        f"{offenders} import ffa.assist. Arithmetic must never depend on "
        "inference — that is what keeps a language model out of a bid ceiling."
    )


def test_only_one_module_is_allowed_to_import_the_sdk():
    """Everything else stays importable with no SDK installed, which is what
    lets the whole suite run offline — the same rule `requests` follows."""
    offenders = []
    for path in sorted((SRC / "assist").rglob("*.py")):
        if path.name == "client.py":
            continue
        if any(m.split(".")[0] == "anthropic" for m in imports_of(path)):
            offenders.append(path.relative_to(SRC))
    assert not offenders, f"{offenders} import anthropic; only client.py may"


def test_the_sdk_is_not_imported_at_module_scope_anywhere():
    """Lazy, inside the function — the pattern `draftroom.py` uses for playwright
    and every network call uses for `requests`."""
    client = SRC / "assist" / "client.py"
    if not client.is_file():
        pytest.skip("client.py not written yet")

    tree = ast.parse(client.read_text(encoding="utf-8"), filename=str(client))
    for node in tree.body:                      # module scope only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            assert not any(n.split(".")[0] == "anthropic" for n in names), (
                "anthropic is imported at module scope in client.py"
            )
