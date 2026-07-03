import argparse
import csv
import hashlib
from pathlib import Path

from db import connect_db, image_path, init_db

REPORT_CSV = "image_validation_report.csv"


SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}

REPORT_FIELDS = [
    "issue_type",
    "cid",
    "ciid",
    "cardname",
    "cardnumber",
    "rarity",
    "setcode",
    "local_path",
    "details",
]


def read_png_dimensions(data):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")

    if data[12:16] != b"IHDR":
        raise ValueError("PNG IHDR chunk not found")

    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def read_jpeg_dimensions(data):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG file")

    index = 2
    while index < len(data):
        while index < len(data) and data[index] != 0xFF:
            index += 1

        while index < len(data) and data[index] == 0xFF:
            index += 1

        if index >= len(data):
            break

        marker = data[index]
        index += 1

        if marker in (0xD8, 0xD9):
            continue

        if marker == 0xDA:
            break

        if index + 2 > len(data):
            break

        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2:
            raise ValueError("invalid JPEG segment length")

        segment_start = index + 2
        segment_end = index + segment_length

        if marker in SOF_MARKERS:
            if segment_start + 5 > len(data):
                raise ValueError("truncated JPEG SOF segment")

            height = int.from_bytes(data[segment_start + 1:segment_start + 3], "big")
            width = int.from_bytes(data[segment_start + 3:segment_start + 5], "big")
            return width, height

        index = segment_end

    raise ValueError("JPEG dimensions not found")


def read_image_metadata(path):
    with open(path, "rb") as file:
        data = file.read()

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = read_png_dimensions(data)
        return "PNG", width, height

    if data[:2] == b"\xff\xd8":
        width, height = read_jpeg_dimensions(data)
        return "JPEG", width, height

    raise ValueError("unsupported image format")


def sha256_file(path):
    hasher = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)

    return hasher.hexdigest()


def fetch_images(connection):
    return connection.execute(
        """
        SELECT cid, ciid, image_url, local_path
        FROM images
        ORDER BY CAST(cid AS INTEGER), CAST(ciid AS INTEGER)
        """
    ).fetchall()


def update_image_validation(
    connection,
    cid,
    ciid,
    local_path,
    size_bytes,
    width,
    height,
    sha256,
    image_format,
    status,
    notes,
):
    connection.execute(
        """
        UPDATE images
        SET local_path = ?,
            size_bytes = ?,
            width = ?,
            height = ?,
            image_format = ?,
            sha256 = ?,
            validation_status = ?,
            validation_notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE cid = ? AND ciid = ?
        """,
        (
            local_path,
            size_bytes,
            width,
            height,
            image_format,
            sha256,
            status,
            notes,
            cid,
            ciid,
        ),
    )


def validate_image_row(connection, row):
    cid = row["cid"]
    ciid = row["ciid"]
    local_path = row["local_path"] or image_path(cid, ciid)
    path = Path(local_path)

    if not path.exists():
        update_image_validation(
            connection,
            cid,
            ciid,
            local_path,
            0,
            0,
            0,
            "",
            "",
            "MISSING",
            "file does not exist",
        )
        return {
            "status": "MISSING",
            "notes": "file does not exist",
            "local_path": local_path,
        }

    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        update_image_validation(
            connection,
            cid,
            ciid,
            local_path,
            size_bytes,
            0,
            0,
            "",
            "",
            "INVALID",
            "empty file",
        )
        return {
            "status": "INVALID",
            "notes": "empty file",
            "local_path": local_path,
        }

    try:
        image_format, width, height = read_image_metadata(path)
        digest = sha256_file(path)
    except Exception as error:
        update_image_validation(
            connection,
            cid,
            ciid,
            local_path,
            size_bytes,
            0,
            0,
            "",
            "",
            "INVALID",
            str(error),
        )
        return {
            "status": "INVALID",
            "notes": str(error),
            "local_path": local_path,
        }

    status = "OK"
    notes = ""
    if width < 100 or height < 100:
        status = "REVIEW"
        notes = f"small dimensions: {width}x{height}"

    update_image_validation(
        connection,
        cid,
        ciid,
        local_path,
        size_bytes,
        width,
        height,
        digest,
        image_format,
        status,
        notes,
    )

    return {
        "status": status,
        "notes": notes,
        "local_path": local_path,
    }


def cardname_for_cid(connection):
    rows = connection.execute(
        """
        SELECT cid, MIN(english_name) AS cardname
        FROM cards
        WHERE cid IS NOT NULL
        GROUP BY cid
        """
    ).fetchall()
    return {row["cid"]: row["cardname"] for row in rows}


