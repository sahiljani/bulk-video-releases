"""Persistent token store — Google accounts keyed by email in tokens.json."""

import json
import os
import threading
import time

TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

_lock = threading.Lock()


def load_tokens(path=TOKENS_FILE):
    if not os.path.exists(path):
        return {"accounts": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"accounts": []}
    if not isinstance(data, dict) or "accounts" not in data:
        return {"accounts": []}
    return data


def save_tokens(data, path=TOKENS_FILE):
    # Unique per-writer temp filename so concurrent callers (e.g. multiple
    # threads inside upsert_account's locked section, or any direct caller)
    # never clobber each other's in-flight temp file. NOTE: this function is
    # intentionally lock-free — upsert_account holds `_lock` for its whole
    # load->modify->save critical section and calls this from inside it;
    # locking here too would deadlock (threading.Lock is not reentrant).
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def upsert_account(email, profile, token, path=TOKENS_FILE):
    with _lock:
        data = load_tokens(path)
        accounts = data.setdefault("accounts", [])
        entry = {
            "email": email,
            "profile": profile,
            "token": token,
            "added_at": int(time.time()),
        }
        for i, a in enumerate(accounts):
            if a.get("email", "").lower() == email.lower():
                accounts[i] = entry
                break
        else:
            accounts.append(entry)
        save_tokens(data, path)
        return entry


def has_account(email, path=TOKENS_FILE):
    data = load_tokens(path)
    return any(a.get("email", "").lower() == email.lower() for a in data.get("accounts", []))
