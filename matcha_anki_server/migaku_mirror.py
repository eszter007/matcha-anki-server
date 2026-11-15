"""The Migaku bridge's own Anki collection: a mirror of the user's Migaku decks.

The device talks to this collection through the same AnkiConnect API as to the Anki one. Each sync
(collection.sync on /migaku/api/v1) brings Migaku's cards and reviews in here, then sends what the
device did here (its reviews and its mined words) back to Migaku. Migaku holds the truth: the mirror
can be deleted at any time and is rebuilt by the next sync.

Which mirror card is which Migaku card is kept in the bridge's own table (bridge_cards), not in the
notes, so nothing of the bridge shows on a card.
"""
from __future__ import annotations

import dataclasses
import html
import logging
from typing import Iterable

from anki.collection import Collection

log = logging.getLogger(__name__)

NOTE_TYPE = "Migaku card"
FIELDS = ["Front", "Back"]
FRONT = "{{Front}}"
BACK = "{{Back}}"  # Migaku's back lists the front's fields itself
# Sized for an e-reader: the device draws .card at the reader's own size and keeps the rest close to it.
# The back reads like a dictionary entry (migaku_sync.render_card).
CSS = "\n".join([
    ".card { font-size: 20px; text-align: left; }",
    ".mgk-word { font-size: 1.25em; font-weight: bold; text-align: center; }",
    ".mgk-cue { margin-top: 0.5em; text-align: center; }",
    ".mgk-head { font-size: 1.25em; }",
    ".mgk-ipa { font-size: 0.8em; }",
    ".mgk-meta { font-size: 0.9em; }",
    ".mgk-def { margin-top: 0.5em; padding-top: 0.4em; border-top: 1px solid #000; }",
    ".mgk-sentence { margin-top: 0.7em; margin-left: 0.8em; font-style: italic; }",
    ".mgk-trans { margin-left: 0.8em; font-size: 0.9em; }",
    ".mgk-image { margin-top: 0.4em; }",
    ".mgk-image img { height: 3.5em; }",
    ".mgk-ex { margin-top: 0.6em; font-size: 0.9em; }",
    ".mgk-field { margin-top: 0.6em; font-size: 0.9em; }",
    ".mgk-note { margin-top: 0.6em; font-size: 0.85em; }",
])
MINED_NOTE_TYPE = "Matcha sentence"  # what the device's mined words arrive as (AnkiSync.cpp)

# revlog.usn marks where a review came from: the device's imports keep -1 until sent to Migaku.
USN_DEVICE = -1
USN_SENT = 1
USN_FROM_MIGAKU = 2


@dataclasses.dataclass
class MigakuReview:
    id: int  # ms since the epoch, unique per card
    rating: int  # 1 again .. 4 easy
    interval_days: int
    previous_interval_days: int


@dataclasses.dataclass
class MigakuCard:
    id: str
    deck_id: str
    deck_name: str
    front_html: str
    back_html: str
    media: dict[str, bytes]  # file name used in the html -> bytes
    new: bool
    suspended: bool
    due_day: int | None  # days since the epoch (UTC) when the card is due; None for new cards
    interval_days: int
    reviews: list[MigakuReview]
    modified: int  # Migaku's change counter or time; cards not changed since the last sync are skipped


