
from datetime import datetime, timezone

import streamlit as st

from app import (
    MAX_LINKS,
    ensure_batch_metadata,
    get_google_service_account_info,
    get_secret,
    import_product,
)

QUEUE_TAB = "Scanner Queue"


def _open_book():
    info = get_google_service_account_info()
    ref = str(get_secret("GOOGLE_SHEET_URL") or "").strip()
    if not info:
        raise RuntimeError("Google Sheets service account is not configured.")
    if not ref:
        raise RuntimeError("GOOGLE_SHEET_URL is not configured.")

    import gspread
    gc = gspread.service_account_from_dict(info)
    book = gc.open_by_url(ref) if ref.startswith(("http://", "https://")) else gc.open_by_key(ref)
    return book, gspread


def _load_queue():
    book, gspread = _open_book()
    try:
        ws = book.worksheet(QUEUE_TAB)
    except gspread.WorksheetNotFound:
        return [], None

    values = ws.get_all_values()
    if len(values) < 2:
        return [], ws

    headers = values[0]
    out = []
    for row_num, raw in enumerate(values[1:], start=2):
        padded = raw + [""] * max(0, len(headers) - len(raw))
        rec = dict(zip(headers, padded))
        rec["_row_num"] = row_num
        status = str(rec.get("Status") or "Pending").strip().lower()
        if status in {"", "pending", "queued"} and str(rec.get("Product Link") or "").strip():
            out.append(rec)

    def n(v):
        try:
            return int(str(v or "0").replace(",", ""))
        except Exception:
            return 0

    out.sort(
        key=lambda r: (
            n(r.get("Creator Count")),
            n(r.get("Video Count")),
            n(r.get("Combined Views")),
        ),
        reverse=True,
    )
    return out, ws


def _mark_imported(ws, records, batch_id):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for rec in records:
        row_num = int(rec["_row_num"])
        ws.update(
            range_name=f"J{row_num}:L{row_num}",
            values=[["Imported", now, batch_id]],
            value_input_option="USER_ENTERED",
        )


st.set_page_config(page_title="Scanner Queue", page_icon="📥", layout="wide")
st.title("Scanner Queue")
st.caption("Select products sent by the Creator Scanner and load them directly into Flow Fashion.")

try:
    pending, ws = _load_queue()
except Exception as exc:
    st.error(f"Could not open Scanner Queue: {exc}")
    st.stop()

if not pending:
    st.info("No pending Scanner products.")
    st.stop()

jobs = list(st.session_state.get("jobs") or [])
behavior = st.radio(
    "Import behavior",
    ["Add to current batch", "Start new batch"],
    horizontal=True,
)

if behavior == "Add to current batch":
    available = max(0, MAX_LINKS - len(jobs))
    st.caption(f"Current batch: {len(jobs)}/{MAX_LINKS} products · {available} slot(s) available.")
else:
    available = MAX_LINKS
    st.caption(f"New batch capacity: {MAX_LINKS} products.")

rows = [{
    "Import": False,
    "Product": r.get("Product Name", ""),
    "Creators": r.get("Creators", ""),
    "Creator Count": r.get("Creator Count", ""),
    "Video Count": r.get("Video Count", ""),
    "Views": r.get("Combined Views", ""),
    "Product Link": r.get("Product Link", ""),
    "Source Video": r.get("Source Video", ""),
    "Queued At": r.get("Queued At", ""),
} for r in pending]

edited = st.data_editor(
    rows,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Import": st.column_config.CheckboxColumn("Import", default=False),
        "Product Link": st.column_config.LinkColumn("Product Link"),
        "Source Video": st.column_config.LinkColumn("Source Video"),
    },
    disabled=["Product", "Creators", "Creator Count", "Video Count", "Views", "Product Link", "Source Video", "Queued At"],
    key="scanner_queue_import_editor",
)

selected_idx = [i for i, row in enumerate(edited) if bool(row.get("Import"))]

if len(selected_idx) > available:
    st.warning(f"You selected {len(selected_idx)}, but only {available} slot(s) are available.")

selected = [pending[i] for i in selected_idx[:available]]

label = (
    f"Add {len(selected)} to current Flow Fashion batch"
    if behavior == "Add to current batch"
    else f"Start new Flow Fashion batch with {len(selected)}"
)

if st.button(
    label,
    type="primary",
    use_container_width=True,
    disabled=(not selected or len(selected_idx) > available or available <= 0),
):
    social_token = get_secret("SOCIAVAULT_API_KEY")
    region = get_secret("SOCIAVAULT_REGION", "US") or "US"

    if not social_token:
        st.error("SOCIAVAULT_API_KEY is missing.")
        st.stop()

    imported = []
    failures = []

    bar = st.progress(0, text="Importing Scanner products with SociaVault…")
    for n, rec in enumerate(selected, start=1):
        link = str(rec.get("Product Link") or "").strip()
        try:
            imported.append(import_product(link, social_token, region))
        except Exception as exc:
            failures.append((rec, str(exc)))
        bar.progress(n / len(selected), text=f"Imported {n}/{len(selected)}")

    if behavior == "Add to current batch":
        existing_urls = {str(j.get("url") or "").strip() for j in jobs}
        additions = [
            j for j in imported
            if str(j.get("url") or "").strip() not in existing_urls
        ]
        new_jobs = jobs + additions
        batch_id, _ = ensure_batch_metadata(new_jobs)
    else:
        new_jobs = imported[:MAX_LINKS]
        batch_id, _ = ensure_batch_metadata(new_jobs, force_new=True)

    st.session_state["jobs"] = new_jobs
    st.session_state.pop("videos_zip", None)
    st.session_state.pop("full_batch_zip", None)
    st.session_state.pop("batch_history_cache", None)

    successful_links = {str(j.get("url") or "").strip() for j in imported}
    successful_rows = [
        rec for rec in selected
        if str(rec.get("Product Link") or "").strip() in successful_links
    ]

    if ws is not None and successful_rows:
        _mark_imported(ws, successful_rows, batch_id)

    if failures:
        st.warning(f"Loaded {len(imported)} product(s); {len(failures)} failed and remain Pending.")
        for rec, error in failures[:5]:
            st.caption(f"{rec.get('Product Name') or rec.get('Product Link')} — {error}")
    else:
        st.success(f"Loaded {len(imported)} product(s) into Flow Fashion and marked them Imported.")

    if hasattr(st, "switch_page"):
        if st.button("Open Flow Fashion workspace", type="primary"):
            st.switch_page("app.py")