def add_image_validation_issues(connection, report_rows, cardnames):
    rows = connection.execute(
        """
        SELECT cid,
               ciid,
               local_path,
               validation_status,
               validation_notes
        FROM images
        WHERE validation_status != 'OK'
        ORDER BY CAST(cid AS INTEGER), CAST(ciid AS INTEGER)
        """
    ).fetchall()

    for row in rows:
        report_rows.append({
            "issue_type": f"IMAGE_{row['validation_status']}",
            "cid": row["cid"],
            "ciid": row["ciid"],
            "cardname": cardnames.get(row["cid"], ""),
            "cardnumber": "",
            "rarity": "",
            "setcode": "",
            "local_path": row["local_path"],
            "details": row["validation_notes"],
        })


def add_duplicate_hash_issues(connection, report_rows, cardnames):
    rows = connection.execute(
        """
        SELECT sha256,
               COUNT(*) AS duplicate_count
        FROM images
        WHERE sha256 != ''
        GROUP BY sha256
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for duplicate in rows:
        images = connection.execute(
            """
            SELECT cid, ciid, local_path
            FROM images
            WHERE sha256 = ?
            ORDER BY CAST(cid AS INTEGER), CAST(ciid AS INTEGER)
            """,
            (duplicate["sha256"],),
        ).fetchall()

        image_ids = ", ".join(f"{row['cid']}_{row['ciid']}" for row in images)
        for row in images:
            report_rows.append({
                "issue_type": "DUPLICATE_IMAGE_HASH",
                "cid": row["cid"],
                "ciid": row["ciid"],
                "cardname": cardnames.get(row["cid"], ""),
                "cardnumber": "",
                "rarity": "",
                "setcode": "",
                "local_path": row["local_path"],
                "details": image_ids,
            })


def add_printing_mapping_issues(connection, report_rows, cardnames):
    rows = connection.execute(
        """
        SELECT p.cid,
               p.cardnumber,
               p.rarity,
               p.setcode,
               p.ciid,
               p.needs_image_review,
               p.image_mapping_source,
               p.image_mapping_notes,
               COUNT(i.ciid) AS image_count
        FROM printings p
        LEFT JOIN images i ON i.cid = p.cid
        GROUP BY p.id
        ORDER BY CAST(p.cid AS INTEGER), p.cardnumber, p.rarity
        """
    ).fetchall()

    for row in rows:
        if not row["ciid"]:
            report_rows.append(printing_issue(row, cardnames, "MISSING_PRINTING_CIID", "printing has no mapped ciid"))

        if row["needs_image_review"]:
            details = (
                f"{row['image_mapping_source']}: {row['image_mapping_notes']} "
                f"({row['image_count']} image(s) for cid)"
            )
            report_rows.append(printing_issue(row, cardnames, "PRINTING_IMAGE_REVIEW", details))


def printing_issue(row, cardnames, issue_type, details):
    return {
        "issue_type": issue_type,
        "cid": row["cid"],
        "ciid": row["ciid"],
        "cardname": cardnames.get(row["cid"], ""),
        "cardnumber": row["cardnumber"],
        "rarity": row["rarity"],
        "setcode": row["setcode"],
        "local_path": "",
        "details": details,
    }


def write_report(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def build_report(connection):
    report_rows = []
    cardnames = cardname_for_cid(connection)
    add_image_validation_issues(connection, report_rows, cardnames)
    add_duplicate_hash_issues(connection, report_rows, cardnames)
    add_printing_mapping_issues(connection, report_rows, cardnames)
    return report_rows


def build_parser():
    parser = argparse.ArgumentParser(
        description="Validate downloaded Konami image files and report mapping risks."
    )
    parser.add_argument(
        "--report",
        default=REPORT_CSV,
        help=f"Output review CSV path. Defaults to {REPORT_CSV}.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    with connect_db() as connection:
        init_db(connection)
        images = fetch_images(connection)

        counts = {}
        for row in images:
            result = validate_image_row(connection, row)
            counts[result["status"]] = counts.get(result["status"], 0) + 1

        connection.commit()

        report_rows = build_report(connection)
        write_report(args.report, report_rows)

    print(f"Validated {len(images)} image record(s).")
    for status in sorted(counts):
        print(f"{status}: {counts[status]}")
    print(f"Wrote {len(report_rows)} review row(s) to {args.report}.")


if __name__ == "__main__":
    main()
