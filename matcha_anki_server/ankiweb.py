"""Keeps the server's collection in step with AnkiWeb (or any Anki sync server).

The server's copy is only a working copy: AnkiWeb holds the truth. When the two cannot be
merged (a full sync), the server takes AnkiWeb's version, so a fresh or wiped server simply
downloads the collection again. That lets the server run without persistent storage. What the
server changed since its last sync (the reader's reviews and mined notes) is then copied into
the downloaded collection and sent with a normal sync, so a full sync does not lose it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from anki.collection import Collection
from anki.sync import SyncAuth

log = logging.getLogger(__name__)


@dataclass
class SyncSettings:
    username: str
    password: str
    endpoint: str | None  # None: AnkiWeb
    auth_file: str  # where the login key is cached, so the password is sent once


def _auth(col: Collection, settings: SyncSettings) -> SyncAuth:
    if os.path.exists(settings.auth_file):
        with open(settings.auth_file, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("username") == settings.username and saved.get("endpoint") == settings.endpoint:
            return SyncAuth(hkey=saved["hkey"], endpoint=saved.get("endpoint") or None)
    auth = col.sync_login(username=settings.username, password=settings.password, endpoint=settings.endpoint)
    with open(settings.auth_file, "w", encoding="utf-8") as f:
        json.dump({"username": settings.username, "endpoint": settings.endpoint, "hkey": auth.hkey}, f)
    os.chmod(settings.auth_file, 0o600)
    return auth


def _stamp_path(col: Collection) -> str:
    return col.path + ".synced"


def _write_stamp(col: Collection, started_ms: int) -> None:
    """Remembers, by the server's own clock, when the last successful sync started: every row made
    before then was sent. AnkiWeb's `ls` is its clock, which may differ by minutes."""
    with open(_stamp_path(col), "w", encoding="utf-8") as f:
        f.write(str(started_ms))


