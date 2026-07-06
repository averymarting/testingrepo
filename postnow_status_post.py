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
            raise RuntimeError(f"Missing required env var: {name}")
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

def get_int_env(name, default):
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


# ═══════════════════════════════════════════════════════════════════════════
#  STATIC WORKFLOW KNOBS
# ═══════════════════════════════════════════════════════════════════════════

_ri = get_float_env("IMAGE_RATIO", 0.60)
_rv = get_float_env("VIDEO_RATIO", 0.40)
_rs = _ri + _rv
IMAGE_RATIO = (_ri / _rs) if _rs > 0 else 0.60
VIDEO_RATIO = (_rv / _rs) if _rs > 0 else 0.40

HASHTAGS_ENABLED_IMAGE = get_bool_env("HASHTAGS_ENABLED_IMAGE", True)
HASHTAGS_ENABLED_VIDEO = get_bool_env("HASHTAGS_ENABLED_VIDEO", False)
ENABLE_REPORT          = get_bool_env("ENABLE_REPORT", False)
ACCOUNT_ROW            = get_int_env("ACCOUNT_ROW", 1)   # 1-based data row (header is row 0)
TOP_POSTS_COUNT        = get_int_env("TOP_POSTS_COUNT", 5)    # how many top posts to report
TOP_POSTS_WITHIN       = get_int_env("TOP_POSTS_WITHIN", 30)  # scan last N posts


# ═══════════════════════════════════════════════════════════════════════════
#  SPREADSHEETS
# ═══════════════════════════════════════════════════════════════════════════

# Master sheet: Sheet1 = credentials, Report = daily stats + top posts
MASTER_SHEET_ID = "1d1ua2bzBt94omZxYgfwZhSJ94PJwAzc6clWpSVumebw"
CREDS_TAB       = "Sheet1"
REPORT_TAB      = "Report"

# 12-column report header (A:L)
REPORT_HEADER = [
    "Date (UTC)", "Handle", "Type",
    "Prev Followers", "Gained", "Total Followers", "Status",
    "Post Preview", "Likes", "Reposts", "Replies", "Quotes",
]

# Post-plan sheet (separate spreadsheet)
POST_PLAN_SHEET_ID  = "1juum0RextNq44mrBN1Uu7ceSZA2V4Tmb9_oly3EORmA"
POSTED_STATUS_VALUE = "posted"

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
#  ACCOUNT CONFIG  — from Sheet1, row ACCOUNT_ROW (row 1 = first data row
#  i.e. the second actual spreadsheet row, since row 1 is the header)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Expected Sheet1 header (case-insensitive):
#  BSKY_HANDLE | BSKY_APP_PW | LINK_URL | LINK_DISPLAY_TEXT |
#  HASHTAGS | UPLOAD_FOLDER_ID | PROCESSED_FOLDER_ID

_account_config = None

def load_account_config():
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
            f"'{CREDS_TAB}' in the master sheet is empty or has only a header. "
            "Add at least one account data row."
        )

    # ACCOUNT_ROW=1 → values index 1 (first data row after the header)
    data_idx = ACCOUNT_ROW
    if data_idx >= len(values):
        raise RuntimeError(
            f"ACCOUNT_ROW={ACCOUNT_ROW} but '{CREDS_TAB}' only has "
            f"{len(values)-1} data row(s)."
        )

    header = [h.strip().upper() for h in values[0]]
    row    = values[data_idx]

    def col(*names):
        for n in names:
            try:
                idx = header.index(n.upper())
                return row[idx].strip() if idx < len(row) else ""
            except ValueError:
                continue
        return ""

    raw_link     = col("LINK_URL") or "https://foodiesposts.com"
    link_url     = raw_link if raw_link.startswith("http") else f"https://{raw_link}"
    link_display = col("LINK_DISPLAY_TEXT") or link_url.replace("https://","").replace("http://","")

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
            f"BSKY_HANDLE is empty for account row {ACCOUNT_ROW} in '{CREDS_TAB}'."
        )

    _account_config = cfg
    return cfg

def _cfg():
    return load_account_config()


# ═══════════════════════════════════════════════════════════════════════════
#  TEXT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _posting_handle():
    h = _cfg()["handle"]
    return h if h.startswith("@") else f"@{h}"

def replace_mentions(text):
    return _MENTION_RE.sub(_posting_handle(), text) if text else text

def replace_urls(text):
    return _URL_RE.sub(_cfg()["link_url"], text) if text else text


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

