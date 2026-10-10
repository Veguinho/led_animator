"""Queue board resets for the thread that owns the serial connection."""

from collections.abc import Callable
import threading


class BoardResetControls:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "idle"
        self._error: str | None = None
        self._revision = 0
        self._on_complete: Callable[[], None] | None = None

    def state(self) -> dict:
        with self._lock:
            return {"status": self._status, "error": self._error, "revision": self._revision}

    def request(self, *, on_complete: Callable[[], None] | None = None) -> dict:
        with self._lock:
            if self._status not in ("pending", "resetting"):
                self._status, self._error = "pending", None
                self._revision += 1
            if on_complete is not None:
                self._on_complete = on_complete
            return {"status": self._status, "error": self._error, "revision": self._revision}

    def process(self, reset: Callable[[], None]) -> bool:
        with self._lock:
            if self._status != "pending":
                return False
            self._status = "resetting"
        try:
            reset()
            with self._lock:
                on_complete, self._on_complete = self._on_complete, None
            if on_complete is not None:
                on_complete()
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._status, self._error = "error", str(exc)
                self._on_complete = None
        else:
            with self._lock:
                self._status = "ready"
        return True
