"""A LangGraph host the watcher can be driven against, with no network.

The watcher's whole job is reconciling what a remote host says against what the
mirror believes, so a fake host is not a convenience — it is the only way to
reach the paths that matter: a run that reaches an interrupt boundary, a thread
wedged by an orphaned tool call, a run that errors with no detail anywhere.

Deliberately dumb. Every method returns what it was told to return, so a failing
test is a statement about the watcher.
"""

from __future__ import annotations

from typing import Any


class FakeThreads:
    def __init__(self, host: "FakeHost") -> None:
        self._h = host

    async def search(self, limit: int = 50) -> list[dict]:
        self._h.calls.append(("threads.search", limit))
        if self._h.fail_on == "threads.search":
            raise RuntimeError("search exploded")
        return [{"thread_id": tid} for tid in self._h.thread_ids]

    async def get(self, thread_id: str) -> dict:
        self._h.calls.append(("threads.get", thread_id))
        if self._h.fail_on == "threads.get":
            raise RuntimeError("get exploded")
        return {
            "thread_id": thread_id,
            "status": self._h.thread_status,
            "values": {"messages": list(self._h.messages)},
            "interrupts": self._h.interrupts,
            "metadata": {},
        }

    async def update_state(self, thread_id: str, values: Any) -> None:
        self._h.calls.append(("threads.update_state", thread_id))
        if self._h.fail_on == "threads.update_state":
            raise RuntimeError("update_state exploded")
        self._h.patched.append(values)


class FakeRuns:
    def __init__(self, host: "FakeHost") -> None:
        self._h = host

    async def list(self, thread_id: str, limit: int = 1) -> list[dict]:
        self._h.calls.append(("runs.list", thread_id))
        if self._h.fail_on == "runs.list":
            raise RuntimeError("list exploded")
        return [
            {
                "run_id": self._h.run_id,
                "status": self._h.run_status,
                "assistant_id": self._h.assistant_id,
                **({"error": self._h.run_error} if self._h.run_error else {}),
            }
        ]

    async def get(self, thread_id: str, run_id: str) -> dict:
        self._h.calls.append(("runs.get", run_id))
        if self._h.fail_on == "runs.get":
            raise RuntimeError("run get exploded")
        return {"run_id": run_id, "status": self._h.run_status, "error": self._h.full_run_error}

    async def cancel(self, thread_id: str, run_id: str) -> None:
        self._h.calls.append(("runs.cancel", run_id))
        if self._h.fail_on == "runs.cancel":
            raise RuntimeError("cancel exploded")
        self._h.cancelled.append(run_id)

    async def create(self, **kwargs: Any) -> dict:
        self._h.calls.append(("runs.create", kwargs.get("thread_id")))
        if self._h.fail_on == "runs.create":
            raise RuntimeError("resume exploded")
        self._h.resumed.append(kwargs)
        return {"run_id": "r-resumed"}


class FakeHost:
    """One host, one thread, whatever state a test needs it in."""

    def __init__(self) -> None:
        self.threads = FakeThreads(self)
        self.runs = FakeRuns(self)
        self.thread_ids: list[str] = ["th-1"]
        self.run_id = "r1"
        self.run_status = "running"
        self.assistant_id = "file-organizer"
        self.run_error: Any = None
        self.full_run_error: Any = None
        self.thread_status = ""
        self.messages: list[dict] = [{"role": "assistant", "content": "done the thing"}]
        self.interrupts: dict[str, list[dict]] = {}
        #: Name of a method that raises instead of working.
        self.fail_on: str | None = None
        # What the watcher did to us.
        self.calls: list[tuple] = []
        self.resumed: list[dict] = []
        self.cancelled: list[str] = []
        self.patched: list[Any] = []

    def interrupt_with(self, *values: Any, ids: tuple[str, ...] = ()) -> None:
        """Park the thread on one or more interrupts."""
        self.run_status = "success"  # a run that reaches an interrupt ends cleanly
        self.thread_status = "interrupted"
        ids = ids or tuple(f"int-{i}" for i in range(len(values)))
        self.interrupts = {"node": [{"id": i, "value": v} for i, v in zip(ids, values)]}

    def called(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)
