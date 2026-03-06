#!/usr/bin/env python3
"""OAuth login server for ChatGPT subscription authentication.

Deploy as a Railway service (see railway.toml) so you can authenticate
from any browser by visiting your Railway URL at /login.

The OAuth client ID (app_EMmlzZpjdHXp1aNBIkGGFMnO) is the OpenAI Codex CLI
app, which only has http://127.0.0.1:1455/auth/callback registered as a valid
redirect URI.  We must always send that exact URI to OpenAI; after the user
approves access their browser is redirected to that localhost address (which
shows a browser error) and the user copies the full URL from the address bar
and pastes it into the form at /login.

This copy-paste flow works the same whether the server is running locally or
on Railway.

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

# ── In-memory PKCE state (single-user auth server) ───────────────────────────
_pending: dict = {}
_lock = threading.Lock()


def _is_local_host(host: str) -> bool:
    """Return True when the Host header looks like a local address."""
    bare = host.split(":")[0]
    return bare in ("127.0.0.1", "localhost", "0.0.0.0")


class _Handler(BaseHTTPRequestHandler):

    def _redirect_uri(self) -> str:
        """Return the registered OAuth redirect_uri.

        The client ID (app_EMmlzZpjdHXp1aNBIkGGFMnO) is the OpenAI Codex CLI
        app, which only has http://127.0.0.1:1455/auth/callback registered.
        We must always send that exact URI to OpenAI regardless of where this
        server is running; the user pastes the resulting callback URL back in.
        """
        return chatgpt_auth.REDIRECT_URI

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
        redirect_uri = self._redirect_uri()

        with _lock:
            _pending["verifier"] = verifier
            _pending["state"] = state
            _pending["redirect_uri"] = redirect_uri

        params = urlencode({
            "client_id":             chatgpt_auth.CLIENT_ID,
            "redirect_uri":          redirect_uri,
            "response_type":         "code",
            "scope":                 chatgpt_auth.SCOPES,
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
        auth_url = f"{chatgpt_auth.AUTHORIZE_URL}?{params}"

        # The registered redirect_uri is always localhost, so OpenAI will never
        # redirect back to this server directly.  Ask the user to copy the
        # callback URL from their browser's address bar and paste it below.
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
        """Receive the OAuth redirect directly (remote/Railway flow)."""
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
            redirect_uri = _pending.get("redirect_uri")

        if state != expected_state:
            self._respond(400, "text/plain", "OAuth state mismatch — possible CSRF")
            return

        try:
            tokens = chatgpt_auth._exchange_code(code, verifier, redirect_uri)
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
            redirect_uri   = _pending.get("redirect_uri")

        try:
            code = chatgpt_auth._parse_code_from_url(redirect_url, expected_state)
        except ValueError as exc:
            self._respond(400, "text/plain", f"Bad redirect URL: {exc}")
            return

        try:
            tokens = chatgpt_auth._exchange_code(code, verifier, redirect_uri)
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
    print(f"Auth server listening on port {port}")
    print(f"Visit /login to authenticate with your ChatGPT subscription")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
