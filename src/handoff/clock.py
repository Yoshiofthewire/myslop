"""The single source of current time, so tests can control it."""

import time


def now() -> int:
    """Current Unix time in whole seconds."""
    return int(time.time())
