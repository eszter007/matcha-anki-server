"""One Migaku bridge sync: Migaku into the mirror collection, the device's work back to Migaku.

The server keeps its own copy of the Migaku collection (migaku/srs.db, the SQLite file Migaku's apps
keep too), brought up to date with Migaku's pull protocol. From it the mirror (migaku_mirror.py) is
rebuilt; what the device did in the mirror (reviews, mined words) is written to the copy with
Migaku's own scheduling rules and pushed. See docs/migaku-protocol.md.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

from anki.collection import Collection

from .api import RpcError
from .migaku_client import Client, MigakuError
from .migaku_mirror import MigakuCard, MigakuReview, Mirror

log = logging.getLogger(__name__)

EPOCH = dt.date(2020, 1, 1)  # Migaku's day 0; a day starts at 4 am local time
DAY_START = dt.timedelta(hours=4)
LEARN, FAIL, PASS = 0, 1, 2
ENCRYPTED = "◴" * 6  # course content Migaku ships encrypted: not shown
BOOL_COLUMNS = {"isSeed", "suspended", "isSample", "isPendingEnqueue", "isPendingApply"}
PULL_TABLES = {"decks": "deck", "cardTypes": "card_type", "cards": "card", "reviews": "review"}
MAX_PUSH_ROWS = 1000

LANG_NAMES = {"ja": "Japanese", "zh": "Chinese", "yue": "Cantonese", "ko": "Korean", "en": "English",
              "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
              "nl": "Dutch", "ru": "Russian", "vi": "Vietnamese", "tr": "Turkish", "pl": "Polish",
              "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "hu": "Hungarian",
              "uk": "Ukrainian", "ar": "Arabic", "th": "Thai", "id": "Indonesian"}
# The device names its mined decks "Matcha::<autonym>" (FlashcardStore.cpp autonym()).
AUTONYMS = {"日本語": "ja", "中文": "zh", "English": "en", "Deutsch": "de", "Français": "fr", "Español": "es",
            "Italiano": "it", "Português": "pt", "Nederlands": "nl", "Magyar": "hu", "Polski": "pl",
            "Русский": "ru", "Українська": "uk"}
CJK = {"ja", "zh", "yue"}
SYNTAX_TOKEN = re.compile(r"([^\s\[\]<>]+)\[([^\]]*)\]")
HAN_TAIL = re.compile(r"[㐀-鿿豈-﫿々〆ヵヶ]+$")
R2_ID = re.compile(r"/SRSMEDIA/([0-9a-fA-F-]{36})")


def migaku_day(ms: int) -> int:
    local = dt.datetime.fromtimestamp(ms / 1000).astimezone() - DAY_START
    return (local.date() - EPOCH).days


def now_ms() -> int:
    return int(time.time() * 1000)


# --- rendering Migaku fields as card HTML

def render_syntax(text: str, lang: str) -> str:
    """Migaku's syntax markup (word[reading,...]) as plain text, with ruby for Japanese and Chinese."""

    def token(m: re.Match) -> str:
        base, info = m.group(1), m.group(2)
        if lang in CJK:
            reading = re.split(r"[,;|]", info, maxsplit=1)[0].lstrip("!^")  # markers before the reading
            if reading and not reading.startswith("$") and reading != base:
                if lang == "ja":
                    kanji = HAN_TAIL.search(base)
                    if kanji:
                        return f"{base[:kanji.start()]}<ruby>{kanji.group(0)}<rt>{reading}</rt></ruby>"
                    return base
                return f"<ruby>{base}<rt>{reading}</rt></ruby>"
        return base

    out = SYNTAX_TOKEN.sub(token, text or "")
    out = out.replace("<t>", "<b>").replace("</t>", "</b>")
    if lang in CJK:  # Migaku separates words with spaces; CJK text has none
        out = re.sub(r"(?:(?<=[^\x00-\x7f])|(?<=>)) (?=[^\x00-\x7f]|<)", "", out)
    return out


def media_name(r2: str) -> str | None:
    m = R2_ID.search(r2)
    return f"mgk-{m.group(1).lower()}.webp" if m else None


class Rendered:
    def __init__(self):
        self.media: dict[str, str] = {}  # mirror media name -> r2 path


