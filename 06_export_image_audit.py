import argparse
import csv

from db import DB_PATH, connect_db, image_path, init_db

OUTPUT_CSV = "image_mapping_audit.csv"

OUTPUT_COLUMNS = [
    "cardname",
    "cardnumber",
    "rarity",
    "rarity_full",
    "setcode",
    "setname",
    "setname_en",
    "cid",
    "ciid",
    "image_mapping_source",
    "image_mapping_notes",
    "imageurl",
    "local_path",
    "validation_status",
    "validation_notes",
]


def fetch_audit_rows(connection):
    return connection.execute(
        """
        WITH card_names AS (
            SELECT cid,
                   COALESCE(MIN(NULLIF(english_name, '')), MIN(page_title), '') AS cardname
            FROM cards
            WHERE cid IS NOT NULL
              AND cid != ''
            GROUP BY cid
        )
        SELECT c.cardname,
               p.cardnumber,
               p.rarity,
               p.rarity_full,
               p.setcode,
               p.setname,
               p.setname_en,
               p.cid,
               p.ciid,
               p.image_mapping_source,
               p.image_mapping_notes,
               COALESCE(i.image_url, '') AS imageurl,
               COALESCE(i.local_path, '') AS local_path,
               COALESCE(i.validation_status, '') AS validation_status,
               COALESCE(i.validation_notes, '') AS validation_notes
        FROM printings p
        LEFT JOIN card_names c ON c.cid = p.cid
        LEFT JOIN images i ON i.cid = p.cid
                          AND i.ciid = p.ciid
        ORDER BY CAST(p.cid AS INTEGER), p.release_date DESC, p.cardnumber, p.rarity
        """
    ).fetchall()


def normalize_row(row):
    local_path = row["local_path"] or (image_path(row["cid"], row["ciid"]) if row["ciid"] else "")

    return {
        "cardname": row["cardname"],
        "cardnumber": row["cardnumber"],
        "rarity": row["rarity"],
        "rarity_full": row["rarity_full"],
        "setcode": row["setcode"],
        "setname": row["setname"],
        "setname_en": row["setname_en"],
        "cid": row["cid"],
        "ciid": row["ciid"],
        "image_mapping_source": row["image_mapping_source"],
        "image_mapping_notes": row["image_mapping_notes"],
        "imageurl": row["imageurl"],
        "local_path": local_path,
        "validation_status": row["validation_status"],
        "validation_notes": row["validation_notes"],
    }


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Export a reviewable audit of printing-to-image mappings."
    )
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite database path. Defaults to {DB_PATH}.")
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help=f"Output CSV path. Defaults to {OUTPUT_CSV}.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    with connect_db(args.db) as connection:
        init_db(connection)
        rows = [normalize_row(row) for row in fetch_audit_rows(connection)]

    write_csv(args.output, rows)
    print(f"Wrote {len(rows)} row(s) to {args.output}.")


if __name__ == "__main__":
    main()