def print_config_summary():
    cfg = _cfg()
    print("── Run config ──────────────────────────────────")
    print(f"  Account row:              {cfg['row_num']}  ({_posting_handle()})")
    print(f"  Post link:                {cfg['link_display_text']}")
    print(f"  Image ratio:              {IMAGE_RATIO:.0%}")
    print(f"  Video ratio:              {VIDEO_RATIO:.0%}")
    print(f"  Hashtags on image posts:  {HASHTAGS_ENABLED_IMAGE}")
    print(f"  Hashtags on video posts:  {HASHTAGS_ENABLED_VIDEO}")
    print(f"  Generate report:          {ENABLE_REPORT}")
    if ENABLE_REPORT:
        print(f"  Top posts to report:      {TOP_POSTS_COUNT}")
        print(f"  Scan last N posts:        {TOP_POSTS_WITHIN}")
    print(f"  Post-plan tab:            {get_post_plan_tab_name()}")
    print("─────────────────────────────────────────────────")


# ═══════════════════════════════════════════════════════════════════════════
#  REPORT TAB
#  - Follower row: Type="followers", cols D-G filled
#  - Top-post row: Type="top_post_N", cols H-L filled
#  - Problem row:  Type="account_status", col G filled with reason
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_report_tab(service):
    """Make sure the Report tab exists and has the full 12-column header.
    Never crashes if the tab already exists."""
    # ── 1. Create tab only if it truly doesn't exist ──────────────────────
    try:
        meta     = service.spreadsheets().get(spreadsheetId=MASTER_SHEET_ID).execute()
        existing = {s["properties"]["title"].strip().lower()
                    for s in meta.get("sheets", [])}
        if REPORT_TAB.lower() not in existing:
            service.spreadsheets().batchUpdate(
                spreadsheetId=MASTER_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": REPORT_TAB}}}]},
            ).execute()
            print(f"Created '{REPORT_TAB}' tab.")
    except Exception as exc:
        # "already exists" can race in multi-workflow setups — safe to ignore
        if "already exists" not in str(exc).lower():
            print(f"Warning: could not verify/create Report tab: {exc}")

    # ── 2. Ensure header row has all 12 columns ───────────────────────────
    try:
        r = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A1:L1"
        ).execute()
        existing_header = r.get("values", [[]])[0] if r.get("values") else []
        if len(existing_header) < len(REPORT_HEADER):
            service.spreadsheets().values().update(
                spreadsheetId=MASTER_SHEET_ID,
                range=f"{REPORT_TAB}!A1:L1",
                valueInputOption="RAW",
                body={"values": [REPORT_HEADER]},
            ).execute()
            print(f"Updated '{REPORT_TAB}' header to {len(REPORT_HEADER)} columns.")
    except Exception as exc:
        print(f"Warning: could not check/update report header: {exc}")


def _report_logged_today(service, handle, type_prefix):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A:C"
        ).execute()
        for row in result.get("values", [])[1:]:
            if (len(row) >= 3
                    and row[0] == today
                    and row[1] == handle
                    and row[2].startswith(type_prefix)):
                return True
    except Exception:
        pass
    return False


def _append_report(service, rows):
    service.spreadsheets().values().append(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"{REPORT_TAB}!A:L",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def generate_follower_report(client, handle, service):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _report_logged_today(service, handle, "followers"):
        print(f"Follower report for {handle} already logged today; skipping.")
        return
    try:
        profile = client.get_profile(actor=handle)
        total   = profile.followers_count or 0

        # Find last logged total for this handle
        all_rows = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A:L"
        ).execute().get("values", [])
        prev_total = total
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
            "", "", "", "", ""
        ]])
        print(f"Follower report: prev={prev_total}, gained={gained:+d}, total={total}")
    except Exception as exc:
        print(f"Warning: follower report failed: {exc}")


