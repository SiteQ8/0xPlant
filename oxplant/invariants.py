"""Physics-based invariants: a safe expression language over process tags.

An invariant is a boolean expression that must hold for consistent process
data, e.g. a mass balance across a tank. Expressions may use:

  v("TAG")     current value          prev("TAG")  value at the previous poll
  dt           seconds since the previous poll
  abs, min, max, round, and, or, not, comparisons, arithmetic

Anything else is rejected at load time, so configuration cannot execute code.
"""
from __future__ import annotations

import ast
from typing import Any, Callable, Dict, Optional

_ALLOWED_NODES = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Name, ast.Constant,
                  ast.Load, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
                  ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp)
_ALLOWED_FUNCS = {"v", "prev", "abs", "min", "max", "round"}


class Invariant:
    def __init__(self, name: str, expr: str, debounce: int = 3, desc: str = ""):
        self.name = name
        self.expr = expr
        self.debounce = max(1, int(debounce))
        self.desc = desc
        self.code = compile(self._check(expr), f"<invariant {name}>", "eval")
        self.violations = 0
        self.ok_streak = 0
        self.alerting = False

    @staticmethod
    def _check(expr: str) -> ast.Expression:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ValueError(f"invariant uses unsupported syntax: {type(node).__name__}")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                    raise ValueError("invariant may only call v, prev, abs, min, max, round")
            if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS | {"dt"}:
                raise ValueError(f"unknown name in invariant: {node.id}")
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool)):
                raise ValueError("unsupported constant in invariant")
        return tree

    def evaluate(self, values: Dict[str, float], previous: Dict[str, float], dt: float) -> Optional[bool]:
        """True = holds, False = violated, None = not evaluable yet (missing values or no previous poll)."""
        if dt <= 0:
            return None
        missing = []

        def v(tag: str) -> float:
            if tag not in values:
                missing.append(tag)
                return 0.0
            return float(values[tag])

        def prev(tag: str) -> float:
            if tag not in previous:
                missing.append(tag)
                return 0.0
            return float(previous[tag])

        env: Dict[str, Any] = {"v": v, "prev": prev, "dt": float(dt), "abs": abs, "min": min, "max": max, "round": round, "__builtins__": {}}
        try:
            result = bool(eval(self.code, env))  # noqa: S307 - AST whitelisted above
        except (ZeroDivisionError, TypeError, ValueError):
            return None
        return None if missing else result

    def update(self, holds: Optional[bool]) -> Optional[str]:
        """Feed an evaluation; returns 'violated' when the debounce threshold is crossed, 'restored' on recovery, else None."""
        if holds is None:
            return None
        if holds:
            self.violations = 0
            self.ok_streak += 1
            if self.alerting and self.ok_streak >= self.debounce:
                self.alerting = False
                return "restored"
            return None
        self.ok_streak = 0
        self.violations += 1
        if not self.alerting and self.violations >= self.debounce:
            self.alerting = True
            return "violated"
        return None
