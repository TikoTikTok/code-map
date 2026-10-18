"""Utilities for the fixture app."""
from __future__ import annotations

CONST = 1


def helper(x):
    """Add one to x."""
    return x + 1


class Greeter:
    """Greets people."""

    def __init__(self, name):
        self.name = name

    def greet(self):
        """Return the greeting."""
        return "hi " + self.name + str(helper(CONST))
