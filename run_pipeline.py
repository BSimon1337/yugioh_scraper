import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from check_pipeline_state import build_state, print_state
from db import DB_PATH

MATCHES_CSV = "konami_matches.csv"
PRINTINGS_CSV = "konami_printings.csv"
EXPORT_CSV = "carduploader_export.csv"


def unbuffered_command(command):
    if not command:
        return command
    if Path(command[0]).name.lower().startswith("python") and "-u" not in command[1:2]:
        return [command[0], "-u", *command[1:]]
    return command


def run(command, label=None, step=None, total_steps=None):
    command = unbuffered_command(command)
    print()
    if label:
        prefix = f"[{step}/{total_steps}] " if step and total_steps else ""
        print(f"{prefix}{label}", flush=True)
    print("$ " + " ".join(command), flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(command, env=env)
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
        "--set-name-source",
        choices=["original", "english"],
        default="english",
        help="Use English set names or original scraped set names. Defaults to english.",
    )
    parser.add_argument(
        "--set-name-map",
        default="set_name_map.csv",
        help="CSV with setcode,setname,setname_en columns for English set names.",
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
    total_steps = 5
    if args.run_match:
        total_steps += 1
    if not args.skip_download:
        total_steps += 1
    if args.audit:
        total_steps += 1
    step_number = 0

    def run_stage(command, label):
        nonlocal step_number
        step_number += 1
        run(command, label=label, step=step_number, total_steps=total_steps)

    copy_source_json(args.from_json)

    if args.run_match:
        run_stage([
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
            "--merge-csv",
        ], "Match cards to Konami")

    ensure_no_card_review(args.interactive_review)

    scrape_command = [sys.executable, "02_scrape_printings.py", "--limit", "0"]
    if args.scrape_all:
        pass
    else:
        scrape_command.append("--missing-only")
    run_stage(scrape_command, "Scrape printings")
    run_stage([sys.executable, "02b_enrich_set_names.py"], "Enrich English set names")

    if not args.skip_download:
        run_stage([sys.executable, "03_download_images.py"], "Download images")

    run_stage([sys.executable, "03b_resolve_image_mappings.py", "--all"], "Resolve image mappings")
    run_stage([sys.executable, "04_validate_images.py"], "Validate images")

    export_command = [
        sys.executable,
        "05_export_carduploader.py",
        "--image-source",
        args.image_source,
        "--output",
        args.output,
        "--set-name-source",
        args.set_name_source,
        "--set-name-map",
        args.set_name_map,
    ]
    if args.image_source == "cdn":
        export_command.extend(["--cdn-base-url", args.cdn_base_url])
    run_stage(export_command, "Export card uploader CSV")

    if args.audit:
        run_stage([sys.executable, "06_export_image_audit.py"], "Export image audit")

    ensure_clean_for_export(args.output)


if __name__ == "__main__":
    main()
