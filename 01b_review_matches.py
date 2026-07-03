import argparse
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db

MATCHES_CSV = "konami_matches.csv"
KONAMI_BASE_URL = "https://www.db.yugioh-card.com"
KONAMI_SEARCH_URL = f"{KONAMI_BASE_URL}/rushdb/card_search.action"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

CSV_FIELDS = [
    "page_title",
    "english_name",
    "japanese_name",
    "search_text",
    "match_status",
    "konami_cid",
    "konami_name",
    "konami_url",
    "notes",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def build_konami_search_url(search_text):
    params = {
        "ope": "1",
        "sess": "1",
        "rp": "10",
        "mode": "",
        "sort": "1",
        "keyword": search_text,
        "stype": "1",
        "ctype": "",
        "othercon": "2",
        "starfr": "",
        "starto": "",
        "atkfr": "",
        "atkto": "",
        "deffr": "",
        "defto": "",
        "releaseDStart": "1",
        "releaseMStart": "1",
        "releaseYStart": "2020",
        "releaseDEnd": "",
        "releaseMEnd": "",
        "releaseYEnd": "",
        "legend_type": "",
    }

    return f"{KONAMI_SEARCH_URL}?{urlencode(params)}"


def detail_url(cid):
    return f"{KONAMI_SEARCH_URL}?ope=2&cid={cid}"


def fetch_search_candidates(search_text):
    response = requests.get(
        build_konami_search_url(search_text),
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    seen = set()

    for row in soup.select(".t_row"):
        cid_tag = row.select_one("input.cid")
        name_tag = row.select_one(".card_name")
        ruby_tag = row.select_one(".card_ruby")

        if not cid_tag:
            continue

        cid = cid_tag.get("value", "").strip()
        if not cid or cid in seen:
            continue

        seen.add(cid)
        name = name_tag.get_text("", strip=True) if name_tag else ""
        ruby = ruby_tag.get_text("", strip=True) if ruby_tag else ""

        candidates.append({
            "cid": cid,
            "name": name,
            "ruby": ruby,
            "url": detail_url(cid),
        })

    return candidates


def fetch_detail_summary(cid):
    response = requests.get(
        f"{detail_url(cid)}&request_locale=ja",
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    name_tag = soup.select_one(".card_name")
    name = name_tag.get_text("", strip=True) if name_tag else ""
    printings = []

    for row in soup.select("#update_list .t_row"):
        cardnumber = get_text(row, ".card_number")
        setname = get_text(row, ".pack_name")
        rarity = get_text(row, ".rarity p")
        release_date = get_text(row, ".time")

        printings.append({
            "cardnumber": cardnumber,
            "setname": setname,
            "rarity": rarity,
            "release_date": release_date,
        })

    return {
        "cid": cid,
        "name": name,
        "printing_count": len(printings),
        "printings": printings[:5],
    }


def get_text(parent, selector):
    tag = parent.select_one(selector)
    return tag.get_text("", strip=True) if tag else ""


def fetch_review_rows(connection):
    return connection.execute(
        """
        SELECT page_title,
               english_name,
               japanese_name,
               search_text,
               match_status,
               cid,
               konami_name,
               konami_url,
               notes
        FROM cards
        WHERE match_status != 'MATCHED'
        ORDER BY page_title
        """
    ).fetchall()


def fetch_card(connection, page_title):
    return connection.execute(
        """
        SELECT page_title,
               english_name,
               japanese_name,
               search_text,
               match_status,
               cid,
               konami_name,
               konami_url,
               notes
        FROM cards
        WHERE page_title = ?
        """,
        (page_title,),
    ).fetchone()


def fetch_all_cards(connection):
    return connection.execute(
        """
        SELECT page_title,
               cid,
               match_status,
               konami_name,
               konami_url,
               notes
        FROM cards
        ORDER BY page_title
        """
    ).fetchall()


def update_card(connection, page_title, cid, status, konami_name, notes):
    konami_url = detail_url(cid) if cid else ""

    connection.execute(
        """
        UPDATE cards
        SET cid = ?,
            match_status = ?,
            konami_name = ?,
            konami_url = ?,
            notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE page_title = ?
        """,
        (cid or None, status, konami_name, konami_url, notes, page_title),
    )
    connection.commit()

    sync_csv_row(page_title, cid, status, konami_name, konami_url, notes)


def sync_csv_from_db(connection):
    path = Path(MATCHES_CSV)
    if not path.exists():
        raise SystemExit(f"CSV not found: {MATCHES_CSV}")

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    cards_by_title = {row["page_title"]: row for row in fetch_all_cards(connection)}
    synced = 0

    for row in rows:
        card = cards_by_title.get(row.get("page_title"))
        if not card:
            continue

        row["match_status"] = card["match_status"]
        row["konami_cid"] = card["cid"] or ""
        row["konami_name"] = card["konami_name"]
        row["konami_url"] = card["konami_url"]
        row["notes"] = card["notes"]
        synced += 1

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return synced


def sync_csv_row(page_title, cid, status, konami_name, konami_url, notes):
    path = Path(MATCHES_CSV)
    if not path.exists():
        return

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    changed = False
    for row in rows:
        if row.get("page_title") != page_title:
            continue

        row["match_status"] = status
        row["konami_cid"] = cid or ""
        row["konami_name"] = konami_name
        row["konami_url"] = konami_url
        row["notes"] = notes
        changed = True
        break

    if not changed:
        return

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def infer_name_from_candidates(cid, candidates):
    for candidate in candidates:
        if candidate["cid"] == cid:
            return candidate["name"]

    return ""


def print_review_rows(rows):
    if not rows:
        print("No review rows found.")
        return

    for index, row in enumerate(rows, start=1):
        cid = row["cid"] or ""
        print(
            f"{index}. {row['page_title']} | {row['match_status']} | "
            f"current cid: {cid} | {row['notes']}"
        )


def print_candidates(row, include_details=False):
    print(f"{row['page_title']}")
    print(f"  English:  {row['english_name']}")
    print(f"  Japanese: {row['japanese_name']}")
    print(f"  Search:   {row['search_text']}")
    print(f"  Status:   {row['match_status']}")
    print()

    candidates = fetch_search_candidates(row["search_text"])
    if not candidates:
        print("No live Konami candidates found.")
        return candidates

    for index, candidate in enumerate(candidates, start=1):
        print(f"{index}. cid {candidate['cid']} | {candidate['name']}")
        if candidate["ruby"]:
            print(f"   ruby: {candidate['ruby']}")
        print(f"   url:  {candidate['url']}")

        if include_details:
            summary = fetch_detail_summary(candidate["cid"])
            print(f"   printings: {summary['printing_count']}")
            for printing in summary["printings"]:
                pieces = [
                    printing["cardnumber"],
                    printing["rarity"],
                    printing["release_date"],
                    printing["setname"],
                ]
                print(f"     - {' | '.join(piece for piece in pieces if piece)}")

    return candidates


def normalize_choice(value, candidates):
    value = value.strip()
    if not value:
        return ""

    if re.fullmatch(r"\d+", value):
        numeric = int(value)
        if 1 <= numeric <= len(candidates):
            return candidates[numeric - 1]["cid"]

    return value


def run_list(args):
    with connect_db() as connection:
        init_db(connection)
        print_review_rows(fetch_review_rows(connection))


def run_candidates(args):
    with connect_db() as connection:
        init_db(connection)
        row = fetch_card(connection, args.page_title)

    if not row:
        raise SystemExit(f"No card found for page title: {args.page_title}")

    print_candidates(row, include_details=args.details)


def run_set(args):
    with connect_db() as connection:
        init_db(connection)
        row = fetch_card(connection, args.page_title)
        if not row:
            raise SystemExit(f"No card found for page title: {args.page_title}")

        candidates = fetch_search_candidates(row["search_text"])
        konami_name = args.name or infer_name_from_candidates(args.cid, candidates)
        notes = args.notes or "manual review"
        update_card(connection, args.page_title, args.cid, "MATCHED", konami_name, notes)

    print(f"Updated {args.page_title}: MATCHED cid {args.cid}")


def run_no_match(args):
    notes = args.notes or "manual review: no Konami match"

    with connect_db() as connection:
        init_db(connection)
        row = fetch_card(connection, args.page_title)
        if not row:
            raise SystemExit(f"No card found for page title: {args.page_title}")

        update_card(connection, args.page_title, "", "NO_MATCH", "", notes)

    print(f"Updated {args.page_title}: NO_MATCH")


def run_sync_csv(args):
    with connect_db() as connection:
        init_db(connection)
        synced = sync_csv_from_db(connection)

    print(f"Synced {synced} row(s) from SQLite to {MATCHES_CSV}.")


def run_interactive(args):
    with connect_db() as connection:
        init_db(connection)
        rows = fetch_review_rows(connection)

        if not rows:
            print("No review rows found.")
            return

        for row in rows:
            print()
            candidates = print_candidates(row, include_details=args.details)
            print()
            choice = input("Choose candidate number/CID, n = NO_MATCH, s = skip, q = quit: ")
            choice = choice.strip().lower()

            if choice == "q":
                break
            if choice == "s" or not choice:
                continue
            if choice == "n":
                update_card(
                    connection,
                    row["page_title"],
                    "",
                    "NO_MATCH",
                    "",
                    "manual review: no Konami match",
                )
                print(f"Updated {row['page_title']}: NO_MATCH")
                continue

            cid = normalize_choice(choice, candidates)
            if not re.fullmatch(r"\d+", cid):
                print(f"Invalid CID: {cid}")
                continue

            konami_name = infer_name_from_candidates(cid, candidates)
            update_card(
                connection,
                row["page_title"],
                cid,
                "MATCHED",
                konami_name,
                "manual review",
            )
            print(f"Updated {row['page_title']}: MATCHED cid {cid}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Review and correct ambiguous Konami match rows."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List rows needing review.")
    list_parser.set_defaults(func=run_list)

    candidates_parser = subparsers.add_parser(
        "candidates",
        help="Show live Konami candidates for one page title.",
    )
    candidates_parser.add_argument("page_title")
    candidates_parser.add_argument(
        "--details",
        action="store_true",
        help="Also fetch a short printing summary for each candidate.",
    )
    candidates_parser.set_defaults(func=run_candidates)

    set_parser = subparsers.add_parser(
        "set",
        help="Mark one page title as MATCHED with a chosen CID.",
    )
    set_parser.add_argument("page_title")
    set_parser.add_argument("cid")
    set_parser.add_argument("--name", default="", help="Override Konami card name.")
    set_parser.add_argument("--notes", default="", help="Override review note.")
    set_parser.set_defaults(func=run_set)

    no_match_parser = subparsers.add_parser(
        "no-match",
        help="Mark one page title as NO_MATCH.",
    )
    no_match_parser.add_argument("page_title")
    no_match_parser.add_argument("--notes", default="", help="Override review note.")
    no_match_parser.set_defaults(func=run_no_match)

    sync_parser = subparsers.add_parser(
        "sync-csv",
        help="Rewrite review fields in konami_matches.csv from SQLite.",
    )
    sync_parser.set_defaults(func=run_sync_csv)

    interactive_parser = subparsers.add_parser(
        "interactive",
        help="Review the queue interactively.",
    )
    interactive_parser.add_argument(
        "--details",
        action="store_true",
        help="Also fetch a short printing summary for each candidate.",
    )
    interactive_parser.set_defaults(func=run_interactive)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
