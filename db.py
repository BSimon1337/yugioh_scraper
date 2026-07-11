import sqlite3
from pathlib import Path

DB_PATH = "yugioh_rush.sqlite3"


def connect_db(path=DB_PATH):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_title TEXT NOT NULL UNIQUE,
            cid TEXT,
            english_name TEXT NOT NULL DEFAULT '',
            japanese_name TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            source_status TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            match_status TEXT NOT NULL,
            konami_name TEXT NOT NULL DEFAULT '',
            konami_url TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_cards_match_status
            ON cards(match_status);

        CREATE INDEX IF NOT EXISTS idx_cards_cid
            ON cards(cid);

        CREATE TABLE IF NOT EXISTS printings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cid TEXT NOT NULL,
            cardnumber TEXT NOT NULL DEFAULT '',
            setname TEXT NOT NULL DEFAULT '',
            setname_en TEXT NOT NULL DEFAULT '',
            rarity TEXT NOT NULL DEFAULT '',
            rarity_full TEXT NOT NULL DEFAULT '',
            setcode TEXT NOT NULL DEFAULT '',
            release_date TEXT NOT NULL DEFAULT '',
            pid TEXT NOT NULL DEFAULT '',
            ciid TEXT NOT NULL DEFAULT '',
            needs_image_review INTEGER NOT NULL DEFAULT 0,
            image_mapping_source TEXT NOT NULL DEFAULT '',
            image_mapping_notes TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cid, cardnumber, setname, rarity, rarity_full)
        );

        CREATE INDEX IF NOT EXISTS idx_printings_cid
            ON printings(cid);

        CREATE INDEX IF NOT EXISTS idx_printings_setcode
            ON printings(setcode);

        CREATE TABLE IF NOT EXISTS images (
            cid TEXT NOT NULL,
            ciid TEXT NOT NULL,
            image_url TEXT NOT NULL,
            local_path TEXT NOT NULL DEFAULT '',
            downloaded_at TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            image_format TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            validation_status TEXT NOT NULL DEFAULT '',
            validation_notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (cid, ciid)
        );

        CREATE INDEX IF NOT EXISTS idx_images_local_path
            ON images(local_path);
        """
    )
    ensure_columns(
        connection,
        "images",
        {
            "size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "width": "INTEGER NOT NULL DEFAULT 0",
            "height": "INTEGER NOT NULL DEFAULT 0",
            "image_format": "TEXT NOT NULL DEFAULT ''",
            "sha256": "TEXT NOT NULL DEFAULT ''",
            "validation_status": "TEXT NOT NULL DEFAULT ''",
            "validation_notes": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        connection,
        "cards",
        {
            "source_status": "TEXT NOT NULL DEFAULT ''",
            "source_file": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        connection,
        "printings",
        {
            "setname_en": "TEXT NOT NULL DEFAULT ''",
            "pid": "TEXT NOT NULL DEFAULT ''",
            "image_mapping_source": "TEXT NOT NULL DEFAULT ''",
            "image_mapping_notes": "TEXT NOT NULL DEFAULT ''",
        },
    )
    connection.commit()


def ensure_columns(connection, table, columns):
    existing = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }

    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def normalize_cid(value):
    value = str(value or "").strip()
    return value or None


def upsert_card_match(connection, row):
    cid = normalize_cid(row.get("konami_cid") or row.get("cid"))

    connection.execute(
        """
        INSERT INTO cards (
            page_title,
            cid,
            english_name,
            japanese_name,
            search_text,
            source_status,
            source_file,
            match_status,
            konami_name,
            konami_url,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_title) DO UPDATE SET
            cid = excluded.cid,
            english_name = excluded.english_name,
            japanese_name = excluded.japanese_name,
            search_text = excluded.search_text,
            source_status = excluded.source_status,
            source_file = excluded.source_file,
            match_status = excluded.match_status,
            konami_name = excluded.konami_name,
            konami_url = excluded.konami_url,
            notes = excluded.notes,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            row.get("page_title", ""),
            cid,
            row.get("english_name", ""),
            row.get("japanese_name", ""),
            row.get("search_text", ""),
            row.get("source_status", ""),
            row.get("source_file", ""),
            row.get("match_status", ""),
            row.get("konami_name", ""),
            row.get("konami_url", ""),
            row.get("notes", ""),
        ),
    )


def upsert_image(connection, image):
    cid = normalize_cid(image.get("cid"))
    ciid = str(image.get("ciid", "")).strip()

    if not cid or not ciid:
        return

    connection.execute(
        """
        INSERT INTO images (cid, ciid, image_url, local_path)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cid, ciid) DO UPDATE SET
            image_url = excluded.image_url,
            local_path = COALESCE(NULLIF(images.local_path, ''), excluded.local_path),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            cid,
            ciid,
            image.get("image_url", ""),
            image.get("local_path", ""),
        ),
    )


def upsert_printing(connection, row):
    cid = normalize_cid(row.get("konami_cid") or row.get("cid"))

    if not cid:
        return

    connection.execute(
        """
        INSERT INTO printings (
            cid,
            cardnumber,
            setname,
            setname_en,
            rarity,
            rarity_full,
            setcode,
            release_date,
            pid,
            ciid,
            needs_image_review,
            image_mapping_source,
            image_mapping_notes,
            source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cid, cardnumber, setname, rarity, rarity_full) DO UPDATE SET
            setname_en = COALESCE(NULLIF(excluded.setname_en, ''), printings.setname_en),
            setcode = excluded.setcode,
            release_date = excluded.release_date,
            pid = excluded.pid,
            ciid = excluded.ciid,
            needs_image_review = excluded.needs_image_review,
            image_mapping_source = excluded.image_mapping_source,
            image_mapping_notes = excluded.image_mapping_notes,
            source_url = excluded.source_url,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            cid,
            row.get("cardnumber", ""),
            row.get("setname", ""),
            row.get("setname_en", ""),
            row.get("rarity", ""),
            row.get("rarity_full", ""),
            row.get("setcode", ""),
            row.get("release_date", ""),
            row.get("pid", ""),
            row.get("ciid", ""),
            int(str(row.get("needs_image_review", "")).lower() == "true"),
            row.get("image_mapping_source", ""),
            row.get("image_mapping_notes", ""),
            row.get("source_url", ""),
        ),
    )


def image_path(cid, ciid, image_dir="images"):
    return str(Path(image_dir) / f"{cid}_{ciid}.jpg")
