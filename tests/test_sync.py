"""End to end: Anki's own sync server stands in for AnkiWeb. A "desktop" collection uploads a
note, the companion server pulls it, adds a card and a review over HTTP, pushes, and a second
"desktop" sees both."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from anki.collection import Collection

from matcha_anki_server import ankiweb
from matcha_anki_server.api import Api
from matcha_anki_server.server import make_handler

TOKEN = "test-token-0123456789"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sync_server(tmp_path):
    port = free_port()
    env = dict(os.environ, SYNC_USER1="user:pass", SYNC_BASE=str(tmp_path / "syncbase"), SYNC_HOST="127.0.0.1",
               SYNC_PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-m", "anki.syncserver"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()
    proc.wait()


def desktop_sync(path, endpoint, upload=None):
    col = Collection(path)
    auth = col.sync_login(username="user", password="pass", endpoint=endpoint)
    out = col.sync_collection(auth, False)
    if upload is not None and out.required not in (0, 1):  # NO_CHANGES, NORMAL_SYNC
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=upload)
        col.reopen(after_full_sync=True)
    return col


def rpc(port, method, params=None, token=TOKEN):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def finish(port, handle):
    return rpc(port, "operations.get", {"operationId": handle["result"]["operationId"]})["result"]


def test_round_trip(tmp_path, sync_server):
    # Desktop A: a deck, a note type with our fields, one note; uploaded.
    a = Collection(str(tmp_path / "a.anki2"))
    nt = a.models.by_name("Basic")
    deck = a.decks.id("Japanese::Mining")
    note = a.new_note(nt)
    note["Front"], note["Back"] = "木漏れ日", "sunlight through leaves"
    a.add_note(note, deck)
    a.close()
    a = desktop_sync(str(tmp_path / "a.anki2"), sync_server, upload=True)
    a.close()

    # The companion server, fresh and empty, behind HTTP.
    settings = ankiweb.SyncSettings("user", "pass", sync_server, str(tmp_path / "auth.json"))
    api = Api(str(tmp_path / "server.anki2"), settings)
    port = free_port()
    http = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api, TOKEN))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            rpc(port, "decks.list", token="wrong")
        assert denied.value.code == 401

        assert finish(port, rpc(port, "collection.sync"))["state"] == "succeeded"
        found = rpc(port, "notes.search", {"query": '"Front:木漏れ日"'})["result"]["ids"]
        assert len(found) == 1

        created = rpc(port, "notes.createMany", {"notes": [{
            "noteType": {"name": "Basic"}, "deck": {"name": "Japanese::Mining"},
            "fields": {"Front": "懐かしい", "Back": "nostalgic"}, "tags": ["matcha"]}]})["result"]["items"][0]
        card_id = created["result"]["cardIds"][0]
        review = {"id": str(int(time.time() * 1000) - 3600_000), "cardId": card_id, "rating": "good",
                  "interval": {"value": 3, "unit": "days"}, "previousInterval": {"value": 0, "unit": "days"},
                  "factor": {"encoding": "unknown", "value": 0}, "elapsedMs": 3000, "kind": "learning"}
        assert finish(port, rpc(port, "advanced.reviews.import", {"reviews": [review]}))["state"] == "succeeded"
        rpc(port, "scheduling.setDueDate", {"cardIds": [card_id], "days": "3"})
        assert finish(port, rpc(port, "collection.sync"))["state"] == "succeeded"
    finally:
        http.shutdown()
        api.col.close()

    # Desktop B, fresh, downloads: it sees the new note and its review.
    b = desktop_sync(str(tmp_path / "b.anki2"), sync_server, upload=False)
    assert len(b.find_notes('"Front:懐かしい"')) == 1
    # The imported review; setting the due date adds Anki's own "rescheduled" entry (ease 0) beside it.
    assert b.db.scalar("select count() from revlog where cid = ? and ease > 0", int(card_id)) == 1
    b.close()
