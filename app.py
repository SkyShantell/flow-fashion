import base64
import hashlib
import io
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def inject_css():
    st.markdown(
        """
        <style>
        .stApp { background: radial-gradient(circle at top left, #111a31 0%, #090e1b 38%, #070b14 100%); color:#f5f7fb; }
        .block-container { max-width: 1500px; padding-top: 1.4rem; padding-bottom: 5rem; }
        .hero { border:1px solid rgba(126,157,255,.22); border-radius:26px; padding:28px 30px; background:linear-gradient(135deg,rgba(31,43,72,.78),rgba(10,15,28,.86)); box-shadow:0 22px 70px rgba(0,0,0,.28); margin-bottom:18px; }
        .hero h1 { margin:0; font-size:2.25rem; letter-spacing:-.04em; }
        .hero p { margin:.65rem 0 0; color:#aab5ca; font-size:1.02rem; }
        .pill { display:inline-block; padding:7px 11px; border-radius:999px; margin:8px 7px 0 0; background:rgba(68,103,255,.13); border:1px solid rgba(116,145,255,.24); color:#cbd6ff; font-size:.82rem; }
        div[data-testid="stVerticalBlockBorderWrapper"] { border-color:rgba(133,157,220,.20)!important; background:rgba(16,22,37,.68); border-radius:22px; }
        div.stButton > button, div.stDownloadButton > button { min-height:48px; border-radius:16px; font-weight:700; }
        div.stButton > button[kind="primary"] { background:linear-gradient(90deg,#3a8cff,#7559ff); border:0; }
        .small-muted { color:#8490a6; font-size:.87rem; }
        .status-ok { color:#68d391; font-weight:700; }
        .status-warn { color:#f6c453; font-weight:700; }
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
    return request_json("GET", f"{FLOW_BASE}/jobs/{quote(job_id, safe='')}", headers=flow_headers(token), timeout=60, retries=1)


def flow_resolve_asset_url(token: str, media_id: str) -> str:
    if not media_id:
        return ""
    try:
        payload = request_json("GET", f"{FLOW_BASE}/assets/{quote(media_id, safe='')}", headers=flow_headers(token), timeout=60, retries=0)
        return str(payload.get("url") or "")
    except Exception:
        return ""


def parse_video_job(payload: dict, token: str) -> dict:
    status = str(payload.get("status") or "unknown").lower()
    result = {"status": status}
    if status == "failed":
        result["error"] = str(payload.get("error") or (payload.get("response") or {}).get("error") or "Video generation failed.")
        return result
    response = payload.get("response") or {}
    media = response.get("media") or []
    if media:
        item = media[0] or {}
        media_id = item.get("mediaGenerationId")
        video_url = item.get("videoUrl")
        if status == "completed" and not video_url and media_id:
            video_url = flow_resolve_asset_url(token, media_id)
        result.update({
            "video_url": video_url,
            "video_media_id": media_id,
            "thumbnail_url": item.get("thumbnailUrl"),
        })
    return result


def download_url(url: str, timeout: int = 90) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content, (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0]


def download_video(token: str, job: dict) -> bytes | None:
    url = job.get("video_url")
    if url:
        try:
            data, _ = download_url(url, 120)
            if data:
                return data
        except Exception:
            pass
    media_id = job.get("video_media_id")
    if media_id:
        try:
            resp = requests.get(
                f"{FLOW_BASE}/assets/{quote(media_id, safe='')}",
                params={"raw": "true"},
                headers=flow_headers(token),
                timeout=180,
            )
            if resp.status_code < 400 and resp.content:
                return resp.content
        except Exception:
            pass
    return None


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


def _sv_values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _sv_first_url(value) -> str:
    if isinstance(value, str):
        return value.strip() if value.startswith(("http://", "https://")) else ""
    if not isinstance(value, dict):
        return ""
    for key in ("url_list", "urlList", "urls", "review_images", "reviewImages"):
        for candidate in _sv_values(value.get(key)):
            url = _sv_first_url(candidate)
            if url:
                return url
    for key in ("url", "image_url", "imageUrl", "display_image_url", "displayImageUrl", "original_url", "originalUrl", "preview_url", "previewUrl"):
        url = _sv_first_url(value.get(key))
        if url:
            return url
    for key in ("thumb_url_list", "thumbUrlList", "thumbnail_url", "thumbnailUrl"):
        url = _sv_first_url(value.get(key))
        if url:
            return url
    return ""


def _sv_collect_urls(value, max_depth=7):
    urls = []
    def add(url):
        url = str(url or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    def walk(node, depth=0, path=()):
        if depth > max_depth:
            return
        if isinstance(node, str):
            p = " ".join(path).lower()
            if node.startswith(("http://", "https://")) and not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(node)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1, path)
        elif isinstance(node, dict):
            best = _sv_first_url(node)
            p = " ".join(path).lower()
            if best and not any(x in p for x in ("avatar", "profile", "seller", "shop_logo", "icon")):
                add(best)
            for key, child in node.items():
                key_l = str(key).lower()
                if key_l in {"url_list", "urllist", "thumb_url_list", "thumburllist"} and best:
                    continue
                walk(child, depth + 1, path + (key_l,))
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
    listing = []
    for obj in _sv_values(product.get("images")):
        u = _sv_first_url(obj)
        if u:
            listing.append(u)
    if not listing:
        listing = _sv_collect_urls(product)[:18]
    listing = dedupe(listing)[:18]

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
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    shoes = ("shoe", "sneaker", "boot", "heel", "sandal", "loafer", "clog", "slipper", "slides")
    bottoms = ("pants", "pant", "jeans", "jean", "shorts", "short", "leggings", "legging", "jogger", "trouser", "skirt", "cargo")
    outfits = ("set", "outfit", "tracksuit", "suit", "dress", "jumpsuit", "romper", "two piece", "2 piece", "matching")
    tops = ("shirt", "t shirt", "tee", "hoodie", "sweater", "jacket", "coat", "blouse", "top", "tank", "cardigan", "jersey", "polo")
    if any(k in text for k in shoes): return "shoes"
    if any(k in text for k in outfits): return "outfit"
    if any(k in text for k in bottoms): return "pants"
    if any(k in text for k in tops): return "shirt"
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
    return re.sub(r"\s+", " ", f"""
Create one photorealistic vertical 9:16 iPhone mirror-selfie image for a TikTok clothing try-on.
REFERENCE RULES: @reference_1 is the exact PERSON/AVATAR. Preserve this person's identity, face, skin, hair, body build and visible personal features. Do not copy the avatar's original clothes. {ref_mentions} are CLOTHING/PRODUCT references for {product}. Ignore any people/models appearing in those clothing references and use only the actual product design, colors, materials, print, construction and fit cues.
Dress @reference_1 in the exact product shown by the clothing references. {focus_rule} {fallback} {back}
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


def fetch_remote_image(url: str) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
    resp.raise_for_status()
    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
    return normalize_image_bytes(resp.content, mime)


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
            "video_url": None,
            "video_media_id": None,
            "video_error": None,
        })
    except Exception as exc:
        updated["image_status"] = "failed"
        updated["image_error"] = str(exc)
    return updated