# Migaku's French definitions: a head line "word (ipa) ★★", then entries "∙ [lemma (g)<br>]pos. meaning".
DEF_HEAD = re.compile(r"^\s*<p>\s*([^<(★]+?)\s*(?:\(([^)<]*)\))?\s*(★*)\s*</p>")
DEF_ENTRY = re.compile(r"<p>\s*∙\s*(.*?)\s*</p>", re.S)
GENDER = re.compile(r"^(.*?)\s*\(([a-z]{1,2}(?:/[a-z]{1,2})?)\)$")
POS = re.compile(r"^([a-z]{1,6}\.)\s+(.*)$", re.S)
TAG = re.compile(r"<[^>]+>")
CODE = re.compile(r"`([^`<>]+)`")


def split_definitions(value: str, headword: str) -> tuple[str, list[str], str]:
    """(pronunciation, meta parts, definition html) from a Migaku definition; unparsed it is kept whole."""
    head = DEF_HEAD.match(value)
    entries = DEF_ENTRY.findall(value[head.end():] if head else value)
    if not entries:
        return "", [], value
    ipa, stars = (head.group(2) or "", head.group(3)) if head else ("", "")
    parsed = []
    for entry in entries:
        pre, _, post = entry.partition("<br>") if "<br>" in entry else ("", "", entry)
        gender = GENDER.match(pre.strip())
        lemma, g = (gender.group(1), gender.group(2)) if gender else (pre.strip(), "")
        pos = POS.match(post.strip())
        parsed.append((lemma, g, pos.group(1) if pos else "", pos.group(2) if pos else post.strip()))
    if len(parsed) == 1:
        lemma, g, pos, meaning = parsed[0]
        meta = [" ".join(x for x in (pos, f"({g})" if g else "") if x)] if pos or g else []
        own = lemma and lemma.lower() != headword.lower()
        body = f"{lemma}: {meaning}" if own else meaning
    else:
        meta, lines = [], []
        for i, (lemma, g, pos, meaning) in enumerate(parsed, 1):
            label = ", ".join(x for x in (" ".join(y for y in (lemma, f"({g})" if g else "") if y), pos) if x)
            lines.append(f"{i}. {label} {meaning}" if label else f"{i}. {meaning}")
        body = "<br>".join(lines)
    return ipa, meta + ([stars] if stars else []), body


def render_card(fields: list[str], config: dict, lang: str, out: Rendered) -> tuple[str, str]:
    """Front and back HTML. The back reads like a dictionary entry: headword, pronunciation, meaning,
    then the sentence, one picture and the notes."""
    infos = config.get("fields") or []
    if not infos:  # course cards: only the word and its reading are readable
        infos = [{"name": "Word", "type": "SYNTAX"}, {"name": "Reading", "type": "TEXT"}]
        front_idx, back_idx = [0], [0, 1]
    else:
        front_idx, back_idx = config.get("front") or [0], config.get("back") or list(range(len(infos)))

    def value(i: int) -> str:
        if i >= len(infos) or i >= len(fields):
            return ""
        v = fields[i] or ""
        if not v or v.startswith(ENCRYPTED):
            return ""
        kind = infos[i].get("type", "TEXT")
        if kind in ("AUDIO", "AUDIO_LONG"):
            return ""  # e-ink cards play no sound
        if kind == "IMAGE":
            for ref in v.split("|"):
                name_ = media_name(ref)
                if name_:  # one picture: it illustrates, the rest would fill pages
                    out.media[name_] = ref
                    return f'<img src="{name_}">'
            return ""
        return render_syntax(v, lang) if kind == "SYNTAX" else v

    def name(i: int) -> str:
        return infos[i].get("name", "") if i < len(infos) else ""

    front = []
    for i in front_idx:
        v = value(i)
        if v:
            front.append(f'<div class="{"mgk-word" if name(i) == "Word" else "mgk-cue"}">{v}</div>')

    shown = {name(i): value(i) for i in back_idx if value(i)}
    word = shown.pop("Word", "")
    ipa, meta, definition = split_definitions(shown.pop("Definitions", ""), TAG.sub("", word).strip())
    if "Reading" in shown:
        meta.insert(0, shown.pop("Reading"))
    back = []
    if word:
        back.append(f'<div class="mgk-head"><b>{word}</b>' + (f' <span class="mgk-ipa">/{ipa}/</span>' if ipa else "")
                    + "</div>")
    if meta:
        back.append(f'<div class="mgk-meta">{" · ".join(meta)}</div>')
    if definition:
        back.append(f'<div class="mgk-def">{definition}</div>')
    for field, css in (("Sentence", "mgk-sentence"), ("Translated Sentence", "mgk-trans"), ("Images", "mgk-image"),
                       ("Example Sentences", "mgk-ex")):
        if field in shown:
            back.append(f'<div class="{css}">{shown.pop(field)}</div>')
    notes = shown.pop("Notes", "")
    back += [f'<div class="mgk-field">{v}</div>' for v in shown.values()]
    if notes:  # Migaku marks the word in its notes as `word`
        back.append('<div class="mgk-note">' + CODE.sub(r"<b>\1</b>", notes) + "</div>")
    return "".join(front), "".join(back)


