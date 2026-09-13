"""Smooth frame transitions before LED encoding without adjusting their gain."""

import math
import time

import numpy as np


class BrightnessEnvelope:
    """Ease scene changes while preserving the user's selected brightness."""

    def __init__(self):
        self.previous_time = None
        self.smoothed = None

    def apply(self, frame: np.ndarray, now: float | None = None) -> np.ndarray:
        now = time.monotonic() if now is None else now
        dt = 0.05 if self.previous_time is None else max(0, min(now - self.previous_time, 0.1))
        self.previous_time = now
        values = np.nan_to_num(frame.astype(np.float32), nan=0, posinf=255, neginf=0).clip(0, 255)
        if self.smoothed is None:
            self.smoothed = np.zeros_like(values)
        self.smoothed += -math.expm1(-dt / 0.5) * (values - self.smoothed)
        return np.rint(self.smoothed.clip(0, 255)).astype(np.uint8)
