"""Queue board resets for the thread that owns the serial connection."""

from collections.abc import Callable
import threading


class BoardResetControls:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "idle"
        self._error: str | None = None
        self._revision = 0

    def state(self) -> dict:
        with self._lock:
            return {"status": self._status, "error": self._error, "revision": self._revision}

    def request(self) -> dict:
        with self._lock:
            if self._status not in ("pending", "resetting"):
                self._status, self._error = "pending", None
                self._revision += 1
            return {"status": self._status, "error": self._error, "revision": self._revision}

    def process(self, reset: Callable[[], None]) -> bool:
        with self._lock:
            if self._status != "pending":
                return False
            self._status = "resetting"
        try:
            reset()
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._status, self._error = "error", str(exc)
        else:
            with self._lock:
                self._status = "ready"
        return True
