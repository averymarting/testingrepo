import io
import json
import os
import random
import re
import socket
import sys
import time
import uuid
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request
from atproto import Client
from atproto_client.utils import TextBuilder

RUN_TAG      = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"


# ═══════════════════════════════════════════════════════════════════════════
#  ENV HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_env(name, required=True):
    v = os.getenv(name)
    if v is None:
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return ""
    return v.strip()

def get_float_env(name, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    raw = raw.strip().rstrip("%")
    try:
        v = float(raw)
        return v / 100.0 if v > 1 else v
    except ValueError:
        return default

def get_bool_env(name, default=False):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ═══════════════════════════════════════════════════════════════════════════
#  STATIC WORKFLOW KNOBS  (per-run, not per-account)
# ═══════════════════════════════════════════════════════════════════════════

_ri = get_float_env("IMAGE_RATIO", 0.60)
_rv = get_float_env("VIDEO_RATIO", 0.40)
_rs = _ri + _rv
IMAGE_RATIO = (_ri / _rs) if _rs > 0 else 0.60
VIDEO_RATIO = (_rv / _rs) if _rs > 0 else 0.40

HASHTAGS_ENABLED_IMAGE = get_bool_env("HASHTAGS_ENABLED_IMAGE", True)
HASHTAGS_ENABLED_VIDEO = get_bool_env("HASHTAGS_ENABLED_VIDEO", False)
MAX_IMAGE_BYTES        = int(get_float_env("MAX_IMAGE_MB", 2.0) * 1024 * 1024)
ENABLE_REPORT          = get_bool_env("ENABLE_REPORT", False)
ACCOUNT_ROW            = max(1, int(get_env("ACCOUNT_ROW", required=False) or "1"))


# ═══════════════════════════════════════════════════════════════════════════
#  MASTER SPREADSHEET
#  Sheet1  → credentials, one row per account (header in row 1, data from row 2)
#  Report  → daily follower stats + top-5 posts (only when ENABLE_REPORT=true)
# ═══════════════════════════════════════════════════════════════════════════

MASTER_SHEET_ID = "1d1ua2bzBt94omZxYgfwZhSJ94PJwAzc6clWpSVumebw"
CREDS_TAB       = "Sheet1"
REPORT_TAB      = "Report"

# Sheet1 expected headers (case-insensitive):
#   BSKY_HANDLE | BSKY_APP_PW | LINK_URL | LINK_DISPLAY_TEXT |
#   HASHTAGS | UPLOAD_FOLDER_ID | PROCESSED_FOLDER_ID

REPORT_HEADER = [
    "Date (UTC)", "Handle", "Type",
    "Prev Followers", "Gained", "Total Followers", "Status",
    "Post Preview (100 chars)", "Likes", "Reposts",
]

# Post-plan spreadsheet (unchanged)
POST_PLAN_SHEET_ID  = "1juum0RextNq44mrBN1Uu7ceSZA2V4Tmb9_oly3EORmA"
POSTED_STATUS_VALUE = "posted"

# Regex
_URL_RE     = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\S+")


# ═══════════════════════════════════════════════════════════════════════════
#  GOOGLE CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════

def get_creds():
    from google.oauth2.credentials import Credentials
    raw = get_env("GOOGLE_OAUTH_CREDENTIALS")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_OAUTH_CREDENTIALS is not valid JSON.") from exc
    creds = Credentials.from_authorized_user_info(info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT CONFIG  — loaded once from Sheet1 row ACCOUNT_ROW
# ═══════════════════════════════════════════════════════════════════════════

_account_config = None   # cached after first load

def load_account_config():
    """Read the selected account row from Sheet1 and return a config dict.

    Sheet1 header row (row 1, case-insensitive):
      BSKY_HANDLE | BSKY_APP_PW | LINK_URL | LINK_DISPLAY_TEXT |
      HASHTAGS | UPLOAD_FOLDER_ID | PROCESSED_FOLDER_ID

    ACCOUNT_ROW=1 → first data row (row 2 in the sheet), and so on.
    """
    global _account_config
    if _account_config is not None:
        return _account_config

    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID, range=f"{CREDS_TAB}!A:G"
    ).execute()
    values = result.get("values", [])

    if len(values) < 2:
        raise RuntimeError(
            f"Credentials tab '{CREDS_TAB}' in the master sheet appears empty. "
            "Add a header row and at least one account data row."
        )

    header   = [h.strip().upper() for h in values[0]]
    data_idx = ACCOUNT_ROW          # row 1 of data = values index 1
    if data_idx >= len(values):
        raise RuntimeError(
            f"ACCOUNT_ROW={ACCOUNT_ROW} but '{CREDS_TAB}' only has "
            f"{len(values)-1} data row(s). Add more rows or lower ACCOUNT_ROW."
        )

    row = values[data_idx]

    def col(*names):
        for n in names:
            try:
                idx = header.index(n.upper())
                return row[idx].strip() if idx < len(row) else ""
            except ValueError:
                continue
        return ""

    raw_link    = col("LINK_URL") or "https://foodiesposts.com"
    link_url    = raw_link if raw_link.startswith("http") else f"https://{raw_link}"
    link_display = (col("LINK_DISPLAY_TEXT")
                    or link_url.replace("https://", "").replace("http://", ""))

    cfg = {
        "handle":              col("BSKY_HANDLE"),
        "app_pw":              col("BSKY_APP_PW"),
        "link_url":            link_url,
        "link_display_text":   link_display,
        "hashtags_raw":        col("HASHTAGS"),
        "upload_folder_id":    col("UPLOAD_FOLDER_ID"),
        "processed_folder_id": col("PROCESSED_FOLDER_ID"),
        "row_num":             ACCOUNT_ROW,
    }

    if not cfg["handle"]:
        raise RuntimeError(
            f"BSKY_HANDLE is empty for account row {ACCOUNT_ROW} in '{CREDS_TAB}'. "
            "Check the sheet."
        )

    _account_config = cfg
    return cfg


def _cfg():
    """Shorthand for load_account_config()."""
    return load_account_config()


# ═══════════════════════════════════════════════════════════════════════════
#  TEXT HELPERS  (use dynamic account config)
# ═══════════════════════════════════════════════════════════════════════════

def _posting_handle():
    h = _cfg()["handle"]
    return h if h.startswith("@") else f"@{h}"

def replace_mentions(text):
    """Swap every @whatever in text with the actual posting handle."""
    return _MENTION_RE.sub(_posting_handle(), text) if text else text

def replace_urls(text):
    """Swap every https://... in text with the account's LINK_URL."""
    return _URL_RE.sub(_cfg()["link_url"], text) if text else text


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

def print_config_summary():
    cfg = _cfg()
    print("── Run config ──────────────────────────────────")
    print(f"  Account row:              {cfg['row_num']}")
    print(f"  Handle:                   {_posting_handle()}")
    print(f"  Post link:                {cfg['link_display_text']}")
    print(f"  Image ratio:              {IMAGE_RATIO:.0%}")
    print(f"  Video ratio:              {VIDEO_RATIO:.0%}")
    print(f"  Hashtags on image posts:  {HASHTAGS_ENABLED_IMAGE}")
    print(f"  Hashtags on video posts:  {HASHTAGS_ENABLED_VIDEO}")
    print(f"  Max image size:           {MAX_IMAGE_BYTES/(1024*1024):.1f} MB")
    print(f"  Generate report:          {ENABLE_REPORT}")
    print(f"  Post-plan tab:            {get_post_plan_tab_name()}")
    print("─────────────────────────────────────────────────")


# ═══════════════════════════════════════════════════════════════════════════
#  REPORT TAB  (followers + top posts, gated by ENABLE_REPORT)
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_report_tab(service):
    """Create the Report tab with a header row if it doesn't exist yet."""
    meta     = service.spreadsheets().get(spreadsheetId=MASTER_SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if REPORT_TAB not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=MASTER_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": REPORT_TAB}}}]},
        ).execute()
    # Ensure header
    r = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A1:J1"
    ).execute()
    if not r.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=MASTER_SHEET_ID,
            range=f"{REPORT_TAB}!A1:J1",
            valueInputOption="RAW",
            body={"values": [REPORT_HEADER]},
        ).execute()
        print(f"Created '{REPORT_TAB}' tab with header.")


