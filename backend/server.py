"""Minimal HTTP front for the Ledger (stdlib only).
POST /sync            {"events": [...]}  -> {"acked": [...], "rejected": {...}}
POST /vouchers/issue  {"user_pub","max_cents","ttl_s","allowed_stations"} -> voucher
GET  /ledger          -> summary
GET  /health
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_server(ledger, host="127.0.0.1", port=0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/ledger":
                return self._send(200, ledger.summary())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if self.path == "/sync":
                    return self._send(200, ledger.ingest(body["events"]))
                if self.path == "/vouchers/issue":
                    return self._send(200, ledger.issue_voucher(
                        body["user_pub"], body["max_cents"], body.get("ttl_s", 86400),
                        body.get("allowed_stations")))
                self._send(404, {"error": "not found"})
            except (KeyError, ValueError) as ex:
                self._send(400, {"error": str(ex)})

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def start_in_thread(server) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