def deck_title(lang: str, name: str) -> str:
    """The mirror's deck name: Migaku names decks per language ("My Cards" in each), so the language
    comes first. A name with "::" (the device's own Matcha decks) is kept as it is."""
    if "::" in name:
        return name
    return f"{LANG_NAMES.get(lang, lang.upper())}::{name}"


# --- Migaku's scheduler (docs/migaku-protocol.md section 5)

def _bucket(deck: dict, elapsed: int) -> float:
    for limit in (10, 35, 100, 350):
        if elapsed <= limit:
            return float(deck[f"intervalFactor{limit}"])
    return float(deck["intervalFactor1000"])


def schedule(card: dict, deck: dict, kind: int, today: int) -> tuple[dict, dict]:
    """(card fields to change, review values) for one answer, as Migaku's apps compute them."""
    elapsed = today - int(card["lastReview"] or 0)
    interval, factor, lapses = float(card["interval"]), float(card["factor"]), int(card["lapseCount"])
    if kind == LEARN:
        new_interval = min(max(1.0 * float(deck["intervalFactor10"]), 1.0), 2.0)
        due = today + int(round(new_interval))  # Python rounds half to even, as Migaku does
        new_factor, review_interval, review_factor = float(deck["intervalFactor10"]), 0.0, 0.0
    elif kind == FAIL:
        new_interval, due, new_factor = interval, today, factor
        review_interval, review_factor = float(elapsed), factor
    else:
        f = _bucket(deck, elapsed)
        if lapses > 0:
            new_interval = max(interval * 0.25, 1.0)
        else:
            new_interval = max(elapsed * min(max(2.5 * f, 1.5), 4.5), interval * 1.5, 2.0)
        due = today + int(math.ceil(new_interval))
        new_factor, review_interval, review_factor = f, float(elapsed), factor
    same_day = int(card["lastReview"] or 0) == today
    changes = {
        "due": due, "balancedDue": due, "interval": new_interval, "factor": new_factor, "lastReview": today,
        "reviewCount": int(card["reviewCount"]) + (0 if same_day or lapses > 0 else 1),
        "passCount": int(card["passCount"]) + (1 if kind == PASS and not (lapses > 0 or same_day) else 0),
        "failCount": int(card["failCount"]) + (1 if kind == FAIL and lapses == 0 else 0),
        "lapseCount": lapses + 1 if kind == FAIL else 0,
    }
    review = {"day": today, "interval": review_interval, "factor": review_factor, "type": kind, "lapseIndex": lapses}
    return changes, review


# --- the bridge

