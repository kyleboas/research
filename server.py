#!/usr/bin/env python3
"""OAuth login server for ChatGPT subscription authentication.

Deploy as a Railway service (see railway.toml) so you can authenticate
from any browser by visiting your Railway URL at /login.

On Railway (RAILWAY_PUBLIC_DOMAIN is set):
  redirect_uri = https://<domain>/auth/callback
  After the user approves access OpenAI redirects directly back to the
  server's /auth/callback endpoint — no copy-paste required.

Locally (no RAILWAY_PUBLIC_DOMAIN):
  redirect_uri = http://127.0.0.1:1455/auth/callback (registered URI)
  The browser redirects to that localhost URL (which shows an error), and
  the user must paste the full redirect URL into the form at /login.

Usage (local):
    python server.py          # visit http://localhost:8080/login

Usage (Railway):
    Deploy as the 'auth' service — visit https://<your-domain>/login
"""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import chatgpt_auth

_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

# When RAILWAY_PUBLIC_DOMAIN is set the server can receive the OAuth callback
# directly, so use the Railway HTTPS URL as the redirect_uri.  Locally fall
# back to the registered localhost URI and ask the user to paste it manually.
if _domain:
    REDIRECT_URI = f"https://{_domain}/auth/callback"
else:
    REDIRECT_URI = chatgpt_auth.REDIRECT_URI  # http://127.0.0.1:1455/auth/callback

# ── In-memory PKCE state (single-user auth server) ───────────────────────────
_pending: dict = {}
_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            self._handle_login()
        elif path == "/auth/callback":
            self._handle_callback()
        else:
            self._respond(404, "text/plain", "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/auth/submit":
            self._handle_submit()
        else:
            self._respond(404, "text/plain", "Not found")

    # ── GET /login ────────────────────────────────────────────────────────────

    def _handle_login(self):
        verifier, challenge = chatgpt_auth._generate_pkce()
        state = chatgpt_auth._random_state()

        with _lock:
            _pending["verifier"] = verifier
            _pending["state"] = state

        params = urlencode({
            "client_id":             chatgpt_auth.CLIENT_ID,
            "redirect_uri":          REDIRECT_URI,
            "response_type":         "code",
            "scope":                 chatgpt_auth.SCOPES,
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
        auth_url = f"{chatgpt_auth.AUTHORIZE_URL}?{params}"

        if _domain:
            # Railway: OAuth will redirect back to /auth/callback automatically.
            body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>ChatGPT Login</title></head>
<body>
<h2>ChatGPT OAuth Login</h2>
<p>
  <a href="{auth_url}" target="_blank">Click here to authenticate with OpenAI</a>
</p>
<p>After you approve access, you will be redirected back here automatically.</p>
</body>
</html>""".encode()
        else:
            # Local: no server on 127.0.0.1:1455, ask user to paste the URL.
            body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>ChatGPT Login</title></head>
<body>
<h2>ChatGPT OAuth Login</h2>
<p>
  <a href="{auth_url}" target="_blank">Click here to authenticate with OpenAI</a>
</p>
<p>
  After you approve access, your browser will try to redirect to
  <code>http://127.0.0.1:1455/auth/callback?...</code> and show an error
  because that address is not reachable. That is expected.
  <strong>Copy the full URL from your browser's address bar</strong>
  and paste it below.
</p>
<form method="POST" action="/auth/submit">
  <label for="url">Redirect URL:</label><br>
  <input type="text" id="url" name="url" size="80"
         placeholder="http://127.0.0.1:1455/auth/callback?code=..."><br><br>
  <button type="submit">Submit</button>
</form>
</body>
</html>""".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── GET /auth/callback ────────────────────────────────────────────────────

    def _handle_callback(self):
        """Receive the OAuth redirect directly (Railway flow)."""
        params = parse_qs(urlparse(self.path).query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        error = (params.get("error") or [None])[0]

        if error:
            self._respond(400, "text/plain", f"OAuth error: {error}")
            return

        if not code:
            self._respond(400, "text/plain", "Missing authorization code in callback")
            return

        with _lock:
            expected_state = _pending.get("state")
            verifier = _pending.get("verifier")

        if state != expected_state:
            self._respond(400, "text/plain", "OAuth state mismatch — possible CSRF")
            return

        try:
            tokens = chatgpt_auth._exchange_code(code, verifier, REDIRECT_URI)
        except Exception as exc:
            self._respond(500, "text/plain", f"Token exchange failed: {exc}")
            return

        creds = {
            "access":    tokens["access_token"],
            "refresh":   tokens.get("refresh_token"),
            "expires":   time.time() + tokens["expires_in"],
            "accountId": chatgpt_auth._extract_account_id(tokens["access_token"]),
        }
        chatgpt_auth.save_credentials(creds)

        with _lock:
            _pending.clear()

        body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Authenticated</title></head>
<body>
<h2>Authentication successful!</h2>
<p>Authenticated as <code>{creds['accountId']}</code>.</p>
<p>Credentials saved. You can close this tab.</p>
</body>
</html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── POST /auth/submit ─────────────────────────────────────────────────────

    def _handle_submit(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        form = parse_qs(raw)
        redirect_url = (form.get("url") or [None])[0]

        if not redirect_url:
            self._respond(400, "text/plain", "Missing 'url' field")
            return

        with _lock:
            expected_state = _pending.get("state")
            verifier       = _pending.get("verifier")

        try:
            code = chatgpt_auth._parse_code_from_url(redirect_url, expected_state)
        except ValueError as exc:
            self._respond(400, "text/plain", f"Bad redirect URL: {exc}")
            return

        try:
            tokens = chatgpt_auth._exchange_code(code, verifier, REDIRECT_URI)
        except Exception as exc:
            self._respond(500, "text/plain", f"Token exchange failed: {exc}")
            return

        creds = {
            "access":    tokens["access_token"],
            "refresh":   tokens.get("refresh_token"),
            "expires":   time.time() + tokens["expires_in"],
            "accountId": chatgpt_auth._extract_account_id(tokens["access_token"]),
        }
        chatgpt_auth.save_credentials(creds)

        with _lock:
            _pending.clear()

        self._respond(
            200,
            "text/plain",
            f"Authenticated as {creds['accountId']}.\n"
            "Credentials saved. You can close this tab.",
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _respond(self, status, content_type, body):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        pass  # suppress per-request stdout noise


def main():
    port = int(os.environ.get("PORT", 8080))
    httpd = HTTPServer(("0.0.0.0", port), _Handler)
    base = f"https://{_domain}" if _domain else f"http://127.0.0.1:{port}"
    print(f"Auth server listening on port {port}")
    print(f"Visit {base}/login to authenticate with your ChatGPT subscription")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
