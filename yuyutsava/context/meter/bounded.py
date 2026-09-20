"""A dict that forgets its oldest key.

Every per-thread map in the meter uses one. A daemon serves conversations
indefinitely, so an unbounded dict keyed by thread is a slow leak that only
shows up on a long-lived process.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

#: How many threads' worth of state to retain. A daemon serves conversations
#: indefinitely; unbounded per-thread dicts are a slow leak.
MAX_THREADS = 64


class Bounded(OrderedDict):
    """Insertion-ordered dict that evicts its oldest key past ``MAX_THREADS``."""

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > MAX_THREADS:
            self.popitem(last=False)


__all__ = ["MAX_THREADS", "Bounded"]
