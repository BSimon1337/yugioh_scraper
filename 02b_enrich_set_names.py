import argparse
import importlib
import re
import sys
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db

YUGIPEDIA_API_URL = "https://yugipedia.com/api.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\s+", "", value)
    return value.strip()


def fetch_cards_to_enrich(connection, missing_only):
    where = "c.match_status = 'MATCHED'"
    if missing_only:
        where += """
            AND EXISTS (
                SELECT 1
                FROM printings p
                WHERE p.cid = c.cid
                  AND p.setname_en = ''
            )
        """

    return connection.execute(
        f"""
        SELECT DISTINCT c.cid,
               c.page_title,
               c.english_name
        FROM cards c
        WHERE {where}
          AND c.cid IS NOT NULL
          AND c.cid != ''
        ORDER BY c.english_name, c.page_title
        """
    ).fetchall()


def fetch_page_html(page_title):
    response = requests.get(
        YUGIPEDIA_API_URL,
        params={
            "action": "parse",
            "format": "json",
            "page": page_title,
            "prop": "text",
            "redirects": "1",
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise ValueError(data["error"].get("info", "Yugipedia parse error"))

    return data["parse"]["text"]["*"]


def parse_set_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    output = []

    for table in soup.select("table.card-list"):
        headers = [
            cell.get_text(" ", strip=True)
            for cell in table.select_one("tr").find_all(["th", "td"])
        ]
        if "Number" not in headers or "Set" not in headers:
            continue

        for row in table.select("tr")[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) < len(headers):
                continue

            values = dict(zip(headers, cells))
            number = values.get("Number", "").strip()
            setname_en = values.get("Set", "").strip()
            setname_jp = values.get("Japanese name", "").strip()

            if number and setname_en:
                output.append({
                    "cardnumber": number,
                    "setname_en": setname_en,
                    "setname_jp_normalized": normalize_text(setname_jp),
                })

    return output


def update_set_names(connection, cid, set_rows):
    updated = 0
    set_rows_by_number = {}
    for set_row in set_rows:
        set_rows_by_number.setdefault(set_row["cardnumber"], []).append(set_row)

    for cardnumber, cardnumber_rows in set_rows_by_number.items():
        printings = connection.execute(
            """
            SELECT id,
                   setname
            FROM printings
            WHERE cid = ?
              AND cardnumber = ?
            """,
            (cid, cardnumber),
        ).fetchall()

        for printing in printings:
            set_row = cardnumber_rows[0]
            if len(cardnumber_rows) > 1:
                scraped_set = normalize_text(printing["setname"])
                exact_rows = [
                    row for row in cardnumber_rows
                    if row["setname_jp_normalized"]
                    and row["setname_jp_normalized"] == scraped_set
                ]
                if exact_rows:
                    set_row = exact_rows[0]
                else:
                    continue

            connection.execute(
                """
                UPDATE printings
                SET setname_en = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (set_row["setname_en"], printing["id"]),
            )
            updated += 1

    return updated


def missing_set_name_count(connection):
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM printings
        WHERE setname_en = ''
        """
    ).fetchone()[0]


def sync_printings_csv():
    resolver = importlib.import_module("03b_resolve_image_mappings")

    with connect_db() as connection:
        init_db(connection)
        return resolver.sync_printings_csv(connection)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Enrich scraped printings with English set names from Yugipedia."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Refresh all matched cards instead of only printings missing English set names.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Delay between Yugipedia page requests in seconds. Defaults to 0.25.",
    )
    parser.add_argument(
        "--no-sync-csv",
        action="store_true",
        help="Do not rewrite konami_printings.csv after updating SQLite.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    with connect_db() as connection:
        init_db(connection)
        cards = fetch_cards_to_enrich(connection, missing_only=not args.all)
        total_updated = 0
        failed = 0

        print(f"Enriching {len(cards)} card page(s).")

        for index, card in enumerate(cards, start=1):
            print(f"[{index}/{len(cards)}] {card['english_name']}")
            try:
                html = fetch_page_html(card["page_title"])
                set_rows = parse_set_rows(html)
                updated = update_set_names(connection, card["cid"], set_rows)
                connection.commit()
                total_updated += updated
                print(f"  Updated {updated} printing row(s).")
            except Exception as error:
                failed += 1
                print(f"  ERROR: {error}")

            if args.delay > 0:
                time.sleep(args.delay)

        remaining = missing_set_name_count(connection)

    synced = 0
    if not args.no_sync_csv:
        synced = sync_printings_csv()

    print(f"Updated {total_updated} printing row(s).")
    print(f"Missing English set names: {remaining}.")
    print(f"Failed page(s): {failed}.")
    if not args.no_sync_csv:
        print(f"Synced {synced} row(s) to konami_printings.csv.")


if __name__ == "__main__":
    main()
