"""The Migaku bridge: serves the user's Migaku decks to the device through the AnkiConnect API.

/migaku/api/v1 answers from the mirror collection (migaku_mirror.py). Its collection.sync runs
sync() below: Migaku's cards and reviews into the mirror, then the device's reviews and mined words
from the mirror to Migaku (migaku_client.py speaks Migaku's own protocol).
"""
from __future__ import annotations

import logging
import os

from anki.collection import Collection

from .api import Api

log = logging.getLogger(__name__)


def make_api(data_dir: str, email: str, password: str) -> Api:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "collection.anki2")
    if not email:
        log.info("No MIGAKU_EMAIL: the Migaku bridge answers, but cannot sync with Migaku.")
        return Api(path, None, service="Migaku")
    from .migaku_sync import Bridge

    bridge = Bridge(data_dir, email, password)
    return Api(path, None, syncer=bridge.sync, service="Migaku", media_source=bridge.media)


def close(api: Api) -> None:
    api.col.close()


__all__ = ["make_api", "close", "Collection"]
