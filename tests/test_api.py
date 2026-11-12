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

    page = call(api, schema, "reviews.list", {"deck": {"name": "Japanese::Mining"}})
    assert [(r["id"], r["rating"], r["interval"]["value"]) for r in page["items"]] == [("1791633600000", "good", 3)]
    assert call(api, schema, "reviews.list", {"cardIds": [card_id], "afterId": "1791633600000"})["items"] == []

    due = call(api, schema, "scheduling.setDueDate", {"cardIds": [card_id], "days": "3"})
    assert "result" in due["items"][0]
    card = call(api, schema, "advanced.cards.update",
                {"cardId": card_id, "patch": {"fsrsMemory": {"difficulty": 5.5, "stabilityDays": 4.2}}})
    memory = card["scheduling"]["fsrsMemory"]  # Anki keeps these as 32-bit floats
    assert memory["difficulty"] == pytest.approx(5.5) and memory["stabilityDays"] == pytest.approx(4.2)
    got = call(api, schema, "cards.getMany", {"ids": [card_id]})["items"][0]["result"]
    assert got["scheduling"]["type"] == "review"


def test_unknown_method(api):
    with pytest.raises(RpcError) as e:
        api.call("notes.delete", {})
    assert e.value.code == -32601
