"""Load the plugin directory as a package, mirroring Hermes' directory-plugin loader."""

import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG = "k6pane_under_test"


def _load():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin():
    return _load()


@pytest.fixture
def k6home(tmp_path, monkeypatch):
    monkeypatch.setenv("K6PANE_HOME", str(tmp_path))
    monkeypatch.delenv("K6PANE_ALLOWED_HOSTS", raising=False)
    return tmp_path


class _Handler(BaseHTTPRequestHandler):
    def _reply(self):
        if self.path.startswith("/missing"):
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_GET = _reply

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
