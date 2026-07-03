import argparse
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db, upsert_card_match, upsert_image, upsert_printing

INPUT_CSV = "konami_matches.csv"
OUTPUT_CSV = "konami_printings.csv"

OUTPUT_COLUMNS = [
    "cardname",
    "japanese_name",
    "konami_cid",
    "cardnumber",
    "imageurl",
    "ciid",
    "all_ciids",
    "all_imageurls",
    "image_count",
    "needs_image_review",
    "setname",
    "rarity",
    "rarity_full",
    "setcode",
    "release_date",
    "pid",
    "image_mapping_source",
    "image_mapping_notes",
    "source_url",
]

PRINTING_KEY = [
    "konami_cid",
    "cardnumber",
    "setname",
    "rarity",
    "rarity_full",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def get_setcode(cardnumber):
    if not cardnumber:
        return ""

    match = re.match(r"(.+)-JP[A-Z0-9]+", cardnumber)
    return match.group(1) if match else ""


def get_detail_url(cid):
    return f"https://www.db.yugioh-card.com/rushdb/card_search.action?ope=2&cid={cid}&request_locale=ja"


def find_all_images(response_text):
    matches = re.findall(
        r"get_image\.action\?type=1([^\"']*?)ciid=(\d+)([^\"']*)",
        response_text
    )

    images = []
    seen_ciids = set()

    for before, ciid, after in matches:
        if ciid in seen_ciids:
            continue

        seen_ciids.add(ciid)

        url = (
            "https://www.db.yugioh-card.com/rushdb/get_image.action?"
            f"type=1{before}ciid={ciid}{after}"
        )

        images.append({
            "ciid": ciid,
            "url": url
        })

    images.sort(key=lambda img: int(img["ciid"]))
    return images


def choose_image_for_printing(rarity, all_images):
    if not all_images:
        return "", "", True

    if len(all_images) == 1:
        return all_images[0]["url"], all_images[0]["ciid"], False

    rarity_upper = rarity.upper()

    if "ORR" in rarity_upper:
        return all_images[-1]["url"], all_images[-1]["ciid"], False

    return all_images[0]["url"], all_images[0]["ciid"], False


def scrape_printings_for_card(row):
    cid = str(row["konami_cid"]).strip()
    english_name = str(row["english_name"]).strip()
    japanese_name = str(row["japanese_name"]).strip()

    url = get_detail_url(cid)

    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    with open("debug_konami_detail.html", "w", encoding="utf-8") as f:
        f.write(response.text)

    all_images = find_all_images(response.text)
    image_rows = [
        {
            "cid": cid,
            "ciid": image["ciid"],
            "image_url": image["url"]
        }
        for image in all_images
    ]
    all_image_urls = [img["url"] for img in all_images]
    all_ciids = [img["ciid"] for img in all_images]

    rows = []

    printing_rows = soup.select("#update_list .t_row")

    for printing in printing_rows:
        cardnumber_tag = printing.select_one(".card_number")
        setname_tag = printing.select_one(".pack_name")
        rarity_short_tag = printing.select_one(".rarity p")
        rarity_full_tag = printing.select_one(".rarity span")
        release_date_tag = printing.select_one(".time")
        link_value_tag = printing.select_one("input.link_value")

        cardnumber = cardnumber_tag.get_text(strip=True) if cardnumber_tag else ""
        setname = setname_tag.get_text(strip=True) if setname_tag else ""
        rarity = rarity_short_tag.get_text(strip=True) if rarity_short_tag else ""
        rarity_full = rarity_full_tag.get_text(strip=True) if rarity_full_tag else ""
        release_date = release_date_tag.get_text(strip=True) if release_date_tag else ""
        link_value = link_value_tag.get("value", "") if link_value_tag else ""
        pid_match = re.search(r"pid=(\d+)", link_value)
        pid = pid_match.group(1) if pid_match else ""

        chosen_imageurl, chosen_ciid, needs_image_review = choose_image_for_printing(
            rarity,
            all_images
        )
        mapping_source = "DETAIL_HEURISTIC" if chosen_ciid else ""
        mapping_notes = "ORR uses last image; other rarities use first image"

        rows.append({
            "cardname": english_name,
            "japanese_name": japanese_name,
            "konami_cid": cid,
            "cardnumber": cardnumber,
            "imageurl": chosen_imageurl,
            "ciid": chosen_ciid,
            "all_ciids": " | ".join(all_ciids),
            "all_imageurls": " | ".join(all_image_urls),
            "image_count": len(all_images),
            "needs_image_review": needs_image_review,
            "setname": setname,
            "rarity": rarity,
            "rarity_full": rarity_full,
            "setcode": get_setcode(cardnumber),
            "release_date": release_date,
            "pid": pid,
            "image_mapping_source": mapping_source,
            "image_mapping_notes": mapping_notes,
            "source_url": url
        })

    return rows, image_rows


def save_card_scrape_to_db(match_row, printing_rows, image_rows):
    with connect_db() as connection:
        init_db(connection)
        upsert_card_match(connection, dict(match_row))

        for image in image_rows:
            upsert_image(connection, image)

        for printing in printing_rows:
            upsert_printing(connection, printing)

        connection.commit()


def existing_printing_cids():
    with connect_db() as connection:
        init_db(connection)
        rows = connection.execute(
            "SELECT DISTINCT cid FROM printings"
        ).fetchall()

    return {row["cid"] for row in rows}


def write_printings_csv(rows, merge_existing):
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)

    if merge_existing:
        try:
            existing = pd.read_csv(OUTPUT_CSV, dtype=str).fillna("")
        except FileNotFoundError:
            existing = pd.DataFrame(columns=OUTPUT_COLUMNS)

        output = pd.concat([existing, output], ignore_index=True)
        output = output.drop_duplicates(subset=PRINTING_KEY, keep="last")

    output.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")


