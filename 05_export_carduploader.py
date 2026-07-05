import argparse
import csv
from pathlib import Path
from urllib.parse import quote

from db import DB_PATH, connect_db, image_path, init_db

OUTPUT_CSV = "carduploader_export.csv"
SET_NAME_MAP_CSV = "set_name_map.csv"
MISSING_SET_NAME_MAP_CSV = "missing_set_name_map.csv"

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


def set_key(row):
    return row["setcode"], row["setname"]


def read_set_name_map(path):
    path = Path(path)
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = csv.DictReader(file)
        mapping = {}
        for row in rows:
            setcode = (row.get("setcode") or "").strip()
            setname = (row.get("setname") or "").strip()
            setname_en = (row.get("setname_en") or "").strip()
            if setcode and setname and setname_en:
                mapping[(setcode, setname)] = setname_en

    return mapping


def write_missing_set_name_map(path, rows):
    unique_sets = {}
    for row in rows:
        unique_sets.setdefault(set_key(row), row)

    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["setcode", "setname", "setname_en"])
        writer.writeheader()
        for key in sorted(unique_sets):
            setcode, setname = key
            writer.writerow({
                "setcode": setcode,
                "setname": setname,
                "setname_en": "",
            })


def set_name_for_row(row, set_name_source, set_name_map):
    if set_name_source == "original":
        return row["setname"]

    if set_name_source == "english":
        if row["setname_en"]:
            return row["setname_en"]

        return set_name_map.get(set_key(row), "")

    raise ValueError(f"Unknown set name source: {set_name_source}")


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
        SELECT p.id,
               p.cid,
               p.cardnumber,
               p.setname,
               p.setname_en,
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


def missing_set_name_rows(rows, set_name_map):
    missing = []
    seen = set()
    for row in rows:
        key = set_key(row)
        if row["setname_en"] or key in set_name_map or key in seen:
            continue

        seen.add(key)
        missing.append(row)

    return missing


def disambiguated_set_names(rows, set_name_source, set_name_map):
    set_names = {}
    grouped = {}

    for row in rows:
        setname = set_name_for_row(row, set_name_source, set_name_map)
        set_names[row["id"]] = setname
        key = (
            row["cardname"],
            row["cardnumber"],
            setname,
            row["rarity"],
            row["setcode"],
        )
        grouped.setdefault(key, set()).add(row["setname"])

    ambiguous_keys = {
        key for key, original_setnames in grouped.items()
        if len(original_setnames) > 1
    }

    for row in rows:
        key = (
            row["cardname"],
            row["cardnumber"],
            set_names[row["id"]],
            row["rarity"],
            row["setcode"],
        )
        if key in ambiguous_keys:
            set_names[row["id"]] = f"{set_names[row['id']]} ({row['setname']})"

    return set_names


def build_output_rows(rows, image_source, cdn_base_url, set_name_source, set_name_map):
    output_rows = []
    missing_image_mappings = []
    set_names = disambiguated_set_names(rows, set_name_source, set_name_map)

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
            "setname": set_names[row["id"]],
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
    parser.add_argument(
        "--set-name-source",
        choices=["original", "english"],
        default="english",
        help="Use English set names from SQLite/map or original scraped set names. Defaults to english.",
    )
    parser.add_argument(
        "--set-name-map",
        default=SET_NAME_MAP_CSV,
        help=f"CSV with setcode,setname,setname_en columns. Defaults to {SET_NAME_MAP_CSV}.",
    )
    parser.add_argument(
        "--missing-set-name-map",
        default=MISSING_SET_NAME_MAP_CSV,
        help=f"Template CSV written when English set mappings are missing. Defaults to {MISSING_SET_NAME_MAP_CSV}.",
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
        set_name_map = read_set_name_map(args.set_name_map)
        if args.set_name_source == "english":
            missing_set_rows = missing_set_name_rows(rows, set_name_map)
            if missing_set_rows:
                write_missing_set_name_map(args.missing_set_name_map, missing_set_rows)
                parser.error(
                    f"{len(missing_set_rows)} set name mapping(s) are missing. "
                    f"Fill {args.missing_set_name_map}, save it as {args.set_name_map}, and rerun."
                )

        output_rows = build_output_rows(
            rows,
            args.image_source,
            cdn_base_url,
            args.set_name_source,
            set_name_map,
        )

    write_csv(args.output, output_rows)

    skipped = len(rows) - len(output_rows)
    print(f"Fetched {len(rows)} reviewed printing row(s).")
    if skipped:
        print(f"Skipped {skipped} row(s).")
    print(f"Wrote {len(output_rows)} row(s) to {args.output}.")


if __name__ == "__main__":
    main()