def _report_rows_today(service, handle, type_prefix):
    """Return True if we already wrote a row of this type for this handle today."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    result = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A:C"
    ).execute()
    for row in result.get("values", [])[1:]:
        if len(row) >= 3 and row[0] == today and row[1] == handle and row[2].startswith(type_prefix):
            return True
    return False


def _append_report(service, rows):
    service.spreadsheets().values().append(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"{REPORT_TAB}!A:J",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def generate_follower_report(client, handle, service):
    """Write one 'followers' row per handle per day to the Report tab."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _report_rows_today(service, handle, "followers"):
        print(f"Follower report for {handle} already logged today; skipping.")
        return
    try:
        profile  = client.get_profile(actor=handle)
        total    = profile.followers_count or 0

        # Find previous total from the Report tab
        all_rows = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A:J"
        ).execute().get("values", [])
        prev_total = total   # default: no change if first entry
        for row in reversed(all_rows[1:]):
            if len(row) >= 6 and row[1] == handle and row[2] == "followers":
                try:
                    prev_total = int(row[5])
                except (ValueError, IndexError):
                    pass
                break

        gained = total - prev_total
        _append_report(service, [[
            today, handle, "followers",
            prev_total, gained, total, "Active",
            "", "", ""
        ]])
        print(f"Follower report: prev={prev_total}, gained={gained:+d}, total={total}")
    except Exception as exc:
        print(f"Warning: follower report failed: {exc}")


