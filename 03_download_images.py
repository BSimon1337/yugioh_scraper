from datetime import datetime, timezone
from pathlib import Path

import requests

from db import connect_db, image_path, init_db

IMAGE_DIR = "images"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def fetch_images_to_download(connection):
    return connection.execute(
        """
        SELECT cid, ciid, image_url, local_path
        FROM images
        ORDER BY CAST(cid AS INTEGER), CAST(ciid AS INTEGER)
        """
    ).fetchall()


def update_downloaded_image(connection, cid, ciid, local_path):
    connection.execute(
        """
        UPDATE images
        SET local_path = ?,
            downloaded_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE cid = ? AND ciid = ?
        """,
        (
            local_path,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cid,
            ciid,
        ),
    )


def download_image(image_url, destination):
    response = requests.get(image_url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)


def main():
    with connect_db() as connection:
        init_db(connection)
        rows = fetch_images_to_download(connection)

        print(f"Found {len(rows)} image record(s).")

        downloaded = 0
        skipped = 0
        failed = 0

        for row in rows:
            cid = row["cid"]
            ciid = row["ciid"]
            local_path = row["local_path"] or image_path(cid, ciid, IMAGE_DIR)
            destination = Path(local_path)

            if destination.exists():
                skipped += 1
                update_downloaded_image(connection, cid, ciid, str(destination))
                print(f"SKIP {cid}_{ciid}: {destination}")
                continue

            print(f"GET  {cid}_{ciid}: {row['image_url']}")

            try:
                download_image(row["image_url"], destination)
                update_downloaded_image(connection, cid, ciid, str(destination))
                connection.commit()
                downloaded += 1
            except Exception as error:
                failed += 1
                print(f"FAIL {cid}_{ciid}: {error}")

        connection.commit()

    print(f"Downloaded {downloaded}, skipped {skipped}, failed {failed}.")


if __name__ == "__main__":
    main()