def submit_one_video(job: dict, token: str, email: str) -> dict:
    updated = dict(job)
    if not updated.get("image_media_id"):
        updated["video_status"] = "failed"
        updated["video_error"] = "No completed image media ID."
        return updated
    try:
        result = flow_submit_video(token, email, updated["image_media_id"], video_prompt(updated))
        updated.update({
            "video_job_id": result["job_id"],
            "video_status": result.get("status") or "created",
            "video_url": None,
            "video_media_id": None,
            "video_error": None,
        })
    except Exception as exc:
        updated["video_status"] = "failed"
        updated["video_error"] = str(exc)
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


def safe_name(text: str, fallback: str = "product") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return (text[:80] or fallback)


def jobs_to_manifest(jobs: list[dict]) -> str:
    clean = []
    for j in jobs:
        clean.append({k: v for k, v in j.items() if k not in {"image_encoded"}})
    return json.dumps(clean, indent=2)


def build_images_zip(jobs: list[dict]) -> bytes | None:
    out = io.BytesIO()
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, job in enumerate(jobs, 1):
            if job.get("image_status") != "completed":
                continue
            data = None
            if job.get("image_encoded"):
                try: data = base64.b64decode(job["image_encoded"])
                except Exception: data = None
            if not data and job.get("image_url"):
                try: data = download_url(job["image_url"], 90)[0]
                except Exception: data = None
            if data:
                z.writestr(f"{i:02d}_{safe_name(job.get('name'))}.jpg", data)
                added += 1
    return out.getvalue() if added else None


