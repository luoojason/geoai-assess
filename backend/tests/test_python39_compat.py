"""Guard the shipped backend against 3.10+-only annotation syntax.

Prof. Shi could not open the site on Windows: `START-HCAS.bat` ran under the
Windows Store Python 3.9, and `backend/gis/counties.py` annotated a return as
`CountyRecord | None`. PEP 604 unions are 3.10+ syntax that Python evaluates at
function-definition time, so the import chain
`app.py -> hcas.py -> gis/counties.py` died before the server ever started.

That site was fixed. A second one in `assess/places.py` was missed for ten days
and would have reproduced the same crash from a different file, because
`hcas.py` imports `places` at module scope. It survived review because a plain
grep for `| None` is dominated by docstrings and by `hcas.py`, which is safe
only because it already carries `from __future__ import annotations`.

So this test parses instead of grepping: it flags a union only in a REAL
annotation position, and only in a file that does not defer evaluation. Two
ways to satisfy it -- add `from __future__ import annotations`, or write
`Optional[X]`.

PEP 585 builtin generics (`list[str]`, `dict[str, int]`, `tuple[...]`) are valid
at runtime on 3.9 and are deliberately not flagged.
"""
from __future__ import annotations

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# Lowest interpreter the installer may plausibly land on. PEP 604 needs 3.10.
MIN_SUPPORTED = (3, 9)


def _defers_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _annotation_nodes(tree: ast.Module):
    """Every expression the interpreter evaluates as an annotation."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                yield node.returns
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                        args.vararg, args.kwarg):
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation


def _pep604_offenders():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _defers_annotations(tree):
            continue
        for annotation in _annotation_nodes(tree):
            for sub in ast.walk(annotation):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                    rel = path.relative_to(BACKEND).as_posix()
                    offenders.append(f"{rel}:{sub.lineno}")
    return offenders


def test_no_pep604_unions_without_deferred_annotations():
    offenders = _pep604_offenders()
    assert not offenders, (
        "PEP 604 unions (X | None) in real annotations would raise TypeError at "
        f"import on Python {MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]}, which is what the "
        "installer can still select. Add `from __future__ import annotations` to "
        "the file, or use Optional[X]. Offenders: " + ", ".join(offenders)
    )


def test_places_defers_annotations():
    """Pin the specific regression: places.py is on the startup import path."""
    src = (BACKEND / "assess" / "places.py").read_text(encoding="utf-8")
    assert "from __future__ import annotations" in src, (
        "backend/assess/places.py is imported at module scope by hcas.py, which "
        "is itself on the startup path, so a 3.10+-only annotation here crashes "
        "the server at import on Python 3.9."
    )
