"""Phase 4.2/4.3: the k6 IP fence blocks redirects and runtime-built URLs to unvalidated IPs."""

import json
import os
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from k6pane import runner, tools


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


@pytest.fixture
def two_address_server():
    """A server on 0.0.0.0 reachable both via 127.0.0.1 and the machine's LAN IP."""
    if not runner.find_k6():
        if os.environ.get("K6PANE_REQUIRE_K6"):
            pytest.fail("k6 required")
        pytest.skip("k6 not installed")
    lan = _lan_ip()
    if not lan:
        pytest.skip("no non-loopback IPv4 address on this machine")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/r":
                self.send_response(302)
                self.send_header("Location", f"http://{lan}:{port}/landed")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), H)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://{lan}:{port}/landed", timeout=3).read()
    except OSError:
        server.shutdown()
        pytest.skip("LAN address not reachable locally")
    yield lan, port
    server.shutdown()


def test_redirect_to_unvalidated_ip_is_blocked(k6home, two_address_server):
    lan, port = two_address_server
    out = json.loads(tools.run({"url": f"http://127.0.0.1:{port}/r", "profile": "constant", "vus": 1,
                                "duration": "2s", "think_time": 0.2}))
    assert out["allowed_ips"] == ["127.0.0.1"]
    assert out["verdict"] == "fail"
    assert out["summary"]["error_rate"] == 1
    log = (k6home / "runs" / out["run_id"] / "k6.log").read_text()
    assert "blacklisted" in log


def test_direct_lan_target_works_when_validated(k6home, two_address_server):
    lan, port = two_address_server
    out = json.loads(tools.run({"url": f"http://{lan}:{port}/landed", "profile": "constant", "vus": 1,
                                "duration": "2s", "think_time": 0.2}))
    assert out["verdict"] == "pass", out


def test_custom_script_runtime_url_is_blocked(k6home, two_address_server, tmp_path):
    lan, port = two_address_server
    script = tmp_path / "sneaky.js"
    # The LAN host is assembled at runtime, so URL extraction cannot see it.
    script.write_text(
        "import http from 'k6/http';\n"
        "export const options = { iterations: 3 };\n"
        "export default function () {\n"
        "  http.get(__ENV.BASE_URL + '/landed');\n"
        "  const r = http.get('ht' + 'tp://' + __ENV.SNEAKY + '/landed');\n"
        "  console.log('sneaky_status=' + r.status);\n"
        "}\n"
    )
    out = json.loads(tools.run({"script_path": str(script),
                                "env": {"BASE_URL": f"http://127.0.0.1:{port}", "SNEAKY": f"{lan}:{port}"}}))
    log = (k6home / "runs" / out["run_id"] / "k6.log").read_text()
    assert "sneaky_status=0" in log and "blacklisted" in log
    assert "sneaky_status=200" not in log
