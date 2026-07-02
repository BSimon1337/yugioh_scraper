import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

INPUT_CSV = "konami_matches.csv"
OUTPUT_CSV = "konami_printings.csv"

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

        cardnumber = cardnumber_tag.get_text(strip=True) if cardnumber_tag else ""
        setname = setname_tag.get_text(strip=True) if setname_tag else ""
        rarity = rarity_short_tag.get_text(strip=True) if rarity_short_tag else ""
        rarity_full = rarity_full_tag.get_text(strip=True) if rarity_full_tag else ""
        release_date = release_date_tag.get_text(strip=True) if release_date_tag else ""

        chosen_imageurl, chosen_ciid, needs_image_review = choose_image_for_printing(
            rarity,
            all_images
        )

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
            "source_url": url
        })

    return rows


def main():
    matches = pd.read_csv(INPUT_CSV, dtype=str).fillna("")
    matched_cards = matches[matches["match_status"] == "MATCHED"]

    print(f"Found {len(matched_cards)} matched cards.")

    all_rows = []

    cards_to_test = matched_cards.head(100)

    for index, row in cards_to_test.iterrows():
        print(f"Scraping CID {row['konami_cid']}: {row['english_name']}")

        try:
            printings = scrape_printings_for_card(row)
            all_rows.extend(printings)
            print(f"  Found {len(printings)} printing row(s).")
        except Exception as error:
            print(f"  ERROR: {error}")

        time.sleep(1)

    output = pd.DataFrame(all_rows)
    output.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Saved {OUTPUT_CSV}")


if __name__ == "__main__":
    main()