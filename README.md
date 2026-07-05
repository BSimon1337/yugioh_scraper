# Yu-Gi-Oh! Rush Duel Scraper

This project builds a normalized Rush Duel printing/image export from a Yugipedia JSON export and Konami's Rush Duel database.

The final card uploader CSV is one row per printing:

```text
cardname, cardnumber, imageurl, setname, rarity, setcode
```

Repeated card names are expected when a card has multiple printings, rarities, or set appearances.

## Setup

```powershell
pip install -r requirements.txt
```

Put the Yugipedia JSON export at:

```text
result.json
```

## Full Pipeline

Run card matching when using a new source JSON:

```powershell
python 01_match_konami.py --input result.json --output konami_matches.csv
```

Cards whose Yugipedia `Status` is `Not yet released` are marked `UNRELEASED` and skipped by default. To include them anyway:

```powershell
python 01_match_konami.py --input result.json --include-unreleased
```

Review any unresolved or ambiguous card matches:

```powershell
python 01b_review_matches.py list --status all
python 01b_review_matches.py interactive --status all --details
python 01b_review_matches.py sync-csv
```

To inspect unreleased skipped cards:

```powershell
python 01b_review_matches.py list --status unreleased
```

Interactive review controls:

```text
candidate number = choose that candidate
CID              = mark matched with that Konami CID
Konami URL       = mark matched with the cid in the URL
n                = mark as NO_MATCH
s or blank       = skip for now
q                = quit review
```

Scrape printings, download images, resolve mappings, validate, and export:

```powershell
python 02_scrape_printings.py --missing-only
python 03_download_images.py
python 03b_resolve_image_mappings.py --all
python 04_validate_images.py
python 05_export_carduploader.py --image-source konami
```

Or use the all-in-one runner after card review is complete:

```powershell
python run_pipeline.py
```

To include matching and launch interactive review if needed:

```powershell
python run_pipeline.py --run-match --interactive-review --from-json result.json
```

If review rows remain after the interactive pass, the runner stops before scraping printings.

## Export Modes

Konami source URLs:

```powershell
python 05_export_carduploader.py --image-source konami
```

Local downloaded files:

```powershell
python 05_export_carduploader.py --image-source local --output carduploader_export_local.csv
```

CDN URLs using downloaded image filenames:

```powershell
python 05_export_carduploader.py --image-source cdn --cdn-base-url "https://cdn.example.com/rush-images/"
```

## Image Mapping Audit

Export a detailed printing-to-image mapping audit:

```powershell
python 06_export_image_audit.py
```

This writes:

```text
image_mapping_audit.csv
```

It includes `cid`, `ciid`, mapping source, mapping notes, image URL, local path, and validation status for every printing.

The all-in-one runner writes this audit by default. Use `--no-audit` to skip it.

## Health Check

Run this before handoff:

```powershell
python check_pipeline_state.py
```

A clean handoff should have:

```text
needs_match_review: 0
needs_image_review: 0
missing_image_joins: 0
non_ok_images: 0
image_validation_report_rows: 0
```

## Handoff Files

For a basic URL ingest:

```text
carduploader_export.csv
```

For a safer handoff, include the downloaded images too:

```text
carduploader_export.csv
images/
```

Generated working files worth keeping for reproducibility:

```text
yugioh_rush.sqlite3
konami_matches.csv
konami_printings.csv
image_validation_report.csv
```
