import asyncio
import functools
import html
import io
import json
import os
import re
import sys
import time
import textwrap
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qs

import gspread
import edge_tts
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from moviepy.editor import AudioFileClip, CompositeAudioClip, ImageClip
from moviepy.audio.fx.all import audio_loop


# GitHub Actions logs should appear immediately.
print = functools.partial(print, flush=True)

# --- 1. LOAD ENVIRONMENT SECRETS ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
YOUTUBE_TOKEN_INFO = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])

REVIEW_WEBAPP_URL = os.environ.get("REVIEW_WEBAPP_URL", "").strip()
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").strip()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
DISPATCH_ACTION = os.environ.get("DISPATCH_ACTION", "").strip()

BG_FOLDER_NAME = os.environ.get("BG_FOLDER_NAME", "Bible Study Backgrounds").strip()
MUSIC_FOLDER_NAME = os.environ.get("MUSIC_FOLDER_NAME", "Bible Study Soundtracks").strip()

SAST_TZ = timezone(timedelta(hours=2))
MODEL_NAMES = [
    name.strip()
    for name in os.environ.get(
        "GEMINI_MODEL_FALLBACKS",
        "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.1-flash,gemini-3.5-flash",
    ).split(",")
    if name.strip()
]
if not MODEL_NAMES:
    raise RuntimeError("GEMINI_MODEL_FALLBACKS contains no model names.")


def ensure_future_sast_iso(raw_sched_str):
    """Return a valid future timestamp in South African Standard Time."""
    now = datetime.now(SAST_TZ)
    minimum = now + timedelta(minutes=15)

    if not raw_sched_str:
        return (now + timedelta(minutes=30)).isoformat(timespec="seconds")

    try:
        parsed = datetime.fromisoformat(raw_sched_str.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SAST_TZ)
        parsed = parsed.astimezone(SAST_TZ)
        if parsed <= minimum:
            parsed = now + timedelta(minutes=30)
            print(f"Scheduled time was too soon; moved to {parsed.isoformat(timespec='seconds')}.")
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        fallback = now + timedelta(minutes=30)
        print(f"Could not parse scheduled date '{raw_sched_str}'; using {fallback.isoformat()}.")
        return fallback.isoformat(timespec="seconds")


