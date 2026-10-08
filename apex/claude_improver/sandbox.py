"""Environnement isolé pour le code de features généré par Claude.

Défense en profondeur :
1. Validation AST par liste blanche : pas d'import (sauf math/statistics), pas
   d'attributs dunder, pas de while, pas de global/nonlocal, pas d'appels à des
   builtins dangereux. La fonction doit s'appeler `compute(ctx)`.
2. Exécution des tests dans un sous-processus séparé (`python -I`), avec limites
   CPU/mémoire (Linux) et timeout, builtins restreints.
3. En production, exécution avec builtins restreints + mesure de durée ; la
   feature est désactivée si elle lève des exceptions ou dépasse son budget.
"""
from __future__ import annotations

import ast
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import textwrap
from typing import Any, Callable

ALLOWED_IMPORTS = {"math", "statistics"}
SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "enumerate": enumerate,
    "filter": filter, "float": float, "int": int, "len": len, "list": list, "map": map,
    "max": max, "min": min, "range": range, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "sum": sum, "tuple": tuple, "zip": zip, "isinstance": isinstance,
    "str": str, "frozenset": frozenset, "ValueError": ValueError, "ZeroDivisionError": ZeroDivisionError,
    "Exception": Exception, "True": True, "False": False, "None": None,
}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "memoryview", "type", "object", "super", "help",
}
ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.AnnAssign, ast.For, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue, ast.Try,
    ast.ExceptHandler, ast.Raise, ast.Assert,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Lambda, ast.IfExp, ast.Dict, ast.Set, ast.ListComp,
    ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Compare, ast.Call, ast.Constant,
    ast.Attribute, ast.Subscript, ast.Name, ast.List, ast.Tuple, ast.Slice, ast.Starred,
    ast.keyword, ast.comprehension, ast.Load, ast.Store, ast.Del,
    ast.And, ast.Or, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In,
    ast.NotIn, ast.Is, ast.IsNot, ast.Import, ast.alias, ast.JoinedStr, ast.FormattedValue,
)


class UnsafeCode(ValueError):
    pass


def validate(code: str, entry: str = "compute") -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCode(f"syntaxe : {e}") from e
    has_entry = False
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise UnsafeCode(f"construction interdite : {type(node).__name__}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    raise UnsafeCode(f"import interdit : {a.name}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeCode(f"attribut interdit : {node.attr}")
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            raise UnsafeCode(f"nom interdit : {node.id}")
        if isinstance(node, ast.FunctionDef) and node.name == entry and node.args.args and len(node.args.args) == 1:
            has_entry = True
    if not has_entry:
        raise UnsafeCode(f"la fonction `{entry}(ctx)` est absente")


def _restricted_globals() -> dict[str, Any]:
    builtins = dict(SAFE_BUILTINS)

    def _imp(name, *a, **k):  # noqa: ANN001
        if name not in ALLOWED_IMPORTS:
            raise ImportError(name)
        return {"math": math, "statistics": statistics}[name]

    builtins["__import__"] = _imp
    return {"__builtins__": builtins, "math": math, "statistics": statistics}


def compile_feature(code: str) -> Callable[[dict], Any]:
    validate(code)
    g = _restricted_globals()
    exec(compile(code, "<claude_feature>", "exec"), g)  # noqa: S102 — code validé par liste blanche AST
    return g["compute"]


_RUNNER = textwrap.dedent(
    """
    import json, sys, math, statistics
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 2**20, 512 * 2**20))
    except Exception:
        pass
    payload = json.load(sys.stdin)
    sys.path.insert(0, payload["pkg_root"])
    from apex.claude_improver.sandbox import compile_feature, validate, _restricted_globals
    compute = compile_feature(payload["code"])
    validate(payload["test_code"], entry="test")
    g = _restricted_globals()
    exec(compile(payload["test_code"], "<claude_test>", "exec"), g)
    g["test"](compute)
    outs = []
    for ctx in payload["samples"]:
        v = compute(ctx)
        if v is not None and not isinstance(v, (int, float)):
            raise TypeError("compute doit renvoyer un nombre ou None")
        outs.append(None if v is None or v != v else float(v))
    print(json.dumps({"ok": True, "outputs": outs}))
    """
)


def run_isolated(code: str, test_code: str, samples: list[dict], timeout_s: float = 10.0) -> dict:
    """Valide puis exécute le test unitaire et la feature sur des échantillons réels,
    dans un sous-processus isolé. Retourne {ok, outputs | error}."""
    try:
        validate(code)
        validate(test_code, entry="test")
    except UnsafeCode as e:
        return {"ok": False, "error": f"validation : {e}"}
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _RUNNER],
                input=json.dumps({"code": code, "test_code": test_code, "samples": samples, "pkg_root": pkg_root}),
                capture_output=True, text=True, timeout=timeout_s, cwd=tmp,
                env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout)[-2000:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "sortie illisible"}
