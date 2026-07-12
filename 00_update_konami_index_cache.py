import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db

YUGIPEDIA_API_URL = "https://yugipedia.com/api.php"
INDEX_PAGE = "List_of_Rush_Duel_cards_by_Konami_index_number"
INDEX_URL = f"https://yugipedia.com/wiki/{INDEX_PAGE}"
OUTPUT_CSV = "konami_index_cache.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

OUTPUT_COLUMNS = [
    "cid",
    "cardname",
    "page_title",
    "release_date",
    "yugipedia_url",
]


def normalize_cid(value):
    return re.sub(r"\D", "", str(value or ""))


def normalize_page_title(url):
    if not url:
        return ""
    title = unquote(url.rstrip("/").rsplit("/", 1)[-1])
    return title.replace("_", " ")


def normalize_release_date(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return ""

    for date_format in ("%d %B %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return value


def fetch_index_html(retries=4, timeout=90, delay=5):
    params = {
        "action": "parse",
        "page": INDEX_PAGE,
        "prop": "text",
        "format": "json",
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[{attempt}/{retries}] Fetching Yugipedia Konami index page...")
            response = requests.get(
                YUGIPEDIA_API_URL,
                params=params,
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["parse"]["text"]["*"]
        except Exception as error:
            last_error = error
            print(f"  ERROR: {error}")
            if attempt < retries:
                time.sleep(delay * attempt)

    raise RuntimeError(f"Could not fetch Yugipedia index page: {last_error}")


def row_from_cells(cells):
    if len(cells) < 3:
        return None

    cid = normalize_cid(cells[0].get_text(" ", strip=True))
    if not cid:
        return None

    card_cell = cells[1]
    cardname = card_cell.get_text(" ", strip=True)
    link = card_cell.find("a", href=True)
    yugipedia_url = urljoin("https://yugipedia.com", link["href"]) if link else ""
    release_date = normalize_release_date(cells[2].get_text(" ", strip=True))

    if not cardname or not re.match(r"^\d{4}-\d{2}-\d{2}$", release_date):
        return None

    return {
        "cid": cid,
        "cardname": cardname,
        "page_title": normalize_page_title(yugipedia_url) or cardname,
        "release_date": release_date,
        "yugipedia_url": yugipedia_url,
    }


def parse_table_rows(soup):
    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            row = row_from_cells(cells)
            if row:
                rows.append(row)
    return rows


def parse_text_rows(soup):
    rows = []
    pattern = re.compile(
        r"^\s*(?P<cid>\d{1,3}(?:,\d{3})+|\d{4,})\s+"
        r"(?P<cardname>.+?)\s+"
        r"(?P<date>\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*$"
    )

    for line in soup.get_text("\n", strip=True).splitlines():
        match = pattern.match(line)
        if not match:
            continue

        cardname = match.group("cardname").strip()
        rows.append({
            "cid": normalize_cid(match.group("cid")),
            "cardname": cardname,
            "page_title": cardname,
            "release_date": normalize_release_date(match.group("date")),
            "yugipedia_url": "",
        })
    return rows


def dedupe_rows(rows):
    by_cid = {}
    for row in rows:
        cid = row["cid"]
        if not cid:
            continue
        existing = by_cid.get(cid)
        if not existing:
            by_cid[cid] = row
            continue
        if row.get("yugipedia_url") and not existing.get("yugipedia_url"):
            by_cid[cid] = row
    return [by_cid[cid] for cid in sorted(by_cid, key=lambda value: int(value))]


def parse_index_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = parse_table_rows(soup)
    if not rows:
        rows = parse_text_rows(soup)
    return dedupe_rows(rows)


def write_csv(rows, output):
    with open(output, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def sync_sqlite(rows):
    with connect_db() as connection:
        init_db(connection)
        connection.execute("DELETE FROM konami_index_cache")
        for row in rows:
            connection.execute(
                """
                INSERT INTO konami_index_cache (
                    cid,
                    cardname,
                    page_title,
                    release_date,
                    yugipedia_url,
                    source_url
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cid) DO UPDATE SET
                    cardname = excluded.cardname,
                    page_title = excluded.page_title,
                    release_date = excluded.release_date,
                    yugipedia_url = excluded.yugipedia_url,
                    source_url = excluded.source_url,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    row["cid"],
                    row["cardname"],
                    row["page_title"],
                    row["release_date"],
                    row["yugipedia_url"],
                    INDEX_URL,
                ),
            )
        connection.commit()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build a local Konami CID cache from Yugipedia's Rush Duel index page."
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help=f"CSV output path. Defaults to {OUTPUT_CSV}.",
    )
    parser.add_argument(
        "--html",
        help="Optional saved HTML file to parse instead of fetching Yugipedia.",
    )
    parser.add_argument(
        "--no-sqlite",
        action="store_true",
        help="Write CSV only and do not sync the SQLite cache table.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Fetch retry count. Defaults to 4.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Fetch timeout in seconds. Defaults to 90.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8")
    else:
        html = fetch_index_html(retries=args.retries, timeout=args.timeout)

    rows = parse_index_rows(html)
    if not rows:
        raise SystemExit("No Konami index rows found.")

    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} row(s) to {args.output}.")

    if not args.no_sqlite:
        sync_sqlite(rows)
        print("Synced yugioh_rush.sqlite3.")


if __name__ == "__main__":
    main()
