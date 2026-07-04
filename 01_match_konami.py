import argparse
import json
import re
import time
from urllib.parse import urlencode

import pandas as pd
import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db, upsert_card_match

INPUT_JSON = "result.json"
OUTPUT_CSV = "konami_matches.csv"

KONAMI_SEARCH_URL = "https://www.db.yugioh-card.com/rushdb/card_search.action"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


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


def load_yugipedia_cards(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cards = []

    for page_title, item in data["results"].items():
        printouts = item.get("printouts", {})

        raw_english_name = first_value(printouts, "Name")
        raw_japanese_name = first_value(printouts, "Japanese name")

        english_name = clean_wiki_name(raw_english_name)
        japanese_name = clean_japanese_name(raw_japanese_name)

        cards.append({
            "page_title": page_title,
            "english_name": english_name,
            "japanese_name": japanese_name
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

    


def search_konami(search_text, verbose=False):
    search_url = build_konami_search_url(search_text)

    response = requests.get(search_url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    cid_matches = re.findall(r"cid=(\d+)", response.text)

    if verbose:
        print("HTML length:", len(response.text))
        print("Contains cid:", "cid=" in response.text)
        print("Contains card name:", search_text in response.text)
        print("CID matches found:", len(cid_matches))
        print("First few CIDs:", cid_matches[:10])
        print("Search text:", search_text)

    results = []

    for cid in sorted(set(cid_matches)):
        results.append({
            "cid": cid,
            "konami_name": search_text,
            "konami_url": f"https://www.db.yugioh-card.com/rushdb/card_search.action?ope=2&cid={cid}"
        })

    return results


def choose_match(matches):
    if len(matches) == 1:
        return "MATCHED", matches[0]

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
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    cards = load_yugipedia_cards(args.input)

    output_rows = []

    cards_to_test = cards if args.limit == 0 else cards[:args.limit]

    for index, card in enumerate(cards_to_test, start=1):
        english_name = card["english_name"]
        japanese_name = card["japanese_name"]

        search_text = japanese_name if japanese_name else english_name

        print(f"[{index}/{len(cards_to_test)}] Searching: {english_name} / {japanese_name}")

        try:
            matches = search_konami(search_text, verbose=args.verbose)
            match_status, chosen = choose_match(matches)

            output_rows.append({
                "page_title": card["page_title"],
                "english_name": english_name,
                "japanese_name": japanese_name,
                "search_text": search_text,
                "match_status": match_status,
                "konami_cid": chosen["cid"],
                "konami_name": chosen["konami_name"],
                "konami_url": chosen["konami_url"],
                "notes": f"{len(matches)} result(s)"
            })

        except Exception as error:
            output_rows.append({
                "page_title": card["page_title"],
                "english_name": english_name,
                "japanese_name": japanese_name,
                "search_text": search_text,
                "match_status": "ERROR",
                "konami_cid": "",
                "konami_name": "",
                "konami_url": "",
                "notes": str(error)
            })

        if args.delay > 0:
            time.sleep(args.delay)

    df = pd.DataFrame(output_rows)
    df.to_csv(args.output, index=False, encoding="utf-8-sig")
    save_matches_to_db(output_rows)

    print(f"Matched {len(output_rows)} card row(s).")
    print(f"Saved {args.output}")
    print("Saved yugioh_rush.sqlite3")


if __name__ == "__main__":
    main()
