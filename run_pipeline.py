import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from check_pipeline_state import build_state, print_state
from db import DB_PATH

MATCHES_CSV = "konami_matches.csv"
PRINTINGS_CSV = "konami_printings.csv"
EXPORT_CSV = "carduploader_export.csv"


def run(command):
    print()
    print("$ " + " ".join(command), flush=True)
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def copy_source_json(source):
    source = Path(source)
    if source.name == "result.json":
        return

    if not source.exists():
        raise SystemExit(f"Source JSON not found: {source}")

    shutil.copyfile(source, "result.json")
    print(f"Copied {source} to result.json.")


def card_review_count():
    state = build_state(DB_PATH, EXPORT_CSV, "image_validation_report.csv")
    return state.get("needs_match_review", 0)


def ensure_no_card_review(interactive_review):
    if not card_review_count():
        return

    if interactive_review:
        run([sys.executable, "01b_review_matches.py", "interactive", "--status", "all", "--details"])
        run([sys.executable, "01b_review_matches.py", "sync-csv"])

        if not card_review_count():
            return

    state = build_state(DB_PATH, EXPORT_CSV, "image_validation_report.csv")
    print_state(state)
    print()
    print("Stop: card match review is not finished.")
    print("Run: python 01b_review_matches.py interactive --status all --details")
    print("Then: python 01b_review_matches.py sync-csv")
    raise SystemExit(1)


def ensure_clean_for_export(export_csv):
    state = build_state(DB_PATH, export_csv, "image_validation_report.csv")
    print()
    print("Pipeline State")
    print_state(state)
    if state["errors"]:
        raise SystemExit(1)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run the Rush Duel scraper/export pipeline with checkpoints."
    )
    parser.add_argument(
        "--from-json",
        default="result.json",
        help="Yugipedia JSON export to use. Defaults to result.json.",
    )
    parser.add_argument(
        "--run-match",
        action="store_true",
        help="Run 01_match_konami.py before downstream steps.",
    )
    parser.add_argument(
        "--match-limit",
        type=int,
        default=0,
        help="Limit for 01_match_konami.py. Use 0 for all cards. Defaults to 0.",
    )
    parser.add_argument(
        "--match-delay",
        type=float,
        default=1.0,
        help="Delay between Konami match requests. Defaults to 1.0.",
    )
    parser.add_argument(
        "--scrape-all",
        action="store_true",
        help="Scrape all matched CIDs instead of only missing printings.",
    )
    parser.add_argument(
        "--image-source",
        choices=["konami", "local", "cdn"],
        default="konami",
        help="Image source for card uploader export. Defaults to konami.",
    )
    parser.add_argument(
        "--cdn-base-url",
        default="",
        help="Required when --image-source cdn.",
    )
    parser.add_argument(
        "--output",
        default=EXPORT_CSV,
        help=f"Exporter output path. Defaults to {EXPORT_CSV}.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip 03_download_images.py.",
    )
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help="Open 01b interactive card review if matching finds rows needing review.",
    )
    parser.add_argument(
        "--audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write image_mapping_audit.csv at the end. Defaults to true.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    copy_source_json(args.from_json)

    if args.run_match:
        run([
            sys.executable,
            "01_match_konami.py",
            "--input",
            "result.json",
            "--output",
            MATCHES_CSV,
            "--limit",
            str(args.match_limit),
            "--delay",
            str(args.match_delay),
        ])

    ensure_no_card_review(args.interactive_review)

    scrape_command = [sys.executable, "02_scrape_printings.py"]
    if args.scrape_all:
        scrape_command.extend(["--limit", "0"])
    else:
        scrape_command.append("--missing-only")
    run(scrape_command)

    if not args.skip_download:
        run([sys.executable, "03_download_images.py"])

    run([sys.executable, "03b_resolve_image_mappings.py", "--all"])
    run([sys.executable, "04_validate_images.py"])

    export_command = [
        sys.executable,
        "05_export_carduploader.py",
        "--image-source",
        args.image_source,
        "--output",
        args.output,
    ]
    if args.image_source == "cdn":
        export_command.extend(["--cdn-base-url", args.cdn_base_url])
    run(export_command)

    if args.audit:
        run([sys.executable, "06_export_image_audit.py"])

    ensure_clean_for_export(args.output)


if __name__ == "__main__":
    main()
