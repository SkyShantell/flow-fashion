import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st
from PIL import Image

APP_NAME = "Flow Try-On Factory"
FLOW_BASE = "https://api.useapi.net/v1/google-flow"
SOCIA_BASE = "https://api.sociavault.com/v1"
SOCIA_PRODUCT_DETAILS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-details"
SOCIA_PRODUCT_REVIEWS = f"{SOCIA_BASE}/scrape/tiktok-shop/product-reviews"
IMAGE_MODEL = "nano-banana-2"
VIDEO_MODEL = "omni-flash"  # Flow UI label: Omni 1.1 Flash
VIDEO_DURATION = 8
VIDEO_RESOLUTION = "720p"
MAX_LINKS = 10
MAX_PRODUCT_REFS = 5  # plus avatar = max 6 total refs, comfortably under NB2 max 10
IMAGE_WORKERS = 3
IMPORT_WORKERS = 4
VIDEO_POLL_INTERVAL = 8
VIDEO_WAIT_TIMEOUT = 600
BATCH_HISTORY_TAB = "Batch History"
BATCH_DATA_TAB = "_Flow Batch Data"

SCENES = {
    "Modern apartment mirror": "a realistic modern upscale apartment with a large full-length mirror, warm ceiling lighting, neutral furniture, and believable lived-in details",
    "Walk-in closet": "a realistic upscale walk-in closet with dark wood shelving, folded clothes, soft warm recessed lighting, and a large full-length mirror",
    "Luxury bathroom mirror": "a realistic upscale bathroom with dark stone surfaces, a large clean mirror, warm ceiling light, and subtle hotel-like details",
    "Penthouse at night": "a realistic modern penthouse at night with floor-to-ceiling windows, city lights, warm recessed lighting, polished floor, and a large full-length mirror",
}


def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value or default).strip()
    except Exception:
        return str(os.environ.get(name, default) or default).strip()


def get_google_service_account_info() -> dict | None:
    """Load a Google service-account credential from Streamlit secrets.

    Supports either:
      GOOGLE_SERVICE_ACCOUNT_JSON = '{...json...}'
    or the common Streamlit table:
      [gcp_service_account]
      type = "service_account"
      ...
    """
    try:
        raw = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    except Exception:
        raw = None
    if raw:
        if isinstance(raw, dict):
            return dict(raw)
        try:
            return json.loads(str(raw))
        except Exception:
            pass
    try:
        raw = st.secrets.get("gcp_service_account")
        if raw:
            return dict(raw)
    except Exception:
        pass
    env_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if env_raw:
        try:
            return json.loads(env_raw)
        except Exception:
            pass
    return None