def select_cards_to_scrape(matches, args):
    matched_cards = matches[matches["match_status"] == "MATCHED"]

    if args.cids:
        matched_cards = matched_cards[matched_cards["konami_cid"].isin(args.cids)]

    if args.missing_only:
        scraped_cids = existing_printing_cids()
        matched_cards = matched_cards[~matched_cards["konami_cid"].isin(scraped_cids)]

    if args.limit:
        matched_cards = matched_cards.head(args.limit)

    return matched_cards


def build_parser():
    parser = argparse.ArgumentParser(
        description="Scrape Konami printing rows for matched cards."
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only scrape matched CIDs that do not already have printing rows in SQLite.",
    )
    parser.add_argument(
        "--cids",
        nargs="+",
        help="Only scrape these Konami CIDs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum matched cards to scrape. Use 0 for no limit.",
    )
    parser.add_argument(
        "--merge-csv",
        action="store_true",
        help="Merge scraped rows into the existing printings CSV instead of replacing it.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    matches = pd.read_csv(INPUT_CSV, dtype=str).fillna("")
    matched_cards = select_cards_to_scrape(matches, args)

    print(f"Found {len(matched_cards)} matched cards.")

    all_rows = []

    for index, row in matched_cards.iterrows():
        print(f"Scraping CID {row['konami_cid']}: {row['english_name']}")

        try:
            printings, images = scrape_printings_for_card(row)
            all_rows.extend(printings)
            save_card_scrape_to_db(row, printings, images)
            print(f"  Found {len(printings)} printing row(s).")
        except Exception as error:
            print(f"  ERROR: {error}")

        time.sleep(1)

    merge_existing = args.merge_csv or args.missing_only or bool(args.cids)
    write_printings_csv(all_rows, merge_existing=merge_existing)

    print(f"Saved {OUTPUT_CSV}")
    print("Saved yugioh_rush.sqlite3")


if __name__ == "__main__":
    main()
