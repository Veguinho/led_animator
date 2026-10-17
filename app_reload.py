"""Carry live controls across a single process reload without changing defaults."""

import json
import os


RELOAD_STATE_KEY = "LED_ANIMATOR_RELOAD_STATE"


def take_reload_state() -> dict:
    """Consume the old process's state once, before starting capture/decoding."""
    value = os.environ.pop(RELOAD_STATE_KEY, None)
    if value is None:
        return {}
    state = json.loads(value)
    if not isinstance(state, dict):
        raise ValueError("invalid app reload state")
    return state


def save_reload_state(palette: dict, video: dict | None = None, *, projectm: dict | None = None) -> None:
    state = {"palette": palette, "video": video}
    if projectm is not None:
        state["projectm"] = projectm
    os.environ[RELOAD_STATE_KEY] = json.dumps(state)
