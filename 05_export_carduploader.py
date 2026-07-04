import argparse
import csv
from pathlib import Path
from urllib.parse import quote

from db import DB_PATH, connect_db, image_path, init_db

OUTPUT_CSV = "carduploader_export.csv"

OUTPUT_COLUMNS = [
    "cardname",
    "cardnumber",
    "imageurl",
    "setname",
    "rarity",
    "setcode",
]


def normalized_cdn_base_url(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("--cdn-base-url is required when --image-source cdn")

    return value.rstrip("/") + "/"


def local_image_path(row):
    return row["local_path"] or image_path(row["cid"], row["ciid"])


def cdn_image_url(base_url, row):
    filename = Path(local_image_path(row)).name
    if not filename:
        raise ValueError(
            f"Printing {row['cid']} {row['cardnumber']} {row['rarity']} has no local image filename"
        )

    return base_url + quote(filename)


def image_url_for_row(row, image_source, cdn_base_url):
    if image_source == "konami":
        if not row["image_url"]:
            raise ValueError(
                f"Printing {row['cid']} {row['cardnumber']} {row['rarity']} has no Konami image URL"
            )
        return row["image_url"]

    if image_source == "local":
        path = Path(local_image_path(row))
        if not path.exists():
            raise FileNotFoundError(
                f"Missing local image for {row['cid']}_{row['ciid']}: {path}"
            )
        return str(path)

    if image_source == "cdn":
        return cdn_image_url(cdn_base_url, row)

    raise ValueError(f"Unknown image source: {image_source}")


def fetch_export_rows(connection):
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
        SELECT p.cid,
               p.cardnumber,
               p.setname,
               p.rarity,
               p.setcode,
               p.ciid,
               c.cardname,
               i.image_url,
               i.local_path
        FROM printings p
        LEFT JOIN card_names c ON c.cid = p.cid
        LEFT JOIN images i ON i.cid = p.cid
                          AND i.ciid = p.ciid
        WHERE p.needs_image_review = 0
        ORDER BY CAST(p.cid AS INTEGER), p.release_date DESC, p.cardnumber, p.rarity
        """
    ).fetchall()


def build_output_rows(rows, image_source, cdn_base_url):
    output_rows = []
    missing_image_mappings = []

    for row in rows:
        if not row["ciid"] or row["image_url"] is None:
            missing_image_mappings.append(
                f"{row['cid']} {row['cardnumber']} {row['rarity']} ciid={row['ciid']}"
            )
            continue

        output_rows.append({
            "cardname": row["cardname"],
            "cardnumber": row["cardnumber"],
            "imageurl": image_url_for_row(row, image_source, cdn_base_url),
            "setname": row["setname"],
            "rarity": row["rarity"],
            "setcode": row["setcode"],
        })

    if missing_image_mappings:
        examples = "; ".join(missing_image_mappings[:5])
        extra = "" if len(missing_image_mappings) <= 5 else f"; +{len(missing_image_mappings) - 5} more"
        raise ValueError(
            f"{len(missing_image_mappings)} exportable printing(s) have no joined image row: "
            f"{examples}{extra}"
        )

    return output_rows


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Export reviewed Rush Duel printings for the card uploader site."
    )
    parser.add_argument(
        "--db",
        default=DB_PATH,
        help=f"SQLite database path. Defaults to {DB_PATH}.",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help=f"Output CSV path. Defaults to {OUTPUT_CSV}.",
    )
    parser.add_argument(
        "--image-source",
        choices=["konami", "local", "cdn"],
        default="konami",
        help="Which image reference to put in the imageurl column. Defaults to konami.",
    )
    parser.add_argument(
        "--cdn-base-url",
        default="",
        help="Base URL for --image-source cdn, for example https://cdn.example.com/rush-images/.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not Path(args.db).exists():
        parser.error(f"SQLite database not found: {args.db}")

    cdn_base_url = ""
    if args.image_source == "cdn":
        try:
            cdn_base_url = normalized_cdn_base_url(args.cdn_base_url)
        except ValueError as error:
            parser.error(str(error))

    with connect_db(args.db) as connection:
        init_db(connection)
        rows = fetch_export_rows(connection)
        output_rows = build_output_rows(rows, args.image_source, cdn_base_url)

    write_csv(args.output, output_rows)

    skipped = len(rows) - len(output_rows)
    print(f"Fetched {len(rows)} reviewed printing row(s).")
    if skipped:
        print(f"Skipped {skipped} row(s).")
    print(f"Wrote {len(output_rows)} row(s) to {args.output}.")


if __name__ == "__main__":
    main()
