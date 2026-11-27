import asyncio
import functools
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


def send_review_notification(row_idx, study_title, youtube_url, raise_on_missing_secrets=False):
    """Send an optional Gmail review notification; email failure won't undo the upload."""
    review_link = f"{REVIEW_WEBAPP_URL}?row={row_idx}" if REVIEW_WEBAPP_URL else youtube_url
    print(f"Review link: {review_link}")

    if not (NOTIFY_EMAIL and SMTP_USER and SMTP_PASS):
        msg = "SMTP secrets (NOTIFY_EMAIL, SMTP_USER, SMTP_PASS) not configured."
        if raise_on_missing_secrets:
            raise RuntimeError(msg)
        print(f"{msg} Skipping email notification.")
        return

    message = EmailMessage()
    message["Subject"] = f"🎬 Review Ready: {study_title} (Row {row_idx})"
    message["From"] = SMTP_USER
    message["To"] = NOTIFY_EMAIL
    message.set_content(
        f"Your Bible study video '{study_title}' (row {row_idx}) has been uploaded as Unlisted.\n\n"
        f"Review portal:\n{review_link}\n\n"
        f"Unlisted YouTube video:\n{youtube_url}\n"
    )

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(message)
    print(f"Review notification sent to {NOTIFY_EMAIL}.")


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


def download_drive_asset(file_ref, out_prefix):
    """Download a Drive ID, share URL or exact filename; return local path or empty string."""
    value = str(file_ref or "").strip()
    if not value or value.casefold() == "none":
        return ""
    if os.path.isfile(value):
        return value

    file_id = None
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
        else:
            escaped_name = value.replace("\\", "\\\\").replace("'", "\\'")
            result = drive_service.files().list(
                q=f"name = '{escaped_name}' and trashed = false",
                fields="files(id,name)",
                pageSize=1,
            ).execute()
            matches = result.get("files", [])
            if not matches:
                print(f"WARNING: Drive asset '{value}' was not found.")
                return ""
            file_id = matches[0]["id"]
            original_name = matches[0]["name"]

        extension = os.path.splitext(original_name)[1] or ".bin"
        local_path = f"{out_prefix}{extension}"
        request = drive_service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as output:
            downloader = MediaIoBaseDownload(output, request)
            complete = False
            while not complete:
                _, complete = downloader.next_chunk()
        print(f"Downloaded Drive asset '{original_name}' to {local_path}.")
        return local_path
    except Exception as exc:
        print(f"WARNING: Could not download Drive asset '{value}': {exc}")
        return ""


# --- 2b. NOTIFICATION-ONLY DISPATCH FOR ROWS IN REVIEW ---
if DISPATCH_ACTION == "send_approval_notifications":
    sent_count = 0
    for row_number, row in enumerate(records, start=2):
        status = str(value_from_row(row, "Video Status")).strip().casefold()
        if status != "review":
            continue

        study_title = str(value_from_row(row, "Title", default="Untitled Study")).strip()
        youtube_url = str(value_from_row(row, "YouTube URL")).strip()
        print(f"Sending approval notification for row {row_number}: {study_title}")
        send_review_notification(
            row_number,
            study_title,
            youtube_url,
            raise_on_missing_secrets=True,
        )
        sent_count += 1

    if sent_count == 0:
        print("No rows marked Review. Nothing to notify.")
    else:
        print(f"Successfully sent {sent_count} approval notification(s).")
    sys.exit(0)


# --- 3. STAGE 2: SCHEDULE AN APPROVED VIDEO BEFORE RENDERING A NEW ONE ---
for row_number, row in enumerate(records, start=2):
    status = str(value_from_row(row, "Video Status")).strip().casefold()
    if status != "approved":
        continue

    video_url = str(value_from_row(row, "YouTube URL")).strip()
    video_id = youtube_video_id(video_url)
    if not video_id:
        print(f"Row {row_number} is Approved but has no valid YouTube URL; skipping.")
        continue

    scheduled_at = ensure_future_sast_iso(
        str(value_from_row(row, "Scheduled Date")).strip()
    )
    print(f"Scheduling approved row {row_number}, video {video_id}, for {scheduled_at}.")

    youtube.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": scheduled_at,
                "selfDeclaredMadeForKids": False,
            },
        },
    ).execute()

    sheets_call_with_retry(
        sheet.batch_update,
        [
            {"range": gspread.utils.rowcol_to_a1(row_number, SCHEDULE_COL), "values": [[scheduled_at]]},
            {"range": gspread.utils.rowcol_to_a1(row_number, STATUS_COL), "values": [["Scheduled"]]},
        ],
    )
    print(f"Row {row_number} successfully scheduled.")
    sys.exit(0)


# --- 4. STAGE 1: SELECT THE EARLIEST READY STUDY ---
ready_rows = []
for row_number, row in enumerate(records, start=2):
    if str(value_from_row(row, "Video Status")).strip().casefold() == "ready":
        schedule_text = str(value_from_row(row, "Scheduled Date")).strip()
        ready_rows.append((schedule_text or "9999-12-31T23:59:59+02:00", row_number, row))

if not ready_rows:
    print("No rows marked Ready or Approved. Nothing to do.")
    sys.exit(0)

