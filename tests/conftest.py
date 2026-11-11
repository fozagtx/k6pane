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
