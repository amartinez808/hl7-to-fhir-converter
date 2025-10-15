"""Simple workflow agent to orchestrate multi-step operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["Agent"]

Step = Callable[[dict[str, Any]], dict[str, Any] | None]


class Agent:
    """Execute a list of step callables, passing a mutable context dict."""

    def __init__(self, steps: list[Step]):
        self.steps = steps

    def run(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx: dict[str, Any] = context or {}
        for step in self.steps:
            result = step(ctx)
            if isinstance(result, dict):
                ctx.update(result)
        return ctx