def generate_top_posts_report(client, handle, service):
    """Fetch last 50 posts, find top 5 by engagement, write to Report tab."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _report_rows_today(service, handle, "top_post_"):
        print(f"Top-posts report for {handle} already logged today; skipping.")
        return
    try:
        response = client.get_author_feed(actor=handle, limit=50)
        posts    = []
        for item in response.feed:
            # Skip reposts of other people's content
            if getattr(item, "reason", None) is not None:
                continue
            post      = item.post
            likes     = getattr(post, "like_count",   0) or 0
            reposts   = getattr(post, "repost_count", 0) or 0
            try:
                text = post.record.text or ""
            except AttributeError:
                text = ""
            posts.append({
                "text":       text,
                "likes":      likes,
                "reposts":    reposts,
                "engagement": likes + reposts,
            })

        top5 = sorted(posts, key=lambda p: p["engagement"], reverse=True)[:5]
        if not top5:
            print(f"No posts found for {handle} to report.")
            return

        rows = []
        for rank, p in enumerate(top5, start=1):
            preview = p["text"][:100] + ("…" if len(p["text"]) > 100 else "")
            rows.append([
                today, handle, f"top_post_{rank}",
                "", "", "", "",
                preview, p["likes"], p["reposts"]
            ])
        _append_report(service, rows)
        print(f"Logged top {len(top5)} posts for {handle} (by likes + reposts).")
    except Exception as exc:
        print(f"Warning: top-posts report failed: {exc}")


def run_report(client, handle):
    """Run both report sections. Called once per run when ENABLE_REPORT=true."""
    try:
        service = get_sheets_service()
        _ensure_report_tab(service)
        generate_follower_report(client, handle, service)
        generate_top_posts_report(client, handle, service)
    except Exception as exc:
        print(f"Warning: report generation failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  ERROR TYPES
# ═══════════════════════════════════════════════════════════════════════════

class AccountTakenDownError(Exception):
    """Fatal — log to sheet and disable workflow forever."""

class NoMediaFoundError(Exception):
    """Clean exit (code 0) — keep schedule running."""


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT STATUS LOGGING  (ban / auth fail)
# ═══════════════════════════════════════════════════════════════════════════

def log_account_problem(handle, status):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        _ensure_report_tab(service)
        _append_report(service, [[
            today, handle, "account_status",
            "", "", "", status,
            "", "", ""
        ]])
        print(f"Logged '{status}' for {handle}.")
    except Exception as exc:
        print(f"Warning: could not log account status: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT DISPLAY
# ═══════════════════════════════════════════════════════════════════════════

def print_target_account(handle):
    display = handle if handle.startswith("@") else f"@{handle}"
    print(f"Target Bluesky account: {display}")
    print(f"  (app password loaded: {'yes' if _cfg().get('app_pw') else 'NO — missing!'})")


# ═══════════════════════════════════════════════════════════════════════════
#  HASHTAGS
# ═══════════════════════════════════════════════════════════════════════════

def get_account_hashtags():
    """Return list of hashtag words (no #) for this account.
    Tries the HASHTAGS cell in Sheet1 first; falls back to hashtags.txt."""
    raw = _cfg().get("hashtags_raw", "")
    if raw:
        tags = [w.lstrip("#") for w in raw.split() if w.startswith("#")]
        if tags:
            return tags
    # Fall back to file
    try:
        with open("hashtags.txt", "r", encoding="utf-8") as f:
            sets = [l.strip() for l in f if l.strip()]
        return [w.lstrip("#") for w in random.choice(sets).split() if w.startswith("#")] if sets else []
    except FileNotFoundError:
        return []


# ═══════════════════════════════════════════════════════════════════════════
#  POST-PLAN SHEET
#  Columns: File Name | Caption | Status
#  Only unposted rows are eligible; Status is written "posted" after success.
# ═══════════════════════════════════════════════════════════════════════════

_post_plan_cache          = None
_post_plan_status_col_idx = None


def get_post_plan_tab_name():
    return get_env("POST_PLAN_SHEET_NAME", required=False) or "Sheet1"


def _col_letter(idx0):
    idx, letters = idx0 + 1, ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters  = chr(65 + rem) + letters
    return letters


def load_post_plan(force_refresh=False):
    global _post_plan_cache, _post_plan_status_col_idx
    if _post_plan_cache is not None and not force_refresh:
        return _post_plan_cache

    tab     = get_post_plan_tab_name()
    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=POST_PLAN_SHEET_ID, range=f"{tab}!A:Z"
    ).execute()
    values  = result.get("values", [])
    if not values:
        print(f"Warning: post-plan tab '{tab}' is empty.")
        _post_plan_cache = {}
        return _post_plan_cache

    header = [h.strip().lower() for h in values[0]]
    def ci(*names):
        for n in names:
            if n in header: return header.index(n)
        return None

    file_idx    = ci("file name", "filename", "file")
    caption_idx = ci("caption", "captions")
    status_idx  = ci("status")
    _post_plan_status_col_idx = status_idx

    if file_idx is None or caption_idx is None:
        print(f"Warning: post-plan tab '{tab}' needs 'File Name' and 'Caption' columns.")
        _post_plan_cache = {}
        return _post_plan_cache
    if status_idx is None:
        print("Warning: no 'Status' column in post-plan — posted files won't be remembered.")

    plan, already = {}, 0
    for i, row in enumerate(values[1:], start=2):
        fname   = row[file_idx].strip()    if len(row) > file_idx    else ""
        caption = row[caption_idx].strip() if len(row) > caption_idx else ""
        status  = row[status_idx].strip()  if status_idx is not None and len(row) > status_idx else ""
        if not fname: continue
        plan[fname] = {"caption": caption, "row": i, "status": status}
        if status.lower() == POSTED_STATUS_VALUE: already += 1

    print(f"Loaded {len(plan)} post-plan rows ({already} already posted).")
    _post_plan_cache = plan
    return plan


def mark_posted(filename, row_number):
    global _post_plan_cache
    if _post_plan_status_col_idx is None:
        print(f"Warning: no 'Status' column — could not mark '{filename}' as posted.")
        return
    try:
        tab     = get_post_plan_tab_name()
        col_l   = _col_letter(_post_plan_status_col_idx)
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=POST_PLAN_SHEET_ID,
            range=f"{tab}!{col_l}{row_number}",
            valueInputOption="RAW",
            body={"values": [[POSTED_STATUS_VALUE]]},
        ).execute()
        if _post_plan_cache and filename in _post_plan_cache:
            _post_plan_cache[filename]["status"] = POSTED_STATUS_VALUE
        print(f"Marked '{filename}' row {row_number} as posted.")
    except Exception as exc:
        print(f"ERROR: could not write posted status for '{filename}': {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  DRIVE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def claim_file(service, file_id, current_name):
    claimed = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed}).execute()
    check = service.files().get(fileId=file_id, fields="id, name").execute()
    if check.get("name") != claimed:
        print(f"Lost claim race on '{current_name}'; skipping.")
        return None
    return claimed


def choose_media_kind():
    return random.choices(["image", "video"], weights=[IMAGE_RATIO, VIDEO_RATIO], k=1)[0]


def _download_file(service, file_id, local_path):
    req = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        dl = MediaIoBaseDownload(f, req)
        done = False
        while not done:
            _, done = dl.next_chunk()


def fetch_media_matching_plan(preferred_kind, plan):
    creds     = get_creds()
    service   = build("drive", "v3", credentials=creds)
    folder_id = _cfg()["upload_folder_id"]
    if not folder_id:
        raise RuntimeError("UPLOAD_FOLDER_ID is empty in the credentials sheet row.")

    results = service.files().list(
        q=f"'{folder_id}' in parents",
        orderBy="createdTime desc",
        pageSize=100,
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No files in upload folder.")
        return None, None, None, None, None

    mime_prefix = f"{preferred_kind}/"
    for file in files:
        name = file["name"]
        if name.startswith(CLAIM_PREFIX): continue

        entry = plan.get(name)
        if entry is None: continue                         # not in plan
        if entry["status"].lower() == POSTED_STATUS_VALUE: continue  # done

        if not file.get("mimeType", "").startswith(mime_prefix): continue

        caption    = entry["caption"]
        row_number = entry["row"]
        print(f"Found {preferred_kind} in post plan: {name}")

        claimed = claim_file(service, file["id"], name)
        if claimed is None: continue

        local = f"/tmp/{name}"
        _download_file(service, file["id"], local)
        file["original_name"] = name
        file["claimed_name"]  = claimed
        return file, local, preferred_kind, caption, row_number

    print(f"No unposted {preferred_kind} matching post-plan found.")
    return None, None, None, None, None


def compress_image_under_limit(local_path):
    from PIL import Image
    orig = os.path.getsize(local_path)
    if orig <= MAX_IMAGE_BYTES:
        print(f"Image {orig/1024:.0f} KB — no compression needed.")
        return local_path
    img = Image.open(local_path)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    for q in range(90, 20, -10):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            with open(local_path, "wb") as f: f.write(buf.getvalue())
            print(f"Compressed {orig/1024:.0f} KB → {buf.tell()/1024:.0f} KB (q={q}).")
            return local_path
    w, h, scale = *img.size, 0.9
    while scale > 0.3:
        r = img.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.LANCZOS)
        buf = io.BytesIO()
        r.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            with open(local_path, "wb") as f: f.write(buf.getvalue())
            print(f"Resized+compressed → {buf.tell()/1024:.0f} KB.")
            return local_path
        scale -= 0.1
    with open(local_path, "wb") as f: f.write(buf.getvalue())
    print(f"Warning: best-effort size = {buf.tell()/1024:.0f} KB.")
    return local_path


def move_file(file_id, restore_name=None):
    creds   = get_creds()
    service = build("drive", "v3", credentials=creds)
    cfg     = _cfg()
    body    = {"name": restore_name} if restore_name else {}
    service.files().update(
        fileId=file_id,
        addParents=cfg["processed_folder_id"],
        removeParents=cfg["upload_folder_id"],
        body=body,
    ).execute()
    print("Moved to processed folder.")


def release_claim(file_id, original_name):
    try:
        service = build("drive", "v3", credentials=get_creds())
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}'.")
    except Exception as exc:
        print(f"Warning: could not release claim: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  POST BUILDING
# ═══════════════════════════════════════════════════════════════════════════

LOOP_INTERVAL_SECONDS = 1800   # 30 minutes between posting cycles


def build_post_from_caption(caption, tags):
    """
    1. @mentions in caption → replaced with actual posting handle
    2. First URL in caption → replaced with a clickable link facet (LINK_URL)
       displayed as LINK_DISPLAY_TEXT (no "leaving Bluesky" warning)
    3. Hashtags appended on new line
    """
    tb  = TextBuilder()
    cfg = _cfg()

    # Step 1: replace @mentions
    text = replace_mentions(caption) if caption else ""

    # Step 2: replace first URL with a proper link facet
    m = _URL_RE.search(text)
    if m:
        before = text[:m.start()].rstrip()
        after  = _URL_RE.sub("", text[m.end():]).strip()
        if before:
            tb.text(before + " ")
        tb.link(cfg["link_display_text"], cfg["link_url"])
        if after:
            tb.text(" " + after)
    elif text:
        tb.text(text)

    if tags:
        tb.text("\n\n")
        for i, tag in enumerate(tags):
            tb.tag(f"#{tag}", tag)
            if i < len(tags) - 1:
                tb.text(" ")

    return tb


def post_to_bluesky(client, media_name, local_path, kind, caption, tags):
    tb = build_post_from_caption(caption, tags)
    if kind == "video":
        with open(local_path, "rb") as f:
            client.send_video(text=tb, video=f.read(), video_alt=media_name)
    else:
        with open(local_path, "rb") as f:
            client.send_image(text=tb, image=f.read(), image_alt=media_name)

    # Log what went out
    preview = replace_mentions(caption or "")
    m = _URL_RE.search(preview)
    if m:
        preview = (preview[:m.start()].rstrip()
                   + f" [{_cfg()['link_display_text']}]"
                   + _URL_RE.sub("", preview[m.end():]).strip())
    print(f"Posted {kind}:")
    print(f"  Caption: {preview!r}")
    if tags:
        print(f"  Tags: {' '.join('#'+t for t in tags)}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN CYCLE
# ═══════════════════════════════════════════════════════════════════════════

def run_once():
    cfg    = _cfg()
    handle = cfg["handle"]
    app_pw = cfg["app_pw"]

    print_target_account(handle)
    client = Client()
    try:
        client.login(handle, app_pw)
    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            raise AccountTakenDownError(f"Account {handle} taken down/suspended.") from exc
        if "AuthenticationRequired" in err or "Invalid identifier or password" in err:
            raise AccountTakenDownError(
                f"Auth failed for {handle} — wrong handle or app password in sheet row {ACCOUNT_ROW}."
            ) from exc
        raise

    # Report (followers + top posts) — gated by ENABLE_REPORT flag
    if ENABLE_REPORT:
        run_report(client, handle)

    # Posting cycle
    plan = load_post_plan()
    if not plan:
        raise NoMediaFoundError("Post-plan sheet has no usable rows.")

    preferred = choose_media_kind()
    fallback  = "video" if preferred == "image" else "image"

    file, path, kind, caption, row_num = fetch_media_matching_plan(preferred, plan)
    if not file:
        print(f"No {preferred} matched; trying {fallback}.")
        file, path, kind, caption, row_num = fetch_media_matching_plan(fallback, plan)

    if not file:
        raise NoMediaFoundError(
            "No unposted Drive file matching the post-plan sheet found."
        )

    original_name = file["original_name"]

    try:
        if kind == "image":
            path = compress_image_under_limit(path)

        hashtags_on = HASHTAGS_ENABLED_IMAGE if kind == "image" else HASHTAGS_ENABLED_VIDEO
        tags = get_account_hashtags() if hashtags_on else []

        post_to_bluesky(client, original_name, path, kind, caption, tags)

    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            release_claim(file["id"], original_name)
            raise AccountTakenDownError(f"Account {handle} taken down mid-cycle.") from exc
        release_claim(file["id"], original_name)
        raise

    mark_posted(original_name, row_num)   # mark BEFORE move
    move_file(file["id"], restore_name=original_name)
    try: os.remove(path)
    except OSError: pass


def main():
    # Load account config first so any sheet/credential errors show up immediately
    try:
        load_account_config()
    except Exception as exc:
        print(f"\n{'='*60}\nFATAL: Could not load account config: {exc}\n{'='*60}\n")
        sys.exit(1)

    print_config_summary()
    print(f"Starting loop. Posting every {LOOP_INTERVAL_SECONDS} seconds.")

    while True:
        cycle_start = time.time()
        try:
            run_once()
        except NoMediaFoundError as exc:
            print(f"\n{'='*60}\nNO MEDIA: {exc}\nStopping — schedule keeps running.\n{'='*60}\n")
            sys.exit(0)
        except AccountTakenDownError as exc:
            handle  = (_cfg().get("handle") if _account_config else None) or "unknown"
            err_str = str(exc)
            reason  = ("🔑 AUTH FAILED — wrong handle or app password in sheet"
                       if "Auth failed" in err_str or "app password" in err_str
                       else "⛔ ACCOUNT TAKEN DOWN / BANNED")
            print(f"\n{'='*60}\n{err_str}\n→ {reason}\n{'='*60}\n")
            log_account_problem(handle, status=reason)
            sys.exit(1)
        except Exception as exc:
            print(f"Error during cycle: {exc}")

        elapsed   = time.time() - cycle_start
        sleep_for = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