def get_drive_archive_config() -> dict:
    """Configuration for the user-owned Google Drive archive web app.

    We intentionally use an Apps Script web app for ordinary My Drive accounts.
    Google service accounts have no Drive storage quota and cannot own files in
    My Drive, so the service account used for Sheets is not a durable media store.
    """
    # Accept both secret names for backward compatibility with the setup guide.
    url = get_secret("GOOGLE_DRIVE_ARCHIVE_WEBHOOK_URL") or get_secret("GOOGLE_DRIVE_ARCHIVE_URL")
    secret = get_secret("GOOGLE_DRIVE_ARCHIVE_SECRET")
    auto_raw = get_secret("GOOGLE_DRIVE_AUTO_ARCHIVE", "true").lower()
    return {
        "url": url,
        "secret": secret,
        "configured": bool(url and secret),
        "auto": auto_raw not in {"0", "false", "no", "off"},
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_batch_id(jobs: list[dict]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    seed = "|".join(str(j.get("url") or j.get("id") or j.get("name") or "") for j in jobs)
    digest = hashlib.sha1(f"{seed}|{time.time_ns()}".encode("utf-8")).hexdigest()[:7]
    return f"{stamp}-{digest}"


def ensure_batch_metadata(jobs: list[dict], *, force_new: bool = False, batch_id: str = "", created_at: str = "") -> tuple[str, str]:
    """Ensure the current Streamlit session has durable batch identity metadata."""
    if force_new or not st.session_state.get("batch_id"):
        st.session_state["batch_id"] = batch_id or _new_batch_id(jobs)
        st.session_state["batch_created_at"] = created_at or _utc_now_iso()
        st.session_state.pop("last_batch_sync_fingerprint", None)
    elif batch_id:
        st.session_state["batch_id"] = batch_id
    if created_at:
        st.session_state["batch_created_at"] = created_at
    st.session_state.setdefault("batch_created_at", _utc_now_iso())
    bid = str(st.session_state.get("batch_id") or _new_batch_id(jobs))
    created = str(st.session_state.get("batch_created_at") or _utc_now_iso())
    for job in jobs:
        job["_batch_id"] = bid
        job["_batch_created_at"] = created
    return bid, created


def _job_usage(job: dict) -> tuple[int, int, int, int]:
    image_calls = int(job.get("image_attempts") or 0)
    video_calls = int(job.get("video_attempts") or 0)
    retries = max(0, image_calls - 1) + max(0, video_calls - 1)
    failures = int(job.get("image_failures") or 0) + int(job.get("video_failures") or 0)
    return image_calls, video_calls, retries, failures


def batch_usage(jobs: list[dict]) -> dict:
    out = {"image_calls": 0, "video_calls": 0, "retries": 0, "failures": 0}
    for job in jobs:
        a, b, c, d = _job_usage(job)
        out["image_calls"] += a
        out["video_calls"] += b
        out["retries"] += c
        out["failures"] += d
    return out


def usage_cost_estimate(jobs: list[dict]) -> tuple[float, bool]:
    try:
        image_rate = float(st.session_state.get("image_cost_rate", get_secret("FLOW_IMAGE_COST_USD", "0")) or 0)
        video_rate = float(st.session_state.get("video_cost_rate", get_secret("FLOW_VIDEO_COST_USD", "0")) or 0)
    except Exception:
        image_rate = video_rate = 0.0
    usage = batch_usage(jobs)
    return usage["image_calls"] * image_rate + usage["video_calls"] * video_rate, bool(image_rate or video_rate)


def _dashboard_stage(job: dict) -> str:
    image_status = str(job.get("image_status") or "pending").lower()
    video_status = str(job.get("video_status") or "pending").lower()
    if image_status == "failed" or video_status == "failed":
        return "Failed"
    if video_status == "completed":
        return "Ready"
    if job.get("video_job_id") and video_status not in {"completed", "failed"}:
        return "Processing"
    if image_status == "completed" and not job.get("approved"):
        return "Needs approval"
    if image_status == "completed" and job.get("approved"):
        return "Ready for video"
    return "Pending"


def _batch_status(jobs: list[dict]) -> str:
    if not jobs:
        return "Empty"
    stages = [_dashboard_stage(j) for j in jobs]
    if all(x == "Ready" for x in stages):
        return "Complete"
    if any(x == "Failed" for x in stages):
        return "Needs attention"
    if any(x == "Processing" for x in stages):
        return "Processing"
    if any(x == "Needs approval" for x in stages):
        return "Needs approval"
    return "In progress"


def _persistable_job(job: dict) -> dict:
    """Compact state snapshot for reopening a batch later; intentionally excludes image base64."""
    allowed = {
        "url", "id", "name", "focus", "back_design", "selected_refs",
        "image_status", "image_job_id", "image_media_id", "image_url", "image_seed", "image_error", "approved",
        "video_status", "video_job_id", "video_submitted_at", "video_url", "video_media_id", "video_error", "thumbnail_url",
        "drive_image_id", "drive_image_url", "drive_image_download_url", "drive_image_error",
        "drive_video_id", "drive_video_url", "drive_video_download_url", "drive_video_error",
        "drive_batch_folder_url", "drive_product_folder_url",
        "drive_reference_ids", "drive_reference_urls", "drive_reference_download_urls", "drive_reference_errors",
        "regen_instruction", "last_regen_instruction",
        "image_attempts", "video_attempts", "image_failures", "video_failures", "sheet_row",
        "_batch_id", "_batch_created_at",
    }
    return {k: v for k, v in job.items() if k in allowed}


def _batch_sync_fingerprint(jobs: list[dict]) -> str:
    payload = {
        "batch_id": st.session_state.get("batch_id"),
        "created": st.session_state.get("batch_created_at"),
        "jobs": [_persistable_job(j) for j in jobs],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def inject_css():
    st.markdown(
        """
        <style>
        :root { color-scheme: dark; }
        html, body, [class*="css"] { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .stApp { background:#070b12; color:#f8fafc; }
        [data-testid="stAppViewContainer"] { background:linear-gradient(180deg,#080d17 0%,#070b12 100%); }
        .block-container { max-width:1280px; padding-top:2rem; padding-bottom:10rem; }

        /* R12.1 scroll safety — Streamlit Cloud can occasionally leave the main pane
           height-constrained after sidebar/image rerenders. Make the app shell fixed to
           the viewport and give the main/sidebar panes their own explicit scrolling. */
        html, body, #root { height:100% !important; min-height:100% !important; }
        [data-testid="stAppViewContainer"] { height:100vh !important; min-height:100vh !important; overflow:hidden !important; }
        [data-testid="stMain"] {
            height:100vh !important;
            min-height:0 !important;
            overflow-y:auto !important;
            overflow-x:hidden !important;
            -webkit-overflow-scrolling:touch !important;
            overscroll-behavior-y:contain;
            scrollbar-gutter:stable;
        }
        [data-testid="stMainBlockContainer"], .block-container {
            min-height:max-content !important;
            overflow:visible !important;
        }
        section[data-testid="stSidebar"] { height:100vh !important; overflow-y:auto !important; }
        section[data-testid="stSidebar"] > div { min-height:max-content !important; }

        /* Sidebar */
        section[data-testid="stSidebar"] { background:#0b111c; border-right:1px solid #1c2636; }
        section[data-testid="stSidebar"] > div { padding-top:1rem; }
        .sidebar-brand { padding:8px 2px 18px; }
        .sidebar-brand .logo { font-size:1.18rem; font-weight:800; letter-spacing:-.02em; color:#fff; }
        .sidebar-brand .sub { color:#7f8ca3; font-size:.78rem; margin-top:2px; }
        .section-label { color:#718096; font-size:.68rem; font-weight:800; letter-spacing:.14em; margin:16px 0 8px; }
        .connection-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:9px 11px; border:1px solid #1b2534; background:#0e1623; border-radius:12px; margin:7px 0; font-size:.82rem; }
        .connection-row strong { color:#dbe5f4; font-weight:600; }
        .dot-ok,.dot-warn { width:8px; height:8px; border-radius:999px; display:inline-block; margin-right:7px; }
        .dot-ok { background:#22c55e; box-shadow:0 0 0 3px rgba(34,197,94,.10); }
        .dot-warn { background:#f59e0b; box-shadow:0 0 0 3px rgba(245,158,11,.10); }

        /* Header */
        .app-header { margin-bottom:22px; }
        .app-kicker { color:#7c8aa3; font-size:.72rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }
        .app-title { margin:4px 0 5px; font-size:2.05rem; line-height:1.12; letter-spacing:-.045em; font-weight:850; color:#fff; }
        .app-subtitle { margin:0; color:#8f9bb0; max-width:760px; font-size:.94rem; line-height:1.55; }
        .top-pills { margin-top:11px; display:flex; flex-wrap:wrap; gap:7px; }
        .top-pill { display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; background:#101827; border:1px solid #202c3f; color:#aebbd0; font-size:.73rem; font-weight:650; }

        /* Panels */
        div[data-testid="stVerticalBlockBorderWrapper"] { border:1px solid #1b2637 !important; background:#0b111b !important; border-radius:18px !important; box-shadow:none !important; }
        div[data-testid="stVerticalBlockBorderWrapper"] > div { padding:4px; }
        .panel-title { font-size:1.05rem; font-weight:780; color:#f8fafc; margin:0 0 2px; }
        .panel-sub { color:#7f8ca3; font-size:.82rem; margin-bottom:12px; }
        .product-title { color:#f8fafc; font-size:1.08rem; font-weight:760; line-height:1.35; margin:0; }
        .product-id { color:#66758d; font-size:.74rem; margin-top:3px; }

        /* Streamlit text / labels */
        h1,h2,h3,h4,h5,h6,p,li,span { color:inherit; }
        [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] span { color:#aeb9ca !important; font-weight:600 !important; }
        [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color:#79869b !important; }
        .stMarkdown p { color:#aeb9ca; }

        /* Inputs */
        .stTextInput input, .stTextArea textarea, .stNumberInput input {
            background:#0f1724 !important; color:#f8fafc !important; border:1px solid #253247 !important; border-radius:12px !important;
            caret-color:#8b7cff !important;
        }
        .stTextInput input::placeholder, .stTextArea textarea::placeholder { color:#59677c !important; opacity:1 !important; }
        div[data-baseweb="select"] > div { background:#0f1724 !important; border-color:#253247 !important; border-radius:12px !important; color:#f8fafc !important; }
        div[data-baseweb="select"] span, div[data-baseweb="select"] input { color:#f8fafc !important; }
        div[data-baseweb="popover"] { color:#f8fafc !important; }
        div[data-baseweb="popover"] ul { background:#101827 !important; }
        div[role="option"] { color:#dce5f2 !important; background:#101827 !important; }
        div[role="option"]:hover { background:#182338 !important; }
        .stRadio label, .stCheckbox label { color:#cbd5e1 !important; }
        .stFileUploader section { background:#0f1724 !important; border:1px dashed #31415b !important; border-radius:14px !important; }
        .stFileUploader section * { color:#aeb9ca !important; }

        /* Buttons */
        div.stButton > button, div.stDownloadButton > button {
            min-height:43px; border-radius:12px; font-weight:760; border:1px solid #27354b; background:#111a29; color:#dbe5f4;
            transition:all .15s ease;
        }
        div.stButton > button:hover, div.stDownloadButton > button:hover { border-color:#566787; background:#152033; color:#fff; }
        div.stButton > button[kind="primary"] { background:linear-gradient(90deg,#4f78ff,#7657ff); border:0; color:white; box-shadow:0 8px 22px rgba(89,90,255,.18); }
        div.stButton > button[kind="primary"]:hover { filter:brightness(1.07); }
        div.stButton > button:disabled, div.stDownloadButton > button:disabled {
            background:#0b111b !important; color:#49566b !important; border-color:#182233 !important; opacity:1 !important;
        }

        /* Tabs */
        .stTabs [data-baseweb="tab-list"] { gap:8px; border-bottom:1px solid #1a2535; }
        .stTabs [data-baseweb="tab"] { height:44px; padding:0 16px; color:#7f8ca3; background:transparent; border-radius:10px 10px 0 0; font-weight:700; }
        .stTabs [aria-selected="true"] { color:#fff !important; background:#101827 !important; }
        .stTabs [data-baseweb="tab-highlight"] { background:#725dff !important; }

        /* Metrics */
        div[data-testid="stMetric"] { background:#0d1521; border:1px solid #1d293a; border-radius:15px; padding:14px 16px; }
        div[data-testid="stMetricLabel"] p { color:#7f8ca3 !important; font-size:.76rem !important; font-weight:700 !important; }
        div[data-testid="stMetricValue"] { color:#f8fafc !important; font-size:1.55rem !important; font-weight:790 !important; }

        /* Alerts / progress */
        div[data-testid="stAlert"] { border-radius:13px; border-width:1px; }
        .stProgress > div > div > div > div { background:linear-gradient(90deg,#4f78ff,#7657ff); }

        /* Images */
        [data-testid="stImage"] img { border-radius:14px; border:1px solid #1d293a; }
        /* Keep reference galleries compact; object-fit preserves the whole garment. */
        [data-testid="stHorizontalBlock"] [data-testid="column"] [data-testid="stImage"] img { max-height:300px !important; object-fit:contain !important; background:#0b1523; }

        /* Dataframe */
        [data-testid="stDataFrame"] { border:1px solid #1b2637; border-radius:14px; overflow:hidden; }

        hr { border-color:#26354a !important; }

        /* R5 high-contrast overrides — keep every control readable */
        .stApp, [data-testid="stAppViewContainer"] { color:#eef4ff !important; }
        [data-testid="stAppViewContainer"] { background:linear-gradient(180deg,#08111f 0%,#07101b 52%,#060d17 100%) !important; }
        section[data-testid="stSidebar"] { background:#0b1625 !important; border-right:1px solid #26364c !important; }
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p { color:#d7e1ef !important; }
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color:#9cabc0 !important; }
        .connection-row { background:#101d2d !important; border-color:#26364c !important; }
        .connection-row > span:last-child { color:#a9b8cb !important; }
        .section-label { color:#9aacc2 !important; }

        .app-kicker { color:#9caec4 !important; }
        .app-subtitle, .panel-sub { color:#a9b7ca !important; }
        .top-pill { background:#122033 !important; border-color:#2b3c55 !important; color:#d0dbea !important; }
        .product-id { color:#9badc3 !important; }
        [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] span { color:#dbe5f2 !important; }
        [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color:#a8b6c9 !important; }
        .stMarkdown p { color:#c3cfde !important; }
        a { color:#72b8ff !important; }

        /* Expander headers were rendering as a white bar on Streamlit Cloud */
        [data-testid="stExpander"] details { background:#0b1523 !important; border:1px solid #27384f !important; border-radius:16px !important; overflow:hidden; }
        [data-testid="stExpander"] summary { background:#101d2d !important; color:#f5f8fc !important; min-height:50px !important; padding:0 16px !important; }
        [data-testid="stExpander"] summary:hover { background:#14243a !important; }
        [data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span { color:#f5f8fc !important; font-weight:750 !important; }
        [data-testid="stExpander"] summary svg { fill:#c9d7e8 !important; color:#c9d7e8 !important; }

        /* Inputs and selectors */
        .stTextInput input, .stTextArea textarea, .stNumberInput input {
            background:#101c2b !important; color:#f7faff !important; border:1px solid #40516a !important;
        }
        .stTextInput input::placeholder, .stTextArea textarea::placeholder { color:#8292a8 !important; opacity:1 !important; }
        div[data-baseweb="select"] > div { background:#101c2b !important; border-color:#40516a !important; }
        div[data-baseweb="select"] span, div[data-baseweb="select"] input { color:#f7faff !important; }
        .stRadio label, .stRadio label p, .stRadio label span, .stCheckbox label, .stCheckbox label p, .stCheckbox label span { color:#dbe5f2 !important; }

        /* Uploader and buttons */
        .stFileUploader section { background:#0f1b2a !important; border-color:#3b516e !important; }
        .stFileUploader section *, [data-testid="stFileUploader"] small { color:#c4d0df !important; }
        [data-testid="stFileUploader"] button { background:#17263a !important; color:#f4f8ff !important; border:1px solid #405675 !important; }
        div.stButton > button, div.stDownloadButton > button { background:#152236 !important; color:#eef4ff !important; border-color:#364b68 !important; }
        div.stButton > button:hover, div.stDownloadButton > button:hover { background:#1b2d46 !important; border-color:#6883a9 !important; color:#ffffff !important; }
        div.stButton > button:disabled, div.stDownloadButton > button:disabled { background:#0e1826 !important; color:#70839d !important; border-color:#24344a !important; }
        div.stButton > button[kind="primary"] { background:linear-gradient(90deg,#3f82ff,#7658ff) !important; color:#ffffff !important; border:0 !important; }

        /* Cards, metrics, alerts */
        div[data-testid="stVerticalBlockBorderWrapper"] { background:#0b1523 !important; border-color:#26374d !important; }
        div[data-testid="stMetric"] { background:#101d2d !important; border-color:#2b3d56 !important; }
        div[data-testid="stMetricLabel"] p { color:#a8b7ca !important; }
        div[data-testid="stMetricValue"] { color:#ffffff !important; }
        [data-testid="stAlert"] p, [data-testid="stAlert"] span { color:#e4ecf7 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

def password_gate():
    password = get_secret("APP_PASSWORD")
    if not password:
        return
    if st.session_state.get("authenticated"):
        return
    st.markdown("### 🔒 Private app")
    entered = st.text_input("Password", type="password")
    if st.button("Enter", type="primary"):
        if entered == password:
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


def flow_headers(token: str, json_content: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def parse_error(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            error = payload.get("error") or payload.get("message") or payload.get("detail")
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error)
            if error:
                return str(error)[:1200]
        return json.dumps(payload)[:1200]
    except Exception:
        return (resp.text or f"HTTP {resp.status_code}")[:1200]


def request_json(method: str, url: str, *, headers=None, params=None, json_body=None, data=None, timeout=180, retries=2) -> dict:
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_body, data=data, timeout=timeout)
            if resp.status_code < 400:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {parse_error(resp)}"
            if resp.status_code in {429, 502, 503} and attempt < retries:
                time.sleep(3 + attempt * 3)
                continue
            raise RuntimeError(last_error)
        except requests.Timeout:
            last_error = "Request timed out."
            if attempt < retries:
                time.sleep(2)
                continue
            raise RuntimeError(last_error)
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(2)
                continue
            raise RuntimeError(last_error)
    raise RuntimeError(last_error or "Request failed")


def normalize_image_bytes(data: bytes, mime: str = "image/jpeg", max_side: int = 1800, quality: int = 92) -> tuple[bytes, str]:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            bg = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                bg.paste(image, mask=image.getchannel("A"))
            else:
                bg.paste(image.convert("RGB"))
            image = bg
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        if mime in {"image/jpeg", "image/png", "image/webp"}:
            return data, mime
        return data, "image/jpeg"


def flow_accounts(token: str) -> dict:
    return request_json("GET", f"{FLOW_BASE}/accounts", headers=flow_headers(token), timeout=45, retries=0)


def flow_upload_asset(token: str, image_bytes: bytes, mime: str, email: str = "") -> str:
    image_bytes, mime = normalize_image_bytes(image_bytes, mime)
    url = f"{FLOW_BASE}/assets"
    if email:
        url += "/" + quote(email, safe="")
    resp = requests.post(url, headers={**flow_headers(token), "Content-Type": mime}, data=image_bytes, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"Flow asset upload failed — HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    media = payload.get("mediaGenerationId")
    if isinstance(media, dict):
        media = media.get("mediaGenerationId")
    if not media:
        raise RuntimeError("Flow uploaded the image but returned no mediaGenerationId.")
    return str(media)


def flow_generate_image(token: str, email: str, prompt: str, refs: list[str]) -> dict:
    body = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "aspectRatio": "9:16",
        "count": 1,
    }
    if email:
        body["email"] = email
    for i, ref in enumerate(refs[:10], start=1):
        body[f"reference_{i}"] = ref
    payload = request_json("POST", f"{FLOW_BASE}/images", headers=flow_headers(token, True), json_body=body, timeout=180, retries=1)
    media = payload.get("media") or []
    if not media:
        raise RuntimeError("Nano Banana 2 returned no image media.")
    generated = (((media[0] or {}).get("image") or {}).get("generatedImage") or {})
    media_id = generated.get("mediaGenerationId")
    if not media_id:
        media_id = (media[0] or {}).get("mediaGenerationId")
    if not media_id:
        raise RuntimeError("Nano Banana 2 returned no generated image mediaGenerationId.")
    return {
        "job_id": payload.get("jobId") or payload.get("jobid"),
        "media_id": media_id,
        "url": generated.get("fifeUrl"),
        "encoded": generated.get("encodedImage"),
        "seed": generated.get("seed"),
    }


def flow_submit_video(token: str, email: str, image_media_id: str, prompt: str) -> dict:
    body = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "aspectRatio": "portrait",
        "duration": VIDEO_DURATION,
        "resolution": VIDEO_RESOLUTION,
        "count": 1,
        "startImage": image_media_id,
        "async": True,
    }
    if email:
        body["email"] = email
    payload = request_json("POST", f"{FLOW_BASE}/videos", headers=flow_headers(token, True), json_body=body, timeout=90, retries=1)
    job_id = payload.get("jobid") or payload.get("jobId")
    if not job_id:
        raise RuntimeError("Omni 1.1 submitted without returning a job ID.")
    return {"job_id": job_id, "status": payload.get("status") or "created"}


def flow_get_job(token: str, job_id: str) -> dict:
    # Flow job IDs intentionally contain literal separators such as ':' (for
    # example ...-email:user@example.com-bot:google-flow).  Unlike asset IDs,
    # useapi's /jobs/{jobId} endpoint validates that textual job-ID format.
    # Percent-encoding the separators (':' -> '%3A') causes HTTP 400
    # "Invalid job ID format", so preserve the ID structure in the path.
    jid = str(job_id or "").strip().strip("\"'")
    if not jid:
        raise RuntimeError("Missing Flow job ID.")
    safe_jid = quote(jid, safe=":@+-._")
    return request_json("GET", f"{FLOW_BASE}/jobs/{safe_jid}", headers=flow_headers(token), timeout=60, retries=1)


def flow_jobs_overview(token: str, options: str = "history") -> dict:
    """Return Flow job activity. history includes executing + last 10 completed jobs from ~15 minutes."""
    return request_json(
        "GET",
        f"{FLOW_BASE}/jobs/",
        headers=flow_headers(token),
        params={"options": options},
        timeout=60,
        retries=1,
    )


def remember_video_job_ids(jobs: list[dict]) -> None:
    """Keep submitted job IDs in the browser URL so a Streamlit redeploy can recover them."""
    ids = []
    for job in jobs:
        jid = str(job.get("video_job_id") or "").strip()
        if jid and jid not in ids:
            ids.append(jid)
    if ids:
        try:
            st.query_params["flow_jobs"] = ",".join(ids[-10:])
        except Exception:
            pass


def flow_resolve_asset_url(token: str, media_id: str) -> str:
    if not media_id:
        return ""
    try:
        payload = request_json("GET", f"{FLOW_BASE}/assets/{quote(media_id, safe='')}", headers=flow_headers(token), timeout=60, retries=0)
        return str(payload.get("url") or "")
    except Exception:
        return ""


def parse_video_job(payload: dict, token: str) -> dict:
    # Keep status polling lightweight. Do NOT resolve/download the MP4 here;
    # doing that during every Streamlit rerun can make the UI appear frozen.
    status = str(payload.get("status") or "unknown").lower()
    result = {"status": status}
    if status == "failed":
        result["error"] = str(payload.get("error") or (payload.get("response") or {}).get("error") or "Video generation failed.")
        return result
    response = payload.get("response") or {}
    media = response.get("media") or []
    if media:
        item = media[0] or {}
        result.update({
            "video_url": item.get("videoUrl") or "",
            "video_media_id": item.get("mediaGenerationId") or "",
            "thumbnail_url": item.get("thumbnailUrl"),
        })
    return result


def download_url(url: str, timeout: int = 90) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content, (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0]


def resolve_video_url(token: str, media_id: str) -> tuple[str, str]:
    """Return (signed_url, message). Fast, explicit action only."""
    if not media_id:
        return "", "No video media ID is available yet."
    try:
        resp = requests.get(
            f"{FLOW_BASE}/assets/{quote(media_id, safe='')}",
            headers=flow_headers(token),
            timeout=30,
        )
        if resp.status_code == 200:
            payload = resp.json() if resp.content else {}
            return str(payload.get("url") or ""), ""
        if resp.status_code == 503:
            wait = resp.headers.get("Retry-After") or "a few"
            return "", f"Video exists but its signed URL is not ready yet. Try again in {wait} seconds."
        try:
            detail = resp.json().get("error")
        except Exception:
            detail = resp.text[:300]
        return "", detail or f"Could not resolve video URL (HTTP {resp.status_code})."
    except Exception as exc:
        return "", f"Could not resolve video URL: {exc}"


def download_video_raw(token: str, media_id: str) -> tuple[bytes | None, str]:
    """Fetch MP4 bytes through useapi raw fallback only when user asks."""
    if not media_id:
        return None, "No video media ID is available yet."
    try:
        resp = requests.get(
            f"{FLOW_BASE}/assets/{quote(media_id, safe='')}",
            params={"raw": "true"},
            headers=flow_headers(token),
            timeout=180,
        )
        if resp.status_code == 200 and resp.content:
            return resp.content, ""
        if resp.status_code == 503:
            wait = resp.headers.get("Retry-After") or "a few"
            return None, f"Google is still preparing this MP4. Try again in {wait} seconds."
        try:
            detail = resp.json().get("error")
        except Exception:
            detail = resp.text[:300]
        return None, detail or f"MP4 fetch failed (HTTP {resp.status_code})."
    except Exception as exc:
        return None, f"MP4 fetch failed: {exc}"


def image_bytes_from_result(result: dict) -> bytes | None:
    if result.get("encoded"):
        try:
            return base64.b64decode(result["encoded"])
        except Exception:
            pass
    if result.get("url"):
        try:
            return download_url(result["url"], 90)[0]
        except Exception:
            pass
    return None


def _normalize_remote_url(value) -> str:
    """Normalize CDN/image URL values returned by SociaVault/TikTok."""
    if value is None:
        return ""
    url = html.unescape(str(value)).strip().strip('"\'')
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return ""


def _sv_values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        return [value]
    return []


def _sv_first_url(value) -> str:
    if isinstance(value, str):
        return _normalize_remote_url(value)
    if not isinstance(value, dict):
        return ""
    for key in ("url_list", "urlList", "urls", "review_images", "reviewImages", "images"):
        for candidate in _sv_values(value.get(key)):
            url = _sv_first_url(candidate)
            if url:
                return url
    for key in ("url", "image_url", "imageUrl", "display_image_url", "displayImageUrl", "original_url", "originalUrl", "preview_url", "previewUrl", "src"):
        url = _sv_first_url(value.get(key))
        if url:
            return url
    for key in ("thumb_url_list", "thumbUrlList", "thumbnail_url", "thumbnailUrl"):
        url = _sv_first_url(value.get(key))
        if url:
            return url
    return ""


def _sv_collect_urls(value, max_depth=8):
    urls = []
    def add(url):
        url = _normalize_remote_url(url)
        if url and url not in urls:
            urls.append(url)
    def walk(node, depth=0, path=()):
        if depth > max_depth:
            return
        if isinstance(node, str):
            p = " ".join(path).lower()
            if not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(node)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child, depth + 1, path)
        elif isinstance(node, dict):
            best = _sv_first_url(node)
            p = " ".join(path).lower()
            if best and not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(best)
            for key, child in node.items():
                walk(child, depth + 1, path + (str(key).lower(),))
    walk(value)
    return urls

def sociavault_get(endpoint: str, token: str, params: dict) -> dict:
    resp = requests.get(endpoint, headers={"X-API-Key": token, "Accept": "application/json"}, params=params, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError(f"SociaVault HTTP {resp.status_code}: {parse_error(resp)}")
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or payload.get("error") or "SociaVault request failed."))
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("SociaVault returned no product data.")
    return data


def dedupe(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def import_product(url: str, token: str, region: str) -> dict:
    data = sociavault_get(SOCIA_PRODUCT_DETAILS, token, {"url": url, "get_related_videos": "false", "region": region})
    product = data.get("product_base") or data.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    name = str(product.get("title") or product.get("name") or "Unknown Product").strip()
    product_id = str(data.get("product_id") or product.get("id") or hashlib.sha1(url.encode()).hexdigest()[:12])
    # SociaVault documents product_base.images as an array of image URLs, but
    # handle strings, objects and nested variants defensively.
    listing = []
    raw_images = product.get("images")
    for obj in _sv_values(raw_images):
        if isinstance(obj, str):
            u = _normalize_remote_url(obj)
        else:
            u = _sv_first_url(obj)
        if u:
            listing.append(u)
    if not listing:
        listing = _sv_collect_urls(raw_images or product)[:18]
    listing = dedupe([_normalize_remote_url(u) for u in listing if _normalize_remote_url(u)])[:18]

    reviews = []
    review_block = data.get("product_detail_review") or {}
    review_items = _sv_values(review_block.get("review_items") if isinstance(review_block, dict) else None)
    for item in review_items:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        reviews.extend(_sv_collect_urls({
            "images": review.get("images"),
            "media": review.get("media"),
            "review_images": review.get("review_images"),
            "display_image_url": review.get("display_image_url"),
        }))
    reviews = [u for u in dedupe(reviews) if u not in set(listing)]
    if not reviews and product_id:
        try:
            review_data = sociavault_get(SOCIA_PRODUCT_REVIEWS, token, {"product_id": product_id, "page": 1})
            review_root = review_data.get("product_reviews") or review_data.get("reviews") or review_data
            for review in _sv_values(review_root):
                if isinstance(review, dict):
                    reviews.extend(_sv_collect_urls(review))
        except Exception:
            pass
    reviews = [u for u in dedupe(reviews) if u not in set(listing)][:24]
    if not listing and not reviews:
        raise RuntimeError("No usable product images were returned.")
    default_refs = dedupe(listing[:2] + reviews[:1])[:MAX_PRODUCT_REFS]
    if not default_refs:
        default_refs = dedupe(listing + reviews)[:3]
    return {
        "id": product_id,
        "url": url,
        "name": name,
        "listing_images": listing,
        "review_images": reviews,
        "selected_refs": default_refs,
        "focus": classify_focus(name),
        "back_design": False,
        "image_status": "pending",
        "approved": False,
        "video_status": "pending",
    }


def classify_focus(name: str) -> str:
    # Token/phrase-aware classification. Avoid treating "short sleeve" as shorts/pants.
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    tokens = set(text.split())
    shoes = {"shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "heel", "heels", "sandal", "sandals", "loafer", "loafers", "clog", "clogs", "slipper", "slippers", "slides"}
    tops = {"shirt", "tee", "hoodie", "sweater", "jacket", "coat", "blouse", "top", "tank", "cardigan", "jersey", "polo"}
    bottoms = {"pants", "pant", "jeans", "jean", "shorts", "leggings", "legging", "jogger", "joggers", "trouser", "trousers", "skirt", "cargo"}
    outfit_phrases = ("two piece", "2 piece", "matching set", "tracksuit", "jumpsuit", "romper")
    outfit_tokens = {"set", "outfit", "suit", "dress"}

    if tokens & shoes:
        return "shoes"
    if any(p in text for p in outfit_phrases) or tokens & outfit_tokens:
        return "outfit"
    # Check tops before bottoms so words such as "short sleeve polo shirt" remain tops.
    if tokens & tops or "t shirt" in text:
        return "shirt"
    if tokens & bottoms:
        return "pants"
    return "outfit"


def image_prompt(job: dict, scene: str, refs_count: int) -> str:
    focus = job.get("focus", "outfit")
    product = job.get("name", "clothing")
    ref_mentions = " ".join(f"@reference_{i}" for i in range(2, refs_count + 1))
    if focus == "pants":
        focus_rule = "The bottoms/pants are the hero. Make the waist, fit through the hips and thighs, leg shape, length, pockets and hem easy to judge. The free hand can naturally touch the waistband, pocket or thigh."
        fallback = "Keep the top simple and neutral so it does not compete with the pants."
    elif focus == "shirt":
        focus_rule = "The shirt/top is the hero. Make the neckline, chest graphic or texture, sleeves, fit, hem and silhouette easy to judge. The free hand can naturally touch the chest, collar, sleeve or hem."
        fallback = "If no matching bottoms are sold in the references, pair the top with simple fitted black pants and clean white sneakers."
    elif focus == "shoes":
        focus_rule = "The footwear is the hero. Keep the full body visible but pose with one foot slightly forward so the shoes are clear and correctly shaped."
        fallback = "Use simple neutral clothing that does not compete with the footwear."
    else:
        focus_rule = "The full outfit is the hero. Show the top and bottom together clearly from head to toe with believable fit, drape and proportions."
        fallback = "Do not substitute, redesign, or add a different matching piece."
    back = "The product references indicate a back design; use a slight angled pose that hints at it without hiding the front." if job.get("back_design") else ""
    revision = str(job.get("active_regen_instruction") or "").strip()
    revision_rule = f"OPERATOR REVISION REQUEST: {revision}. Apply this request while preserving the exact product and avatar." if revision else ""
    return re.sub(r"\s+", " ", f"""
Create one photorealistic vertical 9:16 iPhone mirror-selfie image for a TikTok clothing try-on.
REFERENCE RULES: @reference_1 is the exact PERSON/AVATAR. Preserve this person's identity, face, skin, hair, body build and visible personal features. Do not copy the avatar's original clothes. {ref_mentions} are CLOTHING/PRODUCT references for {product}. Ignore any people/models appearing in those clothing references and use only the actual product design, colors, materials, print, construction and fit cues.
Dress @reference_1 in the exact product shown by the clothing references. {focus_rule} {fallback} {back} {revision_rule}
Scene: {SCENES[scene]}. The person holds a black smartphone at face level in one hand while taking a natural full-length mirror selfie. Full body head-to-toe must stay in frame. Casual confident UGC posture, slight weight shift, natural imperfect framing.
Realism: authentic smartphone photo, natural skin texture, realistic hands and fingers, believable fabric folds and seams, subtle sensor grain, mild phone-camera compression, no studio lighting, no glossy commercial fashion look, no extra people, no duplicated limbs, no morphing, no text overlays, no prices, no added logos or watermarks. The image must look like an ordinary creator actually trying on the product.
""").strip()


def video_prompt(job: dict) -> str:
    focus = job.get("focus", "outfit")
    back = bool(job.get("back_design"))
    if focus == "pants":
        motion = "Start full-body front view. Shift weight naturally, then use the free hand to touch or gesture to the waistband, pocket and upper thigh. Take a small half-step and quarter-turn to show the side fit and leg shape. Finish angled with one leg slightly forward, keeping the pants visible from waist to hem."
    elif focus == "shirt":
        motion = "Start full-body front view. Use the free hand to brush the chest, collar, sleeve or hem while gently shifting weight. Make a natural quarter-turn to show the side silhouette."
        if back:
            motion += " Briefly rotate farther to clearly reveal the back graphic/design, then return toward the mirror."
        motion += " Finish with a relaxed front/three-quarter pose, shirt still clearly visible."
    elif focus == "shoes":
        motion = "Start full-body with one foot slightly forward. Shift weight between feet, make one small natural step, then angle the legs to show the footwear from front and side. Finish with one foot forward and the shoes unobstructed."
    else:
        motion = "Start in a centered full-body front pose. Use the free hand to gesture naturally across the top and lower outfit, then shift weight and make a smooth quarter-turn to show the silhouette from the side."
        if back:
            motion += " Briefly turn enough to reveal the back design."
        motion += " Finish in a confident three-quarter full-body pose showing the complete outfit."
    return re.sub(r"\s+", " ", f"""
8-second vertical 9:16 TikTok UGC mirror try-on, one continuous shot, no cuts. Use the supplied start image as the exact first frame and preserve the exact same person, face, body, clothing, shoes, room, mirror, phone, lighting and product details for the entire clip.
Movement should look like a real person casually checking the fit in a mirror, not a fashion runway and not exaggerated choreography. {motion}
Keep the phone naturally at face level most of the time. Maintain realistic anatomy, garment physics and mirror geometry. The clothing must never change color, print, material, proportions or pieces. No zoom jump, no camera cut, no transformation, no extra people, no duplicate body parts, no text, subtitles, captions, music, speech or sound effects. Natural subtle iPhone handheld motion only.
""").strip()


@st.cache_data(show_spinner=False, ttl=3600, max_entries=256)
def fetch_remote_image(url: str) -> tuple[bytes, str]:
    """Fetch a product image through the Streamlit server.

    Do not advertise AVIF support first: TikTok's CDN may return AVIF and the
    default Pillow build on Streamlit Cloud may not decode it reliably.
    """
    url = _normalize_remote_url(url)
    if not url:
        raise RuntimeError("Invalid image URL")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36",
        "Accept": "image/webp,image/jpeg,image/png,image/*;q=0.8,*/*;q=0.4",
        "Referer": "https://www.tiktok.com/",
        "Cache-Control": "no-cache",
    }
    resp = requests.get(url, timeout=45, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    if not resp.content or len(resp.content) < 64:
        raise RuntimeError("Image response was empty")
    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].lower()
    if mime.startswith("text/") or "json" in mime:
        raise RuntimeError(f"CDN returned {mime}, not an image")
    normalized, normalized_mime = normalize_image_bytes(resp.content, mime, max_side=1400, quality=88)
    # Validate that the normalized result is actually decodable before giving it to Streamlit.
    try:
        check = Image.open(io.BytesIO(normalized))
        check.verify()
    except Exception as exc:
        raise RuntimeError(f"Unsupported/undecodable image format ({mime})") from exc
    return normalized, normalized_mime


def _browser_image_html(url: str, alt: str = "Product reference") -> str:
    safe_url = html.escape(_normalize_remote_url(url), quote=True)
    safe_alt = html.escape(alt, quote=True)
    return (
        f'<div style="width:100%;aspect-ratio:1/1.18;border:1px solid #26364c;border-radius:14px;'
        f'overflow:hidden;background:#0d1521;display:flex;align-items:center;justify-content:center;">'
        f'<img src="{safe_url}" alt="{safe_alt}" referrerpolicy="no-referrer" '
        f'style="width:100%;height:100%;object-fit:contain;display:block;" />'
        f'</div>'
    )


def ensure_job_refs(job: dict, token: str, email: str, avatar_id: str) -> tuple[list[str], dict]:
    signature = hashlib.sha1("|".join(job.get("selected_refs") or []).encode()).hexdigest()
    if job.get("ref_signature") == signature and job.get("flow_product_ref_ids"):
        return [avatar_id] + list(job["flow_product_ref_ids"]), job
    ids = []
    for url in (job.get("selected_refs") or [])[:MAX_PRODUCT_REFS]:
        data, mime = fetch_remote_image(url)
        ids.append(flow_upload_asset(token, data, mime, email))
    if not ids:
        raise RuntimeError("No product references could be uploaded to Flow.")
    job = dict(job)
    job["flow_product_ref_ids"] = ids
    job["ref_signature"] = signature
    return [avatar_id] + ids, job


def generate_one_image(job: dict, token: str, email: str, avatar_id: str, scene: str) -> dict:
    updated = dict(job)
    updated["image_attempts"] = int(updated.get("image_attempts") or 0) + 1
    try:
        refs, updated = ensure_job_refs(updated, token, email, avatar_id)
        result = flow_generate_image(token, email, image_prompt(updated, scene, len(refs)), refs)
        updated.update({
            "image_status": "completed",
            "image_job_id": result.get("job_id"),
            "image_media_id": result.get("media_id"),
            "image_url": result.get("url"),
            "image_encoded": result.get("encoded"),
            "image_seed": result.get("seed"),
            "image_error": None,
            "approved": False,
            "video_status": "pending",
            "video_job_id": None,
            "video_submitted_at": None,
            "video_url": None,
            "video_media_id": None,
            "video_error": None,
            "drive_image_id": None,
            "drive_image_url": None,
            "drive_image_download_url": None,
            "drive_image_error": None,
            "drive_video_id": None,
            "drive_video_url": None,
            "drive_video_download_url": None,
            "drive_video_error": None,
            "drive_batch_folder_url": None,
        })
    except Exception as exc:
        updated["image_status"] = "failed"
        updated["image_error"] = str(exc)
        updated["image_failures"] = int(updated.get("image_failures") or 0) + 1
    updated.pop("active_regen_instruction", None)
    return updated


def submit_one_video(job: dict, token: str, email: str) -> dict:
    updated = dict(job)
    if not updated.get("image_media_id"):
        updated["video_status"] = "failed"
        updated["video_error"] = "No completed image media ID."
        return updated
    updated["video_attempts"] = int(updated.get("video_attempts") or 0) + 1
    try:
        result = flow_submit_video(token, email, updated["image_media_id"], video_prompt(updated))
        updated.update({
            "video_job_id": result["job_id"],
            "video_status": result.get("status") or "created",
            "video_submitted_at": time.time(),
            "video_url": None,
            "video_media_id": None,
            "video_error": None,
            "drive_video_id": None,
            "drive_video_url": None,
            "drive_video_download_url": None,
            "drive_video_error": None,
        })
    except Exception as exc:
        updated["video_status"] = "failed"
        updated["video_error"] = str(exc)
        updated["video_failures"] = int(updated.get("video_failures") or 0) + 1
    return updated


def refresh_one_video(job: dict, token: str) -> dict:
    updated = dict(job)
    if not updated.get("video_job_id"):
        return updated
    try:
        result = parse_video_job(flow_get_job(token, updated["video_job_id"]), token)
        updated["video_status"] = result.get("status") or updated.get("video_status")
        if result.get("video_url"):
            updated["video_url"] = result["video_url"]
        if result.get("video_media_id"):
            updated["video_media_id"] = result["video_media_id"]
        if result.get("thumbnail_url"):
            updated["thumbnail_url"] = result["thumbnail_url"]
        updated["video_error"] = result.get("error")
    except Exception as exc:
        updated["video_error"] = str(exc)
    return updated


def wait_for_video(job: dict, token: str, timeout: int = VIDEO_WAIT_TIMEOUT, poll_interval: int = VIDEO_POLL_INTERVAL, progress_callback=None) -> dict:
    """Poll an async Flow video job until it completes, fails, or the wait window ends."""
    updated = dict(job)
    if not updated.get("video_job_id"):
        return updated

    started_at = time.time()
    while True:
        updated = refresh_one_video(updated, token)
        status = str(updated.get("video_status") or "created").lower()
        elapsed = int(time.time() - started_at)
        if progress_callback:
            progress_callback(status, elapsed)
        if status in {"completed", "failed"}:
            return updated
        if elapsed >= timeout:
            updated["video_error"] = "Still generating in Google Flow. Use Check status later; the job ID has been saved."
            return updated
        time.sleep(poll_interval)


def wait_for_video_batch(jobs: list[dict], token: str, indices: list[int], timeout: int = VIDEO_WAIT_TIMEOUT, poll_interval: int = VIDEO_POLL_INTERVAL, progress_callback=None) -> list[dict]:
    """Poll a group of already-submitted jobs together so batch generation finishes in one action."""
    jobs = [dict(j) for j in jobs]
    pending = {i for i in indices if jobs[i].get("video_job_id") and str(jobs[i].get("video_status") or "").lower() not in {"completed", "failed"}}
    started_at = time.time()
    while pending:
        for idx in list(pending):
            jobs[idx] = refresh_one_video(jobs[idx], token)
            if str(jobs[idx].get("video_status") or "").lower() in {"completed", "failed"}:
                pending.discard(idx)
        elapsed = int(time.time() - started_at)
        if progress_callback:
            progress_callback(len(indices) - len(pending), len(indices), elapsed, jobs)
        if not pending or elapsed >= timeout:
            break
        time.sleep(poll_interval)

    if pending:
        for idx in pending:
            jobs[idx]["video_error"] = "Still generating in Google Flow. Use Check all video statuses later; the job ID has been saved."
    return jobs


def safe_name(text: str, fallback: str = "product") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return (text[:80] or fallback)


def jobs_to_manifest(jobs: list[dict]) -> str:
    clean = []
    for j in jobs:
        clean.append({k: v for k, v in j.items() if k not in {"image_encoded"}})
    return json.dumps(clean, indent=2)


def jobs_export_rows(jobs: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Stable batch export used by CSV and Google Sheets."""
    headers = [
        "Product #",
        "Product name",
        "Product link",
        "Product ID",
        "Try-on focus",
        "Back design",
        "Image status",
        "Approved",
        "Image URL",
        "Image media ID",
        "Google Drive image URL",
        "Google Drive image file ID",
        "Video status",
        "Video URL",
        "Video media ID",
        "Video job ID",
        "Google Drive video URL",
        "Google Drive video file ID",
        "Google Drive batch folder",
        "Selected reference URLs",
        "Regeneration instruction",
        "Image calls",
        "Video calls",
        "Retries",
        "Generation failures",
        "Sheet row",
    ]
    rows = []
    for i, job in enumerate(jobs, 1):
        rows.append([
            str(i),
            str(job.get("name") or ""),
            str(job.get("url") or ""),
            str(job.get("id") or ""),
            str(job.get("focus") or ""),
            "Yes" if job.get("back_design") else "No",
            _status_label(job.get("image_status")),
            "Yes" if job.get("approved") else "No",
            str(job.get("image_url") or ""),
            str(job.get("image_media_id") or ""),
            str(job.get("drive_image_url") or ""),
            str(job.get("drive_image_id") or ""),
            _status_label(job.get("video_status")),
            str(job.get("video_url") or ""),
            str(job.get("video_media_id") or ""),
            str(job.get("video_job_id") or ""),
            str(job.get("drive_video_url") or ""),
            str(job.get("drive_video_id") or ""),
            str(job.get("drive_product_folder_url") or job.get("drive_batch_folder_url") or ""),
            " | ".join(str(x) for x in (job.get("selected_refs") or []) if x),
            str(job.get("last_regen_instruction") or job.get("regen_instruction") or ""),
            str(_job_usage(job)[0]),
            str(_job_usage(job)[1]),
            str(_job_usage(job)[2]),
            str(_job_usage(job)[3]),
            str(job.get("sheet_row") or ""),
        ])
    return headers, rows


def jobs_to_csv(jobs: list[dict]) -> bytes:
    """Excel/Google-Sheets friendly UTF-8 CSV, including the original product link."""
    headers, rows = jobs_export_rows(jobs)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def build_images_zip(jobs: list[dict]) -> bytes | None:
    out = io.BytesIO()
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, job in enumerate(jobs, 1):
            if job.get("image_status") != "completed":
                continue
            data = None
            if job.get("image_encoded"):
                try:
                    data = base64.b64decode(job["image_encoded"])
                except Exception:
                    data = None
            if not data and job.get("image_url"):
                try:
                    data = download_url(job["image_url"], 90)[0]
                except Exception:
                    data = None
            if data:
                z.writestr(f"{i:02d}_{safe_name(job.get('name'))}.jpg", data)
                added += 1
    return out.getvalue() if added else None


def download_video_for_job(token: str, job: dict) -> bytes | None:
    """Fetch a completed MP4 without relying on an expiring signed URL."""
    media_id = str(job.get("video_media_id") or "").strip()
    if media_id:
        data, _ = download_video_raw(token, media_id)
        if data:
            return data
    url = str(job.get("video_url") or "").strip()
    if url and "drive.google.com" not in url:
        try:
            return download_url(url, 180)[0]
        except Exception:
            pass
    if job.get("drive_video_id"):
        data, _mime, _error = read_archived_drive_file(str(job.get("drive_video_id")))
        if data:
            return data
    return None


def build_videos_zip(jobs: list[dict], token: str) -> bytes | None:
    out = io.BytesIO()
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, job in enumerate(jobs, 1):
            if job.get("video_status") != "completed":
                continue
            data = download_video_for_job(token, job)
            if data:
                z.writestr(f"{i:02d}_{safe_name(job.get('name'))}.mp4", data)
                added += 1
    return out.getvalue() if added else None


def build_full_batch_zip(jobs: list[dict], token: str) -> tuple[bytes | None, dict]:
    """Package CSV + JSON + every completed image/video into one ZIP."""
    out = io.BytesIO()
    image_count = 0
    video_count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("batch.csv", jobs_to_csv(jobs))
        z.writestr("manifest.json", jobs_to_manifest(jobs))
        for i, job in enumerate(jobs, 1):
            stem = f"{i:02d}_{safe_name(job.get('name'))}"
            if job.get("image_status") == "completed":
                data = None
                if job.get("image_encoded"):
                    try:
                        data = base64.b64decode(job["image_encoded"])
                    except Exception:
                        data = None
                if not data and job.get("image_url"):
                    try:
                        data = download_url(job["image_url"], 90)[0]
                    except Exception:
                        data = None
                if data:
                    z.writestr(f"images/{stem}.jpg", data)
                    image_count += 1
            if job.get("video_status") == "completed":
                data = download_video_for_job(token, job)
                if data:
                    z.writestr(f"videos/{stem}.mp4", data)
                    video_count += 1
    payload = out.getvalue()
    return (payload if (jobs or image_count or video_count) else None), {"images": image_count, "videos": video_count}


def image_bytes_for_job(job: dict) -> tuple[bytes | None, str]:
    """Return durable bytes for a completed generated image."""
    if job.get("image_encoded"):
        try:
            return base64.b64decode(job["image_encoded"]), "image/jpeg"
        except Exception:
            pass
    if job.get("drive_image_id"):
        data, mime, _ = read_archived_drive_file(str(job.get("drive_image_id")))
        if data:
            return data, (mime or "image/jpeg")
    for raw_url in (job.get("drive_image_download_url"), job.get("image_url"), job.get("drive_image_url")):
        url = str(raw_url or "").strip()
        if not url:
            continue
        try:
            data, mime = download_url(url, 120)
            return data, (mime or "image/jpeg")
        except Exception:
            pass
    return None, "image/jpeg"


def drive_batch_name(jobs: list[dict]) -> str:
    """Deterministic folder name so a retry/redeploy does not create a new batch."""
    fingerprint = "|".join(str(j.get("url") or j.get("id") or j.get("name") or "") for j in jobs)
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:8] if fingerprint else "batch"
    return f"Flow Try-On {digest}"


def _archive_filename(index: int, job: dict, kind: str) -> str:
    media_id = str(job.get("image_media_id") if kind == "image" else job.get("video_media_id") or job.get("video_job_id") or "")
    media_tag = safe_name(media_id, "media")[-18:]
    ext = "jpg" if kind == "image" else "mp4"
    return f"{index:02d}_{safe_name(job.get('name'))}_{media_tag}.{ext}"


def archive_bytes_to_google_drive(data: bytes, mime_type: str, filename: str, kind: str, batch_name: str, description: str = "", product_name: str = "", batch_date: str = "") -> tuple[dict | None, str]:
    """Upload one media file to the user's Drive through the Apps Script bridge."""
    cfg = get_drive_archive_config()
    if not cfg["configured"]:
        return None, "Google Drive archive is not configured."
    # Keep the Apps Script JSON request comfortably below common web-app limits.
    if len(data) > 32 * 1024 * 1024:
        return None, f"{filename} is larger than 32 MB; use the local batch download for this file."
    body = {
        "secret": cfg["secret"],
        "filename": filename,
        "mime_type": mime_type,
        "kind": kind,
        "batch_name": batch_name,
        "product_name": product_name,
        "batch_date": batch_date,
        "description": description,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    try:
        resp = requests.post(cfg["url"], json=body, timeout=180, allow_redirects=True)
        if resp.status_code >= 400:
            return None, f"Drive archive HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            payload = resp.json()
        except Exception:
            return None, f"Drive archive returned a non-JSON response: {resp.text[:300]}"
        if not payload.get("ok"):
            return None, str(payload.get("error") or "Google Drive archive rejected the upload.")
        return payload, ""
    except Exception as exc:
        return None, f"Google Drive archive failed: {exc}"


def read_archived_drive_file(file_id: str) -> tuple[bytes | None, str, str]:
    """Retrieve a permanently archived private Drive file through the authenticated Apps Script bridge."""
    cfg = get_drive_archive_config()
    if not cfg["configured"] or not str(file_id or "").strip():
        return None, "", "Drive archive is not configured or file ID is missing."
    try:
        resp = requests.post(
            cfg["url"],
            json={"secret": cfg["secret"], "action": "read", "file_id": str(file_id).strip()},
            timeout=180,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None, "", f"Drive read HTTP {resp.status_code}: {resp.text[:240]}"
        payload = resp.json()
        if not payload.get("ok") or not payload.get("data_base64"):
            return None, "", str(payload.get("error") or "Drive archive did not return file bytes.")
        return base64.b64decode(payload["data_base64"]), str(payload.get("mime_type") or ""), ""
    except Exception as exc:
        return None, "", f"Drive archive read failed: {exc}"


def archive_completed_jobs(jobs: list[dict], token: str, progress_callback=None) -> tuple[list[dict], dict]:
    """Archive selected references plus every completed image/video into product/date folders."""
    current = [dict(j) for j in jobs]
    batch_name = drive_batch_name(current)
    batch_date = str(st.session_state.get("batch_created_at") or _utc_now_iso())[:10]
    tasks = []
    for idx, job in enumerate(current):
        selected_refs = list(job.get("selected_refs") or [])
        ref_ids = list(job.get("drive_reference_ids") or [])
        for ref_idx, ref_url in enumerate(selected_refs):
            if ref_idx >= len(ref_ids) or not ref_ids[ref_idx]:
                tasks.append((idx, "reference", ref_idx, ref_url))
        if job.get("image_status") == "completed" and not job.get("drive_image_id"):
            tasks.append((idx, "image", None, None))
        if job.get("video_status") == "completed" and not job.get("drive_video_id"):
            tasks.append((idx, "video", None, None))

    report = {"uploaded": 0, "existing": 0, "failed": 0, "attempted": len(tasks), "references": 0, "images": 0, "videos": 0, "errors": []}
    for n, (idx, kind, ref_idx, ref_url) in enumerate(tasks, 1):
        job = current[idx]
        if progress_callback:
            progress_callback(n - 1, len(tasks), kind, job)

        if kind == "reference":
            try:
                data, mime = fetch_remote_image(str(ref_url))
            except Exception as exc:
                data, mime = None, "image/jpeg"
                error = f"Could not retrieve reference {int(ref_idx or 0)+1} for {job.get('name') or 'product'}: {exc}"
        elif kind == "image":
            data, mime = image_bytes_for_job(job)
            error = f"Could not retrieve image bytes for {job.get('name') or 'product'}."
        else:
            data = download_video_for_job(token, job)
            mime = "video/mp4"
            error = f"Could not retrieve video bytes for {job.get('name') or 'product'}."

        if not data:
            if kind == "reference":
                errors = list(job.get("drive_reference_errors") or [])
                while len(errors) <= int(ref_idx or 0): errors.append("")
                errors[int(ref_idx or 0)] = error
                job["drive_reference_errors"] = errors
            else:
                job[f"drive_{kind}_error"] = error
            report["failed"] += 1
            report["errors"].append(error)
            continue

        if kind == "reference":
            source_tag = hashlib.sha1(str(ref_url).encode("utf-8")).hexdigest()[:10]
            filename = f"ref_{int(ref_idx or 0)+1:02d}_{source_tag}.jpg"
            media_id = source_tag
        else:
            filename = _archive_filename(idx + 1, job, kind)
            media_id = job.get("image_media_id") if kind == "image" else job.get("video_media_id") or ""
        description = f"Flow Try-On Factory | Product: {job.get('name') or ''} | Product URL: {job.get('url') or ''} | Media ID: {media_id}"
        payload, error = archive_bytes_to_google_drive(
            data=data,
            mime_type=mime,
            filename=filename,
            kind=kind,
            batch_name=batch_name,
            description=description,
            product_name=str(job.get("name") or f"Product {idx+1}"),
            batch_date=batch_date,
        )
        if payload:
            if kind == "reference":
                ids = list(job.get("drive_reference_ids") or [])
                urls = list(job.get("drive_reference_urls") or [])
                downloads = list(job.get("drive_reference_download_urls") or [])
                errors = list(job.get("drive_reference_errors") or [])
                while len(ids) <= int(ref_idx or 0): ids.append("")
                while len(urls) <= int(ref_idx or 0): urls.append("")
                while len(downloads) <= int(ref_idx or 0): downloads.append("")
                while len(errors) <= int(ref_idx or 0): errors.append("")
                ids[int(ref_idx or 0)] = str(payload.get("file_id") or "")
                urls[int(ref_idx or 0)] = str(payload.get("view_url") or "")
                downloads[int(ref_idx or 0)] = str(payload.get("download_url") or "")
                errors[int(ref_idx or 0)] = ""
                job["drive_reference_ids"] = ids
                job["drive_reference_urls"] = urls
                job["drive_reference_download_urls"] = downloads
                job["drive_reference_errors"] = errors
                report["references"] += 1
            else:
                job[f"drive_{kind}_id"] = str(payload.get("file_id") or "")
                job[f"drive_{kind}_url"] = str(payload.get("view_url") or payload.get("download_url") or "")
                job[f"drive_{kind}_download_url"] = str(payload.get("download_url") or "")
                job[f"drive_{kind}_error"] = None
                report["images" if kind == "image" else "videos"] += 1
            if payload.get("batch_folder_url"):
                job["drive_batch_folder_url"] = str(payload.get("batch_folder_url"))
            if payload.get("product_folder_url"):
                job["drive_product_folder_url"] = str(payload.get("product_folder_url"))
            if payload.get("existing"):
                report["existing"] += 1
            else:
                report["uploaded"] += 1
        else:
            if kind == "reference":
                errors = list(job.get("drive_reference_errors") or [])
                while len(errors) <= int(ref_idx or 0): errors.append("")
                errors[int(ref_idx or 0)] = error
                job["drive_reference_errors"] = errors
            else:
                job[f"drive_{kind}_error"] = error
            report["failed"] += 1
            report["errors"].append(error)
        if progress_callback:
            progress_callback(n, len(tasks), kind, job)
    return current, report


def _sheet_hyperlink(url: str, label: str) -> str:
    """Return a Google Sheets HYPERLINK formula while safely escaping quotes."""
    url = str(url or "").strip()
    if not url:
        return ""
    safe_url = url.replace('"', '""')
    safe_label = str(label or "Open").replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_label}")'


def _jobs_sheet_rows(jobs: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Same export schema as CSV, but replace long URLs with friendly clickable labels."""
    headers, rows = jobs_export_rows(jobs)
    polished = []
    for row in rows:
        r = list(row)
        # Keep the underlying URL in the hyperlink while showing a short readable label.
        r[2] = _sheet_hyperlink(r[2], "Open product")
        r[10] = _sheet_hyperlink(r[10], "View image")
        r[16] = _sheet_hyperlink(r[16], "View video")
        r[18] = _sheet_hyperlink(r[18], "Open archive")
        polished.append(r)
    return headers, polished


def _format_google_sheet(book, ws, headers: list[str], data_rows: int) -> None:
    """Apply a compact human-friendly layout while preserving hidden technical metadata."""
    sheet_id = ws.id
    total_cols = len(headers)
    total_rows = max(2, data_rows + 1)

    # 0-based widths keyed by column index.
    widths = {
        0: 58,   # Product #
        1: 290,  # Product name
        2: 112,  # Product link
        3: 145,  # Product ID (hidden)
        4: 105,  # Try-on focus
        5: 92,   # Back design
        6: 108,  # Image status
        7: 88,   # Approved
        8: 180,  # Image URL (hidden)
        9: 160,  # Image media ID (hidden)
        10: 108, # Drive image
        11: 155, # Drive image ID (hidden)
        12: 108, # Video status
        13: 180, # Video URL (hidden)
        14: 160, # Video media ID (hidden)
        15: 210, # Video job ID (hidden)
        16: 108, # Drive video
        17: 155, # Drive video ID (hidden)
        18: 118, # Archive folder
        19: 280, # Selected refs (hidden)
        20: 250, # Regeneration instruction
        21: 82,  # Image calls
        22: 82,  # Video calls
        23: 72,  # Retries
        24: 82,  # Failures
        25: 72,  # Sheet row
    }
    hidden_cols = {3, 8, 9, 11, 13, 14, 15, 17, 19}

    requests_payload = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": total_cols},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.075, "green": 0.094, "blue": 0.133},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 10},
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_rows, "startColumnIndex": 0, "endColumnIndex": total_cols},
                "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_rows, "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_rows, "startColumnIndex": 4, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_rows, "startColumnIndex": 12, "endColumnIndex": 13},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 42},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": total_rows},
                "properties": {"pixelSize": 48},
                "fields": "pixelSize",
            }
        },
        {
            "setBasicFilter": {
                "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": total_rows, "startColumnIndex": 0, "endColumnIndex": total_cols}}
            }
        },
    ]

    for idx in range(total_cols):
        requests_payload.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": widths.get(idx, 120), "hiddenByUser": idx in hidden_cols},
                "fields": "pixelSize,hiddenByUser",
            }
        })

    book.batch_update({"requests": requests_payload})


def push_jobs_to_google_sheet(jobs: list[dict], spreadsheet_url: str, worksheet_name: str, mode: str = "Replace tab") -> tuple[bool, str]:
    """Push the current batch to Google Sheets using a service account."""
    info = get_google_service_account_info()
    if not info:
        return False, "Google Sheets is not configured. Add GOOGLE_SERVICE_ACCOUNT_JSON (or [gcp_service_account]) to Streamlit Secrets."
    try:
        import gspread
    except Exception:
        return False, "Google Sheets dependency is missing. Make sure gspread is in requirements.txt and redeploy."

    sheet_ref = str(spreadsheet_url or "").strip()
    if not sheet_ref:
        return False, "Paste a Google Sheet URL first."
    tab_name = (str(worksheet_name or "Flow Try-On").strip() or "Flow Try-On")[:100]
    headers, rows = _jobs_sheet_rows(jobs)
    values = [headers] + rows
    try:
        gc = gspread.service_account_from_dict(info)
        if sheet_ref.startswith("http://") or sheet_ref.startswith("https://"):
            book = gc.open_by_url(sheet_ref)
        else:
            book = gc.open_by_key(sheet_ref)
        try:
            ws = book.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=tab_name, rows=max(100, len(values) + 20), cols=max(20, len(headers) + 2))

        if mode == "Append rows":
            existing = ws.get_all_values()
            if not existing:
                ws.update(range_name="A1", values=[headers])
            start_row = max(len(existing), 1) + 1
            for i, job in enumerate(jobs):
                job["sheet_row"] = start_row + i
            headers, rows = _jobs_sheet_rows(jobs)
            if rows:
                ws.append_rows(rows, value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS")
            final_row_count = max(len(existing), 1) + len(rows)
        else:
            for i, job in enumerate(jobs):
                job["sheet_row"] = i + 2
            headers, rows = _jobs_sheet_rows(jobs)
            values = [headers] + rows
            ws.clear()
            ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")
            final_row_count = len(rows) + 1

        # Formatting is best-effort: a data push should still succeed even if Google rejects a visual setting.
        format_warning = ""
        try:
            _format_google_sheet(book, ws, headers, max(0, final_row_count - 1))
        except Exception as fmt_exc:
            format_warning = f" Data was saved, but Sheet formatting could not be applied: {fmt_exc}"

        service_email = str(info.get("client_email") or "service account")
        return True, f"Pushed {len(rows)} product(s) to '{tab_name}' and formatted it for easier reading. Connected as {service_email}.{format_warning}"
    except Exception as exc:
        return False, f"Google Sheets push failed: {exc}"


def _open_google_book(spreadsheet_url: str):
    info = get_google_service_account_info()
    if not info:
        raise RuntimeError("Google Sheets is not configured.")
    import gspread
    gc = gspread.service_account_from_dict(info)
    ref = str(spreadsheet_url or "").strip()
    return (gc.open_by_url(ref) if ref.startswith(("http://", "https://")) else gc.open_by_key(ref)), gspread


def persist_batch_history_to_google_sheet(jobs: list[dict], spreadsheet_url: str) -> tuple[bool, str]:
    """Upsert a readable batch summary plus compact raw state for future reopening."""
    if not jobs or not spreadsheet_url:
        return False, "No batch or Google Sheet URL."
    batch_id, created_at = ensure_batch_metadata(jobs)
    updated_at = _utc_now_iso()
    usage = batch_usage(jobs)
    estimated_cost, has_cost_rates = usage_cost_estimate(jobs)
    images_ready = sum(1 for j in jobs if j.get("image_status") == "completed")
    approved = sum(1 for j in jobs if j.get("approved"))
    videos_ready = sum(1 for j in jobs if j.get("video_status") == "completed")
    processing = sum(1 for j in jobs if _dashboard_stage(j) == "Processing")
    failed = sum(1 for j in jobs if _dashboard_stage(j) == "Failed")
    folder_url = next((str(j.get("drive_batch_folder_url") or "") for j in jobs if j.get("drive_batch_folder_url")), "")
    try:
        book, gspread = _open_google_book(spreadsheet_url)
        history_headers = ["Batch ID", "Created", "Updated", "Products", "Images ready", "Approved", "Videos ready", "Processing", "Failed", "Image calls", "Video calls", "Retries", "Est. cost", "Drive folder", "Status"]
        try:
            ws = book.worksheet(BATCH_HISTORY_TAB)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=BATCH_HISTORY_TAB, rows=300, cols=len(history_headers) + 2)
        existing = ws.get_all_values()
        ws.update(range_name="A1", values=[history_headers])
        if not existing:
            existing = [history_headers]
        row = [
            batch_id, created_at, updated_at, str(len(jobs)), str(images_ready), str(approved), str(videos_ready), str(processing), str(failed),
            str(usage["image_calls"]), str(usage["video_calls"]), str(usage["retries"]), f"${estimated_cost:,.2f}" if has_cost_rates else "", _sheet_hyperlink(folder_url, "Open Drive") if folder_url else "", _batch_status(jobs),
        ]
        row_index = next((i + 1 for i, r in enumerate(existing[1:], start=1) if r and r[0] == batch_id), None)
        if row_index:
            ws.update(range_name=f"A{row_index}:O{row_index}", values=[row], value_input_option="USER_ENTERED")
        else:
            ws.append_row(row, value_input_option="USER_ENTERED")
            row_index = len(existing) + 1
        st.session_state["batch_history_row"] = row_index
        # Basic readable formatting for the visible history tab.
        try:
            book.batch_update({"requests": [
                {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(history_headers)}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": .075, "green": .094, "blue": .133}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            ]})
        except Exception:
            pass

        # Hidden technical state tab. Rewrite only this compact table so batches survive Streamlit redeploys.
        data_headers = ["Batch ID", "Product #", "Updated", "Job JSON"]
        try:
            data_ws = book.worksheet(BATCH_DATA_TAB)
        except gspread.WorksheetNotFound:
            data_ws = book.add_worksheet(title=BATCH_DATA_TAB, rows=1000, cols=6)
        old = data_ws.get_all_values()
        kept = [r[:4] for r in old[1:] if r and r[0] != batch_id]
        current_rows = []
        for i, job in enumerate(jobs, 1):
            snap = _persistable_job(job)
            snap["_batch_id"] = batch_id
            snap["_batch_created_at"] = created_at
            current_rows.append([batch_id, str(i), updated_at, json.dumps(snap, separators=(",", ":"), default=str)])
        all_values = [data_headers] + kept + current_rows
        data_ws.clear()
        data_ws.update(range_name="A1", values=all_values, value_input_option="RAW")
        try:
            book.batch_update({"requests": [{"updateSheetProperties": {"properties": {"sheetId": data_ws.id, "hidden": True}, "fields": "hidden"}}]})
        except Exception:
            pass
        return True, f"Batch history saved ({batch_id})."
    except Exception as exc:
        return False, f"Batch history sync failed: {exc}"


def load_batch_history(spreadsheet_url: str) -> tuple[list[dict], str]:
    if not spreadsheet_url:
        return [], "Google Sheet URL is not configured."
    try:
        book, gspread = _open_google_book(spreadsheet_url)
        try:
            ws = book.worksheet(BATCH_HISTORY_TAB)
        except gspread.WorksheetNotFound:
            return [], "No saved batches yet."
        values = ws.get_all_values()
        if len(values) < 2:
            return [], "No saved batches yet."
        headers = values[0]
        rows = []
        for raw in values[1:]:
            padded = raw + [""] * max(0, len(headers) - len(raw))
            rows.append(dict(zip(headers, padded)))
        rows.sort(key=lambda r: r.get("Created", ""), reverse=True)
        return rows, ""
    except Exception as exc:
        return [], f"Could not load batch history: {exc}"


def load_batch_jobs(spreadsheet_url: str, batch_id: str) -> tuple[list[dict], dict, str]:
    try:
        book, gspread = _open_google_book(spreadsheet_url)
        try:
            data_ws = book.worksheet(BATCH_DATA_TAB)
        except gspread.WorksheetNotFound:
            return [], {}, "Batch data tab does not exist yet."
        values = data_ws.get_all_values()
        found = []
        for row in values[1:]:
            if len(row) < 4 or row[0] != batch_id:
                continue
            try:
                job = json.loads(row[3])
                job["_product_index"] = int(row[1] or 0)
                drive_refs = [x for x in (job.get("drive_reference_download_urls") or []) if x]
                if drive_refs:
                    job["selected_refs"] = drive_refs
                    job["listing_images"] = drive_refs
                    job["review_images"] = []
                else:
                    refs = list(job.get("selected_refs") or [])
                    job["listing_images"] = refs
                    job["review_images"] = []
                if job.get("drive_image_download_url"):
                    job["image_url"] = job.get("drive_image_download_url")
                if job.get("drive_video_download_url"):
                    job["video_url"] = job.get("drive_video_download_url")
                job.pop("image_encoded", None)
                found.append(job)
            except Exception:
                continue
        found.sort(key=lambda j: int(j.pop("_product_index", 0)))
        if not found:
            return [], {}, f"No saved product state found for {batch_id}."
        created = str(found[0].get("_batch_created_at") or "")
        return found, {"batch_id": batch_id, "created_at": created}, ""
    except Exception as exc:
        return [], {}, f"Could not reopen batch: {exc}"


def maybe_sync_batch(jobs: list[dict], spreadsheet_url: str, *, sync_current_tab: bool = True, force: bool = False) -> tuple[bool, str]:
    """Auto-sync only when meaningful batch state changed, avoiding duplicate API traffic."""
    if not jobs or not spreadsheet_url or not get_google_service_account_info():
        return False, ""
    ensure_batch_metadata(jobs)
    fingerprint = _batch_sync_fingerprint(jobs)
    if not force and fingerprint == st.session_state.get("last_batch_sync_fingerprint"):
        return True, ""
    messages = []
    ok_current = True
    if sync_current_tab:
        ok_current, msg = push_jobs_to_google_sheet(jobs, spreadsheet_url, "Flow Try-On", "Replace tab")
        if msg: messages.append(msg)
    ok_history, hist_msg = persist_batch_history_to_google_sheet(jobs, spreadsheet_url)
    if hist_msg: messages.append(hist_msg)
    if ok_current and ok_history:
        st.session_state["last_batch_sync_fingerprint"] = _batch_sync_fingerprint(jobs)
        st.session_state["last_batch_sync_at"] = _utc_now_iso()
        st.session_state.pop("batch_sync_error", None)
        st.session_state.pop("batch_history_cache", None)
        return True, " ".join(messages)
    error = " ".join(messages)
    st.session_state["batch_sync_error"] = error
    return False, error


def render_batch_history(spreadsheet_url: str) -> None:
    st.markdown("<div class='panel-title'>Batch history</div>", unsafe_allow_html=True)
    st.markdown("<div class='panel-sub'>Persistent history lives in your connected Google Sheet, so Streamlit redeploys do not erase prior batches.</div>", unsafe_allow_html=True)
    if not spreadsheet_url:
        st.info("Set GOOGLE_SHEET_URL to enable permanent batch history.")
        return
    c1, c2 = st.columns([1, 3])
    if c1.button("↻ Refresh history", use_container_width=True, key="refresh_batch_history"):
        st.session_state.pop("batch_history_cache", None)
    if "batch_history_cache" not in st.session_state:
        history, error = load_batch_history(spreadsheet_url)
        st.session_state["batch_history_cache"] = history
        st.session_state["batch_history_error"] = error
    history = st.session_state.get("batch_history_cache") or []
    error = st.session_state.get("batch_history_error") or ""
    if error and not history:
        st.info(error)
        return
    if not history:
        st.info("No saved batches yet. Your current batch will appear here automatically after its first sync.")
        return
    table = []
    for row in history[:100]:
        table.append({
            "Created": row.get("Created", ""), "Products": row.get("Products", ""), "Images": row.get("Images ready", ""),
            "Approved": row.get("Approved", ""), "Videos": row.get("Videos ready", ""), "Processing": row.get("Processing", ""),
            "Failed": row.get("Failed", ""), "Retries": row.get("Retries", ""), "Est. cost": row.get("Est. cost", ""), "Status": row.get("Status", ""), "Batch ID": row.get("Batch ID", ""),
        })
    st.dataframe(table, use_container_width=True, hide_index=True)
    options = [r.get("Batch ID", "") for r in history if r.get("Batch ID")]
    chosen = c2.selectbox("Saved batch", options=options, format_func=lambda bid: next((f"{r.get('Created','')} · {r.get('Products','0')} products · {r.get('Status','')}" for r in history if r.get('Batch ID') == bid), bid), key="history_batch_choice")
    selected = next((r for r in history if r.get("Batch ID") == chosen), {})
    h1, h2 = st.columns(2)
    if h1.button("Open selected batch", type="primary", use_container_width=True, key="open_history_batch"):
        loaded, meta, load_error = load_batch_jobs(spreadsheet_url, chosen)
        if load_error:
            st.error(load_error)
        else:
            st.session_state["jobs"] = loaded
            ensure_batch_metadata(loaded, force_new=True, batch_id=meta.get("batch_id", chosen), created_at=meta.get("created_at", ""))
            st.session_state.pop("avatar_flow_id", None)
            st.session_state.pop("full_batch_zip", None)
            st.session_state.pop("videos_zip", None)
            st.success("Batch reopened.")
            st.rerun()
    drive_formula = str(selected.get("Drive folder") or "")
    # History cells may return either a formula or displayed label via gspread. Prefer a folder URL from loaded state when possible.
    if h2.button("Show batch details", use_container_width=True, key="show_history_details"):
        st.session_state["show_history_details_for"] = chosen
    if st.session_state.get("show_history_details_for") == chosen:
        st.json(selected)



def avatar_library():
    roots = [Path("avatars"), Path(__file__).parent / "avatars"]
    records = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.iterdir()):
            if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            key = str(p.resolve())
            if key in seen: continue
            seen.add(key)
            records.append(p)
    return records


def reset_generated(job: dict) -> dict:
    job = dict(job)
    for key in ["flow_product_ref_ids", "ref_signature", "image_status", "image_job_id", "image_media_id", "image_url", "image_encoded", "image_seed", "image_error", "approved", "video_status", "video_job_id", "video_submitted_at", "video_url", "video_media_id", "video_error", "thumbnail_url", "drive_image_id", "drive_image_url", "drive_image_download_url", "drive_image_error", "drive_video_id", "drive_video_url", "drive_video_download_url", "drive_video_error", "drive_batch_folder_url", "drive_product_folder_url", "drive_reference_ids", "drive_reference_urls", "drive_reference_download_urls", "drive_reference_errors"]:
        job.pop(key, None)
    job.update({"image_status": "pending", "approved": False, "video_status": "pending"})
    return job


def _short_title(text: str, limit: int = 72) -> str:
    text = str(text or "Product").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _status_label(status: str) -> str:
    status = str(status or "pending").lower()
    labels = {
        "completed": "Complete",
        "failed": "Failed",
        "pending": "Pending",
        "created": "Queued",
        "processing": "Processing",
        "running": "Processing",
    }
    return labels.get(status, status.replace("_", " ").title())


def render_job_editor(job: dict, index: int, social_token: str = "", region: str = "US") -> dict:
    """Focused product editor. Put the visual reference gallery first so it is immediately visible."""
    updated = dict(job)
    with st.container(border=True):
        st.markdown(f"<div class='product-title'>{index+1}. {_short_title(job.get('name'), 110)}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='product-id'>Product ID · {job.get('id','—')}</div>", unsafe_allow_html=True)
        st.write("")

        # Gallery first: this is the main task on the Products screen.
        st.markdown("<div class='panel-title'>Choose clothing references</div>", unsafe_allow_html=True)
        st.markdown("<div class='panel-sub'>Select up to 5. Your avatar is always sent separately as reference #1.</div>", unsafe_allow_html=True)

        candidates = [("Listing", _normalize_remote_url(u)) for u in job.get("listing_images", [])[:12]] + [("Review", _normalize_remote_url(u)) for u in job.get("review_images", [])[:8]]
        candidates = [(k, u) for k, u in candidates if u]

        info_c1, info_c2 = st.columns([1, 1])
        with info_c1:
            st.caption(f"SociaVault image URLs found: {len(candidates)}")
        with info_c2:
            if st.button("↻ Refresh product images", key=f"refresh_refs_{job['id']}", use_container_width=True, disabled=not bool(social_token)):
                try:
                    fresh = import_product(job.get("url", ""), social_token, region)
                    updated["listing_images"] = fresh.get("listing_images", [])
                    updated["review_images"] = fresh.get("review_images", [])
                    fresh_refs = dedupe(updated["listing_images"][:2] + updated["review_images"][:1])[:MAX_PRODUCT_REFS]
                    updated["selected_refs"] = fresh_refs
                    updated = reset_generated(updated)
                    st.session_state["refresh_job_refs"] = {"id": job.get("id"), "listing_images": updated["listing_images"], "review_images": updated["review_images"], "selected_refs": fresh_refs}
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not refresh product images: {exc}")

        selected = []
        failed_previews = 0
        if not candidates:
            st.error("SociaVault returned the product title, but no usable product image URLs for this item. Use Refresh product images above.")
        else:
            cols = st.columns(4, gap="small")
            for n, (kind, url) in enumerate(candidates):
                with cols[n % 4]:
                    preview_ok = False
                    try:
                        thumb, _ = fetch_remote_image(url)
                        st.image(thumb, use_container_width=True)
                        preview_ok = True
                    except Exception as exc:
                        failed_previews += 1
                        # Browser fallback bypasses Pillow/Streamlit decoding and handles AVIF in modern browsers.
                        st.markdown(_browser_image_html(url, f"{kind} reference {n+1}"), unsafe_allow_html=True)
                        st.caption(f"Browser fallback · {type(exc).__name__}")
                    checked = st.checkbox(
                        f"{kind} {n+1}",
                        value=url in (job.get("selected_refs") or []),
                        key=f"ref_{job['id']}_{hashlib.sha1(url.encode()).hexdigest()[:8]}",
                    )
                    if checked:
                        selected.append(url)
            if failed_previews:
                st.caption(f"{failed_previews} preview(s) needed browser fallback. They can still be selected and used.")

        if len(selected) > MAX_PRODUCT_REFS:
            st.warning(f"Only the first {MAX_PRODUCT_REFS} checked references will be used.")
            selected = selected[:MAX_PRODUCT_REFS]

        if selected != (job.get("selected_refs") or []):
            updated["selected_refs"] = selected
            updated = reset_generated(updated)

        st.caption(f"{len(updated.get('selected_refs') or [])}/{MAX_PRODUCT_REFS} references selected")
        st.divider()

        c1, c2 = st.columns([1.55, 1], gap="large")
        with c1:
            updated["name"] = st.text_input("Product name", value=job.get("name", ""), key=f"name_{job['id']}")
        with c2:
            focus_options = ["shirt", "pants", "outfit", "shoes"]
            current_focus = job.get("focus", "outfit") if job.get("focus") in focus_options else "outfit"
            updated["focus"] = st.selectbox(
                "Try-on emphasis",
                focus_options,
                index=focus_options.index(current_focus),
                key=f"focus_{job['id']}",
            )
        updated["back_design"] = st.checkbox(
            "Important back design / graphic",
            value=bool(job.get("back_design")),
            key=f"back_{job['id']}",
        )
    return updated


def render_job_result(job: dict, index: int, token: str, email: str, avatar_id: str, scene: str) -> dict:
    updated = dict(job)
    with st.container(border=True):
        head1, head2 = st.columns([3, 1])
        with head1:
            st.markdown(f"<div class='product-title'>{index+1}. {_short_title(job.get('name'), 115)}</div>", unsafe_allow_html=True)
            st.markdown(
                f"<div class='product-id'>Focus · {job.get('focus','outfit')} &nbsp;&nbsp;•&nbsp;&nbsp; Image · {_status_label(job.get('image_status'))} &nbsp;&nbsp;•&nbsp;&nbsp; Video · {_status_label(job.get('video_status'))}</div>",
                unsafe_allow_html=True,
            )
        with head2:
            if job.get("approved"):
                st.success("✓ Approved")
            elif job.get("image_status") == "completed":
                st.info("Needs review")

        st.write("")
        img_col, vid_col = st.columns(2, gap="large")
        with img_col:
            st.markdown("<div class='panel-title'>Try-on image</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Nano Banana 2 · portrait 9:16</div>", unsafe_allow_html=True)
            image_bytes = image_bytes_for_job(job)[0] if job.get("image_status") == "completed" else None
            if image_bytes:
                st.image(image_bytes, use_container_width=True)
            elif job.get("image_status") == "failed":
                st.error(job.get("image_error") or "Image generation failed")
            else:
                st.info("Generate the try-on image to preview it here.")

            if job.get("image_status") == "completed":
                approved_now = st.checkbox(
                    "Approved for video",
                    value=bool(job.get("approved")),
                    key=f"approve_{job['id']}",
                )
                updated["approved"] = approved_now
                regen_instruction = st.text_input(
                    "Regeneration instruction (optional)",
                    value=str(job.get("regen_instruction") or ""),
                    placeholder="e.g. Make the shirt looser, keep the logo larger, show the full pants",
                    key=f"regen_note_{job['id']}",
                )
                updated["regen_instruction"] = regen_instruction
                if st.button("↻ Regenerate image", key=f"regen_img_{job['id']}", use_container_width=True):
                    updated["last_regen_instruction"] = regen_instruction
                    updated["active_regen_instruction"] = regen_instruction
                    with st.spinner("Regenerating try-on image with your instruction..."):
                        updated = generate_one_image(updated, token, email, avatar_id, scene)
                    st.session_state["jobs"][index] = updated
                    st.rerun()

        with vid_col:
            st.markdown("<div class='panel-title'>Motion result</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Omni 1.1 Flash · 8 sec · 720p</div>", unsafe_allow_html=True)
            if job.get("video_status") == "completed":
                # Never auto-download the MP4 while rendering the page. That was
                # the source of the long "Opening…"/frozen experience.
                media_id = job.get("video_media_id") or ""
                video_url = job.get("video_url") or job.get("drive_video_download_url") or ""
                cache_key = f"video_bytes_{job['id']}"
                cached_data = st.session_state.get(cache_key)

                # Resolve a playable signed URL once when Results is opened. This is
                # lightweight (metadata only) and does not download the MP4 bytes.
                auto_key = f"video_autoresolve_{job['id']}"
                if not video_url and not cached_data and media_id and not st.session_state.get(auto_key):
                    st.session_state[auto_key] = True
                    resolved, _message = resolve_video_url(token, media_id)
                    if resolved:
                        video_url = resolved
                        updated["video_url"] = resolved
                        job["video_url"] = resolved
                        st.session_state["jobs"][index] = updated

                if video_url:
                    st.video(video_url)
                    st.link_button("↗ Open / download original MP4", video_url, use_container_width=True)
                if job.get("drive_video_url"):
                    st.link_button("☁ Open permanent Drive video", job.get("drive_video_url"), use_container_width=True)
                elif cached_data:
                    st.video(cached_data, format="video/mp4")
                else:
                    st.success("Video generation completed.")
                    st.caption("The MP4 exists in Flow. Use Refresh preview or Prepare download below if the signed preview link is still being prepared.")

                a1, a2 = st.columns(2)
                if a1.button(
                    "↻ Refresh preview",
                    key=f"resolve_vid_{job['id']}",
                    use_container_width=True,
                    disabled=not bool(media_id),
                ):
                    with st.spinner("Getting video link…"):
                        resolved, message = resolve_video_url(token, media_id)
                    if resolved:
                        job["video_url"] = resolved
                        st.session_state["jobs"][index] = job
                        st.rerun()
                    else:
                        st.warning(message or "Video link is not ready yet.")

                if a2.button(
                    "↓ Prepare download",
                    key=f"prepare_vid_{job['id']}",
                    use_container_width=True,
                    disabled=not bool(media_id or job.get("drive_video_id") or video_url),
                ):
                    with st.spinner("Fetching MP4 from Flow or the permanent Drive archive…"):
                        data = download_video_for_job(token, job)
                    if data:
                        st.session_state[cache_key] = data
                        st.rerun()
                    else:
                        st.warning("MP4 could not be retrieved yet.")

                cached_data = st.session_state.get(cache_key)
                if cached_data:
                    st.download_button(
                        "↓ Download MP4",
                        data=cached_data,
                        file_name=f"{safe_name(job.get('name'))}.mp4",
                        mime="video/mp4",
                        key=f"dl_vid_{job['id']}",
                        use_container_width=True,
                    )
            elif job.get("video_status") == "failed":
                st.error(job.get("video_error") or "Video generation failed")
            elif job.get("video_job_id"):
                st.info(f"Omni job: {_status_label(job.get('video_status'))}")
            else:
                st.info("Approve the image, then generate its video.")

            b1, b2 = st.columns(2)
            active_video = bool(job.get("video_job_id")) and str(job.get("video_status") or "").lower() not in {"failed"}
            if b1.button(
                "▶ Generate video",
                key=f"gen_vid_{job['id']}",
                use_container_width=True,
                disabled=(not bool(job.get("image_media_id"))) or active_video,
            ):
                with st.spinner("Submitting Omni 1.1 Flash…"):
                    updated = submit_one_video(updated, token, email)
                st.session_state["jobs"][index] = updated
                remember_video_job_ids(st.session_state.get("jobs") or [])
                if updated.get("video_job_id") and updated.get("video_status") != "failed":
                    st.toast("Video queued. Status will update automatically.")
                st.rerun()
            if b2.button(
                "↻ Check status",
                key=f"refresh_vid_{job['id']}",
                use_container_width=True,
                disabled=not bool(job.get("video_job_id")),
            ):
                with st.spinner("Checking Google Flow and loading the MP4 if it is ready…"):
                    updated = refresh_one_video(updated, token)
                st.session_state["jobs"][index] = updated
                st.rerun()
    return updated


def _video_media_from_job(payload: dict) -> tuple[str, str, str]:
    response = payload.get("response") or {}
    media = response.get("media") or []
    if not media:
        return "", "", ""
    item = media[0] or {}
    return (
        str(item.get("mediaGenerationId") or ""),
        str(item.get("videoUrl") or ""),
        str(item.get("thumbnailUrl") or ""),
    )


def render_flow_recovery(token: str, expanded: bool = False) -> None:
    """Visible recovery UI for Flow jobs independent of the current product batch."""
    with st.expander("Recover existing Flow videos", expanded=expanded):
        st.markdown("<div class='panel-title'>Recover Flow jobs</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='panel-sub'>Recent discovery covers roughly the last 15 minutes. If you already have a job ID, direct lookup works for jobs retained by useapi (currently up to 7 days).</div>",
            unsafe_allow_html=True,
        )

        q_ids = []
        try:
            raw_q = st.query_params.get("flow_jobs", "")
            if isinstance(raw_q, list):
                raw_q = raw_q[-1] if raw_q else ""
            q_ids = [x.strip() for x in str(raw_q).split(",") if x.strip()]
        except Exception:
            pass

        if q_ids and not st.session_state.get("recovery_job_ids"):
            st.session_state["recovery_job_ids"] = q_ids

        c1, c2 = st.columns([1, 2])
        if c1.button("Find recent Flow jobs", use_container_width=True, key="find_recent_flow_jobs"):
            try:
                overview = flow_jobs_overview(token, "history")
                ids = []
                for kind in ("videos", "images"):
                    group = overview.get(kind) or {}
                    for bucket in ("executing", "history"):
                        for jid in (group.get(bucket) or {}).keys():
                            if jid not in ids:
                                ids.append(jid)
                st.session_state["recovery_job_ids"] = ids[:20]
                st.session_state["recovery_overview"] = overview
                st.success(f"Found {len(ids)} recent/executing Flow job(s).")
            except Exception as exc:
                st.error(f"Could not load recent Flow jobs: {exc}")

        with c2:
            manual = st.text_input(
                "Job ID lookup",
                value="",
                placeholder="Paste a Flow job ID here (j...)",
                key="manual_recovery_job_id",
            )
        if st.button("Add job ID", use_container_width=True, disabled=not bool(manual.strip()), key="add_recovery_job_id"):
            ids = list(st.session_state.get("recovery_job_ids") or [])
            jid = manual.strip()
            if jid not in ids:
                ids.insert(0, jid)
            st.session_state["recovery_job_ids"] = ids[:20]
            st.rerun()

        ids = list(st.session_state.get("recovery_job_ids") or [])
        if not ids:
            st.info("No saved job IDs in this browser yet. Use Find recent Flow jobs or paste a job ID above.")
            return

        if st.button("Check / refresh recovered jobs", type="primary", use_container_width=True, key="refresh_recovered_jobs"):
            payloads = dict(st.session_state.get("recovered_flow_jobs") or {})
            errors = {}
            bar = st.progress(0, text="Checking Flow jobs…")
            for n, jid in enumerate(ids, 1):
                try:
                    payloads[jid] = flow_get_job(token, jid)
                except Exception as exc:
                    errors[jid] = str(exc)
                bar.progress(n / max(1, len(ids)), text=f"Checked {n}/{len(ids)}")
            st.session_state["recovered_flow_jobs"] = payloads
            st.session_state["recovered_flow_errors"] = errors
            st.rerun()

        payloads = st.session_state.get("recovered_flow_jobs") or {}
        errors = st.session_state.get("recovered_flow_errors") or {}
        rows = []
        for jid in ids:
            p = payloads.get(jid) or {}
            rows.append({
                "Type": p.get("type") or "—",
                "Status": _status_label(p.get("status") or ("error" if jid in errors else "not checked")),
                "Job ID": jid,
                "Updated": p.get("updated") or p.get("created") or "—",
            })
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)

        video_ids = [jid for jid in ids if (payloads.get(jid) or {}).get("type") == "video"]
        if not video_ids:
            return

        chosen = st.selectbox(
            "Recovered video",
            options=video_ids,
            format_func=lambda jid: f"{_status_label((payloads.get(jid) or {}).get('status'))} · {jid[:34]}…",
            key="recovered_video_choice",
        )
        payload = payloads.get(chosen) or {}
        status = str(payload.get("status") or "unknown").lower()
        media_id, video_url, _ = _video_media_from_job(payload)
        request = payload.get("request") or {}
        prompt = str(request.get("prompt") or "")

        with st.container(border=True):
            st.markdown(f"<div class='product-title'>Recovered Omni video · {_status_label(status)}</div>", unsafe_allow_html=True)
            if prompt:
                st.caption(_short_title(prompt, 180))
            st.code(chosen, language=None)

            cache_url_key = f"recovery_url_{hashlib.sha1(chosen.encode()).hexdigest()[:12]}"
            cache_bytes_key = f"recovery_bytes_{hashlib.sha1(chosen.encode()).hexdigest()[:12]}"
            cached_url = st.session_state.get(cache_url_key) or video_url
            cached_bytes = st.session_state.get(cache_bytes_key)

            if status == "completed":
                if cached_url:
                    st.video(cached_url)
                    st.link_button("Open original video", cached_url, use_container_width=True)
                elif cached_bytes:
                    st.video(cached_bytes, format="video/mp4")
                else:
                    st.success("Video completed. Load its playable link or MP4 below.")

                x1, x2 = st.columns(2)
                if x1.button("Get playable link", use_container_width=True, disabled=not bool(media_id), key=f"recover_link_{chosen}"):
                    url, message = resolve_video_url(token, media_id)
                    if url:
                        st.session_state[cache_url_key] = url
                        st.rerun()
                    st.warning(message or "The signed video link is not ready yet.")
                if x2.button("Prepare MP4", use_container_width=True, disabled=not bool(media_id), key=f"recover_mp4_{chosen}"):
                    data, message = download_video_raw(token, media_id)
                    if data:
                        st.session_state[cache_bytes_key] = data
                        st.rerun()
                    st.warning(message or "The MP4 is not ready yet.")

                cached_bytes = st.session_state.get(cache_bytes_key)
                if cached_bytes:
                    st.download_button(
                        "Download recovered MP4",
                        data=cached_bytes,
                        file_name=f"recovered_{safe_name(chosen[:28])}.mp4",
                        mime="video/mp4",
                        use_container_width=True,
                        key=f"recover_dl_{chosen}",
                    )
            elif status == "failed":
                st.error(str(payload.get("error") or payload.get("errorDetails") or "Video job failed."))
            else:
                st.info("This job is still queued/processing. Press Check / refresh recovered jobs above to update it.")


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🪞", layout="wide", initial_sidebar_state="expanded")
    inject_css()
    password_gate()

    token = get_secret("USEAPI_TOKEN")
    social_token = get_secret("SOCIAVAULT_API_KEY")
    flow_email = get_secret("GOOGLE_FLOW_EMAIL")
    region = get_secret("SOCIAVAULT_REGION", "US") or "US"
    default_google_sheet_url = get_secret("GOOGLE_SHEET_URL")
    drive_archive_cfg = get_drive_archive_config()

    # ---------------- Sidebar: setup only ----------------
    avatar_bytes = None
    avatar_mime = "image/jpeg"
    avatar_label = "Avatar"
    library = avatar_library()

    with st.sidebar:
        st.markdown(
            "<div class='sidebar-brand'><div class='logo'>🪞 Flow Try-On Factory</div><div class='sub'>Bulk clothing try-ons → motion</div></div>",
            unsafe_allow_html=True,
        )
        st.markdown("<div class='section-label'>CONNECTIONS</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='connection-row'><strong><span class='{'dot-ok' if token else 'dot-warn'}'></span>Google Flow</strong><span>{'Ready' if token else 'Missing key'}</span></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div class='connection-row'><strong><span class='{'dot-ok' if social_token else 'dot-warn'}'></span>SociaVault</strong><span>{'Ready' if social_token else 'Missing key'}</span></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div class='connection-row'><strong><span class='{'dot-ok' if drive_archive_cfg['configured'] else 'dot-warn'}'></span>Drive archive</strong><span>{'Ready' if drive_archive_cfg['configured'] else 'Not set up'}</span></div>",
            unsafe_allow_html=True,
        )
        if flow_email:
            st.caption(f"Flow account · {flow_email}")

        if token and st.button("Test Flow connection", use_container_width=True):
            try:
                accounts = flow_accounts(token)
                st.success(f"Flow reachable · {len(accounts)} account(s)")
                if len(accounts) > 1 and not flow_email:
                    st.warning("Set GOOGLE_FLOW_EMAIL when using multiple Flow accounts.")
            except Exception as exc:
                st.error(str(exc))

        st.markdown("<div class='section-label'>AVATAR</div>", unsafe_allow_html=True)
        source_choices = ["Upload image"] + (["Saved avatar"] if library else [])
        avatar_source = st.radio("Avatar source", source_choices, horizontal=True, label_visibility="collapsed")
        if avatar_source == "Saved avatar":
            options = {p.stem.replace("_", " ").title(): p for p in library}
            label = st.selectbox("Saved avatar", list(options.keys()), label_visibility="collapsed")
            path = options[label]
            avatar_bytes = path.read_bytes()
            avatar_mime = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
            avatar_label = label
            st.image(avatar_bytes, use_container_width=True)
            st.caption(label)
        else:
            avatar_file = st.file_uploader("Avatar reference", type=["jpg", "jpeg", "png", "webp"], label_visibility="collapsed")
            if avatar_file:
                avatar_bytes = avatar_file.getvalue()
                avatar_mime = avatar_file.type or "image/jpeg"
                avatar_label = Path(avatar_file.name).stem
                st.image(avatar_bytes, use_container_width=True)
                st.caption("Reference #1 for every product")
            else:
                st.caption("Upload the person used across this batch.")

        st.markdown("<div class='section-label'>BATCH SETTINGS</div>", unsafe_allow_html=True)
        run_mode = st.radio("Pipeline", ["Review images first", "Full auto"])
        scene = st.selectbox("Mirror setting", list(SCENES.keys()), index=0)
        st.caption("Nano Banana 2 · 9:16\n\nOmni 1.1 Flash · 8s · 720p")
        auto_archive = st.checkbox(
            "Auto-archive completed media to Drive",
            value=bool(drive_archive_cfg.get("auto")),
            disabled=not bool(drive_archive_cfg.get("configured")),
            help="Uploads selected product references plus each completed try-on/video to your permanent Google Drive archive.",
        )
        auto_sheet_sync = st.checkbox(
            "Auto-sync production tracker",
            value=bool(default_google_sheet_url),
            disabled=not bool(default_google_sheet_url and get_google_service_account_info()),
            help="Keeps the Flow Try-On tab and permanent Batch History updated when statuses, approvals, Drive links, or generation usage change.",
        )
        if st.session_state.get("last_batch_sync_at"):
            st.caption(f"Last Sheet sync · {st.session_state.get('last_batch_sync_at')}")
        if st.session_state.get("batch_sync_error"):
            st.caption("Sheet sync needs attention — open Results → Google Sheets to retry.")
        with st.expander("Usage cost rates", expanded=False):
            try:
                default_img_rate = float(get_secret("FLOW_IMAGE_COST_USD", "0") or 0)
                default_vid_rate = float(get_secret("FLOW_VIDEO_COST_USD", "0") or 0)
            except Exception:
                default_img_rate = default_vid_rate = 0.0
            st.session_state["image_cost_rate"] = st.number_input("Image cost per call ($)", min_value=0.0, value=float(st.session_state.get("image_cost_rate", default_img_rate)), step=0.01, format="%.4f")
            st.session_state["video_cost_rate"] = st.number_input("Video cost per call ($)", min_value=0.0, value=float(st.session_state.get("video_cost_rate", default_vid_rate)), step=0.01, format="%.4f")
            st.caption("Optional. Set your current provider rates here; usage counts are tracked automatically.")

        if st.session_state.get("jobs"):
            st.divider()
            if st.button("Clear current batch", use_container_width=True):
                st.session_state.pop("jobs", None)
                st.session_state.pop("videos_zip", None)
                st.session_state.pop("full_batch_zip", None)
                st.session_state.pop("batch_id", None)
                st.session_state.pop("batch_created_at", None)
                st.session_state.pop("last_batch_sync_fingerprint", None)
                st.rerun()

    avatar_hash = hashlib.sha1(avatar_bytes).hexdigest() if avatar_bytes else ""
    if avatar_hash and st.session_state.get("avatar_hash") != avatar_hash:
        st.session_state["avatar_hash"] = avatar_hash
        # Upload the new avatar on the next generation request, but do not erase already completed batch history/media.
        st.session_state.pop("avatar_flow_id", None)

    # ---------------- Main header ----------------
    st.markdown(
        """
        <div class='app-header'>
          <div class='app-kicker'>AI TRY-ON WORKSPACE</div>
          <div class='app-title'>Create product try-ons without the clutter.</div>
          <p class='app-subtitle'>Import TikTok Shop clothing, choose the exact reference photos, generate the stills, approve what you like, then turn them into short motion clips.</p>
          <div class='top-pills'>
            <span class='top-pill'>SociaVault import</span>
            <span class='top-pill'>Nano Banana 2</span>
            <span class='top-pill'>Omni 1.1 Flash</span>
            <span class='top-pill'>Up to 10 products</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not token or not social_token:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Finish connecting the app</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>The interface is ready, but generation is locked until both API secrets are configured.</div>", unsafe_allow_html=True)
            if not token:
                st.error("Missing USEAPI_TOKEN")
            if not social_token:
                st.error("Missing SOCIAVAULT_API_KEY")
        return

    # Recovery must stay visible even when there is no imported product batch.
    render_flow_recovery(token, expanded=not bool(st.session_state.get("jobs")))

    # ---------------- Import / replace batch ----------------
    jobs = st.session_state.get("jobs") or []
    with st.expander("Import products" if not jobs else "Import or replace batch", expanded=not bool(jobs)):
        st.markdown("<div class='panel-title'>TikTok Shop products</div>", unsafe_allow_html=True)
        st.markdown("<div class='panel-sub'>Paste one product URL per line. The first 10 valid links are used.</div>", unsafe_allow_html=True)
        raw_links = st.text_area(
            "TikTok Shop links",
            height=145,
            placeholder="https://www.tiktok.com/shop/pdp/...\nhttps://www.tiktok.com/shop/pdp/...",
            label_visibility="collapsed",
        )
        raw_count = len([x for x in raw_links.splitlines() if x.strip()])
        links = dedupe([x.strip() for x in raw_links.splitlines() if x.strip() and not x.strip().startswith("#")])[:MAX_LINKS]
        if raw_count > MAX_LINKS:
            st.warning(f"Only the first {MAX_LINKS} links will be processed.")

        if st.button("Import products with SociaVault", type="primary", use_container_width=True, disabled=not bool(links)):
            imported = []
            errors = []
            progress = st.progress(0, text="Importing products...")
            with ThreadPoolExecutor(max_workers=min(IMPORT_WORKERS, len(links))) as ex:
                future_map = {ex.submit(import_product, link, social_token, region): link for link in links}
                done = 0
                for fut in as_completed(future_map):
                    link = future_map[fut]
                    try:
                        imported.append(fut.result())
                    except Exception as exc:
                        errors.append((link, str(exc)))
                    done += 1
                    progress.progress(done / len(links), text=f"Imported {done}/{len(links)}")
            by_url = {j["url"]: j for j in imported}
            new_jobs = [by_url[x] for x in links if x in by_url]
            ensure_batch_metadata(new_jobs, force_new=True)
            if auto_archive and drive_archive_cfg.get("configured") and new_jobs:
                with st.spinner("Archiving selected product references to Drive…"):
                    new_jobs, _ = archive_completed_jobs(new_jobs, token)
            st.session_state["jobs"] = new_jobs
            st.session_state.pop("batch_history_cache", None)
            st.session_state.pop("videos_zip", None)
            st.session_state.pop("full_batch_zip", None)
            if errors:
                st.session_state["import_errors"] = errors
            st.rerun()

    if st.session_state.get("import_errors"):
        for link, error in st.session_state.pop("import_errors"):
            st.warning(f"Could not import {_short_title(link, 65)} — {error}")

    jobs = st.session_state.get("jobs") or []
    refreshed_refs = st.session_state.pop("refresh_job_refs", None)
    if refreshed_refs and jobs:
        for i, item in enumerate(jobs):
            if str(item.get("id")) == str(refreshed_refs.get("id")):
                item = dict(item)
                item["listing_images"] = refreshed_refs.get("listing_images", [])
                item["review_images"] = refreshed_refs.get("review_images", [])
                item["selected_refs"] = refreshed_refs.get("selected_refs", [])
                jobs[i] = reset_generated(item)
                break
        st.session_state["jobs"] = jobs
    if not jobs:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Start with your products</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Import a new batch above or reopen a saved batch from permanent history below.</div>", unsafe_allow_html=True)
            st.info("No products imported yet.")
        st.write("")
        with st.container(border=True):
            render_batch_history(default_google_sheet_url)
        return

    ensure_batch_metadata(jobs)
    # On the rerun after any generation/status change, persist the new state automatically.
    maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync)

    completed_images = sum(1 for j in jobs if j.get("image_status") == "completed")
    approved = sum(1 for j in jobs if j.get("approved"))
    completed_videos = sum(1 for j in jobs if j.get("video_status") == "completed")

    tabs = st.tabs(["Products", "Generate", "Results", "History"])

    # ---------------- Products tab ----------------
    with tabs[0]:
        st.write("")
        top1, top2 = st.columns([3, 1])
        with top1:
            st.markdown("<div class='panel-title'>Product references</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Edit one product at a time instead of opening giant accordions.</div>", unsafe_allow_html=True)
        with top2:
            st.metric("Imported", len(jobs))

        selected_index = st.selectbox(
            "Product to edit",
            options=list(range(len(jobs))),
            format_func=lambda i: f"{i+1}. {_short_title(jobs[i].get('name'), 82)}",
            key="product_editor_index",
        )
        edited = render_job_editor(jobs[selected_index], selected_index, social_token, region)
        jobs[selected_index] = edited
        st.session_state["jobs"] = jobs

        st.write("")
        st.markdown("<div class='panel-title'>Batch overview</div>", unsafe_allow_html=True)
        rows = []
        for i, job in enumerate(jobs, 1):
            rows.append({
                "#": i,
                "Product": _short_title(job.get("name"), 58),
                "Focus": job.get("focus", "outfit"),
                "Refs": len(job.get("selected_refs") or []),
                "Image": _status_label(job.get("image_status")),
                "Video": _status_label(job.get("video_status")),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync)

    # ---------------- Generate tab ----------------
    with tabs[1]:
        st.write("")
        st.markdown("<div class='panel-title'>Batch generation</div>", unsafe_allow_html=True)
        st.markdown("<div class='panel-sub'>Run the still-image stage, approve images, then generate motion. Full auto can do both stages in one pass.</div>", unsafe_allow_html=True)

        needs_approval = sum(1 for j in jobs if _dashboard_stage(j) == "Needs approval")
        processing_count = sum(1 for j in jobs if _dashboard_stage(j) == "Processing")
        failed_count = sum(1 for j in jobs if _dashboard_stage(j) == "Failed")
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Products", len(jobs))
        m2.metric("Images ready", f"{completed_images}/{len(jobs)}")
        m3.metric("Needs approval", needs_approval)
        m4.metric("Videos ready", f"{completed_videos}/{len(jobs)}")
        m5.metric("Processing", processing_count)
        m6.metric("Failed", failed_count)
        usage = batch_usage(jobs)
        estimated_cost, has_cost_rates = usage_cost_estimate(jobs)
        u1, u2, u3, u4, u5 = st.columns(5)
        u1.metric("Image calls", usage["image_calls"])
        u2.metric("Video calls", usage["video_calls"])
        u3.metric("Retries", usage["retries"])
        u4.metric("Generation failures", usage["failures"])
        u5.metric("Est. cost", f"${estimated_cost:,.2f}" if has_cost_rates else "—")

        if not avatar_bytes:
            st.warning("Add an avatar in the left sidebar before generating.")

        with st.container(border=True):
            st.markdown("<div class='panel-title'>Actions</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Only actions that are currently available are enabled.</div>", unsafe_allow_html=True)
            a1, a2, a3 = st.columns(3)
            generate_images = a1.button(
                "Generate all images",
                type="primary",
                use_container_width=True,
                disabled=not bool(avatar_bytes),
            )
            run_full = a2.button(
                "Run full batch",
                use_container_width=True,
                disabled=not bool(avatar_bytes),
            )
            refresh_all = a3.button(
                "Check all video statuses",
                use_container_width=True,
                disabled=not any(j.get("video_job_id") for j in jobs),
            )

            b1, b2 = st.columns(2)
            approve_all = b1.button(
                "Approve all completed images",
                use_container_width=True,
                disabled=not any(j.get("image_status") == "completed" and not j.get("approved") for j in jobs),
            )
            generate_approved = b2.button(
                "Generate videos for approved",
                use_container_width=True,
                disabled=not any(
                    j.get("approved") and j.get("image_media_id")
                    and str(j.get("video_status") or "pending").lower() not in {"created", "started", "completed"}
                    for j in jobs
                ),
            )
            failed_generation = any(str(j.get("image_status") or "").lower() == "failed" or str(j.get("video_status") or "").lower() == "failed" for j in jobs)
            failed_images_need_avatar = any(str(j.get("image_status") or "").lower() == "failed" for j in jobs)
            retry_failed = st.button(
                f"↻ Retry failed only ({failed_count})",
                use_container_width=True,
                disabled=not failed_generation or (failed_images_need_avatar and not bool(avatar_bytes)),
                help="Retries only failed image generations and failed Omni video submissions. Completed products are untouched.",
            )

        if retry_failed:
            if any(str(j.get("image_status") or "").lower() == "failed" for j in jobs):
                if not st.session_state.get("avatar_flow_id"):
                    with st.spinner("Uploading avatar for failed-image retries…"):
                        st.session_state["avatar_flow_id"] = flow_upload_asset(token, avatar_bytes, avatar_mime, flow_email)
                avatar_id = st.session_state["avatar_flow_id"]
                failed_image_indices = [i for i, j in enumerate(jobs) if str(j.get("image_status") or "").lower() == "failed"]
                progress = st.progress(0, text="Retrying failed images only…")
                with ThreadPoolExecutor(max_workers=min(IMAGE_WORKERS, len(failed_image_indices) or 1)) as ex:
                    futures = {ex.submit(generate_one_image, jobs[i], token, flow_email, avatar_id, scene): i for i in failed_image_indices}
                    done = 0
                    for fut in as_completed(futures):
                        idx = futures[fut]
                        jobs[idx] = fut.result()
                        if run_mode == "Full auto" and jobs[idx].get("image_status") == "completed":
                            jobs[idx]["approved"] = True
                        done += 1
                        progress.progress(done / max(1, len(failed_image_indices)), text=f"Retried images {done}/{len(failed_image_indices)}")
            video_retry_indices = [
                i for i, j in enumerate(jobs)
                if str(j.get("video_status") or "").lower() == "failed"
                and j.get("image_status") == "completed" and (j.get("approved") or run_mode == "Full auto")
            ]
            # Full-auto image retries can become new video submissions immediately.
            if run_mode == "Full auto":
                for i, j in enumerate(jobs):
                    if j.get("image_status") == "completed" and j.get("approved") and str(j.get("video_status") or "pending").lower() == "pending" and i not in video_retry_indices:
                        video_retry_indices.append(i)
            submitted = []
            for idx in video_retry_indices:
                jobs[idx] = submit_one_video(jobs[idx], token, flow_email)
                if jobs[idx].get("video_job_id") and jobs[idx].get("video_status") != "failed":
                    submitted.append(idx)
            if auto_archive and drive_archive_cfg.get("configured"):
                jobs, _ = archive_completed_jobs(jobs, token)
            st.session_state["jobs"] = jobs
            remember_video_job_ids(jobs)
            maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync, force=True)
            st.session_state["retry_notice"] = f"Retried failed items only · {len(video_retry_indices)} video submission(s)."
            st.rerun()

        if generate_images or run_full:
            if not st.session_state.get("avatar_flow_id"):
                with st.spinner("Uploading avatar to Google Flow as reference #1..."):
                    try:
                        st.session_state["avatar_flow_id"] = flow_upload_asset(token, avatar_bytes, avatar_mime, flow_email)
                    except Exception as exc:
                        st.error(f"Avatar upload failed: {exc}")
                        st.stop()
            avatar_id = st.session_state["avatar_flow_id"]
            indices = [i for i, j in enumerate(jobs) if j.get("selected_refs")]
            progress = st.progress(0, text="Generating Nano Banana 2 images...")
            updates = {}
            with ThreadPoolExecutor(max_workers=min(IMAGE_WORKERS, len(indices) or 1)) as ex:
                futures = {ex.submit(generate_one_image, jobs[i], token, flow_email, avatar_id, scene): i for i in indices}
                done = 0
                for fut in as_completed(futures):
                    idx = futures[fut]
                    updates[idx] = fut.result()
                    done += 1
                    progress.progress(done / max(1, len(indices)), text=f"Images {done}/{len(indices)}")
            for idx, update in updates.items():
                jobs[idx] = update
            if auto_archive and drive_archive_cfg.get("configured"):
                jobs, _ = archive_completed_jobs(jobs, token)
            st.session_state["jobs"] = jobs
            maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync, force=True)

            if run_full or run_mode == "Full auto":
                submitted = []
                for i, job in enumerate(jobs):
                    if job.get("image_status") == "completed":
                        job["approved"] = True
                        jobs[i] = submit_one_video(job, token, flow_email)
                        if jobs[i].get("video_job_id") and jobs[i].get("video_status") != "failed":
                            submitted.append(i)
                st.session_state["jobs"] = jobs
                remember_video_job_ids(jobs)
                if submitted:
                    st.session_state["video_batch_notice"] = f"Queued {len(submitted)} Omni 1.1 video job(s). They will update automatically."
            st.rerun()

        if refresh_all:
            targets = [i for i, j in enumerate(jobs) if j.get("video_job_id")]
            bar = st.progress(0, text="Refreshing Omni 1.1 jobs...")
            for n, idx in enumerate(targets, 1):
                jobs[idx] = refresh_one_video(jobs[idx], token)
                bar.progress(n / len(targets), text=f"Refreshed {n}/{len(targets)}")
            st.session_state["jobs"] = jobs
            st.rerun()

        if approve_all:
            for job in jobs:
                if job.get("image_status") == "completed":
                    job["approved"] = True
            st.session_state["jobs"] = jobs
            st.rerun()

        if generate_approved:
            submitted = []
            for i, job in enumerate(jobs):
                status = str(job.get("video_status") or "pending").lower()
                if job.get("approved") and job.get("image_media_id") and status not in {"created", "started", "completed"}:
                    jobs[i] = submit_one_video(job, token, flow_email)
                    if jobs[i].get("video_job_id") and jobs[i].get("video_status") != "failed":
                        submitted.append(i)
            st.session_state["jobs"] = jobs
            remember_video_job_ids(jobs)
            if submitted:
                st.session_state["video_batch_notice"] = f"Queued {len(submitted)} Omni 1.1 video job(s). They will update automatically."
            st.rerun()

        if st.session_state.pop("video_batch_notice", None):
            st.success("Videos queued. You can keep using the app while they generate — this panel checks them automatically.")
        if st.session_state.pop("retry_notice", None):
            st.success("Failed-only retry completed/submitted. Completed products were left untouched.")

        # Non-blocking live monitor. Streamlit reruns only this small fragment every 12s
        # while any Omni job is active, instead of freezing the whole page for up to 10 minutes.
        _has_pending_video = any(
            j.get("video_job_id") and str(j.get("video_status") or "").lower() not in {"completed", "failed"}
            for j in st.session_state.get("jobs", [])
        )
        _video_poll_every = 12 if _has_pending_video else None

        @st.fragment(run_every=_video_poll_every)
        def _live_omni_monitor():
            current = [dict(j) for j in st.session_state.get("jobs", [])]
            pending = [
                i for i, j in enumerate(current)
                if j.get("video_job_id") and str(j.get("video_status") or "").lower() not in {"completed", "failed"}
            ]
            if not pending:
                return

            for idx in pending:
                current[idx] = refresh_one_video(current[idx], token)
            if auto_archive and drive_archive_cfg.get("configured"):
                newly_complete = any(
                    (j.get("image_status") == "completed" and not j.get("drive_image_id"))
                    or (j.get("video_status") == "completed" and not j.get("drive_video_id"))
                    for j in current
                )
                if newly_complete:
                    current, _archive_report = archive_completed_jobs(current, token)
            st.session_state["jobs"] = current
            maybe_sync_batch(current, default_google_sheet_url, sync_current_tab=auto_sheet_sync)

            active_rows = []
            now = time.time()
            for idx, j in enumerate(current, 1):
                if not j.get("video_job_id"):
                    continue
                submitted_at = j.get("video_submitted_at")
                elapsed = int(now - submitted_at) if submitted_at else None
                active_rows.append({
                    "#": idx,
                    "Product": _short_title(j.get("name"), 46),
                    "Omni status": _status_label(j.get("video_status")),
                    "Elapsed": f"{elapsed}s" if elapsed is not None else "—",
                    "Error": j.get("video_error") or "",
                })

            ready = sum(1 for j in current if str(j.get("video_status") or "").lower() == "completed")
            failed = sum(1 for j in current if str(j.get("video_status") or "").lower() == "failed")
            still = sum(1 for j in current if j.get("video_job_id") and str(j.get("video_status") or "").lower() not in {"completed", "failed"})

            st.markdown("<div class='panel-title'>Live Omni 1.1 status</div>", unsafe_allow_html=True)
            st.caption(f"{ready} ready · {still} processing/queued · {failed} failed · auto-checks every 12 seconds")
            if active_rows:
                st.dataframe(active_rows, use_container_width=True, hide_index=True)

            if still == 0:
                st.rerun()

        _live_omni_monitor()

        st.markdown("<div class='panel-title'>Production dashboard</div>", unsafe_allow_html=True)
        dashboard_filter = st.selectbox("Show", ["All", "Ready", "Processing", "Failed", "Needs approval", "Ready for video", "Pending"], key="dashboard_filter")
        status_rows = []
        for i, job in enumerate(st.session_state.get("jobs") or jobs, 1):
            stage = _dashboard_stage(job)
            if dashboard_filter != "All" and stage != dashboard_filter:
                continue
            image_calls, video_calls, retries, failures = _job_usage(job)
            status_rows.append({
                "#": i,
                "Product": _short_title(job.get("name"), 48),
                "Stage": stage,
                "Image": _status_label(job.get("image_status")),
                "Approved": "Yes" if job.get("approved") else "No",
                "Video": _status_label(job.get("video_status")),
                "Drive": "Archived" if job.get("drive_image_id") and (job.get("video_status") != "completed" or job.get("drive_video_id")) else "Pending",
                "Calls": f"{image_calls} img / {video_calls} vid",
                "Retries": retries,
                "Error": job.get("video_error") or job.get("image_error") or "",
            })
        st.dataframe(status_rows, use_container_width=True, hide_index=True)

    # ---------------- Results tab ----------------
    with tabs[2]:
        st.write("")
        r1, r2 = st.columns([3, 1])
        with r1:
            st.markdown("<div class='panel-title'>Review results</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Jump between products without scrolling through a long wall of results.</div>", unsafe_allow_html=True)
        with r2:
            st.metric("Ready videos", completed_videos)

        result_index = st.selectbox(
            "Product result",
            options=list(range(len(jobs))),
            format_func=lambda i: f"{i+1}. {_short_title(jobs[i].get('name'), 82)}",
            key="result_product_index",
        )
        avatar_id = st.session_state.get("avatar_flow_id", "")
        updated = render_job_result(jobs[result_index], result_index, token, flow_email, avatar_id, scene)
        jobs[result_index] = updated
        if auto_archive and drive_archive_cfg.get("configured") and (
            (updated.get("image_status") == "completed" and not updated.get("drive_image_id"))
            or (updated.get("video_status") == "completed" and not updated.get("drive_video_id"))
        ):
            jobs, _ = archive_completed_jobs(jobs, token)
        st.session_state["jobs"] = jobs
        maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync)

        st.write("")
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Export batch</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Download the product list, individual media packages, or one ZIP containing the full finished batch.</div>", unsafe_allow_html=True)

            csv_bytes = jobs_to_csv(jobs)
            e1, e2, e3 = st.columns(3)
            e1.download_button(
                "↓ Download product CSV",
                data=csv_bytes,
                file_name="flow_tryon_products.csv",
                mime="text/csv",
                use_container_width=True,
                help="Includes each original TikTok Shop product link plus generation IDs/statuses.",
            )
            e2.download_button(
                "↓ Download manifest JSON",
                data=jobs_to_manifest(jobs),
                file_name="flow_tryon_batch.json",
                mime="application/json",
                use_container_width=True,
            )
            images_zip = build_images_zip(jobs)
            if images_zip:
                e3.download_button("↓ Download all images", data=images_zip, file_name="flow_tryon_images.zip", mime="application/zip", use_container_width=True)
            else:
                e3.button("↓ Download all images", disabled=True, use_container_width=True)

            st.divider()
            st.markdown("<div class='panel-title'>Download entire batch</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Creates one ZIP with batch.csv, manifest.json, completed try-on images, and completed MP4 videos.</div>", unsafe_allow_html=True)
            b1, b2 = st.columns([1, 2])
            if b1.button("Prepare full batch ZIP", type="primary", use_container_width=True):
                with st.spinner("Collecting completed images and MP4 videos from Flow…"):
                    payload, counts = build_full_batch_zip(jobs, token)
                if payload:
                    st.session_state["full_batch_zip"] = payload
                    st.session_state["full_batch_zip_counts"] = counts
                    st.rerun()
            if st.session_state.get("full_batch_zip"):
                counts = st.session_state.get("full_batch_zip_counts") or {}
                b2.download_button(
                    f"↓ Download entire batch · {counts.get('images', 0)} images · {counts.get('videos', 0)} videos",
                    data=st.session_state["full_batch_zip"],
                    file_name="flow_tryon_full_batch.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

            st.divider()
            st.markdown("<div class='panel-title'>Permanent Google Drive archive</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Copies completed try-on images and MP4s into your own Google Drive so they do not depend on expiring Flow/CDN links. Existing filenames are reused instead of duplicated.</div>", unsafe_allow_html=True)
            archived_refs = sum(len([x for x in (j.get("drive_reference_ids") or []) if x]) for j in jobs)
            total_refs = sum(len(j.get("selected_refs") or []) for j in jobs)
            archived_images = sum(1 for j in jobs if j.get("drive_image_id"))
            archived_videos = sum(1 for j in jobs if j.get("drive_video_id"))
            completed_media = sum(1 for j in jobs if j.get("image_status") == "completed") + sum(1 for j in jobs if j.get("video_status") == "completed")
            archivable_total = total_refs + completed_media
            archived_media = archived_refs + archived_images + archived_videos
            d1, d2, d3, d4 = st.columns([1, 1, 1, 2])
            d1.metric("References", f"{archived_refs}/{total_refs}")
            d2.metric("Drive images", archived_images)
            d3.metric("Drive videos", archived_videos)
            folder_url = next((j.get("drive_product_folder_url") or j.get("drive_batch_folder_url") for j in jobs if j.get("drive_product_folder_url") or j.get("drive_batch_folder_url")), "")
            if folder_url:
                d4.link_button("Open first product folder in Google Drive", folder_url, use_container_width=True)
            elif drive_archive_cfg.get("configured"):
                d4.caption(f"{archived_media}/{archivable_total} reference/generated files archived")
            else:
                d4.caption("Drive archive is not configured yet.")

            if st.button(
                "☁ Archive references + completed media now",
                type="primary",
                use_container_width=True,
                disabled=not bool(drive_archive_cfg.get("configured")) or archivable_total == 0,
            ):
                bar = st.progress(0, text="Preparing Google Drive archive…")
                def _archive_progress(done, total, kind, job):
                    if total:
                        bar.progress(done / total, text=f"Archiving {done}/{total} · {kind} · {_short_title(job.get('name'), 40)}")
                with st.spinner("Copying completed media into your Google Drive…"):
                    jobs, archive_report = archive_completed_jobs(jobs, token, _archive_progress)
                    st.session_state["jobs"] = jobs
                    sheet_message = ""
                    if default_google_sheet_url:
                        ok_sheet, sheet_message = maybe_sync_batch(jobs, default_google_sheet_url, sync_current_tab=auto_sheet_sync, force=True)
                if archive_report.get("failed"):
                    st.warning(f"Drive archive: {archive_report.get('uploaded', 0)} uploaded, {archive_report.get('existing', 0)} already existed, {archive_report.get('failed', 0)} failed. " + (archive_report.get("errors") or [""])[0])
                else:
                    st.success(f"Drive archive ready · {archive_report.get('references', 0)} reference(s), {archive_report.get('images', 0)} try-on(s), {archive_report.get('videos', 0)} video(s) processed · {archive_report.get('uploaded', 0)} new file(s), {archive_report.get('existing', 0)} already archived." + (" Google Sheet/history updated." if default_google_sheet_url else ""))
                st.rerun()

            st.divider()
            st.markdown("<div class='panel-title'>Google Sheets</div>", unsafe_allow_html=True)
            st.markdown("<div class='panel-sub'>Push a clean, formatted batch view to Google Sheets. Headers are frozen, filters are added, long URLs become clickable labels, and technical IDs stay available but hidden by default.</div>", unsafe_allow_html=True)
            gs1, gs2 = st.columns([2, 1])
            sheet_url = gs1.text_input(
                "Google Sheet URL",
                value=default_google_sheet_url,
                placeholder="https://docs.google.com/spreadsheets/d/...",
                key="google_sheet_url",
            )
            tab_name = gs2.text_input("Worksheet/tab", value="Flow Try-On", key="google_sheet_tab")
            gs3, gs4 = st.columns([1, 2])
            push_mode = gs3.selectbox("Push mode", ["Replace tab", "Append rows"], key="google_sheet_mode")
            service_info = get_google_service_account_info()
            if service_info and service_info.get("client_email"):
                gs4.caption(f"Share the Sheet with: {service_info.get('client_email')} · Editor")
            else:
                gs4.caption("Google Sheets not configured yet. Add a service-account credential in Streamlit Secrets.")

            if st.button("↗ Push batch to Google Sheet", use_container_width=True, disabled=not bool(sheet_url)):
                with st.spinner("Updating Google Sheet…"):
                    ok, message = push_jobs_to_google_sheet(jobs, sheet_url, tab_name, push_mode)
                    if ok:
                        persist_batch_history_to_google_sheet(jobs, sheet_url)
                        st.session_state.pop("batch_history_cache", None)
                if ok:
                    st.success(message + " Batch History updated.")
                else:
                    st.error(message)

    # ---------------- History tab ----------------
    with tabs[3]:
        st.write("")
        render_batch_history(default_google_sheet_url)



if __name__ == "__main__":
    main()