def build_videos_zip(jobs: list[dict], token: str) -> bytes | None:
    out = io.BytesIO()
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, job in enumerate(jobs, 1):
            if job.get("video_status") != "completed":
                continue
            data = download_video(token, job)
            if data:
                z.writestr(f"{i:02d}_{safe_name(job.get('name'))}.mp4", data)
                added += 1
    return out.getvalue() if added else None


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
    for key in ["flow_product_ref_ids", "ref_signature", "image_status", "image_job_id", "image_media_id", "image_url", "image_encoded", "image_seed", "image_error", "approved", "video_status", "video_job_id", "video_url", "video_media_id", "video_error", "thumbnail_url"]:
        job.pop(key, None)
    job.update({"image_status": "pending", "approved": False, "video_status": "pending"})
    return job


def render_job_editor(job: dict, index: int) -> dict:
    updated = dict(job)
    with st.expander(f"{index+1}. {job.get('name','Unknown Product')}", expanded=False):
        updated["name"] = st.text_input("Product name", value=job.get("name", ""), key=f"name_{job['id']}")
        focus_options = ["shirt", "pants", "outfit", "shoes"]
        updated["focus"] = st.selectbox("What should the try-on emphasize?", focus_options, index=focus_options.index(job.get("focus", "outfit")) if job.get("focus") in focus_options else 2, key=f"focus_{job['id']}")
        updated["back_design"] = st.checkbox("Important back design / graphic", value=bool(job.get("back_design")), key=f"back_{job['id']}")
        st.caption("Select up to 5 clothing references. The avatar is added separately as reference #1.")
        candidates = [("Listing", u) for u in job.get("listing_images", [])[:10]] + [("Review", u) for u in job.get("review_images", [])[:8]]
        selected = []
        cols = st.columns(4)
        for n, (kind, url) in enumerate(candidates):
            with cols[n % 4]:
                st.image(url, use_container_width=True)
                checked = st.checkbox(f"{kind} {n+1}", value=url in (job.get("selected_refs") or []), key=f"ref_{job['id']}_{hashlib.sha1(url.encode()).hexdigest()[:8]}")
                if checked and len(selected) < MAX_PRODUCT_REFS:
                    selected.append(url)
        if selected != (job.get("selected_refs") or []):
            updated["selected_refs"] = selected
            updated = reset_generated(updated)
        st.caption(f"Selected {len(updated.get('selected_refs') or [])} clothing reference(s).")
    return updated


