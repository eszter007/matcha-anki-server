"""The AnkiConnect v1 methods Matcha Reader uses, implemented on the Anki library.

Request and response shapes follow AnkiConnect's API v1 (JSON-RPC 2.0, entity ids as decimal
strings), so the device talks to this server and to Anki desktop with AnkiConnect the same way.
Only the subset the device needs is implemented; any other method answers "unknown method".
"""
from __future__ import annotations

import datetime as dt
import itertools
import threading
from typing import Any, Callable

from anki.collection import Collection

from . import ankiweb

RATINGS = ["again", "hard", "good", "easy"]  # revlog ease 1..4
KINDS = ["learning", "review", "relearning", "filtered", "manual", "rescheduled"]  # revlog type 0..5
CARD_TYPES = ["new", "learning", "review", "relearning"]
QUEUES = {0: "new", 1: "learning", 2: "review", 3: "dayLearning", 4: "preview", -1: "suspended",
          -2: "buriedByScheduler", -3: "buriedByUser"}
MAX_PAGE = 1000


class RpcError(Exception):
    """A JSON-RPC error with AnkiConnect's code and error type."""

    def __init__(self, code: int, kind: str, message: str):
        super().__init__(message)
        self.code = code
        self.kind = kind

    def to_json(self) -> dict:
        return {"code": self.code, "message": str(self), "data": {"type": self.kind}}


def not_found(what: str) -> RpcError:
    return RpcError(1001, "notFound", f"{what} not found.")


def unsupported(message: str) -> RpcError:
    return RpcError(1006, "unsupportedCapability", message)


def invalid(message: str) -> RpcError:
    return RpcError(-32602, "invalidParams", message)


