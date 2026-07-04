import argparse
import csv
import sqlite3
from collections import Counter
from pathlib import Path

from db import DB_PATH

DEFAULT_EXPORT_CSV = "carduploader_export.csv"
DEFAULT_REPORT_CSV = "image_validation_report.csv"


def scalar(connection, query, params=()):
    return connection.execute(query, params).fetchone()[0]


def rows(connection, query, params=()):
    connection.row_factory = sqlite3.Row
    return connection.execute(query, params).fetchall()


def csv_rows(path):
    path = Path(path)
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def print_count(label, value):
    print(f"{label}: {value}")


def build_state(db_path, export_csv, report_csv):
    state = {
        "errors": [],
        "warnings": [],
        "export_rows": None,
        "report_rows": None,
    }

    if not Path(db_path).exists():
        state["errors"].append(f"SQLite database not found: {db_path}")
        return state

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row

        state.update({
            "cards": scalar(connection, "SELECT COUNT(*) FROM cards"),
            "matched": scalar(connection, "SELECT COUNT(*) FROM cards WHERE match_status = 'MATCHED'"),
            "no_match": scalar(connection, "SELECT COUNT(*) FROM cards WHERE match_status = 'NO_MATCH'"),
            "needs_match_review": scalar(
                connection,
                "SELECT COUNT(*) FROM cards WHERE match_status NOT IN ('MATCHED', 'NO_MATCH')",
            ),
            "printings": scalar(connection, "SELECT COUNT(*) FROM printings"),
            "images": scalar(connection, "SELECT COUNT(*) FROM images"),
            "needs_image_review": scalar(
                connection,
                "SELECT COUNT(*) FROM printings WHERE needs_image_review != 0",
            ),
            "exportable_printings": scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM printings p
                JOIN images i ON i.cid = p.cid
                             AND i.ciid = p.ciid
                WHERE p.needs_image_review = 0
                """,
            ),
            "missing_image_joins": scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM printings p
                LEFT JOIN images i ON i.cid = p.cid
                                  AND i.ciid = p.ciid
                WHERE p.needs_image_review = 0
                  AND i.ciid IS NULL
                """,
            ),
            "non_ok_images": scalar(
                connection,
                "SELECT COUNT(*) FROM images WHERE validation_status != 'OK'",
            ),
        })

        duplicate_cids = rows(
            connection,
            """
            SELECT cid,
                   COUNT(*) AS row_count,
                   GROUP_CONCAT(english_name, ' | ') AS names
            FROM cards
            WHERE cid IS NOT NULL
              AND cid != ''
            GROUP BY cid
            HAVING COUNT(*) > 1
            ORDER BY cid
            """,
        )
        state["duplicate_cids"] = duplicate_cids

    report_rows = csv_rows(report_csv)
    if report_rows is not None:
        state["report_rows"] = len(report_rows)

    export_rows = csv_rows(export_csv)
    if export_rows is not None:
        state["export_rows"] = len(export_rows)
        if export_rows:
            blanks = {
                field: sum(1 for row in export_rows if not (row.get(field) or "").strip())
                for field in export_rows[0]
            }
            state["export_blanks"] = {key: value for key, value in blanks.items() if value}

            key_counts = Counter(
                (
                    row.get("cardname", ""),
                    row.get("cardnumber", ""),
                    row.get("setname", ""),
                    row.get("rarity", ""),
                    row.get("setcode", ""),
                )
                for row in export_rows
            )
            state["duplicate_export_keys"] = sum(1 for count in key_counts.values() if count > 1)
            state["distinct_cardnames"] = len({row.get("cardname", "") for row in export_rows})
            state["distinct_imageurls"] = len({row.get("imageurl", "") for row in export_rows})
        else:
            state["export_blanks"] = {}
            state["duplicate_export_keys"] = 0
            state["distinct_cardnames"] = 0
            state["distinct_imageurls"] = 0

    if state.get("needs_match_review", 0):
        state["warnings"].append("Card match review is not finished.")
    if state.get("needs_image_review", 0):
        state["errors"].append("Some printings still need image review.")
    if state.get("missing_image_joins", 0):
        state["errors"].append("Some reviewed printings do not join to an image row.")
    if state.get("non_ok_images", 0):
        state["errors"].append("Some images are not validation_status OK.")
    if state.get("report_rows", 0):
        state["errors"].append("Image validation report has review rows.")
    if state.get("duplicate_cids"):
        state["warnings"].append("Multiple card rows share the same Konami CID.")
    if state.get("export_blanks"):
        state["errors"].append("Exporter CSV has blank required fields.")
    if state.get("duplicate_export_keys", 0):
        state["warnings"].append("Exporter CSV has duplicate printing identity keys.")
    if (
        state.get("export_rows") is not None
        and state.get("export_rows") != state.get("exportable_printings")
    ):
        state["warnings"].append("Exporter row count does not match current exportable printings.")

    return state


def print_state(state):
    if "cards" not in state:
        for error in state["errors"]:
            print(f"ERROR: {error}")
        return

    print_count("cards", state["cards"])
    print_count("matched", state["matched"])
    print_count("no_match", state["no_match"])
    print_count("needs_match_review", state["needs_match_review"])
    print_count("printings", state["printings"])
    print_count("images", state["images"])
    print_count("needs_image_review", state["needs_image_review"])
    print_count("exportable_printings", state["exportable_printings"])
    print_count("missing_image_joins", state["missing_image_joins"])
    print_count("non_ok_images", state["non_ok_images"])

    if state["report_rows"] is not None:
        print_count("image_validation_report_rows", state["report_rows"])
    if state["export_rows"] is not None:
        print_count("export_rows", state["export_rows"])
        print_count("export_distinct_cardnames", state["distinct_cardnames"])
        print_count("export_distinct_imageurls", state["distinct_imageurls"])
        print_count("export_duplicate_keys", state["duplicate_export_keys"])
        print_count("export_blank_fields", state["export_blanks"])

    if state.get("duplicate_cids"):
        print()
        print("Duplicate CIDs:")
        for row in state["duplicate_cids"]:
            print(f"  cid {row['cid']}: {row['names']}")

    if state["warnings"]:
        print()
        for warning in state["warnings"]:
            print(f"WARNING: {warning}")

    if state["errors"]:
        print()
        for error in state["errors"]:
            print(f"ERROR: {error}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Summarize Rush Duel scraper pipeline health."
    )
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite database path. Defaults to {DB_PATH}.")
    parser.add_argument(
        "--export",
        default=DEFAULT_EXPORT_CSV,
        help=f"Card uploader export path. Defaults to {DEFAULT_EXPORT_CSV}.",
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT_CSV,
        help=f"Image validation report path. Defaults to {DEFAULT_REPORT_CSV}.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings as well as errors.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    state = build_state(args.db, args.export, args.report)
    print_state(state)

    if state["errors"] or (args.strict and state["warnings"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
