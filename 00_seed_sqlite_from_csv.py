import csv
from pathlib import Path

from db import connect_db, init_db, upsert_card_match, upsert_image, upsert_printing

MATCHES_CSV = "konami_matches.csv"
PRINTINGS_CSV = "konami_printings.csv"


def read_csv(path):
    if not Path(path).exists():
        return []

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def image_rows_from_printing(row):
    cid = row.get("konami_cid", "").strip()
    ciids = [value.strip() for value in row.get("all_ciids", "").split("|")]
    urls = [value.strip() for value in row.get("all_imageurls", "").split("|")]

    for ciid, image_url in zip(ciids, urls):
        if cid and ciid and image_url:
            yield {
                "cid": cid,
                "ciid": ciid,
                "image_url": image_url
            }


def main():
    match_rows = read_csv(MATCHES_CSV)
    printing_rows = read_csv(PRINTINGS_CSV)

    with connect_db() as connection:
        init_db(connection)

        for row in match_rows:
            upsert_card_match(connection, row)

        for row in printing_rows:
            for image in image_rows_from_printing(row):
                upsert_image(connection, image)

            upsert_printing(connection, row)

        connection.commit()

    print(f"Imported {len(match_rows)} card match row(s).")
    print(f"Imported {len(printing_rows)} printing row(s).")
    print("Saved yugioh_rush.sqlite3")


if __name__ == "__main__":
    main()