class Bridge:
    def __init__(self, data_dir: str, email: str, password: str):
        self.dir = data_dir
        self.client = Client(data_dir, email, password)
        self.db_path = os.path.join(data_dir, "srs.db")
        self.state_path = os.path.join(data_dir, "migaku-state.json")
        self.last_id = 0
        self.lock = threading.Lock()

    # state: the pull cursor

    def _cursor(self) -> int:
        if not os.path.exists(self.state_path) or not os.path.exists(self.db_path):
            return 0
        with open(self.state_path, encoding="utf-8") as f:
            return int(json.load(f).get("cursor", 0))

    def _set_cursor(self, cursor: int) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cursor": cursor}, f)
        os.replace(tmp, self.state_path)

    def _next_id(self) -> int:
        """Ids are milliseconds, strictly increasing, as Migaku's apps make them."""
        self.last_id = max(now_ms(), self.last_id + 1)
        return self.last_id

    # pull

    def _download(self, db: sqlite3.Connection | None) -> int:
        if db is not None:
            db.close()
        version = self.client.server_version()
        data = self.client.download_database()
        tmp = self.db_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.db_path)
        self._set_cursor(version)
        log.info("Migaku collection downloaded (%d bytes, version %d)", len(data), version)
        return version

    @staticmethod
    def _upsert(db: sqlite3.Connection, table: str, row: dict, columns: list[str]) -> None:
        cols = [c for c in columns if c in row]
        values = [int(row[c]) if isinstance(row[c], bool) else row[c] for c in cols]
        keys = [c for c in ("id",) if c in cols] or None
        if keys is None:
            return  # tables keyed otherwise are not used by the bridge
        updates = ", ".join(f'"{c}" = excluded."{c}"' for c in cols if c != "id")
        db.execute(f'insert into "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) values '
                   f'({", ".join("?" * len(cols))}) on conflict(id) do update set {updates} '
                   f'where excluded.serverVersion > "{table}".serverVersion',

                   values)

    def _pull(self, db: sqlite3.Connection) -> int:
        cursor = self._cursor()
        columns = {t: [r[1] for r in db.execute(f'pragma table_info("{t}")')] for t in PULL_TABLES.values()}
        rows = 0
        for _ in range(200):
            page, new_cursor, latest = self.client.pull(cursor)
            for key, table in PULL_TABLES.items():
                for row in page.get(key) or []:
                    self._upsert(db, table, row, columns[table])
                    rows += 1
            db.commit()
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            self._set_cursor(cursor)
            if latest <= cursor:
                break
        if rows:
            log.info("Migaku pull: %d rows, now at version %d", rows, cursor)
        return cursor

    def _open(self) -> sqlite3.Connection:
        if self._cursor() == 0:
            self._download(None)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    # the mirror

    def _cards(self, db: sqlite3.Connection, rendered: Rendered):
        decks = {r["id"]: r for r in db.execute("select * from deck where del = 0")}
        types = {}
        for r in db.execute("select id, config from card_type where del = 0"):
            try:
                types[r["id"]] = json.loads(r["config"] or "{}")
            except ValueError:
                types[r["id"]] = {}
        reviews: dict[int, list] = {}
        for r in db.execute("select id, cardId, day, type from review where del = 0 order by id"):
            reviews.setdefault(r["cardId"], []).append(r)
        for c in db.execute("select * from card where del = 0 and seedDel = 0"):
            deck = decks.get(c["deckId"])
            if deck is None:
                continue
            fields = [c["primaryField"] or "", c["secondaryField"] or ""] + (c["fields"] or "").split("\x1f")
            front, back = render_card(fields, types.get(c["cardTypeId"], {}), deck["lang"], rendered)
            if not front:
                continue  # nothing readable on the front
            history = reviews.get(c["id"], [])
            due = int(c["balancedDue"] or c["due"] or 0)
            out = []
            for i, r in enumerate(history):
                nxt = history[i + 1]["day"] if i + 1 < len(history) else (due if due > 0 else r["day"])
                out.append(MigakuReview(id=int(r["id"]), rating=1 if r["type"] == FAIL else 3,
                                        interval_days=max(0, int(nxt) - int(r["day"])),
                                        previous_interval_days=0))
            yield MigakuCard(id=str(c["id"]), deck_id=str(c["deckId"]), deck_name=deck_title(deck["lang"], deck["name"]),
                             front_html=front, back_html=back, media={}, new=int(c["due"] or 0) == 0,
                             suspended=bool(c["suspended"]), due_day=due or None,
                             interval_days=int(round(float(c["interval"] or 0))), reviews=out,
                             modified=int(c["mod"] or 0))

    # push

    def _row(self, db: sqlite3.Connection, table: str, row_id: int) -> dict:
        r = db.execute(f'select * from "{table}" where id = ?', (row_id,)).fetchone()
        return {k: (bool(r[k]) if k in BOOL_COLUMNS else r[k]) for k in r.keys()}

    def _push(self, db: sqlite3.Connection, obj: dict) -> None:
        """Sends rows already written to the copy as pending; a conflict pulls first, then sends again."""
        cursor = self._cursor()
        for attempt in range(3):
            if self.client.push(obj, cursor):
                return
            log.info("Migaku push conflict: pulling first (attempt %d)", attempt + 1)
            cursor = self._pull(db)
        raise MigakuError("Migaku kept refusing the changes (conflict).")

    def _mark_dirty(self, db: sqlite3.Connection, table: str, row_id: int, changes: dict) -> dict:
        changes = dict(changes, mod=now_ms(), serverMod=-1, isPendingEnqueue=1, isPendingApply=0)
        sets = ", ".join(f'"{k}" = ?' for k in changes)
        db.execute(f'update "{table}" set {sets} where id = ?', (*changes.values(), row_id))
        return self._row(db, table, row_id)

    def _push_reviews(self, db: sqlite3.Connection, mirror: Mirror) -> int:
        pending = mirror.device_reviews()
        if not pending:
            return 0
        cards, reviews, sent = {}, [], []
        decks = {r["id"]: dict(r) for r in db.execute("select * from deck where del = 0")}
        for migaku_id, review_id, ease, _ in pending:
            if db.execute("select 1 from review where id = ?", (review_id,)).fetchone():
                sent.append(review_id)  # Migaku has it already (sent before, answer lost)
                continue
            row = db.execute("select * from card where id = ? and del = 0", (int(migaku_id),)).fetchone()
            if row is None or row["deckId"] not in decks:
                sent.append(review_id)  # the card is gone from Migaku: nothing to send it to
                continue
            card = dict(row)
            kind = LEARN if int(card["due"] or 0) == 0 else (FAIL if ease == 1 else PASS)
            day = migaku_day(review_id)
            changes, review = schedule(card, decks[card["deckId"]], kind, day)
            cards[card["id"]] = self._mark_dirty(db, "card", card["id"], changes)
            now = now_ms()
            db.execute("insert into review (id, mod, serverMod, del, day, interval, factor, cardId, deckId, duration, "
                       "type, lapseIndex, serverVersion, isPendingEnqueue, isPendingApply) "
                       "values (?, ?, -1, 0, ?, ?, ?, ?, ?, 0, ?, ?, 0, 1, 0)",
                       (review_id, now, review["day"], review["interval"], review["factor"], card["id"],
                        card["deckId"], review["type"], review["lapseIndex"]))
            reviews.append(self._row(db, "review", review_id))
            sent.append(review_id)
        db.commit()
        rows = list(cards.values()) + reviews
        for start in range(0, len(rows), MAX_PUSH_ROWS):
            batch = rows[start:start + MAX_PUSH_ROWS]
            self._push(db, {"cards": [r for r in batch if "cardTypeId" in r],
                            "reviews": [r for r in batch if "cardTypeId" not in r]})
        self._clear_pending(db, rows)
        mirror.mark_sent(sent)
        return len(reviews)

    def _clear_pending(self, db: sqlite3.Connection, rows: list[dict]) -> None:
        for r in rows:
            table = "card" if "cardTypeId" in r else "deck" if "intervalFactor10" in r else "review"
            db.execute(f'update "{table}" set isPendingEnqueue = 0, isPendingApply = 1, serverMod = 0 where id = ?',
                       (r["id"],))
        db.commit()

    def _language_of(self, deck_name: str) -> str | None:
        tail = deck_name.split("::")[-1]
        return AUTONYMS.get(tail) or (tail.lower() if tail.lower() in LANG_NAMES else None)

    def _push_mined(self, db: sqlite3.Connection, mirror: Mirror) -> int:
        notes = mirror.mined_notes()
        if not notes:
            return 0
        rows, links = [], []
        for n in notes:
            lang = self._language_of(n["deck"])
            if lang is None:
                log.error("Mined deck %s: language unknown, words left on the device side", n["deck"])
                continue
            ctype = db.execute("select id from card_type where del = 0 and lang = ? and name = 'Sentence' "
                               "order by id limit 1", (lang,)).fetchone()
            if ctype is None:
                log.error("No Migaku card type for %s yet: use Migaku once for that language", lang)
                continue
            deck = db.execute("select id from deck where del = 0 and lang = ? and name = ?",
                              (lang, n["deck"])).fetchone()
            if deck is None:
                deck_id = self._create_deck(db, lang, n["deck"])
                if deck_id is None:
                    continue
                rows.append(self._row(db, "deck", deck_id))
            else:
                deck_id = deck["id"]
            card_id = self._next_id()
            f = n["fields"]
            word = html.unescape(re.sub(r"<[^>]+>", "", f.get("Word", ""))).strip()
            reading = html.unescape(re.sub(r"<[^>]+>", "", f.get("Reading", ""))).strip()
            primary = f"{word}[{reading}]" if lang in CJK and reading and reading != word else word
            sentence = re.sub(r"</?b>", lambda m: "</t>" if m.group(0) == "</b>" else "<t>", f.get("Sentence", ""))
            source = ", ".join(x for x in (f.get("Book", ""), f.get("Author", "")) if x)
            metadata = json.dumps({"syntax": {"targetWordEdited": False, "sentenceEdited": False}})
            extra = ["", f.get("Definition", ""), "", f"From {source}" if source else "", "", "", "", metadata]
            now = now_ms()
            db.execute("insert into card (id, mod, serverMod, del, deckId, cardTypeId, created, primaryField, "
                       "secondaryField, fields, words, due, balancedDue, interval, factor, lastReview, reviewCount, "
                       "passCount, failCount, lapseCount, pos, lessonId, seedMod, seedDel, notes, serverVersion, "
                       "isPendingEnqueue, isPendingApply) values (?, ?, -1, 0, ?, ?, ?, ?, ?, ?, '', 0, 0, 0, 2.5, "
                       "0, 0, 0, 0, 0, 0, NULL, 0, 0, '', 0, 1, 0)",
                       (card_id, now, deck_id, ctype["id"], now, primary, sentence, "\x1f".join(extra)))
            rows.append(self._row(db, "card", card_id))
            links.append((str(card_id), n["card_id"]))
        db.commit()
        if not rows:
            return 0
        self._push(db, {"decks": [r for r in rows if "intervalFactor10" in r],
                        "cards": [r for r in rows if "cardTypeId" in r]})
        self._clear_pending(db, rows)
        for migaku_id, card_id in links:
            # modified 0: the next mirror build renders the card as Migaku shows it
            mirror.link(migaku_id, card_id, 0)
        return len(links)

    def _create_deck(self, db: sqlite3.Connection, lang: str, name: str) -> int | None:
        """A new Migaku deck with the settings of one the user has in that language (or any)."""
        model = db.execute("select * from deck where del = 0 and lang = ? and learningMaterialId is null "
                           "order by id limit 1", (lang,)).fetchone() or \
            db.execute("select * from deck where del = 0 and learningMaterialId is null order by id limit 1").fetchone()
        if model is None:
            log.error("No Migaku deck to copy settings from")
            return None
        row = dict(model)
        deck_id = self._next_id()
        row.update(id=deck_id, mod=now_ms(), serverMod=-1, lang=lang, name=name, learningMaterialId=None,
                   seedMod=0, seedDel=0, courseType="", serverVersion=0, isPendingEnqueue=1, isPendingApply=0,
                   lastRecalc=0)
        row["del"] = 0
        cols = list(row)
        db.execute(f'insert into deck ({", ".join(chr(34) + c + chr(34) for c in cols)}) values '
                   f'({", ".join("?" * len(cols))})', [row[c] for c in cols])
        return deck_id

    # the whole sync

    def sync(self, col: Collection) -> Collection:
        with self.lock:
            try:
                return self._sync(col)
            except MigakuError as error:
                log.error("Migaku sync failed: %s", error)
                if error.auth:
                    raise RpcError(1011, "authenticationRequired", str(error)) from None
                raise RpcError(1010, "remoteError", str(error)) from None

    def _sync(self, col: Collection) -> Collection:
        mirror = Mirror(col)
        db = self._open()
        try:
            self._pull(db)
            mined = self._push_mined(db, mirror)
            pushed = self._push_reviews(db, mirror)
            rendered = Rendered()
            today = migaku_day(now_ms())
            counts = mirror.apply(self._cards(db, rendered), today)
            dropped = mirror.drop_missing(counts["seen"])
            mirror.remember_media(rendered.media)
            log.info("Migaku sync: %d cards added, %d changed, %d reviews in, %d out, %d mined sent, %d dropped",
                     counts["added"], counts["changed"], counts["reviews"], pushed, mined, dropped)
        finally:
            db.close()
        return col

    def media(self, col: Collection, name: str) -> bytes | None:
        """An image the mirror's cards show, fetched from Migaku the first time a device asks."""
        r2 = Mirror(col).media_ref(name)
        if not r2:
            return None
        with self.lock:
            return self.client.media(r2)