def webp_to_png(data: bytes, max_side: int = 480) -> bytes:
    """A grayscale PNG no wider or taller than an e-ink screen; the original bytes if Pillow fails."""
    import io

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            # Transparent parts on white, as the reader shows them, not black.
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                background = Image.new("RGBA", im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(background, im)
            im = im.convert("L")
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:  # an unreadable image stays as it was
        return data


def stamp(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def shrink_image(data: bytes, name: str, max_side: int = 800) -> bytes:
    """A PNG or JPEG larger than an e-ink screen, scaled down in grayscale in its own format, so
    the reader stores and decodes far fewer bytes. Anything else, or anything Pillow cannot read,
    stays as it was."""
    import io

    lower = name.lower()
    fmt = "PNG" if lower.endswith(".png") else "JPEG" if lower.endswith((".jpg", ".jpeg")) else None
    if fmt is None:
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= max_side:
                return data
            # Transparent parts on white, as the reader shows them, not black.
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                background = Image.new("RGBA", im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(background, im)
            im = im.convert("L")
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            if fmt == "JPEG":
                im.save(out, format="JPEG", quality=85, optimize=True)
            else:
                im.save(out, format="PNG", optimize=True)
            return out.getvalue() if out.tell() < len(data) else data
    except Exception:
        return data


def entity_id(value: Any, name: str) -> int:
    try:
        number = int(str(value))
    except ValueError:
        raise invalid(f"{name} must be a decimal id") from None
    if number <= 0:
        raise invalid(f"{name} must be positive")
    return number


class Api:
    """One collection, one lock: requests run one at a time, as in Anki desktop."""

    def __init__(self, collection_path: str, sync: ankiweb.SyncSettings | None,
                 syncer: Callable[[Collection], Collection] | None = None, service: str = "AnkiWeb",
                 media_source: Callable[[Collection, str], bytes | None] | None = None):
        """syncer, when given, replaces the AnkiWeb sync (the Migaku bridge); service names the
        remote in the error a device sees when neither is configured. media_source fetches a media
        file the collection does not hold yet (the bridge's images, kept by Migaku)."""
        self.path = collection_path
        self.sync_settings = sync
        self.syncer = syncer
        self.service = service
        self.media_source = media_source
        self.lock = threading.Lock()
        self.col = Collection(collection_path)
        self.operations: dict[str, dict] = {}
        self.op_counter = itertools.count(1)
        self.methods: dict[str, Callable[[dict], Any]] = {
            "api.describe": self.describe,
            "collection.sync": self.collection_sync,
            "operations.get": self.operations_get,
            "decks.list": self.decks_list,
            "deckPresets.get": self.deck_presets_get,
            "noteTypes.list": self.note_types_list,
            "noteTypes.get": self.note_types_get,
            "notes.search": self.notes_search,
            "cards.search": self.cards_search,
            "notes.getMany": self.notes_get_many,
            "notes.createMany": self.notes_create_many,
            "notes.update": self.notes_update,
            "cards.getMany": self.cards_get_many,
            "cards.render": self.cards_render,
            "cards.setSuspended": self.cards_set_suspended,
            "cards.move": self.cards_move,
            "decks.create": self.decks_create,
            "noteTypes.create": self.note_types_create,
            "tags.add": self.tags_add,
            "media.get": self.media_get,
            "reviews.list": self.reviews_list,
            "advanced.reviews.import": self.reviews_import,
            "reviews.getLatestId": self.reviews_latest,
            "advanced.cards.update": self.cards_update,
            "scheduling.setDueDate": self.set_due_date,
            # Outside the API schema, for devices: the scheduler's day, so a card's due day
            # ("due": {"kind": "day"}) can be told as days from today.
            "device.today": self.device_today,
        }

    def call(self, method: str, params: dict) -> Any:
        handler = self.methods.get(method)
        if handler is None:
            raise RpcError(-32601, "methodNotFound", f"Unknown method: {method}")
        if not isinstance(params, dict):
            raise invalid("params must be an object")
        with self.lock:
            return handler(params)

    # --- discovery and operations

    def device_today(self, _params: dict) -> dict:
        return {"day": self.col.sched.today}

    def describe(self, _params: dict) -> dict:
        from anki.buildinfo import version

        return {
            "apiMajor": 1,
            "ankiVersion": version,
            "schema": {},
            "methods": [{"name": name, "available": True, "reason": None} for name in sorted(self.methods)],
            "limits": {"maxRequestBytes": 4 * 1024 * 1024, "maxBatchItems": MAX_PAGE, "maxPageSize": MAX_PAGE,
                       "operationRetentionSeconds": 3600, "maxActiveOperations": 1, "maxRetainedOperations": 64,
                       "maxMediaBytes": 1},
        }

    def _operation(self, method: str, run: Callable[[], Any]) -> dict:
        """Runs a long operation at once and keeps its result for operations.get."""
        op_id = f"op-{next(self.op_counter)}"
        now = stamp(dt.datetime.now().timestamp())
        record = {"operationId": op_id, "method": method, "session": None, "createdAt": now, "updatedAt": now,
                  "cancellable": False, "progress": None}
        try:
            record["result"] = run()
            record["state"] = "succeeded"
        except RpcError as error:
            record["error"] = error.to_json()
            record["state"] = "failed"
        self.operations[op_id] = record
        if len(self.operations) > 64:  # keep only recent ones
            self.operations.pop(next(iter(self.operations)))
        return {"operationId": op_id, "method": method, "session": None}

    def operations_get(self, params: dict) -> dict:
        record = self.operations.get(str(params.get("operationId", "")))
        if record is None:
            raise not_found("Operation")
        return record

    def collection_sync(self, params: dict) -> dict:
        def run():
            if self.syncer is not None:
                self.col = self.syncer(self.col)
            elif self.sync_settings is not None:
                self.col = ankiweb.sync(self.col, self.sync_settings)
            else:
                raise RpcError(1011, "authenticationRequired",
                               f"No {self.service} account is configured on the server.")
            return {"collectionComplete": True, "mediaComplete": not params.get("includeMedia", True)}

        return self._operation("collection.sync", run)

    # --- decks and note types

    def decks_list(self, _params: dict) -> list:
        out = []
        for d in self.col.decks.all_names_and_ids():
            deck = self.col.decks.get(d.id)
            filtered = bool(deck.get("dyn"))
            out.append({"id": str(d.id), "name": d.name, "kind": "filtered" if filtered else "normal",
                        "presetId": None if filtered else str(deck.get("conf", 1))})
        return sorted(out, key=lambda d: d["name"])

    def decks_create(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        if not name:
            raise invalid("name is required")
        deck_id = self.col.decks.id(name, create=True)
        deck = self.col.decks.get(deck_id)
        return {"id": str(deck_id), "name": deck["name"], "kind": "normal", "presetId": str(deck.get("conf", 1))}

    def deck_presets_get(self, params: dict) -> dict:
        ref = params.get("preset") or {}
        if "id" in ref:
            wanted = entity_id(ref["id"], "preset.id")
            # get_config falls back to the default preset for an unknown id.
            conf = next((c for c in self.col.decks.all_config() if c["id"] == wanted), None)
        else:
            conf = next((c for c in self.col.decks.all_config() if c["name"] == ref.get("name")), None)
        if not conf:
            raise not_found("Preset")
        settings = {
            "newPerDay": max(conf["new"]["perDay"], 0),
            "reviewsPerDay": max(conf["rev"]["perDay"], 0),
            "maximumIntervalDays": max(conf["rev"]["maxIvl"], 1),
            "learnStepsMinutes": [m for m in conf["new"]["delays"] if m > 0],
            "relearnStepsMinutes": [m for m in conf["lapse"]["delays"] if m > 0],
            "leechThreshold": max(conf["lapse"]["leechFails"], 0),
        }
        def pick(names: list, value: int, default: str) -> str:
            return names[value] if isinstance(value, int) and 0 <= value < len(names) else default

        gather = ["deck", "lowestPosition", "highestPosition", "randomNotes", "randomCards", "deckThenRandomNotes"]
        sort = ["template", "none", "templateThenRandom", "randomNoteThenTemplate", "randomCard"]
        review = ["day", "dayThenDeck", "deckThenDay", "intervalAscending", "intervalDescending", "easeAscending",
                  "easeDescending", "retrievabilityAscending", "random", "added", "reverseAdded",
                  "retrievabilityDescending"]
        mix = ["mixed", "afterReviews", "beforeReviews"]
        settings.update({
            "newGatherOrder": pick(gather, conf.get("newGatherPriority", 0), "deck"),
            "newSortOrder": pick(sort, conf.get("newSortOrder", 0), "template"),
            "reviewOrder": pick(review, conf.get("reviewOrder", 0), "day"),
            "newMix": pick(mix, conf.get("newMix", 0), "mixed"),
            "buryNew": bool(conf["new"].get("bury", False)),
            "buryReviews": bool(conf["rev"].get("bury", False)),
            "leechAction": "tagOnly" if conf["lapse"].get("leechAction", 0) == 1 else "suspend",
        })
        easy = conf.get("easyDaysPercentages")
        if isinstance(easy, list) and len(easy) == 7:
            settings["easyDaysPercentages"] = [max(float(v), 0.0) for v in easy]
        retention = conf.get("desiredRetention")
        if retention and 0 < retention < 1:
            settings["desiredRetention"] = retention
        return {"id": str(conf["id"]), "name": conf["name"], "settings": settings, "modifiedAt": stamp(conf["mod"])}

    def note_types_list(self, _params: dict) -> list:
        return sorted(({"id": str(m.id), "name": m.name} for m in self.col.models.all_names_and_ids()),
                      key=lambda m: m["name"])

    def _note_type(self, ref: Any) -> dict:
        if not isinstance(ref, dict):
            raise invalid("noteType must be a reference")
        if "id" in ref:
            model = self.col.models.get(entity_id(ref["id"], "noteType.id"))
        else:
            model = self.col.models.by_name(str(ref.get("name", "")))
        if not model:
            raise not_found("Note type")
        return model

    def note_types_create(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        fields = params.get("fields") or []
        templates = params.get("templates") or []
        if not name or not fields or not templates:
            raise invalid("name, fields and templates are required")
        if self.col.models.by_name(name):
            raise RpcError(1003, "conflict", f"A note type named {name} exists.")
        mm = self.col.models
        model = mm.new(name)
        if params.get("kind") == "cloze":
            model["type"] = 1
        for f in fields:
            mm.add_field(model, mm.new_field(str(f.get("name"))))
        for t in templates:
            tmpl = mm.new_template(str(t.get("name")))
            tmpl["qfmt"] = str(t.get("questionHtml", ""))
            tmpl["afmt"] = str(t.get("answerHtml", ""))
            mm.add_template(model, tmpl)
        model["css"] = str(params.get("css", ""))
        mm.add(model)
        return self.note_types_get({"noteType": {"name": name}})

    def note_types_get(self, params: dict) -> dict:
        m = self._note_type(params.get("noteType"))
        return {
            "id": str(m["id"]),
            "name": m["name"],
            "kind": "cloze" if m.get("type") == 1 else "normal",
            "fields": [{"name": f["name"]} for f in m["flds"]],
            "templates": [{"name": t["name"], "questionHtml": t["qfmt"], "answerHtml": t["afmt"]} for t in m["tmpls"]],
            "css": m.get("css", ""),
            "sortField": m["flds"][m.get("sortf", 0)]["name"],
            "latexHeader": m.get("latexPre", ""),
            "latexFooter": m.get("latexPost", ""),
            "latexSvg": bool(m.get("latexsvg", False)),
            "modifiedAt": stamp(m.get("mod", 0)),
        }

    # --- notes

    def _deck_id(self, ref: Any) -> int:
        if not isinstance(ref, dict):
            raise invalid("deck must be a reference")
        if "id" in ref:
            deck_id = entity_id(ref["id"], "deck.id")
            if not self.col.decks.get(deck_id, default=False):
                raise not_found("Deck")
            return deck_id
        name = str(ref.get("name", ""))
        if not name:
            raise invalid("deck needs an id or a name")
        found = self.col.decks.id_for_name(name)
        if found is None:
            raise not_found("Deck")
        return found

    def _note_json(self, note) -> dict:
        model = note.note_type()
        return {"id": str(note.id), "noteType": {"id": str(model["id"]), "name": model["name"]},
                "fields": dict(note.items()), "tags": list(note.tags),
                "cardIds": [str(c) for c in note.card_ids()], "modifiedAt": stamp(note.mod)}

    def notes_search(self, params: dict) -> dict:
        query = params.get("query")
        if not isinstance(query, str):
            raise invalid("query must be a string")
        limit = max(1, min(int(params.get("limit", 100)), MAX_PAGE))
        offset = int(params.get("cursor") or 0)
        try:
            ids = self.col.find_notes(query)
        except Exception as error:  # Anki raises on malformed searches
            raise RpcError(1013, "invalidSearch", str(error)) from None
        page = ids[offset:offset + limit]
        more = offset + limit < len(ids)
        return {"ids": [str(i) for i in page], "nextCursor": str(offset + limit) if more else None}

    def cards_search(self, params: dict) -> dict:
        query = params.get("query")
        if not isinstance(query, str):
            raise invalid("query must be a string")
        limit = max(1, min(int(params.get("limit", 100)), MAX_PAGE))
        offset = int(params.get("cursor") or 0)
        try:
            ids = self.col.find_cards(query)
        except Exception as error:
            raise RpcError(1013, "invalidSearch", str(error)) from None
        page = ids[offset:offset + limit]
        more = offset + limit < len(ids)
        return {"ids": [str(i) for i in page], "nextCursor": str(offset + limit) if more else None}

    def notes_get_many(self, params: dict) -> dict:
        items = []
        for index, raw in enumerate(params.get("ids") or []):
            try:
                items.append({"index": index, "result": self._note_json(self.col.get_note(entity_id(raw, "id")))})
            except RpcError as error:
                items.append({"index": index, "error": error.to_json()})
            except Exception:
                items.append({"index": index, "error": not_found("Note").to_json()})
        return {"items": items}

    def notes_create_many(self, params: dict) -> dict:
        from anki.notes import NoteFieldsCheckResult

        items = []
        for index, spec in enumerate(params.get("notes") or []):
            try:
                model = self._note_type(spec.get("noteType"))
                deck_id = self._deck_id(spec.get("deck"))
                note = self.col.new_note(model)
                fields = spec.get("fields") or {}
                for name, value in fields.items():
                    if name not in note:
                        raise RpcError(1002, "validationFailed", f"No field named {name}.")
                    note[name] = str(value)
                note.tags = [str(t) for t in spec.get("tags") or []]
                allow_dupes = bool((spec.get("duplicates") or {}).get("allow", False))
                check = note.fields_check()
                if check == NoteFieldsCheckResult.EMPTY:
                    raise RpcError(1002, "validationFailed", "The first field is empty.")
                if check == NoteFieldsCheckResult.DUPLICATE and not allow_dupes:
                    raise RpcError(1002, "validationFailed", "Duplicate note.")
                self.col.add_note(note, deck_id)
                items.append({"index": index, "result": {"noteId": str(note.id),
                                                         "cardIds": [str(c) for c in note.card_ids()]}})
            except RpcError as error:
                items.append({"index": index, "error": error.to_json()})
        return {"items": items}

    def notes_update(self, params: dict) -> dict:
        try:
            note = self.col.get_note(entity_id(params.get("id"), "id"))
        except RpcError:
            raise
        except Exception:
            raise not_found("Note") from None
        patch = params.get("patch") or {}
        for name, value in (patch.get("fields") or {}).items():
            if name not in note:
                raise RpcError(1002, "validationFailed", f"No field named {name}.")
            note[name] = str(value)
        if "tags" in patch:
            note.tags = [str(t) for t in patch["tags"]]
        self.col.update_note(note)
        return self._note_json(self.col.get_note(note.id))

    # --- cards and scheduling

    def cards_move(self, params: dict) -> dict:
        deck_id = self._deck_id(params.get("deck"))
        ids = [self._card(raw).id for raw in params.get("ids") or []]
        self.col.set_deck(ids, deck_id)
        return {"changedCount": len(ids)}

    def cards_set_suspended(self, params: dict) -> dict:
        suspend = bool(params.get("suspended"))
        items = []
        for i, raw in enumerate(params.get("ids") or []):
            try:
                card = self._card(raw)
            except RpcError as e:
                items.append({"index": i, "error": e.to_json()})
                continue
            was = card.queue == -1
            if suspend and not was:
                self.col.sched.suspend_cards([card.id])
            elif not suspend and was:
                self.col.sched.unsuspend_cards([card.id])
            items.append({"index": i, "result": {"changed": was != suspend}})
        return {"items": items}

    def tags_add(self, params: dict) -> dict:
        tags = [str(t) for t in params.get("tags") or []]
        items = []
        for i, raw in enumerate(params.get("noteIds") or []):
            try:
                note = self.col.get_note(entity_id(raw, f"noteIds[{i}]"))
            except RpcError as e:
                items.append({"index": i, "error": e.to_json()})
                continue
            except Exception:
                items.append({"index": i, "error": not_found("Note").to_json()})
                continue
            added = [t for t in tags if not note.has_tag(t)]
            for t in added:
                note.add_tag(t)
            if added:
                self.col.update_note(note)
            items.append({"index": i, "result": {"changed": bool(added)}})
        return {"items": items}

    def _card_json(self, card) -> dict:
        deck = self.col.decks.get(card.did)
        home = self.col.decks.get(card.odid) if card.odid else None
        memory = card.memory_state
        due = {"kind": "position", "position": card.due} if card.queue == 0 else {"kind": "day", "day": card.due}
        return {
            "id": str(card.id), "noteId": str(card.nid),
            "deck": {"id": str(card.did), "name": deck["name"]},
            "homeDeck": {"id": str(card.odid), "name": home["name"]} if home else None,
            "templateIndex": card.ord, "flags": card.flags, "modifiedAt": stamp(card.mod),
            "scheduling": {
                "type": CARD_TYPES[card.type], "queue": QUEUES.get(card.queue, "new"), "due": due,
                "intervalDays": max(card.ivl, 0), "learningIntervalSeconds": None, "remainingSteps": card.left % 1000,
                "repetitions": card.reps, "lapses": card.lapses, "sm2EasePermille": card.factor,
                "fsrsMemory": {"difficulty": memory.difficulty, "stabilityDays": memory.stability} if memory else None,
                "desiredRetention": card.desired_retention,
            },
        }

    def _card(self, raw: Any):
        from anki.errors import NotFoundError

        card_id = entity_id(raw, "cardId")
        try:
            return self.col.get_card(card_id)
        except NotFoundError:
            # Only a missing card: the device drops a review it gets notFound for.
            raise not_found("Card") from None

    def cards_get_many(self, params: dict) -> dict:
        items = []
        for index, raw in enumerate(params.get("ids") or []):
            try:
                items.append({"index": index, "result": self._card_json(self._card(raw))})
            except RpcError as error:
                items.append({"index": index, "error": error.to_json()})
        return {"items": items}

    def cards_render(self, params: dict) -> dict:
        """The card's front and back as Anki renders them, with the note type's CSS."""
        import re

        from .device_html import for_device

        card = self._card(params.get("id"))
        css = card.note_type().get("css", "")
        av = re.compile(r"\[anki:play:[qa]:\d+\]")  # audio has no place on an e-ink card
        scripts = re.compile(r"<script\b.*?</script\s*>", re.S | re.I)  # the reader runs no scripts
        inline_css = f"<style>{css}</style>"  # Anki repeats the note type's CSS; it is sent once below

        # Type-in answers: the reader has no typing box. The front leaves the box out; the back shows
        # the expected answer, as Anki does when nothing was typed.
        type_in = re.compile(r"\[\[type:(?:[^\]:]*:)?([^\]]+)\]\]")
        note = card.note()

        def expected(m: re.Match) -> str:
            name = m.group(1).strip()
            return f'<code id="typeans">{note[name]}</code>' if name in note else ""

        def clean(html: str, answer: bool = False) -> str:
            html = type_in.sub(expected if answer else "", html)
            return for_device(scripts.sub("", av.sub("", html.replace(inline_css, ""))))

        return {"cardId": str(card.id), "questionHtml": clean(card.question(reload=True)),
                "answerHtml": clean(card.answer(), answer=True), "css": css}

    def media_get(self, params: dict) -> dict:
        import base64
        import os

        name = str(params.get("filename", ""))
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise invalid("filename must be a plain media file name")

        def local(file: str) -> str | None:
            path = os.path.join(self.col.media.dir(), file)
            if not os.path.isfile(path) and self.media_source is not None:
                fetched = self.media_source(self.col, file)
                if fetched:
                    self.col.media.write_data(file, fetched)
            return path if os.path.isfile(path) else None

        path = local(name)
        # "<file>.png" for a picture stored in another format (a JPEG, a GIF): a reader that only
        # decodes PNG asks for it this way, as the device asks for "<file>.webp". Same params as
        # AnkiConnect's media.get, so the request stays within its schema.
        converted = path is None and name.lower().endswith(".png") and len(name) > 4
        if converted:
            path = local(name[:-4])
        if path is None:
            raise not_found("Media file")
        with open(path, "rb") as f:
            data = f.read()
        if converted or name.lower().endswith(".webp"):
            data = webp_to_png(data)  # e-ink readers decode PNG, not WebP
        else:
            data = shrink_image(data, name)
        return {"filename": name, "data": base64.b64encode(data).decode("ascii")}

    def cards_update(self, params: dict) -> dict:
        from anki.cards import FSRSMemoryState

        card = self._card(params.get("cardId"))
        patch = params.get("patch") or {}
        if "fsrsMemory" in patch:
            # As Anki desktop: an SM-2 collection keeps no FSRS memory.
            if not self.col.get_config("fsrs", False):
                raise unsupported("FSRS is off in this collection.")
            memory = patch["fsrsMemory"]
            card.memory_state = None if memory is None else FSRSMemoryState(
                stability=float(memory["stabilityDays"]), difficulty=float(memory["difficulty"]))
        for key, attr in (("intervalDays", "ivl"), ("sm2EasePermille", "factor"), ("repetitions", "reps"),
                          ("lapses", "lapses")):
            if key in patch:
                setattr(card, attr, int(patch[key]))
        self.col.update_card(card)
        return self._card_json(self.col.get_card(card.id))

    def set_due_date(self, params: dict) -> dict:
        ids = [entity_id(i, "cardIds") for i in params.get("cardIds") or []]
        days = params.get("days")
        if not isinstance(days, str) or not days:
            raise invalid("days must be a string such as \"3\"")
        items = []
        for index, card_id in enumerate(ids):
            try:
                self._card(card_id)
                self.col.sched.set_due_date([card_id], days)
                items.append({"index": index, "result": {"changed": True}})
            except RpcError as error:
                items.append({"index": index, "error": error.to_json()})
        return {"items": items}

    # --- review history

    @staticmethod
    def _interval(ivl: int) -> dict:
        return {"value": ivl, "unit": "days"} if ivl >= 0 else {"value": -ivl, "unit": "seconds"}

    def reviews_list(self, params: dict) -> dict:
        if params.get("cardIds") is not None and params.get("deck") is not None:
            raise invalid("cardIds and deck are mutually exclusive")
        where, args = [], []
        if params.get("cardIds") is not None:
            ids = [entity_id(c, "cardIds") for c in params["cardIds"]]
            if not ids:
                return {"items": [], "nextCursor": None}
            where.append(f"cid in ({','.join('?' * len(ids))})")
            args += ids
        elif params.get("deck") is not None:
            deck_id = self._deck_id(params["deck"])
            deck_ids = [deck_id]
            if params.get("includeChildren"):
                deck_ids = list(self.col.decks.deck_and_child_ids(deck_id))
            where.append(f"cid in (select id from cards where did in ({','.join('?' * len(deck_ids))}) "
                         f"or odid in ({','.join('?' * len(deck_ids))}))")
            args += deck_ids + deck_ids
        after = params.get("cursor") or params.get("afterId")
        if after is not None:
            where.append("id > ?")
            args.append(entity_id(after, "afterId"))
        if params.get("includeManual") is False:
            where.append("type != 4")
        limit = max(1, min(int(params.get("limit", 100)), MAX_PAGE))
        sql = "select id, cid, usn, ease, ivl, lastIvl, factor, time, type from revlog"
        if where:
            sql += " where " + " and ".join(where)
        rows = self.col.db.all(sql + " order by id limit ?", *args, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{
            "id": str(r[0]), "cardId": str(r[1]), "answeredAt": stamp(r[0] / 1000),
            "rating": RATINGS[r[3] - 1] if 1 <= r[3] <= 4 else None,
            "interval": self._interval(r[4]), "previousInterval": self._interval(r[5]),
            "factor": {"encoding": "fsrsNormalizedDifficulty" if 0 < r[6] <= 100 and r[8] != 4 and
                       self.col.get_config("fsrs", False) else "legacySm2EasePermille", "value": max(r[6], 0)},
            "elapsedMs": max(r[7], 0), "kind": KINDS[r[8]] if 0 <= r[8] < len(KINDS) else "manual",
            "syncSequence": r[2],
        } for r in rows]
        return {"items": items, "nextCursor": str(rows[-1][0]) if more and rows else None}

    def reviews_latest(self, params: dict) -> dict:
        if params.get("cardIds") is not None and params.get("deck") is not None:
            raise invalid("cardIds and deck are mutually exclusive")
        if params.get("cardIds") is not None:
            ids = [entity_id(c, "cardIds") for c in params["cardIds"]] or [0]
            latest = self.col.db.scalar(f"select max(id) from revlog where cid in ({','.join('?' * len(ids))})", *ids)
        elif params.get("deck") is not None:
            deck_id = self._deck_id(params["deck"])
            latest = self.col.db.scalar("select max(id) from revlog where cid in "
                                        "(select id from cards where did = ? or odid = ?)", deck_id, deck_id)
        else:
            latest = self.col.db.scalar("select max(id) from revlog")
        return {"id": str(latest) if latest else None}

    def reviews_import(self, params: dict) -> dict:
        def run():
            records = params.get("reviews") or []
            rows = []
            seen = set()
            for i, rec in enumerate(records):
                review_id = entity_id(rec.get("id"), f"reviews[{i}].id")
                card_id = entity_id(rec.get("cardId"), f"reviews[{i}].cardId")
                if review_id in seen or self.col.db.scalar("select 1 from revlog where id = ?", review_id):
                    raise RpcError(1008, "conflict", f"Review {review_id} already exists.")
                seen.add(review_id)
                self._card(card_id)
                rating = rec.get("rating")
                ease = RATINGS.index(rating) + 1 if rating in RATINGS else 0
                kind = rec.get("kind", "review")
                if kind not in KINDS:
                    raise invalid(f"reviews[{i}].kind is not a review kind")

                def ivl(value):
                    value = value or {"value": 0, "unit": "days"}
                    return int(value["value"]) if value.get("unit") == "days" else -int(value["value"])

                factor = int((rec.get("factor") or {}).get("value", 0))
                rows.append((review_id, card_id, -1, ease, ivl(rec.get("interval")), ivl(rec.get("previousInterval")),
                             factor, int(rec.get("elapsedMs", 0)), KINDS.index(kind)))
            for row in rows:
                self.col.db.execute("insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
                                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?)", *row)
            return {"changedCount": len(rows)}

        return self._operation("advanced.reviews.import", run)
