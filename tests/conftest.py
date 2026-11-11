"""Load the plugin directory as a package, mirroring Hermes' directory-plugin loader."""

import importlib.util
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_as_hermes_plugin(name="hermes_plugins_test.k6pane"):
    """Import the repo root exactly like Hermes' directory-plugin loader does."""
    parent = name.rsplit(".", 1)[0]
    if parent not in sys.modules:
        ns = types.ModuleType(parent)
        ns.__path__ = []
        sys.modules[parent] = ns
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes_loader():
    return load_as_hermes_plugin


@pytest.fixture(scope="session")
def plugin():
    import k6pane

    return k6pane


@pytest.fixture
def k6home(tmp_path, monkeypatch):
    monkeypatch.setenv("K6PANE_HOME", str(tmp_path))
    monkeypatch.delenv("K6PANE_ALLOWED_HOSTS", raising=False)
    return tmp_path


SECURE = {
    "authorization": "Bearer s3cr3t-token-value-123",
    "x-api-key": "literal-key-abc123",
    "query_token": "literal-query-tok9",
    "password": "pw-literal-777",
}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b'{"ok":true}'):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _secure_ok(self, body=None):
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(self.path).query)
        ok = (self.headers.get("Authorization") == SECURE["authorization"]
              and self.headers.get("X-Api-Key") == SECURE["x-api-key"]
              and q.get("token", [""])[0] == SECURE["query_token"])
        if body is not None:
            ok = ok and body.get("password") == SECURE["password"]
        return ok

    def do_GET(self):
        if self.path.startswith("/missing"):
            return self._send(404)
        if self.path.startswith("/secure"):
            return self._send(200 if self._secure_ok() else 401)
        return self._send(200)

    def do_POST(self):
        import json as _json

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path.startswith("/secure"):
            try:
                body = _json.loads(raw or b"{}")
            except ValueError:
                body = {}
            return self._send(200 if self._secure_ok(body) else 401)
        return self._send(404 if self.path.startswith("/missing") else 200)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def secure_values():
    return dict(SECURE)