def youtube_video_id(url):
    """Extract a YouTube video ID from common watch, short and shorts URLs."""
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()

    if host in ("youtu.be", "www.youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
            return parts[1]
    # Also allow a bare video ID.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    return ""


def send_review_notification(
    row_idx,
    study_title,
    youtube_url,
    scheduled_sast="",
    raise_on_missing_secrets=False,
):
    """Send an HTML Gmail review alert with clickable video preview and direct Approve/Reject buttons."""
    video_id = youtube_video_id(youtube_url)
    thumb_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
    base_url = REVIEW_WEBAPP_URL.rstrip("?")
    separator = "&" if "?" in base_url else "?"
    approve_link = f"{base_url}{separator}row={row_idx}&action=approve" if base_url else youtube_url
    reject_link = f"{base_url}{separator}row={row_idx}&action=reject" if base_url else youtube_url

    print(f"Approve link: {approve_link}")

    if not (NOTIFY_EMAIL and SMTP_USER and SMTP_PASS):
        msg = "SMTP secrets (NOTIFY_EMAIL, SMTP_USER, SMTP_PASS) not configured."
        if raise_on_missing_secrets:
            raise RuntimeError(msg)
        print(f"{msg} Skipping email notification.")
        return

    safe_title = html.escape(study_title)
    safe_sched = html.escape(scheduled_sast or "Next available slot")

    message = EmailMessage()
    message["Subject"] = f"🎬 Review Ready: {study_title} (Row {row_idx})"
    message["From"] = SMTP_USER
    message["To"] = NOTIFY_EMAIL
    message.set_content(
        f"Your Bible study video '{study_title}' (row {row_idx}) has been uploaded as Unlisted.\n"
        f"Scheduled Publish (SAST): {scheduled_sast or 'Next available slot'}\n\n"
        f"1. Watch Video on YouTube:\n{youtube_url}\n\n"
        f"2. Approve & Schedule:\n{approve_link}\n\n"
        f"3. Reject:\n{reject_link}\n"
    )

    thumb_block = (
        f'<a href="{youtube_url}" target="_blank" style="display:block;margin:18px 0;text-decoration:none;">'
        f'<img src="{thumb_url}" alt="Watch Preview on YouTube" '
        f'style="width:100%;border-radius:10px 10px 0 0;border:1px solid #334155;border-bottom:none;display:block;">'
        f'<div style="background:#dc2626;color:#ffffff;text-align:center;padding:11px;border-radius:0 0 10px 10px;'
        f'font-weight:bold;font-size:14px;">▶ Click to Watch Video on YouTube</div></a>'
        if thumb_url
        else f'<p><a href="{youtube_url}" target="_blank" style="color:#38bdf8;font-weight:bold;">▶ Watch Video on YouTube</a></p>'
    )

    html_body = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;background:#0f172a;color:#f8fafc;padding:24px;border-radius:14px;max-width:560px;margin:0 auto;">
      <div style="font-size:12px;color:#38bdf8;font-weight:bold;text-transform:uppercase;letter-spacing:0.5px;">
        Row {row_idx} &bull; Approval Required
      </div>
      <h2 style="color:#f4ead4;margin:8px 0 10px;font-size:21px;">{safe_title}</h2>
      <p style="color:#cbd5e1;font-size:14px;margin:0 0 14px;">
        <strong>Target Publish (SAST):</strong> {safe_sched}
      </p>

      {thumb_block}

      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">
        <tr>
          <td width="48%" align="center" style="padding-right:6px;">
            <a href="{approve_link}" target="_blank"
               style="display:block;background:#16a34a;color:#ffffff;text-decoration:none;padding:14px 12px;border-radius:8px;font-weight:bold;font-size:15px;">
              &#9989; Approve &amp; Schedule
            </a>
          </td>
          <td width="48%" align="center" style="padding-left:6px;">
            <a href="{reject_link}" target="_blank"
               style="display:block;background:#dc2626;color:#ffffff;text-decoration:none;padding:14px 12px;border-radius:8px;font-weight:bold;font-size:15px;">
              &#10060; Reject
            </a>
          </td>
        </tr>
      </table>
    </div>
    """
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(message)
    print(f"HTML review notification sent to {NOTIFY_EMAIL}.")


# --- 2. CONNECT TO GOOGLE SHEETS, DRIVE AND YOUTUBE ---
SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
sheets_creds = ServiceAccountCredentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SHEETS_SCOPES
)
gc = gspread.authorize(sheets_creds)
sheet = gc.open("Bible study list").sheet1


def sheets_call_with_retry(fn, *args, **kwargs):
    """Retry Google Sheets API calls on 429 quota or transient 5xx errors."""
    for attempt in range(1, 7):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("429", "Quota exceeded", "500", "502", "503", "504")):
                delay = min(15 * attempt, 75)
                print(f"Sheets API rate/server limit ({msg[:120]}); retrying in {delay}s (attempt {attempt}/6)...")
                time.sleep(delay)
                continue
            raise
    return fn(*args, **kwargs)


# Ensure operational columns exist without changing existing column order.
headers = sheets_call_with_retry(sheet.row_values, 1)


def find_column(header_name):
    wanted = header_name.strip().casefold()
    for index, value in enumerate(headers, start=1):
        if str(value).strip().casefold() == wanted:
            return index
    return None


def ensure_column(header_name):
    column = find_column(header_name)
    if column is not None:
        return column
    column = len(headers) + 1
    sheets_call_with_retry(sheet.update_cell, 1, column, header_name)
    headers.append(header_name)
    print(f"Added missing spreadsheet header: {header_name} (column {column}).")
    return column


STATUS_COL = ensure_column("Video Status")
SCHEDULE_COL = ensure_column("Scheduled Date")
FORMAT_COL = ensure_column("Video Format")
YOUTUBE_URL_COL = ensure_column("YouTube URL")

records = sheet.get_all_records()
drive_service = build("drive", "v3", credentials=sheets_creds)
yt_creds = UserCredentials.from_authorized_user_info(YOUTUBE_TOKEN_INFO)
youtube = build("youtube", "v3", credentials=yt_creds)


def value_from_row(row, *possible_names, default=""):
    """Case-insensitive spreadsheet field lookup."""
    lowered = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in possible_names:
        key = name.strip().casefold()
        if key in lowered and lowered[key] is not None:
            return lowered[key]
    return default


def get_fallback_drive_asset(folder_name, row_index):
    """Find a Drive folder by name and deterministically pick a file by row_index."""
    if not folder_name:
        return None, None
    try:
        escaped_folder = folder_name.replace("\\", "\\\\").replace("'", "\\'")
        q_folder = (
            f"name = '{escaped_folder}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        folders = (
            drive_service.files()
            .list(q=q_folder, fields="files(id,name)", pageSize=1)
            .execute()
            .get("files", [])
        )
        if not folders:
            print(f"WARNING: Fallback Drive folder '{folder_name}' not found.")
            return None, None

        folder_id = folders[0]["id"]
        q_files = (
            f"'{folder_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        files = (
            drive_service.files()
            .list(q=q_files, orderBy="name", fields="files(id,name)", pageSize=100)
            .execute()
            .get("files", [])
        )
        if not files:
            print(f"WARNING: Fallback Drive folder '{folder_name}' is empty.")
            return None, None

        chosen = files[row_index % len(files)]
        print(f"Selected rotating Drive asset from '{folder_name}': {chosen['name']} ({chosen['id']}).")
        return chosen["id"], chosen["name"]
    except Exception as exc:
        print(f"WARNING: Could not list fallback Drive folder '{folder_name}': {exc}")
        return None, None


def download_drive_asset(file_ref, out_prefix, fallback_folder_name="", row_index=0):
    """Download a Drive ID, share URL, exact filename, or rotate from fallback_folder_name."""
    value = str(file_ref or "").strip()
    if value.casefold() == "none":
        return ""
    if value and os.path.isfile(value):
        return value

    file_id = None
    original_name = "asset.bin"
    match = re.search(r"/d/([A-Za-z0-9_-]{20,})", value) or re.search(
        r"[?&]id=([A-Za-z0-9_-]{20,})", value
    )
    if match:
        file_id = match.group(1)
    elif re.fullmatch(r"[A-Za-z0-9_-]{25,}", value):
        file_id = value

    try:
        if file_id:
            metadata = drive_service.files().get(fileId=file_id, fields="name").execute()
            original_name = metadata.get("name", "asset.bin")
        elif value:
            escaped_name = value.replace("\\", "\\\\").replace("'", "\\'")
            result = drive_service.files().list(
                q=f"name = '{escaped_name}' and trashed = false",
                fields="files(id,name)",
                pageSize=1,
            ).execute()
            matches = result.get("files", [])
            if matches:
                file_id = matches[0]["id"]
                original_name = matches[0]["name"]

        # If cell was blank or contained a descriptive creative prompt instead of a Drive file,
        # automatically rotate through the designated Google Drive library folder.
        if not file_id and fallback_folder_name:
            if value:
                print(f"Drive asset '{value[:60]}...' is a text prompt; rotating from '{fallback_folder_name}'.")
            file_id, original_name = get_fallback_drive_asset(fallback_folder_name
