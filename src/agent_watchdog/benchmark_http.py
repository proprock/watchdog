"""Direct HTTP framing for the opt-in synthetic policy transport benchmark."""

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@contextmanager
def benchmark_http_server(
    discovery: Path,
    document: dict,
    answer: Callable[[bytes, Callable[[bytes], None]], None],
) -> Iterator[ThreadingHTTPServer]:
    """Frame the daemon's common evaluator directly, with no forwarding socket."""
    limit = int(document["max_request_bytes"])
    timeout = min(1500, max(1, int(document.get("timeout_ms", 300)))) / 1000

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(timeout)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/decision" or not 0 < length <= limit:
                    return
                payload = self.rfile.read(length)
                if len(payload) == length:
                    answer(payload, self.send_decision)
            except (OSError, ValueError):
                return
            finally:
                self.close_connection = True

        def send_decision(self, response: bytes):
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()
            # Control recording follows this callback, as on the TCP channel.
            self.connection.shutdown(socket.SHUT_WR)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scratch_discovery = discovery.with_name("benchmark-http.json")
    try:
        published = {**document, "benchmark_http_port": server.server_port}
        scratch_discovery.write_text(json.dumps(published), encoding="utf-8")
        yield server
    finally:
        with suppress(OSError):
            scratch_discovery.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
