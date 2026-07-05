import argparse
import csv
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from db import connect_db, init_db

OUTPUT_CSV = "konami_printings.csv"
KONAMI_BASE_URL = "https://www.db.yugioh-card.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

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
    "setname_en",
    "rarity",
    "rarity_full",
    "setcode",
    "release_date",
    "pid",
    "image_mapping_source",
    "image_mapping_notes",
    "source_url",
]


def pack_url(pid):
    return (
        f"{KONAMI_BASE_URL}/rushdb/card_search.action"
        f"?ope=1&sess=1&pid={pid}&rp=99999&request_locale=ja"
    )


def fetch_pack_candidates(pid):
    response = requests.get(pack_url(pid), headers=HEADERS, timeout=20)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    images_by_index = parse_pack_image_assignments(html)
    candidates = []

    for index, row in enumerate(soup.select("#card_list .t_row")):
        cid = get_row_cid(row)
        if not cid:
            continue

        image = images_by_index.get(index, {})
        candidates.append({
            "index": index,
            "cid": cid,
            "ciid": image.get("ciid", ""),
            "image_url": image.get("image_url", ""),
            "name": get_text(row, ".card_name"),
            "rarity": get_text(row, ".rarity p"),
        })

    return candidates


def parse_pack_image_assignments(html):
    rows = {}
    pattern = re.compile(
        r"#card_image_(\d+)_(\d+)'\)\.attr\('src',\s*'([^']+)'",
        re.S,
    )

    for index, ciid, image_url in pattern.findall(html):
        url = urljoin(KONAMI_BASE_URL, image_url)
        cid_match = re.search(r"[?&]cid=(\d+)", url)
        ciid_match = re.search(r"[?&]ciid=(\d+)", url)

        rows[int(index)] = {
            "cid": cid_match.group(1) if cid_match else "",
            "ciid": ciid_match.group(1) if ciid_match else ciid,
            "image_url": url,
        }

    return rows


def get_row_cid(row):
    cid_tag = row.select_one("input.cid")
    if cid_tag and cid_tag.get("value"):
        return cid_tag["value"].strip()

    link_tag = row.select_one("input.link_value")
    if not link_tag:
        return ""

    match = re.search(r"cid=(\d+)", link_tag.get("value", ""))
    return match.group(1) if match else ""


def get_text(parent, selector):
    tag = parent.select_one(selector)
    return tag.get_text("", strip=True) if tag else ""


def fetch_printings_to_map(connection, multi_image_only):
    where = "p.pid != ''"
    if multi_image_only:
        where += """
            AND p.cid IN (
                SELECT cid
                FROM images
                GROUP BY cid
                HAVING COUNT(*) > 1
            )
        """

    return connection.execute(
        f"""
        SELECT p.id,
               p.cid,
               p.cardnumber,
               p.rarity,
               p.pid,
               p.ciid,
               (
                   SELECT COUNT(*)
                   FROM printings p2
                   WHERE p2.cid = p.cid
                     AND p2.pid = p.pid
               ) AS cid_pid_printing_count
        FROM printings p
        WHERE {where}
        ORDER BY CAST(p.cid AS INTEGER), p.cardnumber, p.rarity
        """
    ).fetchall()


def image_ciids_for_cid(connection, cid):
    rows = connection.execute(
        """
        SELECT ciid
        FROM images
        WHERE cid = ?
        ORDER BY CAST(ciid AS INTEGER)
        """,
        (cid,),
    ).fetchall()
    return [row["ciid"] for row in rows]


def heuristic_ciid(printing, ciids):
    if not ciids:
        return ""

    if len(ciids) == 1:
        return ciids[0]

    if "ORR" in printing["rarity"].upper():
        return ciids[-1]

    return ciids[0]


def heuristic_rule_note(printing):
    if "ORR" in printing["rarity"].upper():
        return "confirmed rule: ORR uses highest ciid"

    return "confirmed rule: non-ORR uses lowest ciid"


def choose_candidate(printing, candidates):
    matching_cid = [
        candidate for candidate in candidates
        if candidate["cid"] == printing["cid"]
    ]

    if not matching_cid:
        return None, "no matching cid on pack page"

    if printing["cid_pid_printing_count"] > 1 and len(matching_cid) == 1:
        return None, (
            "pack page has one representative image for multiple "
            "printing rarities on the same cid/pid"
        )

    if len(matching_cid) == 1:
        return matching_cid[0], "matched cid on pack page"

    matching_rarity = [
        candidate for candidate in matching_cid
        if candidate["rarity"] == printing["rarity"]
    ]
    if len(matching_rarity) == 1:
        return matching_rarity[0], "matched cid and rarity on pack page"

    return None, f"ambiguous pack candidates: {len(matching_cid)}"


