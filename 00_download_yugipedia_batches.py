import argparse
import json
import time
from pathlib import Path

import requests

YUGIPEDIA_API_URL = "https://yugipedia.com/api.php"
OUTPUT_DIR = "batches"
DEFAULT_PREFIX = "yugipedia_rush_cards"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

PRINT_REQUESTS = [
    "?English name (linked)=Name",
    "?Japanese name",
    "?Primary type",
    "?Secondary type",
    "?Rush Duel status=Status",
]


def build_query(limit, offset):
    parts = [
        "[[Rush Duel status::+]]",
        *PRINT_REQUESTS,
        "sort=",
        "order=asc",
        f"limit={limit}",
        f"offset={offset}",
    ]
    return "|".join(parts)


def fetch_batch(limit, offset, timeout, retries):
    params = {
        "action": "ask",
        "query": build_query(limit, offset),
        "format": "json",
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                YUGIPEDIA_API_URL,
                params=params,
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            query = response.json()["query"]
            results = query.get("results", {})
            if isinstance(results, list):
                results = {}
            return {
                "printrequests": query.get("printrequests", []),
                "results": results,
                "serializer": "SMW\\Serializers\\QueryResultSerializer",
                "version": 2,
                "rows": len(results),
            }
        except Exception as error:
            last_error = error
            print(f"  ERROR: {error}", flush=True)
            if attempt < retries:
                time.sleep(attempt * 3)

    raise RuntimeError(f"Could not fetch Yugipedia batch at offset {offset}: {last_error}")


def write_batch(batch, output_dir, prefix, offset):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{prefix}_{offset:04d}.json"
    path.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def build_parser():
    parser = argparse.ArgumentParser(
        description="Download Yugipedia Rush Duel card JSON batches through the Ask API."
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help=f"Directory for JSON batches. Defaults to {OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Output filename prefix. Defaults to {DEFAULT_PREFIX}.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per JSON batch. Defaults to 500.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between API requests in seconds. Defaults to 0.5.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Request timeout in seconds. Defaults to 45.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries per batch. Defaults to 3.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Optional cap for testing. Use 0 for all batches.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    offset = 0
    batch_count = 0
    total_rows = 0
    written = []

    while True:
        batch_count += 1
        print(f"[{batch_count}/?] Fetching offset {offset}", flush=True)
        batch = fetch_batch(
            limit=args.batch_size,
            offset=offset,
            timeout=args.timeout,
            retries=args.retries,
        )

        row_count = batch["rows"]
        if row_count == 0:
            break

        path = write_batch(batch, output_dir, args.prefix, offset)
        written.append(path)
        total_rows += row_count
        print(f"  Wrote {row_count} row(s) to {path}", flush=True)

        if row_count < args.batch_size:
            break

        if args.max_batches and batch_count >= args.max_batches:
            break

        offset += args.batch_size
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"Downloaded {total_rows} row(s) in {len(written)} file(s).")


if __name__ == "__main__":
    main()
