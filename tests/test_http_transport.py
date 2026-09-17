from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from tests.support import RouterTestCase


class Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    session = "fixture-session"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        method = request.get("method")
        if method != "initialize" and self.headers.get("Mcp-Session-Id") != self.session:
            self.send_error(400)
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "serverInfo": {"name": "http-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Read one value.",
                        "inputSchema": {
                            "type": "object",
                            "required": ["query"],
                            "properties": {"query": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            type(self).calls.append(
                {
                    "authorization": self.headers.get("Authorization"),
                    "params": request["params"],
                }
            )
            result = {
                "content": [{"type": "text", "text": "found"}],
                "structuredContent": {"found": True},
            }
        else:
            self.send_error(404)
            return
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if method == "initialize":
            self.send_header("Mcp-Session-Id", self.session)
        self.end_headers()
        self.wfile.write(payload)


class HttpTransportTests(RouterTestCase):
    def setUp(self) -> None:
        super().setUp()
        Handler.calls = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.workspace.environment["ROUTER_TEST_HTTP_TOKEN"] = "Bearer fixture-token"
        self.workspace.write_config(
            {
                "remote": {
                    "transport": "http",
                    "url": f"http://127.0.0.1:{self.http.server_port}/mcp",
                    "headers_from_parent": {
                        "Authorization": "ROUTER_TEST_HTTP_TOKEN"
                    },
                    "default_access": "read",
                    "access_overrides": {},
                }
            }
        )

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def test_refresh_and_call_remote_http_tool(self) -> None:
        refreshed = self.workspace.refresh()
        self.assertEqual(refreshed["capability_count"], 3)

        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "remote.lookup",
                    "arguments": {"query": "needle"},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertEqual(
            Handler.calls,
            [
                {
                    "authorization": "Bearer fixture-token",
                    "params": {
                        "name": "lookup",
                        "arguments": {"query": "needle"},
                    },
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