def render_job_result(job: dict, index: int, token: str, email: str, avatar_id: str, scene: str) -> dict:
    updated = dict(job)
    st.markdown(f"#### {index+1}. {job.get('name','Product')}")
    meta1, meta2, meta3 = st.columns(3)
    meta1.caption(f"Focus: **{job.get('focus','outfit')}**")
    meta2.caption(f"Image: **{job.get('image_status','pending')}**")
    meta3.caption(f"Video: **{job.get('video_status','pending')}**")
    img_col, vid_col = st.columns(2, gap="large")
    with img_col:
        image_bytes = image_bytes_from_result({"encoded": job.get("image_encoded"), "url": job.get("image_url")}) if job.get("image_status") == "completed" else None
        if image_bytes:
            st.image(image_bytes, caption="Nano Banana 2", width=360)
        elif job.get("image_status") == "failed":
            st.error(job.get("image_error") or "Image failed")
        else:
            st.info("Image not generated yet.")
        if job.get("image_status") == "completed":
            updated["approved"] = st.checkbox("Approve image for video", value=bool(job.get("approved")), key=f"approve_{job['id']}")
            if st.button("🔁 Regenerate image", key=f"regen_img_{job['id']}", use_container_width=True):
                with st.spinner("Regenerating Nano Banana 2 image..."):
                    updated = generate_one_image(updated, token, email, avatar_id, scene)
                st.session_state["jobs"][index] = updated
                st.rerun()
    with vid_col:
        if job.get("video_status") == "completed":
            data = download_video(token, job)
            if data:
                st.video(data)
                st.download_button("⬇️ Download video", data=data, file_name=f"{safe_name(job.get('name'))}.mp4", mime="video/mp4", key=f"dl_vid_{job['id']}", use_container_width=True)
            elif job.get("video_url"):
                st.video(job["video_url"])
            else:
                st.warning("Video completed but the file could not be fetched yet. Refresh the result.")
        elif job.get("video_status") == "failed":
            st.error(job.get("video_error") or "Video failed")
        elif job.get("video_job_id"):
            st.info(f"Omni 1.1: {job.get('video_status','processing')}")
        else:
            st.info("Video not submitted yet.")
        b1, b2 = st.columns(2)
        if b1.button("🎬 Generate video", key=f"gen_vid_{job['id']}", use_container_width=True, disabled=not bool(job.get("image_media_id"))):
            with st.spinner("Submitting Omni 1.1 Flash..."):
                updated = submit_one_video(updated, token, email)
            st.session_state["jobs"][index] = updated
            st.rerun()
        if b2.button("🔄 Refresh video", key=f"refresh_vid_{job['id']}", use_container_width=True, disabled=not bool(job.get("video_job_id"))):
            with st.spinner("Refreshing video result..."):
                updated = refresh_one_video(updated, token)
            st.session_state["jobs"][index] = updated
            st.rerun()
    st.divider()
    return updated


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🪞", layout="wide")
    inject_css()
    password_gate()

    token = get_secret("USEAPI_TOKEN")
    social_token = get_secret("SOCIAVAULT_API_KEY")
    flow_email = get_secret("GOOGLE_FLOW_EMAIL")
    region = get_secret("SOCIAVAULT_REGION", "US") or "US"

    st.markdown(
        f"""<div class='hero'><div class='small-muted'>STANDALONE BULK TRY-ON PIPELINE</div><h1>🪞 {APP_NAME}</h1><p>Paste 5–10 TikTok Shop links. SociaVault imports the products, Google Flow Nano Banana 2 creates the try-on stills, and Omni 1.1 Flash turns them into 8-second 720p videos.</p><span class='pill'>Nano Banana 2 · 9:16</span><span class='pill'>Omni 1.1 Flash · 720p · 8s</span><span class='pill'>Avatar always reference #1</span></div>""",
        unsafe_allow_html=True,
    )

    with st.expander("Connections", expanded=not bool(token and social_token)):
        c1, c2, c3 = st.columns(3)
        c1.success("useapi connected") if token else c1.error("Add USEAPI_TOKEN")
        c2.success("SociaVault connected") if social_token else c2.error("Add SOCIAVAULT_API_KEY")
        c3.info(f"Flow account: {flow_email}") if flow_email else c3.info("Flow account: auto (best with one configured account)")
        if token and st.button("Test Flow connection"):
            try:
                accounts = flow_accounts(token)
                st.success(f"Flow API reachable · {len(accounts)} configured account(s)")
                if len(accounts) > 1 and not flow_email:
                    st.warning("You have multiple Flow accounts. Set GOOGLE_FLOW_EMAIL so the avatar and every clothing reference are guaranteed to land on the same account.")
            except Exception as exc:
                st.error(str(exc))

    if not token or not social_token:
        st.stop()

    st.markdown("## 1 · Choose the avatar")
    avatar_bytes = None
    avatar_mime = "image/jpeg"
    avatar_label = "Avatar"
    library = avatar_library()
    source_choices = ["Upload image"] + (["Saved avatar"] if library else [])
    avatar_source = st.radio("Avatar source", source_choices, horizontal=True)
    if avatar_source == "Saved avatar":
        options = {p.stem.replace("_", " ").title(): p for p in library}
        label = st.selectbox("Saved avatar", list(options.keys()))
        path = options[label]
        avatar_bytes = path.read_bytes()
        avatar_mime = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
        avatar_label = label
        st.image(avatar_bytes, width=280, caption=label)
    else:
        avatar_file = st.file_uploader("Avatar reference image", type=["jpg", "jpeg", "png", "webp"])
        if avatar_file:
            avatar_bytes = avatar_file.getvalue()
            avatar_mime = avatar_file.type or "image/jpeg"
            avatar_label = Path(avatar_file.name).stem
            st.image(avatar_bytes, width=280, caption="This person will be reference #1 for every product")

    avatar_hash = hashlib.sha1(avatar_bytes).hexdigest() if avatar_bytes else ""
    if avatar_hash and st.session_state.get("avatar_hash") != avatar_hash:
        st.session_state["avatar_hash"] = avatar_hash
        st.session_state.pop("avatar_flow_id", None)
        # Changing avatar invalidates every generated result.
        if st.session_state.get("jobs"):
            st.session_state["jobs"] = [reset_generated(j) for j in st.session_state["jobs"]]

    st.markdown("## 2 · Import 5–10 products")
    raw_links = st.text_area("One TikTok Shop link per line", height=150, placeholder="https://www.tiktok.com/view/product/...\nhttps://www.tiktok.com/view/product/...")
    links = dedupe([x.strip() for x in raw_links.splitlines() if x.strip() and not x.strip().startswith("#")])[:MAX_LINKS]
    if len([x for x in raw_links.splitlines() if x.strip()]) > MAX_LINKS:
        st.warning(f"This app processes up to {MAX_LINKS} links per batch. The first {MAX_LINKS} will be used.")

    if st.button("📦 Import products with SociaVault", type="primary", use_container_width=True, disabled=not bool(links)):
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
        # Restore original link order.
        by_url = {j["url"]: j for j in imported}
        st.session_state["jobs"] = [by_url[x] for x in links if x in by_url]
        if errors:
            st.session_state["import_errors"] = errors
        st.rerun()

    if st.session_state.get("import_errors"):
        for link, error in st.session_state.pop("import_errors"):
            st.warning(f"Could not import {link[:65]}… — {error}")

    jobs = st.session_state.get("jobs") or []
    if not jobs:
        st.stop()

    st.success(f"{len(jobs)} product(s) ready")
    st.markdown("## 3 · Review references and batch settings")
    settings1, settings2 = st.columns(2)
    with settings1:
        run_mode = st.radio("Pipeline mode", ["Review images first", "Full auto"], horizontal=True)
    with settings2:
        scene = st.selectbox("Mirror setting", list(SCENES.keys()), index=0)

    new_jobs = []
    for i, job in enumerate(jobs):
        new_jobs.append(render_job_editor(job, i))
    st.session_state["jobs"] = new_jobs
    jobs = new_jobs

    st.markdown("## 4 · Run the batch")
    if not avatar_bytes:
        st.warning("Choose an avatar before generating.")

    completed_images = sum(1 for j in jobs if j.get("image_status") == "completed")
    approved = sum(1 for j in jobs if j.get("approved"))
    completed_videos = sum(1 for j in jobs if j.get("video_status") == "completed")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Products", len(jobs))
    m2.metric("Images complete", f"{completed_images}/{len(jobs)}")
    m3.metric("Approved", approved)
    m4.metric("Videos complete", f"{completed_videos}/{len(jobs)}")

    b1, b2, b3 = st.columns(3)
    generate_images = b1.button("🖼️ Generate all images", type="primary", use_container_width=True, disabled=not bool(avatar_bytes))
    run_full = b2.button("🚀 Run full batch", use_container_width=True, disabled=not bool(avatar_bytes))
    refresh_all = b3.button("🔄 Refresh all videos", use_container_width=True, disabled=not any(j.get("video_job_id") for j in jobs))

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
        st.session_state["jobs"] = jobs

        if run_full or run_mode == "Full auto":
            for i, job in enumerate(jobs):
                if job.get("image_status") == "completed":
                    job["approved"] = True
                    jobs[i] = submit_one_video(job, token, flow_email)
            st.session_state["jobs"] = jobs
        st.rerun()

    if refresh_all:
        bar = st.progress(0, text="Refreshing Omni 1.1 jobs...")
        targets = [i for i, j in enumerate(jobs) if j.get("video_job_id")]
        for n, idx in enumerate(targets, 1):
            jobs[idx] = refresh_one_video(jobs[idx], token)
            bar.progress(n / len(targets), text=f"Refreshed {n}/{len(targets)}")
        st.session_state["jobs"] = jobs
        st.rerun()

    review_controls = st.columns(3)
    if review_controls[0].button("✅ Approve all completed images", use_container_width=True):
        for job in jobs:
            if job.get("image_status") == "completed":
                job["approved"] = True
        st.session_state["jobs"] = jobs
        st.rerun()
    if review_controls[1].button("🎬 Generate videos for approved", use_container_width=True, disabled=not any(j.get("approved") and j.get("image_media_id") for j in jobs)):
        for i, job in enumerate(jobs):
            if job.get("approved") and job.get("image_media_id"):
                jobs[i] = submit_one_video(job, token, flow_email)
        st.session_state["jobs"] = jobs
        st.rerun()
    if review_controls[2].button("🧹 Clear batch", use_container_width=True):
        st.session_state.pop("jobs", None)
        st.rerun()

    st.markdown("## 5 · Results")
    avatar_id = st.session_state.get("avatar_flow_id", "")
    results = []
    for i, job in enumerate(st.session_state.get("jobs") or []):
        results.append(render_job_result(job, i, token, flow_email, avatar_id, scene))
    st.session_state["jobs"] = results

    st.markdown("## Batch downloads")
    d1, d2, d3 = st.columns(3)
    images_zip = build_images_zip(results)
    if images_zip:
        d1.download_button("⬇️ All images ZIP", data=images_zip, file_name="flow_tryon_images.zip", mime="application/zip", use_container_width=True)
    else:
        d1.button("⬇️ All images ZIP", disabled=True, use_container_width=True)
    if any(j.get("video_status") == "completed" for j in results):
        if d2.button("Prepare videos ZIP", use_container_width=True):
            with st.spinner("Downloading completed videos from Flow..."):
                z = build_videos_zip(results, token)
            if z:
                st.session_state["videos_zip"] = z
                st.rerun()
        if st.session_state.get("videos_zip"):
            d2.download_button("⬇️ All videos ZIP", data=st.session_state["videos_zip"], file_name="flow_tryon_videos.zip", mime="application/zip", use_container_width=True)
    else:
        d2.button("⬇️ All videos ZIP", disabled=True, use_container_width=True)
    d3.download_button("⬇️ Batch manifest", data=jobs_to_manifest(results), file_name="flow_tryon_batch.json", mime="application/json", use_container_width=True)


if __name__ == "__main__":
    main()
