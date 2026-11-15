"""Migaku's own sync protocol: sign-in, the full database download, pull and push.

See docs/migaku-protocol.md. Only these Migaku requests are made: Firebase sign-in and token
refresh, /server-version, /db-force-sync-download-url (and the download it names), /pull-sync,
/push/enqueue and media reads. The endpoints that overwrite, delete or back up the whole Migaku
database are never called.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API_KEY = "AIzaSyDZvwYKYTsQoZkf3oKsfIQ4ykuy2GZAiH8"  # Migaku's Firebase web key, from its extension
SIGN_IN = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH = "https://securetoken.googleapis.com/v1/token"
CORE = "https://core-server.migaku.com"
DB_URLS = "https://srs-db-presigned-url-service-api.migaku.com"
FILES = "https://file-sync-worker-api.migaku.com"
CORE_VERSION = "21.35.0-earlyaccess.99"  # the extension version the protocol was read from
ENVIRONMENT = "extension"
USER_AGENT = "matcha-anki-server"


class MigakuError(Exception):
    def __init__(self, message: str, status: int = 0, auth: bool = False):
        super().__init__(message)
        self.status = status
        self.auth = auth  # the login was refused


def lzo1x_stored(data: bytes) -> bytes:
    """An LZO1X stream that holds `data` as one literal run (valid LZO1X, just not smaller)."""
    n = len(data)
    if n == 0:
        return b"\x11\x00\x00"
    if 4 <= n <= 238:
        head = bytes([n + 17])
    elif n > 238:
        rest = n - 18
        zeros, last = divmod(rest, 255)
        if last == 0:
            zeros, last = zeros - 1, 255
        head = b"\x00" + b"\x00" * zeros + bytes([last])
    else:  # 1..3 bytes: pad never happens for JSON, but keep it valid
        head = bytes([n + 17])
    return head + data + b"\x11\x00\x00"


def lzo_body(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"LZ" + struct.pack("<I", len(raw)) + lzo1x_stored(raw)


class Client:
    def __init__(self, data_dir: str, email: str, password: str):
        self.email = email
        self.password = password
        self.auth_file = os.path.join(data_dir, "migaku-auth.json")
        self.id_token = ""
        self.expires = 0.0
        self.refresh_token = ""
        self.uid = ""
        self.device_id = ""
        self._load()

    # --- login

    def _load(self) -> None:
        if not os.path.exists(self.auth_file):
            return
        with open(self.auth_file, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("email") == self.email:
            self.refresh_token = saved.get("refreshToken", "")
            self.uid = saved.get("uid", "")
        self.device_id = saved.get("deviceId", "")

    def _save(self) -> None:
        if not self.device_id:
            self.device_id = secrets.token_hex(16)
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump({"email": self.email, "uid": self.uid, "refreshToken": self.refresh_token,
                       "deviceId": self.device_id}, f)
        os.chmod(self.auth_file, 0o600)

    def _token(self, force: bool = False) -> str:
        if self.id_token and not force and time.time() < self.expires - 60:
            return self.id_token
        reply = None
        if self.refresh_token:
            try:
                reply = self._post_form(REFRESH, {"grant_type": "refresh_token", "refresh_token": self.refresh_token})
                self.id_token, self.refresh_token = reply["id_token"], reply["refresh_token"]
                self.uid = reply.get("user_id", self.uid)
                self.expires = time.time() + int(reply.get("expires_in", 3600))
            except MigakuError as error:
                log.info("Migaku token refresh failed (%s); signing in again", error)
                reply = None
        if reply is None:
            if not self.password:
                raise MigakuError("No Migaku password is configured on the server.", auth=True)
            reply = self._post_json(SIGN_IN, {"email": self.email, "password": self.password,
                                              "returnSecureToken": True})
            self.id_token, self.refresh_token = reply["idToken"], reply["refreshToken"]
            self.uid = reply["localId"]
            self.expires = time.time() + int(reply.get("expiresIn", 3600))
        self._save()
        return self.id_token

    def _post_json(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(f"{url}?key={API_KEY}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        return self._firebase(req)

    def _post_form(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(f"{url}?key={API_KEY}", data=urllib.parse.urlencode(body).encode(),
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded",
                                                             "User-Agent": USER_AGENT})
        return self._firebase(req)

    @staticmethod
    def _firebase(req) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read())["error"]["message"]
            except Exception:
                message = str(error.code)
            raise MigakuError(f"Migaku sign-in refused: {message}", error.code, auth=True) from None
        except urllib.error.URLError as error:
            raise MigakuError(f"Migaku sign-in unreachable: {error.reason}") from None

    # --- Migaku requests

    def _params(self, **extra) -> str:
        params = {"deviceId": self.device_id, "uid": self.uid, "applicationEnvironment": ENVIRONMENT,
                  "coreVersion": CORE_VERSION}
        params.update(extra)
        return urllib.parse.urlencode(params)

    def _request(self, method: str, url: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: int = 60, auth: bool = True):
        """(status, headers, body). Retries once with a fresh token on 401."""
        for attempt in range(2):
            h = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
            if auth:
                h["Authorization"] = "Bearer " + self._token(force=attempt > 0)
            h.update(headers or {})
            req = urllib.request.Request(url, data=body, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                        data = gzip.decompress(data)
                    return resp.status, resp.headers, data
            except urllib.error.HTTPError as error:
                if error.code == 401 and auth and attempt == 0:
                    continue
                text = error.read()[:300].decode("utf-8", "replace")
                raise MigakuError(f"Migaku {method} {url.split('?')[0]}: {error.code} {text}", error.code) from None
            except urllib.error.URLError as error:
                raise MigakuError(f"Migaku unreachable: {error.reason}") from None
        raise MigakuError("Migaku refused the login token.", 401, auth=True)

    def server_version(self) -> int:
        self._token()
        _, headers, _ = self._request("GET", f"{CORE}/server-version?uid={urllib.parse.quote(self.uid)}")
        return int(headers.get("X-Server-Version") or 0)

    def download_database(self) -> bytes:
        """The whole collection as Migaku keeps it: SQLite bytes (gunzipped)."""
        _, _, url = self._request("GET", f"{DB_URLS}/db-force-sync-download-url")
        url = url.decode().strip().strip('"')
        if not url.startswith("https://"):
            raise MigakuError("Migaku sent no download address for the collection.")
        _, _, data = self._request("GET", url, auth=False, timeout=600)
        if not data.startswith(b"SQLite format 3\x00"):
            raise MigakuError("Migaku's collection download is not a database.")
        return data

    def pull(self, cursor: int) -> tuple[dict, int, int]:
        """(SyncObject, new cursor, latest cursor)."""
        self._token()
        _, headers, data = self._request("GET", f"{CORE}/pull-sync?" + self._params(serverVersion=cursor))
        page = int(headers.get("X-Server-Version") or cursor)
        latest = int(headers.get("X-Latest-Server-Version") or page)
        return json.loads(data or b"{}"), page, latest

    def push(self, sync_object: dict, cursor: int) -> bool:
        """True when Migaku queued it; False on a conflict (pull first, then push again)."""
        self._token()
        try:
            status, _, _ = self._request("POST", f"{CORE}/push/enqueue?" + self._params(deviceVersion=cursor),
                                         body=lzo_body(sync_object),
                                         headers={"Content-Type": "application/octet-stream",
                                                  "X-Content-Encoding": "lzo"})
        except MigakuError as error:
            if error.status == 409:
                return False
            raise
        if status not in (200, 202):
            raise MigakuError(f"Migaku push answered {status}", status)
        return True

    def media(self, r2_path: str) -> bytes:
        path = urllib.parse.quote(r2_path.removeprefix("r2://") if hasattr(str, "removeprefix")
                                  else r2_path[5:] if r2_path.startswith("r2://") else r2_path)
        _, _, data = self._request("GET", f"{FILES}/data/{path}")
        return data