def generate_top_posts_report(client, handle, service):
    """Fetch last TOP_POSTS_WITHIN posts, rank by total engagement
    (likes + reposts + replies + quotes), write top TOP_POSTS_COUNT rows."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _report_logged_today(service, handle, "top_post_"):
        print(f"Top-posts report for {handle} already logged today; skipping.")
        return
    try:
        response = client.get_author_feed(actor=handle, limit=TOP_POSTS_WITHIN)
        posts = []
        for item in response.feed:
            if getattr(item, "reason", None) is not None:
                continue   # skip reposts of others
            p       = item.post
            likes   = getattr(p, "like_count",    0) or 0
            reposts = getattr(p, "repost_count",  0) or 0
            replies = getattr(p, "reply_count",   0) or 0
            quotes  = getattr(p, "quote_count",   0) or 0
            try:
                text = p.record.text or ""
            except AttributeError:
                text = ""
            posts.append({
                "text":       text,
                "likes":      likes,
                "reposts":    reposts,
                "replies":    replies,
                "quotes":     quotes,
                "engagement": likes + reposts + replies + quotes,
            })

        if not posts:
            print(f"No own posts found for {handle}.")
            return

        top_n = sorted(posts, key=lambda p: p["engagement"], reverse=True)[:TOP_POSTS_COUNT]
        print(f"\nTop {len(top_n)} posts for {handle} (out of {len(posts)} scanned):")
        rows = []
        for rank, p in enumerate(top_n, start=1):
            preview = p["text"][:100] + ("…" if len(p["text"]) > 100 else "")
            print(f"  #{rank}: likes={p['likes']} reposts={p['reposts']} "
                  f"replies={p['replies']} quotes={p['quotes']} "
                  f"total={p['engagement']} | {preview[:60]!r}")
            rows.append([
                today, handle, f"top_post_{rank}",
                "", "", "", "",
                preview, p["likes"], p["reposts"], p["replies"], p["quotes"],
            ])

        _append_report(service, rows)
        print(f"Logged top {len(top_n)} posts to Report tab.")
    except Exception as exc:
        print(f"Warning: top-posts report failed: {exc}")


def run_report(client, handle):
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
    """Fatal — log to sheet, disable workflow."""

class NoMediaFoundError(Exception):
    """Clean exit (code 0) — keep schedule running."""


def log_account_problem(handle, status):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        _ensure_report_tab(service)
        _append_report(service, [[
            today, handle, "account_status",
            "", "", "", status,
            "", "", "", "", ""
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
    print(f"  (app password: {'loaded' if _cfg().get('app_pw') else 'MISSING!'})")


# ═══════════════════════════════════════════════════════════════════════════
#  HASHTAGS
# ═══════════════════════════════════════════════════════════════════════════

def get_account_hashtags():
    raw = _cfg().get("hashtags_raw", "")
    if raw:
        tags = [w.lstrip("#") for w in raw.split() if w.startswith("#")]
        if tags:
            return tags
    try:
        with open("hashtags.txt", "r", encoding="utf-8") as f:
            sets = [l.strip() for l in f if l.strip()]
        return [w.lstrip("#") for w in random.choice(sets).split() if w.startswith("#")] if sets else []
    except FileNotFoundError:
        return []


# ═══════════════════════════════════════════════════════════════════════════
#  POST-PLAN SHEET
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
        print(f"Warning: post-plan needs 'File Name' and 'Caption' columns. Found: {header}")
        _post_plan_cache = {}
        return _post_plan_cache
    if status_idx is None:
        print("Warning: no 'Status' column — posted files won't be tracked.")

    # Build TWO lookup dicts: exact case AND lowercase key
    plan_exact   = {}
    plan_lower   = {}
    already      = 0
    for i, row in enumerate(values[1:], start=2):
        fname   = row[file_idx].strip()    if len(row) > file_idx    else ""
        caption = row[caption_idx].strip() if len(row) > caption_idx else ""
        status  = row[status_idx].strip()  if status_idx is not None and len(row) > status_idx else ""
        if not fname: continue
        entry = {"caption": caption, "row": i, "status": status}
        plan_exact[fname]         = entry
        plan_lower[fname.lower()] = entry
        if status.lower() == POSTED_STATUS_VALUE: already += 1

    print(f"Loaded {len(plan_exact)} post-plan rows ({already} already posted).")
    _post_plan_cache = {"exact": plan_exact, "lower": plan_lower}
    return _post_plan_cache


def find_plan_entry(plan, drive_filename):
    """Lookup a Drive filename in the plan — tries exact match first,
    then case-insensitive, then without extension."""
    exact = plan.get("exact", {})
    lower = plan.get("lower", {})
    return (
        exact.get(drive_filename)
        or lower.get(drive_filename.lower())
        or lower.get(os.path.splitext(drive_filename.lower())[0])
    )


def mark_posted(filename, row_number, retries=3):
    """Write 'posted' to Status column, retrying up to `retries` times on timeout."""
    global _post_plan_cache
    if _post_plan_status_col_idx is None:
        print(f"Warning: no 'Status' column — cannot mark '{filename}' as posted.")
        return
    for attempt in range(1, retries + 1):
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
            # Update in-memory cache
            if _post_plan_cache:
                for d in (_post_plan_cache.get("exact",{}), _post_plan_cache.get("lower",{})):
                    if filename in d: d[filename]["status"] = POSTED_STATUS_VALUE
                    if filename.lower() in d: d[filename.lower()]["status"] = POSTED_STATUS_VALUE
            print(f"Marked '{filename}' row {row_number} as posted.")
            return
        except Exception as exc:
            if attempt < retries:
                wait = 2 ** attempt
                print(f"  mark_posted attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s…")
                time.sleep(wait)
            else:
                print(f"ERROR: could not mark '{filename}' as posted after {retries} attempts: {exc}")
                print("  Post was successful — file will be moved. Row may need manual update.")


# ═══════════════════════════════════════════════════════════════════════════
#  DRIVE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def claim_file(service, file_id, current_name):
    claimed = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed}).execute()
    check = service.files().get(fileId=file_id, fields="id,name").execute()
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


# File-extension → media kind mapping.
# Drive API mimeType can be unreliable (depends on how the file was uploaded),
# so we detect kind from the filename extension which is always trustworthy.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".avif", ".heic"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".3gp", ".ts"}


def _kind_from_filename(filename):
    """Return 'image', 'video', or None based on the file extension."""
    ext = os.path.splitext(filename.lower())[1]
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    return None


def fetch_media_matching_plan(preferred_kind, plan):
    """Find the newest unclaimed Drive file that:
      - is in the upload folder
      - has a matching row in the post plan (case-insensitive filename match)
      - is not yet marked posted
      - has a mimeType matching preferred_kind (image or video)
    Returns (file_dict, local_path, kind, caption, row_number) or 5 Nones.
    """
    creds     = get_creds()
    service   = build("drive", "v3", credentials=creds)
    folder_id = _cfg()["upload_folder_id"]
    if not folder_id:
        raise RuntimeError("UPLOAD_FOLDER_ID is empty in credentials sheet.")

    # Explicitly request the fields we need (mimeType kept for logging only)
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime desc",
        pageSize=100,
        fields="files(id,name,mimeType)",
    ).execute()
    files = results.get("files", [])
    if not files:
        print("Upload folder is empty.")
        return None, None, None, None, None

    skipped_claim  = skipped_plan = skipped_posted = skipped_mime = 0
    debug_rows = []   # (name, ext, detected_kind, reason) for every file we look at

    for file in files:
        name      = file.get("name", "")
        mime_type = file.get("mimeType", "unknown")   # only used for logging now
        ext       = os.path.splitext(name.lower())[1]

        if name.startswith(CLAIM_PREFIX):
            skipped_claim += 1
            debug_rows.append((name, ext, "-", "already claimed by another run"))
            continue

        entry = find_plan_entry(plan, name)
        if entry is None:
            skipped_plan += 1
            debug_rows.append((name, ext, "-", "NOT FOUND in post-plan sheet (filename mismatch?)"))
            continue

        if entry["status"].lower() == POSTED_STATUS_VALUE:
            skipped_posted += 1
            debug_rows.append((name, ext, "-", "plan row already marked 'posted'"))
            continue

        # ── Detect kind from file extension (more reliable than Drive mimeType) ──
        file_kind = _kind_from_filename(name)
        if file_kind is None:
            # Unknown extension — fall back to mimeType prefix
            if mime_type.startswith("image/"):
                file_kind = "image"
            elif mime_type.startswith("video/"):
                file_kind = "video"

        if file_kind != preferred_kind:
            skipped_mime += 1
            debug_rows.append((name, ext, file_kind or "UNKNOWN",
                                f"detected as '{file_kind}', wanted '{preferred_kind}'"))
            continue

        caption    = entry["caption"]
        row_number = entry["row"]
        print(f"Found {preferred_kind}: '{name}' (mime={mime_type}, ext={ext})")

        claimed = claim_file(service, file["id"], name)
        if claimed is None:
            continue

        print(f"Claimed as '{claimed}'.")
        local = f"/tmp/{name}"
        _download_file(service, file["id"], local)
        file["original_name"] = name
        file["claimed_name"]  = claimed
        return file, local, preferred_kind, caption, row_number

    print(f"No match for {preferred_kind}: "
          f"{skipped_plan} not in plan, {skipped_posted} already posted, "
          f"{skipped_mime} wrong type, {skipped_claim} claimed by other run.")
    if debug_rows:
        print(f"  ── file-by-file detail ({len(debug_rows)} files scanned) ──")
        for name, ext, detected, reason in debug_rows:
            print(f"    '{name}' (ext={ext or '<none>'}, detected={detected}) → {reason}")
    return None, None, None, None, None


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

LOOP_INTERVAL_SECONDS = 120


def build_post_from_caption(caption, tags):
    """
    1. @mentions → replaced with actual posting handle
    2. First URL → replaced with clickable link facet (LINK_DISPLAY_TEXT → LINK_URL)
    3. Hashtags appended on new line
    """
    tb  = TextBuilder()
    cfg = _cfg()
    text = replace_mentions(caption) if caption else ""

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

    preview = replace_mentions(caption or "")
    m = _URL_RE.search(preview)
    if m:
        preview = (preview[:m.start()].rstrip()
                   + f" [{_cfg()['link_display_text']}]"
                   + _URL_RE.sub("", preview[m.end():]).strip())
    print(f"✓ Posted {kind}: {preview!r}")
    if tags:
        print(f"  Tags: {' '.join('#'+t for t in tags)}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN CYCLE
# ═══════════════════════════════════════════════════════════════════════════

def run_once():
    cfg    = _cfg()
    handle = cfg["handle"]

    print_target_account(handle)
    client = Client()
    try:
        client.login(handle, cfg["app_pw"])
    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            raise AccountTakenDownError(f"Account {handle} taken down/suspended.") from exc
        if "AuthenticationRequired" in err or "Invalid identifier or password" in err:
            raise AccountTakenDownError(
                f"Auth failed for {handle} — check BSKY_HANDLE / BSKY_APP_PW in sheet row {ACCOUNT_ROW}."
            ) from exc
        raise

    if ENABLE_REPORT:
        run_report(client, handle)

    plan = load_post_plan()
    if not plan:
        raise NoMediaFoundError("Post-plan sheet has no usable rows.")

    preferred = choose_media_kind()
    fallback  = "video" if preferred == "image" else "image"
    print(f"This cycle: preferred kind = '{preferred}' (ratios: image={IMAGE_RATIO:.0%}, video={VIDEO_RATIO:.0%})")

    file, path, kind, caption, row_num = fetch_media_matching_plan(preferred, plan)
    if not file:
        print(f"No {preferred} matched; trying {fallback}.")
        file, path, kind, caption, row_num = fetch_media_matching_plan(fallback, plan)

    if not file:
        raise NoMediaFoundError("No unposted Drive file matching the post-plan sheet.")

    original_name = file["original_name"]
    post_succeeded = False

    print(f"About to post {kind}: '{original_name}' "
          f"({os.path.getsize(path)/1024:.1f} KB) from '{path}'.")

    try:
        hashtags_on = HASHTAGS_ENABLED_IMAGE if kind == "image" else HASHTAGS_ENABLED_VIDEO
        tags = get_account_hashtags() if hashtags_on else []

        post_to_bluesky(client, original_name, path, kind, caption, tags)
        post_succeeded = True   # ← only True if the above line completes without error

    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            release_claim(file["id"], original_name)
            raise AccountTakenDownError(f"Account {handle} taken down mid-cycle.") from exc
        # Any other posting error → release claim, do NOT mark or move
        release_claim(file["id"], original_name)
        print(f"Post failed ({kind}) — claim released, file stays in upload folder.")
        print(f"  Error: {exc}")
        import traceback
        traceback.print_exc()
        raise

    # Post succeeded — mark and move regardless of whether marking times out
    mark_posted(original_name, row_num)   # retries internally; logs error but doesn't raise
    try:
        move_file(file["id"], restore_name=original_name)
    except Exception as exc:
        print(f"Warning: move_file failed: {exc}. File may still be in upload folder — remove manually.")
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    try:
        load_account_config()
    except Exception as exc:
        print(f"\n{'='*60}\nFATAL: {exc}\n{'='*60}\n")
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
            handle  = (_account_config or {}).get("handle", "unknown")
            err_str = str(exc)
            reason  = ("🔑 AUTH FAILED — check handle/app-password in sheet"
                       if "Auth failed" in err_str or "app password" in err_str
                       else "⛔ ACCOUNT TAKEN DOWN / BANNED")
            print(f"\n{'='*60}\n{err_str}\n→ {reason}\n{'='*60}\n")
            log_account_problem(handle, status=reason)
            sys.exit(1)
        except Exception as exc:
            print(f"Error during cycle: {exc}")

        elapsed   = time.time() - cycle_start
        sleep_for = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s…")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
