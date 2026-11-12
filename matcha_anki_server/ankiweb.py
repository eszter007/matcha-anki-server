"""Keeps the server's collection in step with AnkiWeb (or any Anki sync server).

The server's copy is only a working copy: AnkiWeb holds the truth. When the two cannot be
merged (a full sync), the server takes AnkiWeb's version, so a fresh or wiped server simply
downloads the collection again. That lets the server run without persistent storage.
"""
from __future__ import annotations

import json
import logging
import os
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


def sync(col: Collection, settings: SyncSettings) -> Collection:
    """One sync. Returns the collection to use afterwards (reopened after a full download)."""
    from anki.sync_pb2 import SyncCollectionResponse

    Required = SyncCollectionResponse.ChangesRequired
    auth = _auth(col, settings)
    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
    if out.required in (Required.FULL_DOWNLOAD, Required.FULL_SYNC):
        log.info("Full download from the sync server")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=False)
        col.reopen(after_full_sync=True)
    elif out.required == Required.FULL_UPLOAD:
        log.info("Full upload to the sync server")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
        col.reopen(after_full_sync=True)
    return col
