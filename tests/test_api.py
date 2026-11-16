"""Exercises every method against a temporary collection and checks each answer against
AnkiConnect's published v1 schema, so the device can treat this server and Anki desktop alike.

The schema is fetched once into tests/.cache (set ANKICONNECT_SCHEMA to use a local copy)."""
import json
import os
import urllib.request

import jsonschema
import pytest

from matcha_anki_server.api import Api, RpcError

SCHEMA_URL = "https://git.sr.ht/~foosoft/anki-connect/blob/master/api-v1.schema.json"
CACHE = os.path.join(os.path.dirname(__file__), ".cache", "api-v1.schema.json")


@pytest.fixture(scope="session")
def schema():
    path = os.environ.get("ANKICONNECT_SCHEMA", CACHE)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        urllib.request.urlretrieve(SCHEMA_URL, path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check(schema, method, params, result):
    """Validates params and result with the schema's own definitions for that method."""
    entry = schema["x-methods"][method]
    for part, value in (("params", params), ("result", result)):
        sub = dict(entry[part])
        sub["$defs"] = schema["$defs"]
        jsonschema.validate(value, sub)


@pytest.fixture
def api(tmp_path):
    a = Api(str(tmp_path / "collection.anki2"), None)
    col = a.col
    mm = col.models
    nt = mm.new("Matcha sentence")
    for name in ["Word", "Reading", "Sentence", "Furigana"]:
        mm.add_field(nt, mm.new_field(name))
    t = mm.new_template("Card 1")
    t["qfmt"], t["afmt"] = "{{Sentence}}", "{{furigana:Furigana}}"
    mm.add_template(nt, t)
    mm.add(nt)
    col.decks.id("Japanese::Mining")
    yield a
    a.col.close()


def call(api, schema, method, params):
    result = api.call(method, params)
    check(schema, method, params, result)
    return result


def test_describe_lists_methods(api, schema):
    out = call(api, schema, "api.describe", {})
    names = {m["name"] for m in out["methods"]}
    assert {"notes.createMany", "reviews.list", "advanced.reviews.import"} <= names


def test_decks_and_note_types(api, schema):
    decks = call(api, schema, "decks.list", {})
    assert "Japanese::Mining" in [d["name"] for d in decks]
    types = call(api, schema, "noteTypes.list", {})
    nt = [t for t in types if t["name"] == "Matcha sentence"][0]
    full = call(api, schema, "noteTypes.get", {"noteType": {"id": nt["id"]}})
    assert [f["name"] for f in full["fields"]] == ["Word", "Reading", "Sentence", "Furigana"]


def add(api, schema, word="木漏れ日"):
    params = {"notes": [{"noteType": {"name": "Matcha sentence"}, "deck": {"name": "Japanese::Mining"},
                         "fields": {"Word": word, "Reading": "こもれび", "Sentence": f"<b>{word}</b>",
                                    "Furigana": "木[こ]漏[も]れ 日[び]"}, "tags": ["matcha"]}]}
    return call(api, schema, "notes.createMany", params)["items"][0]


def test_create_search_get_update(api, schema):
    created = add(api, schema)["result"]
    dupe = add(api, schema)
    assert dupe["error"]["data"]["type"] == "validationFailed"
    found = call(api, schema, "notes.search", {"query": '"deck:Japanese::Mining" "Word:木漏れ日"'})
    assert found["ids"] == [created["noteId"]]
    notes = call(api, schema, "notes.getMany", {"ids": [created["noteId"], "999"]})["items"]
    assert notes[0]["result"]["fields"]["Reading"] == "こもれび"
    assert notes[1]["error"]["data"]["type"] == "notFound"
    updated = call(api, schema, "notes.update", {"id": created["noteId"], "patch": {"fields": {"Reading": "x"}}})
    assert updated["fields"]["Reading"] == "x"


def test_bad_search_is_reported(api, schema):
    with pytest.raises(RpcError) as e:
        api.call("notes.search", {"query": "(unclosed"})
    assert e.value.kind == "invalidSearch"


def test_reviews_import_list_and_schedule(api, schema):
    card_id = add(api, schema)["result"]["cardIds"][0]
    review = {"id": "1791633600000", "cardId": card_id, "rating": "good", "interval": {"value": 3, "unit": "days"},
              "previousInterval": {"value": 0, "unit": "days"}, "factor": {"encoding": "unknown", "value": 0},
              "elapsedMs": 4000, "kind": "learning"}
    handle = call(api, schema, "advanced.reviews.import", {"reviews": [review]})
    op = call(api, schema, "operations.get", {"operationId": handle["operationId"]})
    assert op["state"] == "succeeded" and op["result"] == {"changedCount": 1}
    again = api.call("advanced.reviews.import", {"reviews": [review]})
    assert api.call("operations.get", {"operationId": again["operationId"]})["error"]["data"]["type"] == "conflict"

    assert call(api, schema, "reviews.getLatestId", {"deck": {"name": "Japanese::Mining"}})["id"] == "1791633600000"
    page = call(api, schema, "reviews.list", {"deck": {"name": "Japanese::Mining"}})
    assert [(r["id"], r["rating"], r["interval"]["value"]) for r in page["items"]] == [("1791633600000", "good", 3)]
    assert call(api, schema, "reviews.list", {"cardIds": [card_id], "afterId": "1791633600000"})["items"] == []

    due = call(api, schema, "scheduling.setDueDate", {"cardIds": [card_id], "days": "3"})
    assert "result" in due["items"][0]
    api.col.set_config("fsrs", True)
    card = call(api, schema, "advanced.cards.update",
                {"cardId": card_id, "patch": {"fsrsMemory": {"difficulty": 5.5, "stabilityDays": 4.2}}})
    memory = card["scheduling"]["fsrsMemory"]  # Anki keeps these as 32-bit floats
    assert memory["difficulty"] == pytest.approx(5.5) and memory["stabilityDays"] == pytest.approx(4.2)
    got = call(api, schema, "cards.getMany", {"ids": [card_id]})["items"][0]["result"]
    assert got["scheduling"]["type"] == "review"


def test_cards_update_without_fsrs(api, schema):
    card_id = add(api, schema)["result"]["cardIds"][0]
    call(api, schema, "scheduling.setDueDate", {"cardIds": [card_id], "days": "4"})
    card = call(api, schema, "advanced.cards.update", {"cardId": card_id, "patch": {"intervalDays": 4}})
    assert card["scheduling"]["intervalDays"] == 4
    with pytest.raises(RpcError) as e:
        api.call("advanced.cards.update",
                 {"cardId": card_id, "patch": {"fsrsMemory": {"difficulty": 5.5, "stabilityDays": 4.2}}})
    assert (e.value.code, e.value.kind) == (1006, "unsupportedCapability")
    assert api.call("cards.getMany", {"ids": [card_id]})["items"][0]["result"]["scheduling"]["fsrsMemory"] is None


def test_missing_card_is_not_found(api, schema):
    review = {"id": "1791633600000", "cardId": "1700000000999", "rating": "good",
              "interval": {"value": 3, "unit": "days"}, "previousInterval": {"value": 0, "unit": "days"},
              "factor": {"encoding": "unknown", "value": 0}, "elapsedMs": 0, "kind": "review"}
    handle = api.call("advanced.reviews.import", {"reviews": [review]})
    assert api.call("operations.get", {"operationId": handle["operationId"]})["error"]["data"]["type"] == "notFound"
    with pytest.raises(RpcError) as e:
        api.call("advanced.cards.update", {"cardId": "1700000000999", "patch": {"intervalDays": 1}})
    assert e.value.kind == "notFound"


def test_unknown_method(api):
    with pytest.raises(RpcError) as e:
        api.call("notes.delete", {})
    assert e.value.code == -32601


def test_cards_search_render_and_media(api, schema):
    import base64
    import os

    created = add(api, schema)["result"]
    ids = call(api, schema, "cards.search", {"query": '"deck:Japanese::Mining"'})["ids"]
    assert ids == created["cardIds"]
    with open(os.path.join(api.col.media.dir(), "pic.png"), "wb") as f:
        f.write(b"\x89PNG fake")
    note = api.col.get_note(int(created["noteId"]))
    note["Sentence"] = '<b>木漏れ日</b><img src="pic.png"> [sound:x.mp3]'
    api.col.update_note(note)
    r = call(api, schema, "cards.render", {"id": ids[0]})
    assert "<b>木漏れ日</b>" in r["questionHtml"] and "anki:play" not in r["questionHtml"]
    assert "<ruby>" in r["answerHtml"]
    assert "<style>" not in r["questionHtml"] and r["css"]
    media = call(api, schema, "media.get", {"filename": "pic.png"})
    assert base64.b64decode(media["data"]) == b"\x89PNG fake"
    with pytest.raises(RpcError):
        api.call("media.get", {"filename": "../collection.anki2"})


def test_deck_presets_get(api, schema):
    conf = api.col.decks.get_config(1)
    conf["new"]["perDay"] = 15
    conf["rev"]["maxIvl"] = 200
    api.col.decks.save(conf)
    deck = next(d for d in call(api, schema, "decks.list", {}) if d["name"] == "Japanese::Mining")
    r = call(api, schema, "deckPresets.get", {"preset": {"id": deck["presetId"]}})
    assert r["settings"]["newPerDay"] == 15 and r["settings"]["maximumIntervalDays"] == 200
    assert r["settings"]["newMix"] in ("mixed", "afterReviews", "beforeReviews")
    assert len(r["settings"]["easyDaysPercentages"]) == 7
    assert call(api, schema, "deckPresets.get", {"preset": {"name": r["name"]}})["id"] == r["id"]
    with pytest.raises(RpcError):
        api.call("deckPresets.get", {"preset": {"id": "999999"}})


def test_set_suspended_and_tags_add(api, schema):
    created = add(api, schema)["result"]
    cid, nid = created["cardIds"][0], created["noteId"]
    r = call(api, schema, "cards.setSuspended", {"ids": [cid], "suspended": True})
    assert r["items"][0]["result"]["changed"] is True
    assert api.col.get_card(int(cid)).queue == -1
    r = call(api, schema, "cards.setSuspended", {"ids": [cid, "999"], "suspended": True})
    assert r["items"][0]["result"]["changed"] is False and "error" in r["items"][1]
    r = call(api, schema, "tags.add", {"noteIds": [nid], "tags": ["leech"]})
    assert r["items"][0]["result"]["changed"] is True
    assert api.col.get_note(int(nid)).has_tag("leech")


def test_media_get_shrinks_large_images(api, schema):
    import base64
    import io
    import os

    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (2400, 1600), (200, 30, 30)).save(big, format="PNG")
    with open(os.path.join(api.col.media.dir(), "big.png"), "wb") as f:
        f.write(big.getvalue())
    data = base64.b64decode(call(api, schema, "media.get", {"filename": "big.png"})["data"])
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == 800 and im.format == "PNG"


