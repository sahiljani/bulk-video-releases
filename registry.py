"""In-memory OAuth login registry, keyed by `state`. Thread-safe.

Ephemeral per Flask process; the durable record of a successful login is
tokens.json (see store.py). This exists so a separate CloakBrowser process
can complete /callback without sharing the human flow's session cookie.
"""

import threading

_lock = threading.Lock()
_registry = {}  # state -> {"status", "email", "profile", "error"}


def register(state, test=False):
    with _lock:
        _registry[state] = {
            "status": "pending",
            "email": None,
            "profile": None,
            "error": None,
            "test": bool(test),
        }


def is_pending(state):
    with _lock:
        entry = _registry.get(state)
        return bool(entry) and entry["status"] == "pending"


def mark_ok(state, email, profile):
    with _lock:
        entry = _registry.get(state)
        if entry is None:
            return
        entry["status"] = "ok"
        entry["email"] = email
        entry["profile"] = profile


def mark_error(state, error):
    with _lock:
        entry = _registry.get(state)
        if entry is None:
            return
        entry["status"] = "error"
        entry["error"] = error


def get(state):
    with _lock:
        entry = _registry.get(state)
        return dict(entry) if entry else None


def clear():
    with _lock:
        _registry.clear()
