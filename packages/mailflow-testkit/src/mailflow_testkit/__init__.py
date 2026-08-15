"""Reusable fake components for tests and local demos."""

from mailflow_testkit.fakes import (
    FakeLLMBackend,
    FakeMailSource,
    FakeNotifier,
    fixed_timestamps,
    make_mail,
)

__all__ = [
    "FakeLLMBackend",
    "FakeMailSource",
    "FakeNotifier",
    "fixed_timestamps",
    "make_mail",
]
