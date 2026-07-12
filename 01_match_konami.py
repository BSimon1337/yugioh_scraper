import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db, upsert_card_match

INPUT_JSON = "result.json"
OUTPUT_CSV = "konami_matches.csv"

KONAMI_SEARCH_URL = "https://www.db.yugioh-card.com/rushdb/card_search.action"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def clean_wiki_name(value):
    if not value:
        return ""

    value = str(value).strip()

    match = re.match(r"^\[\[(?:[^|\]]+\|)?(.+?)\]\]$", value)
    if match:
        return match.group(1).strip()

    return value


def clean_japanese_name(value):
    if not value:
        return ""

    soup = BeautifulSoup(str(value), "html.parser")

    # Remove furigana pronunciation and ruby parentheses
    for tag in soup.find_all(["rt", "rp"]):
        tag.decompose()

    # Keep only the main visible card name text
    text = soup.get_text("", strip=True)

    # Normalize spacing
    text = re.sub(r"\s+", "", text)

    return text.strip()


def first_value(printouts, key):
    values = printouts.get(key, [])

    if not values:
        return ""

    return values[0]


def clean_status(value):
    if not value:
        return ""

    if isinstance(value, dict):
        return str(value.get("fulltext", "")).strip()

    return str(value).strip()


def load_yugipedia_cards(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cards = []

    for page_title, item in data["results"].items():
        printouts = item.get("printouts", {})

        raw_english_name = first_value(printouts, "Name")
        raw_japanese_name = first_value(printouts, "Japanese name")
        raw_status = first_value(printouts, "Status")

        english_name = clean_wiki_name(raw_english_name)
        japanese_name = clean_japanese_name(raw_japanese_name)
        source_status = clean_status(raw_status)

        cards.append({
            "page_title": page_title,
            "english_name": english_name,
            "japanese_name": japanese_name,
            "source_status": source_status,
        })

    return cards


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


def detail_url_ja(cid):
    return f"{detail_url(cid)}&request_locale=ja"


def normalize_match_text(value):
    value = str(value or "").strip()
    value = re.sub(r"\s+", "", value)
    value = value.replace("－", "-")
    return value


def normalize_cache_key(value):
    value = clean_wiki_name(value)
    value = str(value or "").strip().casefold()
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    return value


def add_cache_index(index, key, row):
    if not key:
        return
    index.setdefault(key, []).append(row)


def load_konami_index_cache():
    cache = {
        "page_title": {},
        "cardname": {},
    }

    with connect_db() as connection:
        init_db(connection)
        rows = connection.execute(
            """
            SELECT cid, cardname, page_title, release_date, yugipedia_url
            FROM konami_index_cache
            ORDER BY CAST(cid AS INTEGER)
            """
        ).fetchall()

    for row in rows:
        row = dict(row)
        add_cache_index(cache["page_title"], normalize_cache_key(row["page_title"]), row)
        add_cache_index(cache["cardname"], normalize_cache_key(row["cardname"]), row)

    return cache


def unique_cache_hit(rows):
    rows = rows or []
    cids = {row["cid"] for row in rows if row.get("cid")}
    if len(cids) == 1:
        return rows[0]
    return None


def find_index_cache_match(card, cache):
    page_key = normalize_cache_key(card.get("page_title"))
    name_key = normalize_cache_key(card.get("english_name"))

    page_match = unique_cache_hit(cache["page_title"].get(page_key))
    if page_match:
        return page_match, "YUGIPEDIA_INDEX_PAGE_TITLE"

    name_match = unique_cache_hit(cache["cardname"].get(name_key))
    if name_match:
        return name_match, "YUGIPEDIA_INDEX_CARDNAME"

    return None, ""


def verify_detail_page(cid):
    response = requests.get(detail_url_ja(cid), headers=HEADERS, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    has_printings = bool(soup.select("#update_list .t_row"))
    has_images = "get_image.action" in response.text
    headings = [
        tag.get_text(" ", strip=True)
        for tag in soup.select("h1")
        if tag.get_text(" ", strip=True)
    ]
    konami_name = headings[1] if len(headings) > 1 else ""

    if not has_printings and not has_images:
        raise ValueError(f"Konami detail page did not contain printings/images for cid {cid}")

    return {
        "cid": str(cid),
        "konami_name": konami_name,
        "konami_url": detail_url(cid),
    }


def search_konami(search_text, verbose=False):
    search_url = build_konami_search_url(search_text)

    response = requests.get(search_url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
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
        results.append({
            "cid": cid,
            "konami_name": name_tag.get_text("", strip=True) if name_tag else "",
            "konami_ruby": ruby_tag.get_text("", strip=True) if ruby_tag else "",
            "konami_url": detail_url(cid),
        })

    if verbose:
        print("HTML length:", len(response.text))
        print("Candidate rows found:", len(results))
        print("Search text:", search_text)
        for result in results[:10]:
            print(
                f"  cid {result['cid']} | {result['konami_name']} | "
                f"{result['konami_ruby']}"
            )

    return results


def choose_match(matches, search_text):
    if len(matches) == 1:
        return "MATCHED", matches[0]

    normalized_search = normalize_match_text(search_text)
    exact_matches = [
        match for match in matches
        if normalized_search
        and normalized_search in {
            normalize_match_text(match.get("konami_name")),
            normalize_match_text(match.get("konami_ruby")),
        }
    ]

    if len(exact_matches) == 1:
        return "MATCHED", exact_matches[0]

    if len(matches) > 1:
        return "REVIEW_MULTIPLE", matches[0]

    return "NO_MATCH", {
        "cid": "",
        "konami_name": "",
        "konami_url": ""
    }


def save_matches_to_db(rows):
    with connect_db() as connection:
        init_db(connection)

        for row in rows:
            upsert_card_match(connection, row)

        connection.commit()


OUTPUT_COLUMNS = [
    "page_title",
    "english_name",
    "japanese_name",
    "search_text",
    "source_status",
    "source_file",
    "match_status",
    "konami_cid",
    "konami_name",
    "konami_url",
    "notes",
]


def write_matches_csv(path, rows, merge_existing):
    path = Path(path)
    merged_rows = []

    if merge_existing and path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            merged_rows.extend(csv.DictReader(file))

    rows_by_title = {row.get("page_title", ""): row for row in merged_rows}
    for row in rows:
        rows_by_title[row["page_title"]] = row

    output_rows = [
        {field: row.get(field, "") for field in OUTPUT_COLUMNS}
        for row in rows_by_title.values()
        if row.get("page_title")
    ]
    output_rows.sort(key=lambda row: row["page_title"])

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(output_rows)

    return len(output_rows)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Match Yugipedia Rush Duel cards to Konami Rush Duel CIDs."
    )
    parser.add_argument(
        "--input",
        default=INPUT_JSON,
        help=f"Yugipedia JSON export path. Defaults to {INPUT_JSON}.",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help=f"Match CSV output path. Defaults to {OUTPUT_CSV}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum cards to match. Use 0 for all cards. Defaults to 0.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between Konami requests in seconds. Defaults to 1.0.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print search debug details for each card.",
    )
    parser.add_argument(
        "--include-unreleased",
        action="store_true",
        help="Also search cards whose Yugipedia Status is 'Not yet released'.",
    )
    parser.add_argument(
        "--merge-csv",
        action="store_true",
        help="Merge rows into the existing match CSV instead of replacing it.",
    )
    parser.add_argument(
        "--no-index-cache",
        action="store_true",
        help="Do not use the local Yugipedia Konami index cache before live search.",
    )
    parser.add_argument(
        "--verify-index-cache",
        action="store_true",
        help="Verify Yugipedia index cache hits by loading the direct Konami detail page.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_path = Path(args.input)
    source_file = str(input_path)
    cards = load_yugipedia_cards(input_path)

    output_rows = []

    cards_to_test = cards if args.limit == 0 else cards[:args.limit]
    index_cache = None
    if not args.no_index_cache:
        try:
            index_cache = load_konami_index_cache()
            cache_count = sum(len(rows) for rows in index_cache["page_title"].values())
            print(f"Loaded {cache_count} Yugipedia Konami index cache row(s).")
        except Exception as error:
            index_cache = None
            print(f"Could not load Yugipedia Konami index cache: {error}")

    for index, card in enumerate(cards_to_test, start=1):
        english_name = card["english_name"]
        japanese_name = card["japanese_name"]
        source_status = card["source_status"]

        search_text = japanese_name if japanese_name else english_name

        if source_status == "Not yet released" and not args.include_unreleased:
            print(
                f"[{index}/{len(cards_to_test)}] Skipping unreleased: "
                f"{english_name} / {japanese_name}",
                flush=True,
            )
            output_rows.append({
                "page_title": card["page_title"],
                "english_name": english_name,
                "japanese_name": japanese_name,
                "search_text": search_text,
                "source_status": source_status,
                "source_file": source_file,
                "match_status": "UNRELEASED",
                "konami_cid": "",
                "konami_name": "",
                "konami_url": "",
                "notes": "skipped: Yugipedia Status is Not yet released",
            })
            continue

        print(
            f"[{index}/{len(cards_to_test)}] Matching: {english_name} / {japanese_name}",
            flush=True,
        )

        try:
            cache_match = None
            cache_method = ""
            if index_cache:
                cache_match, cache_method = find_index_cache_match(card, index_cache)

            if cache_match:
                cid = cache_match["cid"]
                chosen = {
                    "cid": cid,
                    "konami_name": cache_match.get("cardname", ""),
                    "konami_url": detail_url(cid),
                }
                note = (
                    f"matched from Yugipedia Konami index cache "
                    f"({cache_method}); release_date={cache_match.get('release_date', '')}"
                )

                if args.verify_index_cache:
                    chosen = verify_detail_page(cid)
                    note += "; direct Konami detail page verified"

                print(f"  Cache hit {cid} via {cache_method}", flush=True)
                output_rows.append({
                    "page_title": card["page_title"],
                    "english_name": english_name,
                    "japanese_name": japanese_name,
                    "search_text": search_text,
                    "source_status": source_status,
                    "source_file": source_file,
                    "match_status": "MATCHED",
                    "konami_cid": chosen["cid"],
                    "konami_name": chosen["konami_name"],
                    "konami_url": chosen["konami_url"],
                    "notes": note,
                })
                if args.verify_index_cache and args.delay > 0:
                    time.sleep(args.delay)
                continue

            print("  Cache miss; searching Konami", flush=True)
            matches = search_konami(search_text, verbose=args.verbose)
            match_status, chosen = choose_match(matches, search_text)
            exact_note = ""
            if match_status == "MATCHED" and len(matches) > 1:
                exact_note = "; exact Konami name/ruby match"

            output_rows.append({
                "page_title": card["page_title"],
                "english_name": english_name,
                "japanese_name": japanese_name,
                "search_text": search_text,
                "source_status": source_status,
                "source_file": source_file,
                "match_status": match_status,
                "konami_cid": chosen["cid"],
                "konami_name": chosen["konami_name"],
                "konami_url": chosen["konami_url"],
                "notes": f"{len(matches)} result(s){exact_note}"
            })

        except Exception as error:
            output_rows.append({
                "page_title": card["page_title"],
                "english_name": english_name,
                "japanese_name": japanese_name,
                "search_text": search_text,
                "source_status": source_status,
                "source_file": source_file,
                "match_status": "ERROR",
                "konami_cid": "",
                "konami_name": "",
                "konami_url": "",
                "notes": str(error)
            })

        if args.delay > 0:
            time.sleep(args.delay)

    csv_rows = write_matches_csv(args.output, output_rows, merge_existing=args.merge_csv)
    save_matches_to_db(output_rows)

    print(f"Matched {len(output_rows)} card row(s).")
    print(f"Saved {csv_rows} row(s) to {args.output}")
    print("Saved yugioh_rush.sqlite3")


if __name__ == "__main__":
    main()