ready_rows.sort(key=lambda item: item[0])
_, target_row, study = ready_rows[0]

study_title = str(value_from_row(study, "Title", default="Untitled Study")).strip()
study_scripture = str(value_from_row(study, "Scripture")).strip()
study_summary = str(value_from_row(study, "Summary")).strip()
study_details = str(value_from_row(study, "Details")).strip()

video_format = str(value_from_row(study, "Video Format", default="Devotional")).strip()
is_short = video_format.casefold() == "short"
creative_brief = str(value_from_row(study, "Creative Brief")).strip()
visual_theme = str(value_from_row(study, "Visual Theme", default="navy_gold")).strip().lower()
voice_choice = str(value_from_row(study, "Voice Style", default="Luke")).strip().lower()
music_ref = str(value_from_row(study, "Music Track")).strip()
background_ref = str(value_from_row(study, "Background Image")).strip()

print(f"Rendering row {target_row}: {study_title} ({video_format}).")

local_background = download_drive_asset(background_ref, "drive_background")
local_music = download_drive_asset(music_ref, "drive_music")

# --- 5. GENERATE THE DEVOTIONAL SCRIPT WITH GEMINI ---
client = genai.Client(api_key=GEMINI_API_KEY)
length_guidance = (
    "45 to 55 seconds, approximately 115-135 words."
    if is_short else "3 to 4 minutes, approximately 450-550 words."
)

prompt = f"""
You are a reverent, Christ-centred Bible teacher creating a {video_format} devotional.
Write a cohesive, warm spoken script based only on the Bible study supplied below.

Creative direction: {creative_brief or "Warm, reverent, clear teaching ending with a short prayer."}
Target length: {length_guidance}

Requirements:
- narration must be natural spoken prose only: no stage directions, bullets or Markdown.
- card_subtitle must be a concise 1-2 sentence takeaway or anchor verse, under 28 words.
- Preserve the study's theological meaning and Scripture references. Do not invent quotations
  or claim that a paraphrase is a direct Bible quotation.
- Create a suitable YouTube title and a helpful description.

Study title: {study_title}
Scripture: {study_scripture}
Summary: {study_summary}
Detailed notes: {study_details}

Return only valid JSON:
{{
  "youtube_title": "Compelling title | Scripture Study",
  "youtube_description": "YouTube description with Scripture references and prayer",
  "card_subtitle": "Short anchor Scripture or takeaway",
  "narration": "Complete spoken devotional script"
}}
"""


def discover_available_flash_models():
    discovered = []
    try:
        for model in client.models.list():
            name = getattr(model, "name", "") or ""
            methods = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", None
            ) or []
            if methods and "generateContent" not in methods:
                continue
            clean_name = name.removeprefix("models/")
            if "flash" in clean_name.lower() and "image" not in clean_name.lower() and "audio" not in clean_name.lower():
                discovered.append(clean_name)
    except Exception as exc:
        print(f"Warning: could not list Gemini models dynamically: {exc}")
    return discovered


def generate_with_fallback(prompt_text):
    candidates = list(dict.fromkeys(MODEL_NAMES))
    last_error = None
    discovered_checked = False
    attempt = 0

    while attempt < 10:
        if not candidates and not discovered_checked:
            discovered_checked = True
            discovered = discover_available_flash_models()
            if discovered:
                print(f"Discovered available Gemini models: {discovered}")
                candidates.extend(discovered)
        if not candidates:
            break

        model_name = candidates.pop(0)
        attempt += 1
        try:
            print(f"Calling Gemini ({model_name}), attempt {attempt}...")
            result = client.models.generate_content(
                model=model_name,
                contents=prompt_text,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            if not getattr(result, "text", None):
                raise RuntimeError(f"{model_name} returned an empty response.")
            return result
        except Exception as exc:
            last_error = exc
            message = str(exc)
            print(f"Gemini attempt {attempt} ({model_name}) failed: {message}")
            if "404" in message or "NOT_FOUND" in message:
                continue
            if any(code in message for code in (
                "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                "500", "INTERNAL", "502", "BAD_GATEWAY",
            )):
                delay = min(5 * (2 ** min(attempt - 1, 4)), 60)
                print(f"Temporary Gemini error; waiting {delay}s before retry.")
                time.sleep(delay)
                candidates.append(model_name)
                continue
            raise
    raise RuntimeError(f"Gemini failed after {attempt} attempts. Last error: {last_error}")


def parse_json_response(raw_text):
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


script_response = generate_with_fallback(prompt)
script_data = parse_json_response(script_response.text)
narration = str(script_data.get("narration", "")).strip()
if not narration:
    raise RuntimeError("Gemini returned no narration; refusing to create a silent video.")

youtube_title = str(script_data.get("youtube_title") or study_title).strip()[:100]
youtube_description = str(
    script_data.get("youtube_description")
    or f"{study_title}\n\nScripture: {study_scripture}\n\nA Bible study devotional."
).strip()
card_subtitle = str(script_data.get("card_subtitle") or study_scripture).strip()


# --- 6. SYNTHESIZE VOICEOVER ---
VOICE_MAP = {
    "luke": "en-ZA-Luke
