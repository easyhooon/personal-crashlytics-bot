"""User-run, loopback Pub/Sub OAuth consent into the personal login Keychain."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPE = 'https://www.googleapis.com/auth/pubsub'


def scope_set(scopes):
    """Normalize Google's space-delimited grant or a sequence of scopes."""
    if isinstance(scopes, str):
        return set(scopes.split())
    return set(scopes or [])


def authorization_response_url(request_target, port, expected_state):
    """Adapt only our loopback callback URI for OAuthlib's HTTPS parser."""
    path = urlparse(request_target)
    if (path.scheme or path.netloc or path.fragment
            or path.path != '/oauth2callback'
            or parse_qs(path.query).get('state') != [expected_state]):
        raise ValueError('invalid OAuth loopback callback')
    # Match InstalledAppFlow.run_local_server: the browser reaches local HTTP,
    # while OAuthlib parses a virtual HTTPS URI; no HTTPS request is made here.
    return f'https://127.0.0.1:{port}/oauth2callback?{path.query}'


def desktop_client(project):
    matches = []
    for path in (Path.home() / 'Downloads').glob('*.json'):
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        installed = config.get('installed', {}) if isinstance(config, dict) else {}
        if (installed.get('project_id') == project
                and path.stat().st_mode & 0o777 == 0o600):
            matches.append(config)
    if len(matches) != 1:
        raise RuntimeError('expected one owner-only personal Desktop OAuth client')
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    flow = InstalledAppFlow.from_client_config(
        desktop_client(args.project), scopes=[SCOPE])
    complete = False
    outcome = 'authorization_incomplete'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass  # Callback URLs contain one-time authorization codes.

        def do_GET(self):
            nonlocal complete, outcome
            path = urlparse(self.path)
            if path.path == '/':
                self.send_response(302)
                self.send_header('Location', consent_url)
                self.end_headers()
                return
            if path.path == '/oauth2callback':
                try:
                    response_url = authorization_response_url(
                        self.path, server.server_port, state)
                    flow.fetch_token(authorization_response=response_url)
                    credentials = flow.credentials
                    if (scope_set(credentials.scopes) != {SCOPE}
                            or scope_set(credentials.granted_scopes) != {SCOPE}
                            or not credentials.refresh_token):
                        raise ValueError('Pub/Sub scope or refresh token missing')
                    writer = Path(__file__).with_name('store_pubsub_oauth.swift')
                    result = subprocess.run(
                        ['/usr/bin/swift', '-module-cache-path',
                         str(Path(tempfile.gettempdir()) / 'personal-crashlytics-swift-cache'),
                         str(writer)],
                        input=credentials.to_json().encode(), capture_output=True,
                    )
                    if result.returncode:
                        raise RuntimeError('login Keychain save denied')
                    outcome = 'pubsub_oauth_keychain_saved'
                    self.send_response(302)
                    self.send_header('Location', '/done')
                except Exception as error:
                    outcome = f'authorization_failed: {type(error).__name__}'
                    self.send_response(302)
                    self.send_header('Location', '/failed')
                complete = True
                self.end_headers()
                return
            if path.path in ('/done', '/failed'):
                text = ('Pub/Sub OAuth saved. You may close this tab.'
                        if path.path == '/done' else
                        'Authorization failed. Check the Terminal status.')
                body = text.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        flow.redirect_uri = f'http://127.0.0.1:{server.server_port}/oauth2callback'
        consent_url, state = flow.authorization_url(
            access_type='offline', prompt='consent', include_granted_scopes='false')
        print(f'Open in the already signed-in PERSONAL Chrome profile: '
              f'http://127.0.0.1:{server.server_port}/', flush=True)
        server.timeout = 1
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not complete:
            server.handle_request()
        # Serve the final clean URL after the OAuth callback redirect.
        if complete:
            server.handle_request()
    print(outcome)
    return 0 if outcome == 'pubsub_oauth_keychain_saved' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'authorization_failed: {type(error).__name__}')
        raise SystemExit(1)
