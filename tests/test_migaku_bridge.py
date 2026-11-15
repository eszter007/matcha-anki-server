"""The Migaku bridge's endpoint and its mirror collection, without Migaku itself."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from matcha_anki_server import migaku_bridge
from matcha_anki_server.api import Api
from matcha_anki_server.migaku_mirror import MigakuCard, MigakuReview, Mirror, USN_FROM_MIGAKU
from matcha_anki_server.server import make_handler

from test_sync import TOKEN, finish, free_port


def rpc(port, path, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_endpoint_without_migaku_login(tmp_path):
    anki = Api(str(tmp_path / "anki.anki2"), None)
    migaku = migaku_bridge.make_api(str(tmp_path / "migaku"), "", "")
    port = free_port()
    http = ThreadingHTTPServer(("127.0.0.1", port), make_handler({"/api/v1": anki, "/migaku/api/v1": migaku}, TOKEN))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        assert rpc(port, "/migaku/api/v1", "api.describe")["result"]["apiMajor"] == 1
        handle = rpc(port, "/migaku/api/v1", "collection.sync")
        done = rpc(port, "/migaku/api/v1", "operations.get", {"operationId": handle["result"]["operationId"]})
        assert done["result"]["state"] == "failed"
        assert done["result"]["error"]["data"]["type"] == "authenticationRequired"
        assert "Migaku" in done["result"]["error"]["message"]
    finally:
        http.shutdown()
        anki.col.close()
        migaku.col.close()


def card(**kw):
    base = dict(id="c1", deck_id="d1", deck_name="Migaku::Japanese", front_html="<b>猫</b>", back_html="cat",
                media={}, new=False, suspended=False, due_day=20000, interval_days=4,
                reviews=[MigakuReview(id=1_700_000_000_000, rating=3, interval_days=4, previous_interval_days=1)],
                modified=1)
    base.update(kw)
    return MigakuCard(**base)


def test_mirror_pull_and_device_reviews(tmp_path):
    api = Api(str(tmp_path / "m.anki2"), None)
    mirror = Mirror(api.col)
    out = mirror.apply([card()], today=19998)
    assert out["added"] == 1 and out["reviews"] == 1
    cid = mirror.card_for("c1")
    c = api.col.get_card(cid)
    assert c.type == 2 and c.ivl == 4
    assert api.col.db.scalar("select usn from revlog where cid = ? and ease > 0", cid) == USN_FROM_MIGAKU
    # Unchanged on the next pull: nothing is touched.
    assert mirror.apply([card()], today=19998)["changed"] == 0
    # A device review arrives through the API, as the reader sends it.
    handle = api.call("advanced.reviews.import", {"reviews": [{
        "id": "1700000100000", "cardId": str(cid), "rating": "good", "interval": {"value": 9, "unit": "days"},
        "previousInterval": {"value": 4, "unit": "days"}, "kind": "review"}]})
    assert api.call("operations.get", {"operationId": handle["operationId"]})["state"] == "succeeded"
    api.call("scheduling.setDueDate", {"cardIds": [str(cid)], "days": "9"})
    assert mirror.device_reviews() == [("c1", 1700000100000, 3, cid)]
    mirror.mark_sent([1700000100000])
    assert mirror.device_reviews() == []
    # Gone from Migaku: gone from the mirror.
    assert mirror.drop_missing(set()) == 1
    assert mirror.card_for("c1") is None
    api.col.close()


def test_render_syntax():
    from matcha_anki_server.migaku_sync import render_syntax
    assert render_syntax("白長須鯨[!しろながすくじら]", "ja") == "<ruby>白長須鯨<rt>しろながすくじら</rt></ruby>"
    assert render_syntax("お茶[ちゃ]を 飲[の]む", "ja") == "お<ruby>茶<rt>ちゃ</rt></ruby>を<ruby>飲<rt>の</rt></ruby>む"
    assert render_syntax("Il[il,pron,] <t>fore[forer,v,]</t> le[le,art,m] mur[mur,nn,m].", "fr") == "Il <b>fore</b> le mur."
    assert render_syntax("여우[$:nng]", "ko") == "여우"


def test_render_card_fields():
    from matcha_anki_server.migaku_sync import Rendered, render_card
    config = {"fields": [{"name": "Word", "type": "SYNTAX"}, {"name": "Sentence", "type": "SYNTAX"},
                         {"name": "Images", "type": "IMAGE"}, {"name": "Word Audio", "type": "AUDIO"}],
              "front": [0], "back": [0, 1, 2, 3]}
    out = Rendered()
    ref = "r2://uid/SRSMEDIA/4468aae3-51b9-4729-b589-c3f9556de9b8_photo.webp"
    second = "r2://uid/SRSMEDIA/5468aae3-51b9-4729-b589-c3f9556de9b8_photo.webp"
    front, back = render_card(["chat[chat,nn,m]", "Le <t>chat</t>.", ref + "|" + second, "r2://uid/SRSMEDIA/x"],
                              config, "fr", out)
    assert front == '<div class="mgk-word">chat</div>'
    assert back == ('<div class="mgk-head"><b>chat</b></div><div class="mgk-sentence">Le <b>chat</b>.</div>'
                    '<div class="mgk-image"><img src="mgk-4468aae3-51b9-4729-b589-c3f9556de9b8.webp"></div>')
    assert out.media == {"mgk-4468aae3-51b9-4729-b589-c3f9556de9b8.webp": ref}  # one picture only
    # Course cards: encrypted fields are left out.
    front, back = render_card(["狼[おおかみ]", "", "◴" * 6 + "secret"], {}, "ja", out)
    assert back == '<div class="mgk-head"><b><ruby>狼<rt>おおかみ</rt></ruby></b></div>'


def test_render_card_reads_like_a_dictionary_entry():
    from matcha_anki_server.migaku_sync import Rendered, render_card
    names = ["Word", "Sentence", "Translated Sentence", "Definitions", "Example Sentences", "Notes", "Images"]
    config = {"fields": [{"name": n, "type": "SYNTAX" if n in ("Word", "Sentence") else "TEXT"} for n in names],
              "front": [0, 1], "back": [0, 1, 2, 3, 6, 4, 5]}
    fields = ["serre", "La petite <t>serre</t>.", "The small greenhouse.",
              "<p>serre (sɛʁ) ★★★★</p><p>∙ serre (f)<br>n. greenhouse, glass house</p>", "", "The word `serre`.", ""]
    front, back = render_card(fields, config, "fr", Rendered())
    assert front == '<div class="mgk-word">serre</div><div class="mgk-cue">La petite <b>serre</b>.</div>'
    assert back == ('<div class="mgk-head"><b>serre</b> <span class="mgk-ipa">/sɛʁ/</span></div>'
                    '<div class="mgk-meta">n. (f) · ★★★★</div><div class="mgk-def">greenhouse, glass house</div>'
                    '<div class="mgk-sentence">La petite <b>serre</b>.</div><div class="mgk-trans">The small greenhouse.</div>'
                    '<div class="mgk-note">The word <b>serre</b>.</div>')


def test_split_definitions():
    from matcha_anki_server.migaku_sync import split_definitions
    # An inflected headword keeps its dictionary form; several entries are numbered.
    assert split_definitions("<p>cibles (sibl) ★★★</p><p>∙ cible (f)<br>n. butt, target</p>", "cibles") == \
        ("sibl", ["n. (f)", "★★★"], "cible: butt, target")
    assert split_definitions("<p>trompeuses (tʁɔ̃pøz) ★★</p><p>∙ adj. deceptive</p>"
                             "<p>∙ trompeur (m)<br>n. deceiver</p>", "trompeuses") == \
        ("tʁɔ̃pøz", ["★★"], "1. adj. deceptive<br>2. trompeur (m), n. deceiver")
    assert split_definitions("<p>instar ★</p><p>∙ prep. following, like</p>", "instar") == \
        ("", ["prep.", "★"], "following, like")
    assert split_definitions("<p>PTT</p><p>∙ PTT (Postes et télégraphes)<br>postal services</p>", "PTT") == \
        ("", [], "PTT (Postes et télégraphes): postal services")
    # Anything else is shown as Migaku wrote it.
    assert split_definitions("<b>dog</b>", "dog") == ("", [], "<b>dog</b>")


def test_schedule_matches_migaku():
    from matcha_anki_server.migaku_sync import FAIL, LEARN, PASS, schedule
    deck = {f"intervalFactor{n}": 1.0 for n in (10, 35, 100, 350, 1000)}
    card = {"lastReview": 2383, "interval": 40.0, "factor": 1.0, "lapseCount": 0, "reviewCount": 10,
            "passCount": 6, "failCount": 0, "due": 2423}
    changes, review = schedule(card, deck, PASS, 2510)  # the live check: Migaku stored the same values
    assert (changes["due"], changes["interval"], changes["reviewCount"], changes["passCount"]) == (2828, 317.5, 11, 7)
    assert (review["interval"], review["type"]) == (127.0, PASS)
    changes, _ = schedule(card, deck, FAIL, 2510)
    assert changes["due"] == 2510 and changes["lapseCount"] == 1 and changes["failCount"] == 1
    new = dict(card, due=0, lastReview=0, interval=0.0)
    changes, review = schedule(new, deck, LEARN, 2510)
    assert changes["due"] == 2511 and changes["interval"] == 1.0 and review["interval"] == 0.0


def test_lzo_stream_is_one_literal_run():
    from matcha_anki_server.migaku_client import lzo1x_stored
    assert lzo1x_stored(b"abcd") == bytes([21]) + b"abcd" + b"\x11\x00\x00"
    long = b"x" * 300  # the extension's encoder: 0, a 0 per 255, then the rest of length - 18
    assert lzo1x_stored(long)[:3] == bytes([0, 0, 300 - 18 - 255]) and lzo1x_stored(long).endswith(b"\x11\x00\x00")
    assert lzo1x_stored(b"y" * 528)[:3] == bytes([0, 0, 255])
