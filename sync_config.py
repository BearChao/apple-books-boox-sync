#!/usr/bin/env python3
"""Configuration and local database discovery for Apple Books <-> BOOX sync."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
VERSION = "0.1.0"
CONFIG_PATH = Path(os.environ.get("APPLE_BOOX_CONFIG", APP_DIR / "config.json")).expanduser().resolve()


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a JSON object: {CONFIG_PATH}")
    return data


CONFIG = load_config()
STATE_DIR = Path(
    os.environ.get("APPLE_BOOX_SYNC_STATE_DIR", CONFIG.get("state_dir", APP_DIR / "state"))
).expanduser().resolve()
BOOX_BOOKS_DIR = str(CONFIG.get("boox_books_dir", "/sdcard/Books")).rstrip("/")
MATCH_THRESHOLD = float(CONFIG.get("match_threshold", 0.80))
REQUIRE_COLLECTION_FOR_EVERY_BOOK = bool(CONFIG.get("require_collection_for_every_book", False))

if not 0.0 < MATCH_THRESHOLD <= 1.0:
    raise ValueError("match_threshold must be greater than 0 and at most 1")
if not BOOX_BOOKS_DIR.startswith("/"):
    raise ValueError("boox_books_dir must be an absolute Android path")


def discover_database(environment_name: str, config_name: str, directory: Path, pattern: str) -> Path:
    override = os.environ.get(environment_name) or CONFIG.get(config_name)
    if override:
        return Path(override).expanduser().resolve()
    candidates = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    # Return a useful diagnostic path instead of raising during module import,
    # so `--help` and `doctor` still work on a machine that is not configured.
    return directory / pattern.replace("*", "")


BOOKS_DB = discover_database(
    "APPLE_BOOKS_DB",
    "apple_books_db",
    Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary",
    "BKLibrary-*.sqlite",
)
ANNOTATION_DB = discover_database(
    "APPLE_BOOKS_ANNOTATION_DB",
    "apple_books_annotation_db",
    Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation",
    "AEAnnotation*_local.sqlite",
)


BUILTIN_COLLECTION_IDS = frozenset(
    {
        "All_Collection_ID",
        "AudioBooks_Collection_ID",
        "Books_Collection_ID",
        "Downloaded_Collection_ID",
        "Finished_Collection_ID",
        "Pdfs_Collection_ID",
        "Samples_Collection_ID",
        "Want_To_Read_Collection_ID",
    }
)


def collection_names() -> tuple[str, ...]:
    configured = CONFIG.get("collections")
    if configured is not None:
        if not isinstance(configured, list) or not all(isinstance(item, str) and item.strip() for item in configured):
            raise ValueError("collections must be null or an array of non-empty names")
        names = [item.strip() for item in configured]
    else:
        if not BOOKS_DB.is_file():
            raise FileNotFoundError(f"Apple Books database not found: {BOOKS_DB}")
        connection = sqlite3.connect(BOOKS_DB)
        try:
            rows = connection.execute(
                """
                SELECT ZTITLE, ZCOLLECTIONID
                FROM ZBKCOLLECTION
                WHERE COALESCE(ZDELETEDFLAG,0)=0
                  AND COALESCE(ZHIDDEN,0)=0
                  AND COALESCE(ZPLACEHOLDER,0)=0
                  AND ZTITLE IS NOT NULL
                ORDER BY COALESCE(ZSORTKEY,0), Z_PK
                """
            ).fetchall()
        finally:
            connection.close()
        names = [title for title, identifier in rows if identifier not in BUILTIN_COLLECTION_IDS]

    excluded = CONFIG.get("exclude_collections", [])
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise ValueError("exclude_collections must be an array of names")
    excluded_names = set(excluded)
    return tuple(dict.fromkeys(name for name in names if name not in excluded_names))
