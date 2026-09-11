"""Small request-boundary primitives; no application policy or provider dependency."""

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RoundControl:
    allowed_tools: frozenset[str]

    @classmethod
    def from_tools(cls, tools):
        return cls(frozenset(tool["name"] for tool in tools))

    def rejection(self, name):
        if name not in self.allowed_tools:
            return (
                "Error: Tool is not available this round. Use the current tool schemas."
            )
        return None


class CallBudget:
    """Absolute elapsed-time cap: incoming tokens do not reset the deadline."""

    def __init__(self, seconds, clock=time.monotonic):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("call budget must be positive and finite")
        self.clock = clock
        self.deadline = clock() + seconds

    def expired(self):
        return self.clock() >= self.deadline
