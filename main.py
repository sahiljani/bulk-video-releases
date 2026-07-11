#!/usr/bin/env python3
"""
Google OAuth test app — server-side Authorization Code flow (Flask).

Milestone 1: click "Sign in with Google", complete the real Google login,
and see your profile. This is the seam that browser auto-login drives later.

Run:
  app/.venv/Scripts/python.exe app/main.py
Then open http://localhost:5000
"""

import os
import secrets
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for

import registry
from store import upsert_account

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Google endpoints ──
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# ── Config from .env ──
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
SCOPES = "openid email profile"

app = Flask(__name__)
# Session secret — random per process is fine for a local test app.
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)


@app.route("/")
def index():
    user = session.get("user")
    token = session.get("token")
    if user:
        return render_template("profile.html", user=user, token=token)
    return render_template("index.html", configured=bool(CLIENT_ID and CLIENT_SECRET))


@app.route("/login")
def login():
    """Build Google's authorization URL and redirect there."""
    if not (CLIENT_ID and CLIENT_SECRET):
        return "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in app/.env", 500

    # CSRF protection: random state validated at /callback via the registry.
    state = secrets.token_urlsafe(24)
    registry.register(state)
    session["oauth_state"] = state  # kept for continuity; not the validation source

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",   # ask for a refresh_token (returned on first consent)
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@app.route("/health")
def health():
    return {"ok": True}


@app.route("/api/login-url", methods=["POST"])
def api_login_url():
    """Driver calls this per account to get an OAuth URL + tracked state."""
    if not (CLIENT_ID and CLIENT_SECRET):
        return {"error": "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in app/.env"}, 500

    body = request.get_json(silent=True) or {}
    test_flag = bool(body.get("test") or request.form.get("test"))

    state = secrets.token_urlsafe(24)
    registry.register(state, test=test_flag)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",   # refresh_token returned on first consent only
    }
    return {"oauth_url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}", "state": state}


@app.route("/api/login-status")
def api_login_status():
    """Driver polls this with ?state=... to learn a login's result."""
    entry = registry.get(request.args.get("state", ""))
    if entry is None:
        return {"status": "unknown"}, 404
    return {"status": entry["status"], "email": entry["email"], "error": entry["error"]}


@app.route("/callback")
def callback():
    """Google redirects here with ?code=... — exchange it for tokens.

    State is validated against the server-side registry (not the session
    cookie) so a separate CloakBrowser process can complete the flow.
    """
    state = request.args.get("state", "")

    error = request.args.get("error")
    if error:
        if registry.is_pending(state):
            registry.mark_error(state, error)
        return f"Google returned an error: {error}", 400

    if not registry.is_pending(state):
        return "Invalid or expired OAuth state.", 400

    code = request.args.get("code")
    if not code:
        registry.mark_error(state, "no_authorization_code")
        return "No authorization code returned.", 400

    # Exchange the authorization code for tokens.
    token_resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not token_resp.ok:
        registry.mark_error(state, f"token_exchange_{token_resp.status_code}")
        return f"Token exchange failed: {token_resp.status_code} {token_resp.text}", 400

    token = token_resp.json()

    # Fetch the user's profile with the access token.
    userinfo_resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {token['access_token']}"},
        timeout=30,
    )
    if not userinfo_resp.ok:
        registry.mark_error(state, f"userinfo_{userinfo_resp.status_code}")
        return f"Userinfo fetch failed: {userinfo_resp.status_code} {userinfo_resp.text}", 400

    user = userinfo_resp.json()
    email = user.get("email", "")

    # Durable record for batch runs, plus mark the registry for the driver poll.
    # Test-mode logins (see /api/login-url) are validated end-to-end but must
    # not touch tokens.json.
    entry = registry.get(state)
    if not (entry and entry.get("test")):
        upsert_account(email, user, token)
    registry.mark_ok(state, email, user)

    # Keep the human flow working: this browser's session shows the profile.
    session["user"] = user
    session["token"] = token
    session.pop("oauth_state", None)
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    # host=localhost keeps the redirect URI (http://localhost:5000/callback) valid.
    app.run(host="localhost", port=5000, debug=True)
