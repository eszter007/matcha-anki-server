"""HTTP front: POST /api/v1 (JSON-RPC 2.0, AnkiConnect v1 style) for the Anki collection, the same
API at POST /migaku/api/v1 for the Migaku bridge's collection, and GET /health.

Configuration comes from the environment:

  MATCHA_TOKEN       required: the key the device sends as "Authorization: Bearer <key>"
  ANKIWEB_USERNAME   AnkiWeb (or sync server) account; without it collection.sync is refused
  ANKIWEB_PASSWORD
  SYNC_ENDPOINT      optional: a self-hosted Anki sync server instead of AnkiWeb
  MIGAKU_EMAIL       Migaku account for the Migaku bridge; without it the bridge's collection.sync
  MIGAKU_PASSWORD    is refused
  DATA_DIR           where the collection lives (default ./data); may be temporary
  PORT               default 8080 (Cloud Run sets it)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ankiweb, migaku_bridge
from .api import Api, RawReply, RpcError

MAX_BODY = 4 * 1024 * 1024
log = logging.getLogger("matcha_anki_server")


def make_handler(apis: dict[str, Api], token: str):
    """apis: request path (no trailing slash) to the Api that answers it."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "MatchaAnkiServer/1"

        def log_message(self, fmt, *args):  # route through logging, without request bodies
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, b'{"ok":true}')
            else:
                self._send(404)

        def do_POST(self):
            api = apis.get(self.path.rstrip("/"))
            if api is None:
                self._send(404)
                return
            # Same transport rules as AnkiConnect: empty bodies for 401 and 413.
            given = self.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
                self._send(401)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413)
                return
            raw = self.rfile.read(length)
            try:
                request = json.loads(raw)
            except ValueError:
                self._reply(None, error={"code": -32700, "message": "Parse error.", "data": {"type": "parseError"}})
                return
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "method" not in request:
                self._reply(request.get("id") if isinstance(request, dict) else None,
                            error={"code": -32600, "message": "Invalid request.", "data": {"type": "invalidRequest"}})
                return
            try:
                result = api.call(str(request["method"]), request.get("params") or {})
                if isinstance(result, RawReply):  # packed for a device: sent as it is
                    self._send(200, bytes(result), "application/octet-stream")
                else:
                    self._reply(request.get("id"), result=result)
            except RpcError as error:
                self._reply(request.get("id"), error=error.to_json())
            except Exception:  # never leak a traceback to the client
                log.exception("Internal error in %s", request.get("method"))
                self._reply(request.get("id"),
                            error={"code": -32603, "message": "Internal error.", "data": {"type": "internalError"}})

        def _reply(self, request_id, result=None, error=None):
            body = {"jsonrpc": "2.0", "id": request_id}
            if error is not None:
                body["error"] = error
            else:
                body["result"] = result
            self._send(200, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    return Handler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("MATCHA_TOKEN", "")
    if len(token) < 16:
        log.error("Set MATCHA_TOKEN to a random key of at least 16 characters.")
        return 1
    data_dir = os.environ.get("DATA_DIR", "data")
    os.makedirs(data_dir, exist_ok=True)
    user = os.environ.get("ANKIWEB_USERNAME", "")
    settings = None
    if user:
        settings = ankiweb.SyncSettings(username=user, password=os.environ.get("ANKIWEB_PASSWORD", ""),
                                        endpoint=os.environ.get("SYNC_ENDPOINT") or None,
                                        auth_file=os.path.join(data_dir, "auth.json"))
    else:
        log.warning("No ANKIWEB_USERNAME: the collection stays on this server only.")
    api = Api(os.path.join(data_dir, "collection.anki2"), settings)
    migaku_api = migaku_bridge.make_api(os.path.join(data_dir, "migaku"), os.environ.get("MIGAKU_EMAIL", ""),
                                        os.environ.get("MIGAKU_PASSWORD", ""))
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port),
                                 make_handler({"/api/v1": api, "/migaku/api/v1": migaku_api}, token))
    log.info("Listening on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.col.close()
        migaku_api.col.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
