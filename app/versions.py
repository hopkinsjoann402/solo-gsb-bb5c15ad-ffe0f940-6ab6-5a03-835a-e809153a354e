"""Item-bank revision history.

Every content or availability change to a bank item appends one immutable
row to ``item_versions`` — inside the same serialised write transaction as
the change itself — carrying the change source and a full post-change
snapshot: stem, options, answer, difficulty, discrimination, content tag
and the enabled flag.  ``exposure_count`` is deliberately NOT part of the
snapshot: it changes with examinee traffic, not with bank edits.

Versions are consecutive per item (1, 2, 3, ...) and strictly append-only:
later imports never rewrite recorded history.  Same-value retries, dry-run
pre-checks and batches rejected by validation record nothing.  On startup
every pre-existing item without any version row receives exactly one
``baseline`` snapshot, so legacy databases acquire a history and repeated
restarts never duplicate it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .importer import NormalizedItem

SOURCE_BASELINE = "baseline"
SOURCE_IMPORT = "import"
SOURCE_AVAILABILITY = "availability"

SCHEMA_ITEM_VERSIONS = """
-- Append-only revision history of the item bank.  One row per recorded
-- change; (item_id, version) is the primary key and versions are
-- consecutive per item starting at 1.  ``source`` is 'baseline' (startup
-- backfill of a pre-existing bank), 'import' (bulk-import create or
-- content change) or 'availability' (withdraw / re-enable toggle).
CREATE TABLE IF NOT EXISTS item_versions (
    item_id        TEXT NOT NULL REFERENCES items(id),
    version        INTEGER NOT NULL CHECK (version >= 1),
    source         TEXT NOT NULL
                   CHECK (source IN ('baseline', 'import', 'availability')),
    difficulty     REAL NOT NULL,
    discrimination REAL NOT NULL,
    content_tag    TEXT,
    stem           TEXT NOT NULL,
    options        TEXT NOT NULL, -- JSON snapshot: [{"key": "A", "text": "..."}]
    answer         TEXT NOT NULL,
    is_active      INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at     TEXT NOT NULL,
    PRIMARY KEY (item_id, version)
);
"""

_INSERT_VERSION_SQL = """
INSERT INTO item_versions
    (item_id, version, source, difficulty, discrimination,
     content_tag, stem, options, answer, is_active, created_at)
VALUES (
    ?,
    -- Next consecutive version for this item.  Writers are serialised by
    -- BEGIN IMMEDIATE, so MAX+1 cannot race and versions stay gap-free.
    (SELECT COALESCE(MAX(version), 0) + 1 FROM item_versions
     WHERE item_id = ?),
    ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""

_ENSURE_BASELINES_SQL = """
INSERT INTO item_versions
    (item_id, version, source, difficulty, discrimination,
     content_tag, stem, options, answer, is_active, created_at)
SELECT i.id, 1, ?, i.difficulty, i.discrimination,
       i.content_tag, i.stem, i.options, i.answer, i.is_active, ?
FROM items i
WHERE NOT EXISTS (
    SELECT 1 FROM item_versions v WHERE v.item_id = i.id
)
"""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_version(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    source: str,
    difficulty: float,
    discrimination: float,
    content_tag: str | None,
    stem: str,
    options_json: str,
    answer: str,
    is_active: bool | int,
    created_at: str,
) -> None:
    """Append the next consecutive version of one item.

    Must run inside the caller's serialised write transaction so the
    snapshot and the change it describes commit (or roll back) together.
    """
    conn.execute(
        _INSERT_VERSION_SQL,
        (
            item_id,
            item_id,
            source,
            difficulty,
            discrimination,
            content_tag,
            stem,
            options_json,
            answer,
            1 if is_active else 0,
            created_at,
        ),
    )


def record_version_for_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    source: str,
    created_at: str,
) -> None:
    """Snapshot the current ``items`` row as the item's next version."""
    record_version(
        conn,
        item_id=row["id"],
        source=source,
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        content_tag=row["content_tag"],
        stem=row["stem"],
        options_json=row["options"],
        answer=row["answer"],
        is_active=row["is_active"],
        created_at=created_at,
    )


def record_version_for_import(
    conn: sqlite3.Connection,
    item: NormalizedItem,
    *,
    is_active: bool | int,
    created_at: str,
) -> None:
    """Snapshot a validated import row as the item's next version.

    ``is_active`` is the flag the bank row carries *after* the upsert: 1
    for a brand-new item (column default), the preserved current value for
    an updated one.
    """
    record_version(
        conn,
        item_id=item.id,
        source=SOURCE_IMPORT,
        difficulty=item.difficulty,
        discrimination=item.discrimination,
        content_tag=item.content_tag,
        stem=item.stem,
        options_json=json.dumps(item.options, ensure_ascii=False),
        answer=item.answer,
        is_active=is_active,
        created_at=created_at,
    )


def import_content_changed(
    existing: sqlite3.Row, item: NormalizedItem
) -> bool:
    """True when a re-imported row alters any snapshot-covered content field.

    ``is_active`` and ``exposure_count`` are not compared: an import never
    changes them, and a same-value re-import must not produce a version.
    Options are compared structurally so JSON key order cannot cause a
    spurious "change".
    """
    return (
        existing["difficulty"] != item.difficulty
        or existing["discrimination"] != item.discrimination
        or existing["content_tag"] != item.content_tag
        or existing["stem"] != item.stem
        or existing["answer"] != item.answer
        or json.loads(existing["options"]) != item.options
    )


def ensure_baseline_versions(conn: sqlite3.Connection) -> None:
    """Give every version-less item its version-1 baseline snapshot.

    Idempotent: items that already have any version row are skipped, so
    repeated startups never duplicate baselines.
    """
    conn.execute(_ENSURE_BASELINES_SQL, (SOURCE_BASELINE, utc_now_text()))