def test_render_type_in_answer(api, schema):
    m = api.col.models.by_name("Basic (type in the answer)")
    n = api.col.new_note(m)
    n["Front"], n["Back"] = "猫", "neko"
    api.col.add_note(n, api.col.decks.id("Japanese::Mining"))
    r = call(api, schema, "cards.render", {"id": str(n.cards()[0].id)})
    assert "[[type:" not in r["questionHtml"] and "[[type:" not in r["answerHtml"]
    assert '<code id="typeans">neko</code>' in r["answerHtml"]


def test_media_get_shrinks_transparent_onto_white(api, schema):
    import base64
    import io
    import os

    from PIL import Image

    big = io.BytesIO()
    Image.new("RGBA", (1600, 1600), (0, 0, 0, 0)).save(big, format="PNG")
    with open(os.path.join(api.col.media.dir(), "clear.png"), "wb") as f:
        f.write(big.getvalue())
    data = base64.b64decode(call(api, schema, "media.get", {"filename": "clear.png"})["data"])
    with Image.open(io.BytesIO(data)) as im:
        assert im.getpixel((10, 10)) == 255


def test_deck_note_type_and_move(api):
    deck = api.call("decks.create", {"name": "Matcha::日本語"})
    assert deck["name"] == "Matcha::日本語"
    assert api.call("decks.create", {"name": "Matcha::日本語"})["id"] == deck["id"]
    nt = api.call("noteTypes.create", {"name": "Mined", "fields": [{"name": "Word"}, {"name": "Sentence"}],
                                       "templates": [{"name": "Card 1", "questionHtml": "{{Sentence}}",
                                                      "answerHtml": "{{Word}}"}], "css": ".card{}"})
    assert [f["name"] for f in nt["fields"]] == ["Word", "Sentence"]
    made = api.call("notes.createMany", {"notes": [{"noteType": {"name": "Mined"}, "deck": {"name": "Default"},
                                                    "fields": {"Word": "犬", "Sentence": "犬がいる"}}]})
    card = made["items"][0]["result"]["cardIds"][0]
    api.call("cards.move", {"ids": [card], "deck": {"id": deck["id"]}})
    assert api.call("cards.getMany", {"ids": [card]})["items"][0]["result"]["deck"]["id"] == deck["id"]