def update_printing_mapping(connection, printing, candidate, notes):
    if candidate:
        connection.execute(
            """
            UPDATE printings
            SET ciid = ?,
                needs_image_review = 0,
                image_mapping_source = 'PACK_PAGE',
                image_mapping_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (candidate["ciid"], notes, printing["id"]),
        )
        return "mapped"

    ciids = image_ciids_for_cid(connection, printing["cid"])
    guessed_ciid = heuristic_ciid(printing, ciids)

    if guessed_ciid:
        connection.execute(
            """
            UPDATE printings
            SET ciid = ?,
                needs_image_review = 0,
                image_mapping_source = 'DETAIL_HEURISTIC_CONFIRMED',
                image_mapping_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (guessed_ciid, f"{heuristic_rule_note(printing)}; {notes}", printing["id"]),
        )
        return "confirmed"

    connection.execute(
        """
        UPDATE printings
        SET ciid = ?,
            needs_image_review = 1,
            image_mapping_source = 'DETAIL_HEURISTIC_REVIEW',
            image_mapping_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (guessed_ciid, notes, printing["id"]),
    )
    return "review"


def images_by_cid(connection):
    rows = connection.execute(
        """
        SELECT cid, ciid, image_url
        FROM images
        ORDER BY CAST(cid AS INTEGER), CAST(ciid AS INTEGER)
        """
    ).fetchall()

    output = {}
    for row in rows:
        output.setdefault(row["cid"], []).append(row)
    return output


def cards_by_cid(connection):
    rows = connection.execute(
        """
        SELECT cid,
               MIN(english_name) AS english_name,
               MIN(japanese_name) AS japanese_name
        FROM cards
        WHERE cid IS NOT NULL
        GROUP BY cid
        """
    ).fetchall()
    return {row["cid"]: row for row in rows}


def sync_printings_csv(connection):
    images = images_by_cid(connection)
    cards = cards_by_cid(connection)
    rows = connection.execute(
        """
        SELECT cid,
               cardnumber,
               setname,
               setname_en,
               rarity,
               rarity_full,
               setcode,
               release_date,
               pid,
               ciid,
               needs_image_review,
               image_mapping_source,
               image_mapping_notes,
               source_url
        FROM printings
        ORDER BY CAST(cid AS INTEGER), release_date DESC, cardnumber, rarity
        """
    ).fetchall()

    output_rows = []
    for row in rows:
        card = cards.get(row["cid"])
        image_rows = images.get(row["cid"], [])
        image_for_printing = next(
            (image for image in image_rows if image["ciid"] == row["ciid"]),
            None,
        )

        output_rows.append({
            "cardname": card["english_name"] if card else "",
            "japanese_name": card["japanese_name"] if card else "",
            "konami_cid": row["cid"],
            "cardnumber": row["cardnumber"],
            "imageurl": image_for_printing["image_url"] if image_for_printing else "",
            "ciid": row["ciid"],
            "all_ciids": " | ".join(image["ciid"] for image in image_rows),
            "all_imageurls": " | ".join(image["image_url"] for image in image_rows),
            "image_count": len(image_rows),
            "needs_image_review": bool(row["needs_image_review"]),
            "setname": row["setname"],
            "setname_en": row["setname_en"],
            "rarity": row["rarity"],
            "rarity_full": row["rarity_full"],
            "setcode": row["setcode"],
            "release_date": row["release_date"],
            "pid": row["pid"],
            "image_mapping_source": row["image_mapping_source"],
            "image_mapping_notes": row["image_mapping_notes"],
            "source_url": row["source_url"],
        })

    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(output_rows)

    return len(output_rows)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Resolve printing image mappings through Konami pack pages."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Resolve all printings with a pid, not just multi-image cards.",
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
        printings = fetch_printings_to_map(connection, multi_image_only=not args.all)
        cache = {}
        mapped = 0
        confirmed = 0
        review = 0

        print(f"Resolving {len(printings)} printing row(s).")

        for printing in printings:
            pid = printing["pid"]
            if pid not in cache:
                cache[pid] = fetch_pack_candidates(pid)

            candidate, notes = choose_candidate(printing, cache[pid])
            result = update_printing_mapping(connection, printing, candidate, notes)

            if result == "mapped":
                mapped += 1
            elif result == "confirmed":
                confirmed += 1
            else:
                review += 1
                print(
                    f"REVIEW cid {printing['cid']} {printing['cardnumber']} "
                    f"{printing['rarity']}: {notes}"
                )

        connection.commit()

        synced = 0
        if not args.no_sync_csv:
            synced = sync_printings_csv(connection)

    print(f"Mapped {mapped}, confirmed {confirmed}, review {review}.")
    if not args.no_sync_csv:
        print(f"Synced {synced} row(s) to {OUTPUT_CSV}.")


if __name__ == "__main__":
    main()
