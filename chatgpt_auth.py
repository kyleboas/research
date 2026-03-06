"""ChatGPT OAuth (PKCE) authentication for OpenAI Codex subscriptions.

Flow:
1. Generate PKCE verifier/challenge + random state
2. Open https://auth.openai.com/oauth/authorize?...
3. Try to capture callback on http://127.0.0.1:1455/auth/callback
4. If callback port unavailable (remote/headless), prompt user to paste redirect URL
5. Exchange code at https://auth.openai.com/oauth/token
6. Extract accountId from the access token JWT and store credentials

Runtime token management:
- expires in the future → use stored access token
- expired → refresh under a file lock and overwrite stored credentials
"""

import base64
import fcntl
import hashlib
import http.server
import json
import logging
import os
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from db_conn import resolve_database_conninfo

log = logging.getLogger("chatgpt_auth")

# OAuth app registration for OpenAI Codex CLI
CLIENT_ID = "app_EMmlzZpjdHXp1aNBIkGGFMnO"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPES = "openid profile email offline_access"

# Credentials stored at ~/.codex/auth.json (matches Codex CLI convention)
CREDS_PATH = Path.home() / ".codex" / "auth.json"


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _generate_pkce():
    """Return (verifier, challenge) using S256 method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _random_state():
    return secrets.token_urlsafe(16)


# ── Credential storage ────────────────────────────────────────────────────────

_DB_KEY = "chatgpt"
_DB_LOCK_ID = 20250306  # arbitrary stable advisory-lock id


def _db_url():
    conninfo, _ = resolve_database_conninfo()
    return conninfo


def _row_to_creds(row):
    return {"access": row[0], "refresh": row[1], "expires": row[2], "accountId": row[3]}


def load_credentials():
    """Return stored credentials dict or None.

    Reads from the pgvector DB when DATABASE_URL is set; falls back to
    ~/.codex/auth.json for local development.
    """
    url = _db_url()
    if url:
        try:
            import psycopg
            with psycopg.connect(url) as conn:
                row = conn.execute(
                    "SELECT access_token, refresh_token, expires_at, account_id "
                    "FROM auth_tokens WHERE key = %s",
                    (_DB_KEY,),
                ).fetchone()
                if row:
                    return _row_to_creds(row)
        except Exception as exc:
            log.warning("DB credential load failed: %s", exc)
    # local fallback
    if CREDS_PATH.exists():
        try:
            return json.loads(CREDS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_credentials(creds):
    """Persist credentials to DB (when DATABASE_URL is set) or disk."""
    url = _db_url()
    if url:
        try:
            import psycopg
            with psycopg.connect(url) as conn:
                conn.execute(
                    """
                    INSERT INTO auth_tokens (key, access_token, refresh_token, expires_at, account_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        expires_at    = EXCLUDED.expires_at,
                        account_id    = EXCLUDED.account_id,
                        updated_at    = NOW()
                    """,
                    (_DB_KEY, creds["access"], creds["refresh"], creds["expires"], creds["accountId"]),
                )
                conn.commit()
            return
        except Exception as exc:
            log.warning("DB credential save failed, falling back to file: %s", exc)
    # local fallback
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDS_PATH.write_text(json.dumps(creds, indent=2))
    CREDS_PATH.chmod(0o600)


# ── JWT payload decode (no signature verification needed) ─────────────────────

def _extract_account_id(access_token):
    """Extract the 'sub' claim from the JWT payload as accountId."""
    try:
        parts = access_token.split(".")
        payload = parts[1]
        # Pad to a valid base64 length
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub")
    except Exception:
        return None


# ── Callback capture ──────────────────────────────────────────────────────────

def _try_local_callback(expected_state):
    """Try to bind on port 1455 and wait for the OAuth redirect.

    Returns the authorization code if captured, or None if the port
    is unavailable (remote/headless environment).
    """
    captured = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            captured["code"] = params.get("code", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            body = b"<h1>Authentication complete. You can close this tab.</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # suppress server logs
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", 1455), _Handler)
        server.timeout = 120  # 2-minute window
        server.handle_request()
        server.server_close()
    except OSError:
        return None  # port unavailable

    if captured.get("state") != expected_state:
        raise ValueError("OAuth state mismatch — possible CSRF")
    return captured.get("code")


def _parse_code_from_url(redirect_url, expected_state):
    """Extract authorization code from a pasted redirect URL."""
    params = parse_qs(urlparse(redirect_url).query)
    state = params.get("state", [None])[0]
    if state != expected_state:
        raise ValueError("OAuth state mismatch — possible CSRF")
    code = params.get("code", [None])[0]
    if not code:
        raise ValueError("No 'code' parameter found in redirect URL")
    return code


# ── Token exchange & refresh ──────────────────────────────────────────────────

def _post_token(payload):
    """POST form-encoded payload to TOKEN_URL and return parsed JSON."""
    data = urlencode(payload).encode()
    req = Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req) as resp:
        return json.loads(resp.read())


def _exchange_code(code, verifier, redirect_uri=None):
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri or REDIRECT_URI,
        "code_verifier": verifier,
    })


def _refresh(creds):
    """Refresh tokens under an exclusive lock.

    Uses a PostgreSQL advisory lock when DATABASE_URL is set so all Railway
    services coordinate without a shared filesystem.  Falls back to an
    exclusive file lock for local development.
    """
    url = _db_url()
    if url:
        import psycopg
        with psycopg.connect(url) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_DB_LOCK_ID,))
            try:
                # Another service may have refreshed while we were waiting
                row = conn.execute(
                    "SELECT access_token, refresh_token, expires_at, account_id "
                    "FROM auth_tokens WHERE key = %s",
                    (_DB_KEY,),
                ).fetchone()
                if row and row[2] > time.time() + 60:
                    return _row_to_creds(row)

                tokens = _post_token({
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": creds["refresh"],
                })
                new_creds = {
                    "access": tokens["access_token"],
                    "refresh": tokens.get("refresh_token", creds["refresh"]),
                    "expires": time.time() + tokens["expires_in"],
                    "accountId": creds["accountId"],
                }
                conn.execute(
                    """
                    INSERT INTO auth_tokens (key, access_token, refresh_token, expires_at, account_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        expires_at    = EXCLUDED.expires_at,
                        account_id    = EXCLUDED.account_id,
                        updated_at    = NOW()
                    """,
                    (_DB_KEY, new_creds["access"], new_creds["refresh"], new_creds["expires"], new_creds["accountId"]),
                )
                conn.commit()
                log.info("ChatGPT token refreshed (accountId=%s)", new_creds["accountId"])
                return new_creds
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_DB_LOCK_ID,))
    else:
        # local fallback: exclusive file lock
        lock_path = CREDS_PATH.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                current = load_credentials()
                if current and current.get("expires", 0) > time.time() + 60:
                    return current

                tokens = _post_token({
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": creds["refresh"],
                })
                new_creds = {
                    "access": tokens["access_token"],
                    "refresh": tokens.get("refresh_token", creds["refresh"]),
                    "expires": time.time() + tokens["expires_in"],
                    "accountId": creds["accountId"],
                }
                save_credentials(new_creds)
                log.info("ChatGPT token refreshed (accountId=%s)", new_creds["accountId"])
                return new_creds
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


# ── Public API ────────────────────────────────────────────────────────────────

def get_access_token():
    """Return a valid access token, refreshing automatically if expired.

    Raises RuntimeError if no credentials are stored (run `login()` first).
    """
    creds = load_credentials()
    if not creds:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        hint = f"https://{domain}/login" if domain else "python main.py --login"
        raise RuntimeError(
            f"No ChatGPT credentials found. Authenticate via: {hint}"
        )

    if creds.get("expires", 0) > time.time() + 60:
        return creds["access"]  # still valid

    log.info("ChatGPT access token expired, refreshing...")
    return _refresh(creds)["access"]


def login():
    """Interactive PKCE login flow.

    Opens the browser (or prints the URL for headless environments),
    waits for the callback, exchanges the code, and persists credentials.
    """
    verifier, challenge = _generate_pkce()
    state = _random_state()

    params = urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{AUTHORIZE_URL}?{params}"

    print("\n=== ChatGPT OAuth Login ===")
    print("Opening browser for authentication...")
    print(f"\nIf the browser does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Try to capture callback automatically
    code = _try_local_callback(state)

    if code is None:
        # Headless / remote: ask user to paste the redirect URL
        print("Could not bind to port 1455 (remote or headless environment).")
        print("After authenticating in the browser, paste the full redirect URL here.")
        redirect_url = input("Redirect URL: ").strip()
        code = _parse_code_from_url(redirect_url, state)

    print("Exchanging authorization code for tokens...")
    tokens = _exchange_code(code, verifier)

    creds = {
        "access": tokens["access_token"],
        "refresh": tokens.get("refresh_token"),
        "expires": time.time() + tokens["expires_in"],
        "accountId": _extract_account_id(tokens["access_token"]),
    }
    save_credentials(creds)
    print(f"Logged in as accountId={creds['accountId']}")
    print(f"Credentials saved to {CREDS_PATH}")
    return creds
