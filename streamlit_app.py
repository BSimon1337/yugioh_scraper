import csv
import os
import importlib
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

from check_pipeline_state import build_state, csv_rows
from db import DB_PATH, connect_db, init_db

BATCH_DIR = Path("batches")
MATCHES_CSV = "konami_matches.csv"
EXPORT_CSV = "carduploader_export.csv"
REPORT_CSV = "image_validation_report.csv"

review_tools = importlib.import_module("01b_review_matches")


st.set_page_config(
    page_title="Rush Duel Pipeline",
    layout="wide",
)


def unbuffered_python_command(command):
    if not command:
        return command
    executable = Path(command[0]).name.lower()
    if executable.startswith("python") and "-u" not in command[1:2]:
        return [command[0], "-u", *command[1:]]
    return command


def command_box(command, timeout=None, title="Running command...", transcript_key=None):
    command = unbuffered_python_command(command)
    output_lines = []
    code = None

    with st.status(title, expanded=True) as status:
        st.code(" ".join(command), language="powershell")
        latest_placeholder = st.empty()
        state_placeholder = st.empty()
        progress_bar = st.progress(0, text="Waiting for output...")
        eta_placeholder = st.empty()
        log_placeholder = st.empty()
        progress_placeholder = st.empty()
        started_at = time.monotonic()
        last_state_at = 0

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        while process.poll() is None:
            if timeout and time.monotonic() - started_at > timeout:
                process.kill()
                code = 124
                if transcript_key:
                    st.session_state[transcript_key] = {
                        "command": command,
                        "code": code,
                        "lines": output_lines,
                    }
                status.update(label="Command timed out", state="error")
                return 124

            line = process.stdout.readline() if process.stdout else ""
            if line:
                output_lines.append(line.rstrip())
                latest_line = output_lines[-1]
                latest_placeholder.info(latest_line)
                log_placeholder.code("\n".join(output_lines[-30:]), language="text")
                progress_placeholder.caption(f"Showing last {min(len(output_lines), 30)} line(s)")

                match = re.search(r"\[(\d+)/(\d+)\]", latest_line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    progress = current / total if total else 0
                    elapsed = time.monotonic() - started_at
                    average_seconds = elapsed / current if current else 0
                    remaining = max(total - current, 0)
                    eta_seconds = remaining * average_seconds
                    finish_at = datetime.now() + timedelta(seconds=eta_seconds)
                    progress_bar.progress(
                        min(progress, 1.0),
                        text=f"{current}/{total}",
                    )
                    eta_placeholder.caption(
                        "ETA: "
                        f"{format_duration(eta_seconds)} remaining | "
                        f"est. finish {finish_at.strftime('%I:%M %p').lstrip('0')} | "
                        f"{average_seconds:.1f}s/item"
                    )

            if transcript_key == "pipeline_console" and time.monotonic() - last_state_at >= 5:
                last_state_at = time.monotonic()
                try:
                    current_state = state_summary()
                    if "cards" in current_state:
                        state_placeholder.caption(
                            "Live state: "
                            f"cards {current_state.get('cards', 0)} | "
                            f"matched {current_state.get('matched', 0)} | "
                            f"printings {current_state.get('printings', 0)} | "
                            f"images {current_state.get('images', 0)} | "
                            f"export rows {current_state.get('export_rows', 0)}"
                        )
                except Exception as error:
                    state_placeholder.caption(f"Live state unavailable: {error}")

            if not line:
                time.sleep(0.1)

        if process.stdout:
            for line in process.stdout.read().splitlines():
                output_lines.append(line)

        if output_lines:
            log_placeholder.code("\n".join(output_lines[-120:]), language="text")

        code = process.returncode
        if transcript_key:
            st.session_state[transcript_key] = {
                "command": command,
                "code": code,
                "lines": output_lines,
            }

        if code == 0:
            progress_bar.progress(1.0, text="Done")
            status.update(label="Command finished", state="complete")
        else:
            status.update(label=f"Command failed with exit code {code}", state="error")

    return code


def render_saved_command_output(transcript_key):
    transcript = st.session_state.get(transcript_key)
    if not transcript:
        st.caption("Run a command to see live output here.")
        return

    command = transcript.get("command", [])
    code = transcript.get("code")
    lines = transcript.get("lines", [])

    status = "finished" if code == 0 else f"exited with code {code}"
    st.caption(f"Last command {status}")
    st.code(" ".join(command), language="powershell")
    if lines:
        st.code("\n".join(lines[-200:]), language="text")
    else:
        st.caption("No output was captured.")


def db_exists():
    return Path(DB_PATH).exists()


def query_rows(query, params=()):
    if not db_exists():
        return []

    with connect_db() as connection:
        init_db(connection)
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def state_summary():
    try:
        return build_state(DB_PATH, EXPORT_CSV, REPORT_CSV)
    except sqlite3.OperationalError as error:
        if "database is locked" in str(error).lower():
            return {
                "errors": [
                    "SQLite is busy because another pipeline step is writing. "
                    "Refresh again in a moment."
                ],
                "warnings": [],
            }
        raise


def format_duration(seconds):
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def render_health():
    state = state_summary()
    if "cards" not in state:
        st.error("\n".join(state["errors"]) or "No database found yet.")
        return state

    metrics = [
        ("Cards", "cards"),
        ("Matched", "matched"),
        ("Needs Review", "needs_match_review"),
        ("No Match", "no_match"),
        ("Deferred", "deferred"),
        ("Unreleased", "unreleased"),
        ("Source Dupes", "source_duplicate"),
        ("Printings", "printings"),
        ("Images", "images"),
        ("Export Rows", "export_rows"),
        ("Image Issues", "report_rows"),
    ]

    columns = st.columns(5)
    for index, (label, key) in enumerate(metrics):
        value = state.get(key)
        if value is None:
            value = 0
        columns[index % len(columns)].metric(label, value)

    if state["errors"]:
        for error in state["errors"]:
            st.error(error)
    if state["warnings"]:
        for warning in state["warnings"]:
            st.warning(warning)

    with st.expander("Full State", expanded=False):
        st.json({
            key: value
            for key, value in state.items()
            if key not in {"duplicate_cids"}
        })
        if state.get("duplicate_cids"):
            st.write("Duplicate CIDs")
            st.dataframe([dict(row) for row in state["duplicate_cids"]], use_container_width=True)

    return state


def save_uploads(uploaded_files):
    BATCH_DIR.mkdir(exist_ok=True)
    saved_paths = []
    for uploaded_file in uploaded_files:
        target = BATCH_DIR / Path(uploaded_file.name).name
        target.write_bytes(uploaded_file.getbuffer())
        saved_paths.append(target)
    return saved_paths


def match_json_files(paths, delay, limit, include_unreleased):
    for path in paths:
        command = [
            sys.executable,
            "01_match_konami.py",
            "--input",
            str(path),
            "--output",
            MATCHES_CSV,
            "--delay",
            str(delay),
            "--limit",
            str(limit),
            "--merge-csv",
        ]
        if include_unreleased:
            command.append("--include-unreleased")

        code = command_box(command, timeout=None)
        if code != 0:
            return False
    return True


def konami_index_cache_summary():
    if not db_exists():
        return {"rows": 0, "updated_at": ""}

    with connect_db() as connection:
        init_db(connection)
        row = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COALESCE(MAX(updated_at), '') AS updated_at
            FROM konami_index_cache
            """
        ).fetchone()

    return dict(row)


def review_rows(status_filter):
    where = "match_status NOT IN ('MATCHED', 'UNRELEASED', 'SOURCE_DUPLICATE', 'DEFERRED')"
    params = []
    if status_filter != "all":
        status_map = {
            "review": ["REVIEW_MULTIPLE", "REVIEW", "ERROR"],
            "no-match": ["NO_MATCH"],
            "deferred": ["DEFERRED"],
            "unreleased": ["UNRELEASED"],
            "source-duplicate": ["SOURCE_DUPLICATE"],
        }
        statuses = status_map[status_filter]
        placeholders = ",".join("?" for _ in statuses)
        where = f"match_status IN ({placeholders})"
        params = statuses

    return query_rows(
        f"""
        SELECT page_title,
               english_name,
               japanese_name,
               search_text,
               source_status,
               source_file,
               match_status,
               cid,
               konami_name,
               konami_url,
               notes
        FROM cards
        WHERE {where}
        ORDER BY page_title
        """,
        params,
    )


def apply_review_action(page_title, action, cid_or_url="", notes=""):
    if action == "match":
        return command_box([
            sys.executable,
            "01b_review_matches.py",
            "set",
            page_title,
            cid_or_url,
            "--notes",
            notes or "manual review",
        ], timeout=60)

    if action == "no-match":
        return command_box([
            sys.executable,
            "01b_review_matches.py",
            "no-match",
            page_title,
            "--notes",
            notes or "manual review: no Konami match",
        ], timeout=60)

    if action == "defer":
        return command_box([
            sys.executable,
            "01b_review_matches.py",
            "defer",
            page_title,
            "--notes",
            notes or "manual review: skipped for later",
        ], timeout=60)

    if action == "source-duplicate":
        return command_box([
            sys.executable,
            "01b_review_matches.py",
            "source-duplicate",
            page_title,
            "--notes",
            notes or "manual review: source duplicate",
        ], timeout=60)

    return 1


def cards_for_cid(cid):
    return query_rows(
        """
        SELECT page_title,
               english_name,
               japanese_name,
               match_status,
               cid,
               konami_name,
               notes
        FROM cards
        WHERE cid = ?
        ORDER BY english_name, page_title
        """,
        (cid,),
    )


def candidate_rows(row):
    try:
        candidates = review_tools.fetch_search_candidates(row["search_text"])
    except Exception as error:
        st.error(f"Could not fetch candidates: {error}")
        return []

    known_names = {}
    if db_exists():
        with connect_db() as connection:
            init_db(connection)
            known_names = review_tools.fetch_known_names_by_cid(connection)

    output = []
    for index, candidate in enumerate(candidates, start=1):
        output.append({
            "choice": index,
            "cid": candidate["cid"],
            "konami_name": candidate["name"],
            "known_english": " | ".join(known_names.get(candidate["cid"], [])),
            "ruby": candidate["ruby"],
            "url": candidate["url"],
        })
    return output


def render_dashboard_tab():
    st.subheader("Pipeline Health")
    if st.button("Refresh Pipeline Health"):
        st.rerun()
    render_health()

    st.subheader("Quick Actions")
    col1, col2, col3 = st.columns(3)
    if col1.button("Run Downstream Pipeline", use_container_width=True):
        command_box([sys.executable, "run_pipeline.py"], timeout=None)
        st.rerun()
    if col2.button("Validate Images", use_container_width=True):
        command_box([sys.executable, "04_validate_images.py"], timeout=None)
        st.rerun()
    if col3.button("Export Card Uploader CSV", use_container_width=True):
        command_box([sys.executable, "05_export_carduploader.py"], timeout=None)
        st.rerun()


def render_import_tab():
    st.subheader("Import And Match JSON Batches")
    st.caption("Upload one or more Yugipedia JSON result files, or paste local paths. Matching merges into SQLite and konami_matches.csv.")

    st.write("Yugipedia CID Index")
    cache_summary = konami_index_cache_summary()
    cache_col1, cache_col2, cache_col3 = st.columns([1, 1, 2])
    cache_col1.metric("Cached CIDs", cache_summary.get("rows", 0))
    cache_col2.write(f"Updated: {cache_summary.get('updated_at') or 'never'}")
    if cache_col3.button("Refresh Yugipedia CID Index", use_container_width=True):
        command_box(
            [sys.executable, "00_update_konami_index_cache.py"],
            timeout=None,
            title="Refreshing Yugipedia CID index...",
        )

    st.write("Yugipedia JSON Batches")
    if st.button("Download Rush Duel JSON Batches", use_container_width=True):
        command_box(
            [sys.executable, "00_download_yugipedia_batches.py", "--output-dir", str(BATCH_DIR)],
            timeout=None,
            title="Downloading Yugipedia Rush Duel JSON batches...",
        )

    uploaded_files = st.file_uploader(
        "Upload JSON files",
        type=["json"],
        accept_multiple_files=True,
    )
    path_text = st.text_area(
        "Or paste JSON file paths, one per line",
        placeholder=r"C:\Users\Beau\Downloads\result (1).json",
    )
    use_downloaded_batches = st.checkbox("Use downloaded batches folder", value=False)

    col1, col2, col3 = st.columns(3)
    delay = col1.number_input("Match delay seconds", min_value=0.0, value=0.5, step=0.1)
    limit = col2.number_input("Limit per file, 0 = all", min_value=0, value=0, step=50)
    include_unreleased = col3.checkbox("Include unreleased", value=False)

    if st.button("Import / Match Selected Files", type="primary"):
        paths = []
        if uploaded_files:
            paths.extend(save_uploads(uploaded_files))
        if use_downloaded_batches:
            paths.extend(sorted(BATCH_DIR.glob("*.json")))
        paths.extend(Path(line.strip()) for line in path_text.splitlines() if line.strip())

        if not paths:
            st.warning("Choose at least one upload or path.")
        else:
            missing = [path for path in paths if not path.exists()]
            if missing:
                st.error("Missing files:\n" + "\n".join(str(path) for path in missing))
            else:
                if match_json_files(paths, delay=delay, limit=limit, include_unreleased=include_unreleased):
                    st.success("Matching finished.")
                    st.rerun()


def render_review_tab():
    st.subheader("Match Review")
    if not db_exists():
        st.info("Run matching first.")
        return

    if st.session_state.get("review_notice"):
        notice = st.session_state.pop("review_notice")
        if notice.get("type") == "success":
            st.success(notice["message"])
        else:
            st.error(notice["message"])

    status_filter = st.selectbox(
        "Status",
        ["all", "review", "no-match", "deferred", "unreleased", "source-duplicate"],
    )
    rows = review_rows(status_filter)
    st.write(f"{len(rows)} row(s)")

    if not rows:
        return

    labels = [
        f"{row['page_title']} | {row['match_status']} | cid {row.get('cid') or ''}"
        for row in rows
    ]
    selected_index = st.selectbox("Card", range(len(rows)), format_func=lambda index: labels[index])
    row = rows[selected_index]

    left, right = st.columns([1, 1])
    with left:
        st.write("Source")
        st.json({
            "page_title": row["page_title"],
            "english_name": row["english_name"],
            "japanese_name": row["japanese_name"],
            "source_status": row["source_status"],
            "source_file": row["source_file"],
            "status": row["match_status"],
            "current_cid": row["cid"],
            "notes": row["notes"],
        })

    with right:
        st.write("Candidates")
        if st.button("Fetch Live Candidates"):
            st.session_state["candidate_page"] = row["page_title"]
            st.session_state["candidate_rows"] = candidate_rows(row)

        if st.session_state.get("candidate_page") == row["page_title"]:
            candidates = st.session_state.get("candidate_rows", [])
            if candidates:
                st.dataframe(candidates, use_container_width=True, hide_index=True)
            else:
                st.info("No live candidates found.")

    st.divider()
    st.write("Apply Review Decision")
    if st.session_state.get("clear_review_inputs"):
        st.session_state["review_cid_or_url"] = ""
        st.session_state["review_notes"] = "manual review"
        st.session_state["clear_review_inputs"] = False

    cid_or_url = st.text_input("CID or Konami URL", key="review_cid_or_url")
    notes = st.text_input("Notes", value="manual review", key="review_notes")
    col1, col2, col3, col4 = st.columns(4)
    if col1.button("Match", type="primary", use_container_width=True):
        if not cid_or_url.strip():
            st.warning("Enter a CID or Konami URL.")
        else:
            code = apply_review_action(row["page_title"], "match", cid_or_url=cid_or_url.strip(), notes=notes)
            if code == 0:
                st.session_state["clear_review_inputs"] = True
                st.session_state["review_notice"] = {
                    "type": "success",
                    "message": f"Matched {row['page_title']}.",
                }
                st.rerun()
            else:
                st.error("Match command failed. Check the command output above.")
    if col2.button("No Match", use_container_width=True):
        code = apply_review_action(row["page_title"], "no-match", notes=notes)
        if code == 0:
            st.session_state["clear_review_inputs"] = True
            st.session_state["review_notice"] = {
                "type": "success",
                "message": f"Marked {row['page_title']} as NO_MATCH.",
            }
            st.rerun()
        else:
            st.error("No Match command failed. Check the command output above.")
    if col3.button("Skip For Later", use_container_width=True):
        code = apply_review_action(row["page_title"], "defer", notes=notes)
        if code == 0:
            st.session_state["clear_review_inputs"] = True
            st.session_state["review_notice"] = {
                "type": "success",
                "message": f"Skipped {row['page_title']} for later.",
            }
            st.rerun()
        else:
            st.error("Skip command failed. Check the command output above.")
    if col4.button("Source Duplicate", use_container_width=True):
        code = apply_review_action(row["page_title"], "source-duplicate", notes=notes)
        if code == 0:
            st.session_state["clear_review_inputs"] = True
            st.session_state["review_notice"] = {
                "type": "success",
                "message": f"Marked {row['page_title']} as SOURCE_DUPLICATE.",
            }
            st.rerun()
        else:
            st.error("Source Duplicate command failed. Check the command output above.")


def render_pipeline_tab():
    st.subheader("Pipeline Commands")
    image_source = st.selectbox("Image source", ["konami", "local", "cdn"])
    cdn_base_url = st.text_input("CDN base URL")
    set_name_source = st.selectbox("Set name source", ["english", "original"])
    skip_download = st.checkbox("Skip image download", value=False)
    no_audit = st.checkbox("Skip image audit export", value=False)

    command = [
        sys.executable,
        "run_pipeline.py",
        "--image-source",
        image_source,
        "--set-name-source",
        set_name_source,
    ]
    if image_source == "cdn":
        command.extend(["--cdn-base-url", cdn_base_url])
    if skip_download:
        command.append("--skip-download")
    if no_audit:
        command.append("--no-audit")

    st.code(" ".join(command), language="powershell")
    if st.button("Run Pipeline", type="primary"):
        if image_source == "cdn" and not cdn_base_url.strip():
            st.warning("CDN base URL is required for CDN mode.")
        else:
            command_box(
                command,
                timeout=None,
                title="Running pipeline...",
                transcript_key="pipeline_console",
            )

    st.subheader("Individual Steps")
    steps = [
        ("Scrape missing printings", [sys.executable, "02_scrape_printings.py", "--missing-only", "--limit", "0"]),
        ("Enrich English set names", [sys.executable, "02b_enrich_set_names.py"]),
        ("Download images", [sys.executable, "03_download_images.py"]),
        ("Resolve image mappings", [sys.executable, "03b_resolve_image_mappings.py", "--all"]),
        ("Validate images", [sys.executable, "04_validate_images.py"]),
        ("Export card uploader CSV", [sys.executable, "05_export_carduploader.py"]),
        ("Export image audit", [sys.executable, "06_export_image_audit.py"]),
    ]
    for label, step_command in steps:
        if st.button(label):
            command_box(
                step_command,
                timeout=None,
                title=f"Running {label.lower()}...",
                transcript_key="pipeline_console",
            )

    st.subheader("Live Pipeline Console")
    render_saved_command_output("pipeline_console")


def render_issues_tab():
    st.subheader("Image Validation Report")
    report_rows = csv_rows(REPORT_CSV) or []
    st.write(f"{len(report_rows)} row(s)")
    if report_rows:
        st.dataframe(report_rows, use_container_width=True)

    st.subheader("Duplicate CIDs")
    state = state_summary()
    duplicate_cids = [dict(row) for row in state.get("duplicate_cids", [])]
    if duplicate_cids:
        st.dataframe(duplicate_cids, use_container_width=True)

        st.write("Resolve Duplicate CID")
        cid_options = [row["cid"] for row in duplicate_cids]
        selected_cid = st.selectbox(
            "Duplicate CID",
            cid_options,
            format_func=lambda cid: next(
                f"{row['cid']} | {row['names']}"
                for row in duplicate_cids
                if row["cid"] == cid
            ),
        )
        duplicate_rows = cards_for_cid(selected_cid)
        st.dataframe(duplicate_rows, use_container_width=True, hide_index=True)

        if duplicate_rows:
            row_options = list(range(len(duplicate_rows)))
            selected_index = st.selectbox(
                "Card row to update",
                row_options,
                format_func=lambda index: (
                    f"{duplicate_rows[index]['english_name']} "
                    f"({duplicate_rows[index]['page_title']})"
                ),
            )
            selected_row = duplicate_rows[selected_index]
            new_cid = st.text_input(
                "Replacement CID or Konami URL",
                key=f"duplicate-replacement-{selected_cid}",
            )
            notes = st.text_input(
                "Issue notes",
                value="duplicate CID cleanup",
                key=f"duplicate-notes-{selected_cid}",
            )
            col1, col2, col3 = st.columns(3)
            if col1.button("Update Match", use_container_width=True):
                if not new_cid.strip():
                    st.warning("Enter a replacement CID or Konami URL.")
                else:
                    code = apply_review_action(
                        selected_row["page_title"],
                        "match",
                        cid_or_url=new_cid.strip(),
                        notes=notes,
                    )
                    if code == 0:
                        st.success(f"Updated {selected_row['page_title']}.")
                        st.rerun()
                    else:
                        st.error("Update Match failed. Check command output above.")
            if col2.button("Source Duplicate", use_container_width=True):
                code = apply_review_action(
                    selected_row["page_title"],
                    "source-duplicate",
                    notes=notes or "duplicate CID cleanup: source duplicate",
                )
                if code == 0:
                    st.success(f"Marked {selected_row['page_title']} as SOURCE_DUPLICATE.")
                    st.rerun()
                else:
                    st.error("Source Duplicate failed. Check command output above.")
            if col3.button("Skip For Later", use_container_width=True):
                code = apply_review_action(
                    selected_row["page_title"],
                    "defer",
                    notes=notes or "duplicate CID cleanup: skipped for later",
                )
                if code == 0:
                    st.success(f"Deferred {selected_row['page_title']}.")
                    st.rerun()
                else:
                    st.error("Skip For Later failed. Check command output above.")
    else:
        st.success("No duplicate matched CIDs.")


def render_exports_tab():
    st.subheader("Generated Files")
    files = [
        EXPORT_CSV,
        "carduploader_export_local.csv",
        "carduploader_export_cdn.csv",
        "image_mapping_audit.csv",
        MATCHES_CSV,
        "konami_printings.csv",
        REPORT_CSV,
    ]
    for path in files:
        file_path = Path(path)
        if not file_path.exists():
            continue

        col1, col2, col3 = st.columns([3, 1, 1])
        col1.write(path)
        col2.write(f"{file_path.stat().st_size:,} bytes")
        with file_path.open("rb") as file:
            col3.download_button("Download", file, file_name=file_path.name, key=f"download-{path}")

    if Path(EXPORT_CSV).exists():
        st.subheader("Export Preview")
        with open(EXPORT_CSV, "r", encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        st.dataframe(rows[:100], use_container_width=True)


st.title("Rush Duel Scraper Pipeline")
tabs = st.tabs(["Dashboard", "Import", "Review", "Pipeline", "Issues", "Exports"])

with tabs[0]:
    render_dashboard_tab()
with tabs[1]:
    render_import_tab()
with tabs[2]:
    render_review_tab()
with tabs[3]:
    render_pipeline_tab()
with tabs[4]:
    render_issues_tab()
with tabs[5]:
    render_exports_tab()
