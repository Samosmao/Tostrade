"""XAU Signal MVP: Flask API, SQLite/Postgres accounts, and a static dashboard.

Run: python app.py
This is a development starter, not a production-hardened trading system.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import market
import storage

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("XAU_DB_PATH", str(ROOT / "data" / "app.sqlite3")))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if os.environ.get("RENDER", "").lower() == "true" and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required on Render; local files are ephemeral.")
API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
CACHE_SECONDS = max(30, int(os.environ.get("MARKET_CACHE_SECONDS", "120")))

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=os.environ.get("APP_SECRET") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=16 * 1024,
)


def init_db() -> None:
    storage.init_users(DB_PATH, DATABASE_URL)


init_db()
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_provider_errors: dict[tuple[str, str], tuple[float, str]] = {}
_cache_lock = threading.Lock()
_attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_attempt_lock = threading.Lock()


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def public_user() -> dict | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    row = storage.find_by_id(DB_PATH, DATABASE_URL, user_id)
    if not row:
        session.pop("user_id", None)
        return None
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def reset_session(user_id: int | None = None) -> str:
    session.clear()
    if user_id is not None:
        session["user_id"] = user_id
        session.permanent = True
    return csrf_token()


def within_limit(kind: str, limit: int, window: int = 600) -> bool:
    # Simple per-process development throttle, not a substitute for a shared
    # production rate limiter, especially behind a proxy or multiple workers.
    key = (request.remote_addr or "unknown", kind)
    now = time.monotonic()
    with _attempt_lock:
        times = _attempts[key]
        while times and times[0] < now - window:
            times.popleft()
        if len(times) >= limit:
            return False
        times.append(now)
        return True


@app.before_request
def check_csrf():
    if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        given = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(str(given), str(expected)):
            return jsonify({"error": "Your session expired. Refresh the page and try again."}), 403
    return None


@app.after_request
def secure_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/session")
def get_session():
    return jsonify({
        "user": public_user(),
        "csrf": csrf_token(),
        "providerConfigured": bool(API_KEY),
        "cacheSeconds": CACHE_SECONDS,
    })


def read_fields() -> dict:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def error(message: str, code: int):
    return jsonify({"error": message}), code


@app.post("/api/auth/register")
def register():
    if not within_limit("register", 12):
        return error("Too many attempts. Please try again in 10 minutes.", 429)
    data = read_fields()
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().casefold()
    password = data.get("password", "")
    if not 2 <= len(name) <= 60:
        return error("Please enter a name between 2 and 60 characters.", 400)
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return error("Please enter a valid email address.", 400)
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return error("Your password must be between 8 and 128 characters.", 400)
    hashed = generate_password_hash(password, method="scrypt")
    try:
        user_id = storage.create_user(DB_PATH, DATABASE_URL, name, email, hashed)
    except storage.DuplicateUser:
        return error("An account with this email already exists. Try signing in.", 409)
    token = reset_session(user_id)
    return jsonify({"user": {"id": user_id, "name": name, "email": email}, "csrf": token}), 201


@app.post("/api/auth/login")
def login():
    if not within_limit("login", 8):
        return error("Too many attempts. Please try again in 10 minutes.", 429)
    data = read_fields()
    email = str(data.get("email", "")).strip().casefold()
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) > 128:
        return error("Email or password is incorrect.", 401)
    row = storage.find_by_email(DB_PATH, DATABASE_URL, email)
    correct = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
    if not row or not correct:
        return error("Email or password is incorrect.", 401)
    token = reset_session(row["id"])
    return jsonify({"user": {"id": row["id"], "name": row["name"], "email": row["email"]}, "csrf": token})


@app.post("/api/auth/logout")
def logout():
    token = reset_session()
    return jsonify({"ok": True, "csrf": token})


def get_snapshot(interval: str, use_demo: bool) -> dict:
    mode = "demo" if use_demo else "provider"
    key = (mode, interval)
    with _cache_lock:
        now = time.time()
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            if use_demo:
                return cached[1]
            # The forming candle in a cached response is a past observation,
            # never promote it to closed without a fresh provider response.
            closed = [bar for bar in cached[1]["candles"] if bar.get("closed", True)]
            snapshot = market.analyze(closed, interval, mode, now=now)
            forming = next((bar for bar in cached[1]["candles"] if bar.get("closed") is False), None)
            if forming and market._parse_timestamp(forming["time"]) + market.INTERVALS[interval][1] > now:
                market.append_forming_candle(snapshot, forming)
            snapshot["source"]["retrieved_at"] = cached[1]["source"]["retrieved_at"]
            snapshot["source"]["poll_seconds"] = CACHE_SECONDS
            return snapshot
        if use_demo:
            candles = market.demo_candles(interval, now=now)
            forming = None
        else:
            # Briefly cache failures too, so many viewers do not hammer an
            # exhausted or unreachable provider with identical retries.
            cached_error = _provider_errors.get(key)
            if cached_error and now - cached_error[0] < min(CACHE_SECONDS, 60):
                raise market.MarketDataError(cached_error[1])
            try:
                provider_bars = market.twelve_data_candles(interval, API_KEY, now=now, include_open=True)
            except market.MarketDataError as exc:
                _provider_errors[key] = (now, str(exc))
                raise
            _provider_errors.pop(key, None)
            candles = [bar for bar in provider_bars if bar.get("closed", True)]
            forming = next((bar for bar in provider_bars if bar.get("closed") is False), None)
        snapshot = market.analyze(candles, interval, mode, now=now)
        if forming:
            market.append_forming_candle(snapshot, forming)
        snapshot["source"]["poll_seconds"] = CACHE_SECONDS
        _cache[key] = (now, snapshot)
        return snapshot


def market_response(use_demo: bool):
    interval = request.args.get("interval", "1m")
    if interval not in market.INTERVALS:
        return error("Choose 1m, 5m, 15m, 1h, or 4h.", 400)
    try:
        return jsonify(get_snapshot(interval, use_demo))
    except market.MarketDataError as exc:
        return error(str(exc), 503)


@app.get("/api/demo-market")
def demo_market():
    # Safe public preview, even when the server has a real provider key.
    return market_response(use_demo=True)


@app.get("/api/market")
def member_market():
    if public_user() is None:
        return error("Sign in to access member market data.", 401)
    # Until a key is configured, members receive the explicitly labelled demo.
    return market_response(use_demo=not bool(API_KEY))


if __name__ == "__main__":
    if not os.environ.get("APP_SECRET"):
        print("APP_SECRET is not set; using an ephemeral development secret (sessions reset on restart).", flush=True)
    if not API_KEY:
        print("TWELVE_DATA_API_KEY is not set; demo data only.", flush=True)
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