def _read_stamp(col: Collection, fallback_ms: int) -> int:
    try:
        with open(_stamp_path(col), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return fallback_ms - 5 * 60 * 1000  # Anki allows the clocks to differ by up to 5 minutes


# A note type both sides have is the same shape when its fields (position, name) and card
# templates (positions) match; other edits, such as CSS, do not matter for its notes.
SAME_SHAPE = """o.id in (select id from main.notetypes)
    and not exists (select ord, name from old.fields where ntid = o.id
                    except select ord, name from main.fields where ntid = o.id)
    and not exists (select ord, name from main.fields where ntid = o.id
                    except select ord, name from old.fields where ntid = o.id)
    and not exists (select ord from old.templates where ntid = o.id
                    except select ord from main.templates where ntid = o.id)
    and not exists (select ord from main.templates where ntid = o.id
                    except select ord from old.templates where ntid = o.id)"""


def _keep_unsent(col: Collection, before: str, last_ms: int) -> int:
    """Copies what the server changed since its last sync (usn -1 in `before`, the collection as
    it was before a full download) into the downloaded collection, in SQL and in one transaction:
    - decks and note types only when AnkiWeb has none with that id or name;
    - notes and cards only of note types new here or of the same shape on both sides;
    - a row AnkiWeb has: only when the server's is newer (the newer change wins, as in Anki) and
      still the same note type, note and template; a row AnkiWeb lacks: only when made after
      `last_ms`, the start of the last successful sync, so a deletion in Anki stays deleted; a
      new note only with at least one of its cards;
    - reviews of cards that are there, and tags.
    Returns how many rows were copied."""
    db = col.db
    db.execute("attach database ? as old", before)
    copied = 0

    def run() -> None:
        nonlocal copied

        def copy(sql: str, *args) -> None:
            nonlocal copied
            db.execute(sql, *args)
            copied += db.scalar("select changes()")

        copy("""insert into main.decks select * from old.decks o where o.usn = -1
                and o.id not in (select id from main.decks) and o.name not in (select name from main.decks)""")
        db.execute("drop table if exists temp.kept_nt")
        db.execute("""create temp table kept_nt as select o.id from old.notetypes o where o.usn = -1
                      and o.id not in (select id from main.notetypes)
                      and o.name not in (select name from main.notetypes)""")
        for table, key in (("notetypes", "id"), ("fields", "ntid"), ("templates", "ntid")):
            copy(f"insert into main.{table} select * from old.{table} where {key} in (select id from temp.kept_nt)")
        db.execute(f"insert into temp.kept_nt select o.id from old.notetypes o where {SAME_SHAPE}")
        copy("""insert or replace into main.notes select o.* from old.notes o where o.usn = -1
                and o.mid in (select id from temp.kept_nt)
                and (exists (select 1 from main.notes m where m.id = o.id and m.mid = o.mid and m.mod < o.mod)
                     or (o.id > ? and o.id not in (select id from main.notes)
                         and exists (select 1 from old.cards c where c.nid = o.id
                                     and c.did in (select id from main.decks))))""", last_ms)
        copy("""insert or replace into main.cards select o.* from old.cards o
                join main.notes n on n.id = o.nid and n.mid in (select id from temp.kept_nt)
                where o.usn = -1 and o.did in (select id from main.decks)
                and (exists (select 1 from main.cards m where m.id = o.id and m.nid = o.nid
                             and m.ord = o.ord and m.mod < o.mod)
                     or (o.id > ? and o.id not in (select id from main.cards)))""", last_ms)
        copy("""insert into main.revlog select * from old.revlog o where o.usn = -1
                and o.cid in (select id from main.cards) and o.id not in (select id from main.revlog)""")
        copy("insert or ignore into main.tags select * from old.tags where usn = -1")
        if copied:
            # Raw writes do not mark the collection changed; without it the next sync sends nothing.
            db.execute("update col set mod = ?", int(time.time() * 1000))
        db.execute("drop table temp.kept_nt")

    try:
        db.transact(run)
        for what, table in (("notes", "notes"), ("reviews", "revlog")):
            lost = db.scalar(f"select count() from old.{table} where usn = -1 "
                             f"and id not in (select id from main.{table})")
            if lost:
                log.warning("%d unsent %s could not be kept: changed or deleted in Anki", lost, what)
        return copied
    finally:
        db.execute("detach database old")


def sync(col: Collection, settings: SyncSettings) -> Collection:
    """One sync. Returns the collection to use afterwards (reopened after a full download)."""
    from anki.sync_pb2 import SyncCollectionResponse

    Required = SyncCollectionResponse.ChangesRequired
    auth = _auth(col, settings)
    started = int(time.time() * 1000)
    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
    if out.required in (Required.FULL_DOWNLOAD, Required.FULL_SYNC):
        # A collection that never synced has nothing of the reader's to keep.
        last_ls = col.db.scalar("select ls from col")
        log.info("Full download from the sync server")
        # Removed only once its rows are kept, so nothing of the reader's is lost for good.
        before = f"{col.path}.before-full-sync-{int(time.time())}"
        if last_ls > 0:
            # Through the collection's own connection: the newest changes may not be in the file yet.
            col.db.execute("vacuum into ?", before)
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=False)
        col.reopen(after_full_sync=True)
        if last_ls > 0:
            kept = _keep_unsent(col, before, _read_stamp(col, last_ls))
            os.remove(before)  # the kept rows are in the collection now, still marked unsent
            if kept:
                log.info("Kept %d unsent rows; sending them", kept)
                col.close()
                col.reopen()
                started = int(time.time() * 1000)
                again = col.sync_collection(auth, False)
                if again.required not in (Required.NO_CHANGES, Required.NORMAL_SYNC):
                    # The stamp stays, so the next full sync keeps these rows again.
                    log.error("The sync server asks for another full sync; the kept rows stay unsent")
                    return col
    elif out.required == Required.FULL_UPLOAD:
        log.info("Full upload to the sync server")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
        col.reopen(after_full_sync=True)
    _write_stamp(col, started)
    return col
