"""Errors of the demo package (D3.1). Messages are fixed texts: never a passcode, a hash, a
key, a provider message or question text."""

from __future__ import annotations


class GuardRefused(Exception):
    """A request refused before any model call. `reason` is safe to show the visitor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PasskeyConfigError(Exception):
    """The stored passkey hash is malformed: the gate stays closed."""


class DemoCacheError(Exception):
    """The demo cache could not be built or does not replay exactly."""