class Mirror:
    def __init__(self, col: Collection):
        self.col = col
        self.col.db.execute("create table if not exists bridge_cards (migaku_id text primary key, "
                            "card_id integer not null unique, modified integer not null)")
        self.col.db.execute("create table if not exists bridge_state (key text primary key, value text)")
        self.col.db.execute("create table if not exists bridge_media (name text primary key, ref text not null)")
        self.model = self._note_type()

    # --- bridge state

    def state(self, key: str, default: str = "") -> str:
        value = self.col.db.scalar("select value from bridge_state where key = ?", key)
        return default if value is None else value

    def set_state(self, key: str, value: str) -> None:
        self.col.db.execute("insert or replace into bridge_state (key, value) values (?, ?)", key, value)

    def card_for(self, migaku_id: str) -> int | None:
        return self.col.db.scalar("select card_id from bridge_cards where migaku_id = ?", migaku_id)

    def migaku_for(self, card_id: int) -> str | None:
        return self.col.db.scalar("select migaku_id from bridge_cards where card_id = ?", card_id)

    # --- pull: Migaku into the mirror

    def _note_type(self):
        model = self.col.models.by_name(NOTE_TYPE)
        if model:
            if model.get("css") != CSS:  # a mirror made by an older version
                model["css"] = CSS
                self.col.models.save(model)
            return model
        mm = self.col.models
        model = mm.new(NOTE_TYPE)
        for name in FIELDS:
            mm.add_field(model, mm.new_field(name))
        template = mm.new_template("Card 1")
        template["qfmt"] = FRONT
        template["afmt"] = BACK
        mm.add_template(model, template)
        model["css"] = CSS
        mm.add(model)
        return mm.by_name(NOTE_TYPE)

    def deck(self, name: str) -> int:
        return self.col.decks.id(name, create=True)

    def apply(self, cards: Iterable[MigakuCard], today: int) -> dict:
        """Brings the given Migaku cards into the mirror. Returns counts for the log."""
        added = changed = reviews = 0
        seen: set[str] = set()
        for mc in cards:
            seen.add(mc.id)
            card_id = self.card_for(mc.id)
            last = self.col.db.scalar("select modified from bridge_cards where migaku_id = ?", mc.id)
            if card_id is not None and not self.col.db.scalar("select 1 from cards where id = ?", card_id):
                self.col.db.execute("delete from bridge_cards where migaku_id = ?", mc.id)
                card_id = None
            for name, data in mc.media.items():
                if not self.col.media.have(name):
                    self.col.media.write_data(name, data)
            deck_id = self.deck(mc.deck_name)
            if card_id is None:
                note = self.col.new_note(self.model)
                note["Front"], note["Back"] = mc.front_html, mc.back_html
                self.col.add_note(note, deck_id)
                card_id = note.card_ids()[0]
                self.col.db.execute("insert into bridge_cards (migaku_id, card_id, modified) values (?, ?, ?)",
                                    mc.id, card_id, mc.modified)
                added += 1
            elif last == mc.modified:
                self._take_html(card_id, mc)  # unchanged in Migaku, but the card layout may be new
                continue
            else:
                changed += 1
            card = self.col.get_card(card_id)
            self._take_html(card_id, mc)
            if card.did != deck_id:
                self.col.set_deck([card_id], deck_id)
            reviews += self._take_reviews(card_id, mc.reviews)
            self._take_schedule(card_id, mc, today)
            self.col.db.execute("update bridge_cards set modified = ? where migaku_id = ?", mc.modified, mc.id)
        return {"added": added, "changed": changed, "reviews": reviews, "seen": seen}

    def drop_missing(self, seen: set[str]) -> int:
        """Cards gone from Migaku leave the mirror (only the mirror: nothing is deleted in Migaku)."""
        gone = [(m, c) for m, c in self.col.db.all("select migaku_id, card_id from bridge_cards") if m not in seen]
        if not gone:
            return 0
        notes = {self.col.get_card(c).nid for _, c in gone if self.col.db.scalar("select 1 from cards where id = ?", c)}
        if notes:
            self.col.remove_notes(list(notes))
        self.col.db.executemany("delete from bridge_cards where migaku_id = ?", [(m,) for m, _ in gone])
        return len(gone)

    def _take_html(self, card_id: int, mc: MigakuCard) -> None:
        note = self.col.get_card(card_id).note()
        if note["Front"] != mc.front_html or note["Back"] != mc.back_html:
            note["Front"], note["Back"] = mc.front_html, mc.back_html
            self.col.update_note(note)

    def _take_reviews(self, card_id: int, reviews: list[MigakuReview]) -> int:
        count = 0
        for r in reviews:
            if self.col.db.scalar("select 1 from revlog where id = ?", r.id):
                continue  # already here: pulled before, or the device's own review sent earlier
            self.col.db.execute("insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
                                "values (?, ?, ?, ?, ?, ?, 0, 0, 1)", r.id, card_id, USN_FROM_MIGAKU,
                                max(1, min(4, r.rating)), r.interval_days, r.previous_interval_days)
            count += 1
        return count

    def _take_schedule(self, card_id: int, mc: MigakuCard, today: int) -> None:
        card = self.col.get_card(card_id)
        if mc.suspended:
            self.col.sched.suspend_cards([card_id])
        elif card.queue == -1:
            self.col.sched.unsuspend_cards([card_id])
        if mc.new or mc.due_day is None:
            if card.type != 0:
                self.col.sched.schedule_cards_as_new([card_id], restore_position=True, reset_counts=False)
            return
        days = max(0, mc.due_day - today)
        self.col.sched.set_due_date([card_id], str(days))
        card = self.col.get_card(card_id)
        if mc.interval_days > 0 and card.ivl != mc.interval_days:
            card.ivl = mc.interval_days
            self.col.update_card(card)

    # --- push: what the device did here, for Migaku

    def device_reviews(self) -> list[tuple[str, int, int, int]]:
        """Reviews the device imported and Migaku does not have yet: (migaku id, review id, rating, mirror card id)."""
        # ease 0 / type 4: Anki's own "rescheduled" entries from setDueDate, not reviews.
        rows = self.col.db.all("select r.id, r.cid, r.ease, b.migaku_id from revlog r join bridge_cards b "
                               "on b.card_id = r.cid where r.usn = ? and r.ease > 0 and r.type != 4 "
                               "order by r.id", USN_DEVICE)
        return [(m, rid, ease, cid) for rid, cid, ease, m in rows]

    def mark_sent(self, review_ids: list[int]) -> None:
        self.col.db.executemany("update revlog set usn = ? where id = ?", [(USN_SENT, i) for i in review_ids])

    def mined_notes(self) -> list[dict]:
        """Mined words the device added here that are not Migaku cards yet."""
        model = self.col.models.by_name(MINED_NOTE_TYPE)
        if not model:
            return []
        out = []
        for card_id in self.col.find_cards(f'"note:{MINED_NOTE_TYPE}"'):
            if self.migaku_for(card_id):
                continue
            card = self.col.get_card(card_id)
            note = card.note()
            out.append({"card_id": card_id, "deck": self.col.decks.name(card.did),
                        "fields": {name: note[name] for name in note.keys()}})
        return out

    def remember_media(self, refs: dict[str, str]) -> None:
        """Image names on the mirror's cards and where Migaku keeps them; fetched when first asked for."""
        self.col.db.executemany("insert or replace into bridge_media (name, ref) values (?, ?)", list(refs.items()))

    def media_ref(self, name: str) -> str | None:
        return self.col.db.scalar("select ref from bridge_media where name = ?", name)

    def link(self, migaku_id: str, card_id: int, modified: int) -> None:
        self.col.db.execute("insert or replace into bridge_cards (migaku_id, card_id, modified) values (?, ?, ?)",
                            migaku_id, card_id, modified)


def plain(text: str) -> str:
    return html.escape(text or "", quote=False)
