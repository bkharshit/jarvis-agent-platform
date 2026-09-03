"""Calculator builtin — safe arithmetic via whitelisted AST evaluation."""

from __future__ import annotations

import ast
import operator
from typing import Any

from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.tools.base import BaseTool

DESCRIPTOR = ToolDescriptor(
    name="calculator",
    description="Evaluate a basic arithmetic expression (numbers, + - * / ** % and parentheses).",
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "e.g. '2 + 2 * (10 - 4)'"}
        },
        "required": ["expression"],
    },
)

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class CalculatorTool(BaseTool):
    def __init__(self) -> None:
        super().__init__(DESCRIPTOR)

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        expression = str(arguments["expression"])
        try:
            tree = ast.parse(expression, mode="eval")
            value = self._eval(tree.body)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
            raise ValueError(f"cannot evaluate {expression!r}: {exc}") from exc
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)

    def _eval(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Expression):
            return self._eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](self._eval(node.left), self._eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](self._eval(node.operand))
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")


__all__ = ["CalculatorTool", "DESCRIPTOR"]
