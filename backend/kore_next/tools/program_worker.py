"""Isolated worker for the restricted program tool.

The worker speaks JSON lines on stdin/stdout. User code runs with a small
builtin set and a strict AST policy; every tool call is proxied to the parent
Runtime, which performs visibility, argument, and permission checks.
"""

from __future__ import annotations

import ast
import json
import sys
from typing import Any


class ProgramPolicyError(ValueError):
    pass


_FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "object",
    "open",
    "quit",
    "setattr",
    "super",
    "type",
    "vars",
}

_ALLOWED_NODES = {
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.For,
    ast.While,
    ast.If,
    ast.IfExp,
    ast.Compare,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.keyword,
    ast.Starred,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Raise,
    ast.Try,
    ast.ExceptHandler,
    ast.Assert,
    ast.Delete,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.NamedExpr,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
}


class _Policy(ast.NodeVisitor):
    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, tuple(_ALLOWED_NODES)):
            raise ProgramPolicyError(f"{type(node).__name__} is not allowed in a program")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise ProgramPolicyError(f"Name {node.id!r} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr == "format":
            raise ProgramPolicyError(f"Attribute {node.attr!r} is not allowed")
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            raise ProgramPolicyError("Attribute assignment is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            raise ProgramPolicyError(f"Call to {node.func.id!r} is not allowed")
        self.generic_visit(node)


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class _Tools:
    def __init__(self, logs: list[str]) -> None:
        self.calls = 0
        self.logs = logs

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a dict")
        self.calls += 1
        request_id = self.calls
        _send({"type": "call", "id": request_id, "name": name, "arguments": arguments})
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("tool channel closed")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise RuntimeError("tool response id mismatch")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "tool call failed"))
        return response.get("value")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value


def _build_builtins(logs: list[str], tools: _Tools) -> dict[str, Any]:
    def emit(*values: Any, sep: str = " ") -> None:
        text = sep.join(str(value) for value in values)
        if len(logs) < 100:
            logs.append(text[:500])

    return {
        "__build_class__": None,
        "__import__": None,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": emit,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "Exception": Exception,
        "KeyError": KeyError,
        "RuntimeError": RuntimeError,
        "TypeError": TypeError,
        "ValueError": ValueError,
    }


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    request = json.loads(sys.stdin.readline() or "{}")
    source = request.get("source")
    if not isinstance(source, str) or not source.strip():
        _send({"type": "error", "message": "program source is required"})
        return 2
    try:
        tree = ast.parse(source, filename="<program>", mode="exec")
        _Policy().visit(tree)
        logs: list[str] = []
        tools = _Tools(logs)
        namespace = {"__builtins__": _build_builtins(logs, tools), "tools": tools}
        exec(compile(tree, "<program>", "exec"), namespace)
        _send(
            {
                "type": "final",
                "value": _jsonable(namespace.get("result")),
                "logs": logs,
                "calls": tools.calls,
            }
        )
        return 0
    except BaseException as exc:
        _send(
            {
                "type": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "logs": [],
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
