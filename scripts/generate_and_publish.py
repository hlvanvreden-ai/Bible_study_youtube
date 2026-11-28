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
            file_id, original_name = get_fallback_drive_asset(fallback_folder_name, row_index)

        if not file_id:
            return ""

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


# --- 2b. NOTIFICATION-ONLY DISPATCH FOR ROWS IN PENDING REVIEW ---
if DISPATCH_ACTION == "send_approval_notifications":
    sent_count = 0
    for row_number, row in enumerate(records, start=2):
        status = str(value_from_row(row, "Video Status")).strip().casefold()
        if status not in ("pending review", "review"):
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
        print("No rows marked Pending Review. Nothing to notify.")
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
                "embeddable": True,
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

local_background = download_drive_asset(
    background_ref, "drive_background", fallback_folder_name=BG_FOLDER_NAME, row_index=target_row
)
local_music = download_drive_asset(
    music_ref, "drive_music", fallback_folder_name=MUSIC_FOLDER_NAME, row_index=target_row
)

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
    "luke": "en-ZA-LukeNeural",
    "leah": "en-ZA-LeahNeural",
    "christopher": "en-US-ChristopherNeural",
    "andrew": "en-US-AndrewNeural",
    "aria": "en-US-AriaNeural",
    "jenny": "en-US-JennyNeural",
}
selected_voice = VOICE_MAP.get(voice_choice, "en-ZA-LukeNeural")
VOICE_FALLBACKS = {
    "en-ZA-LukeNeural": "en-US-ChristopherNeural",
    "en-ZA-LeahNeural": "en-US-AriaNeural",
    "en-US-ChristopherNeural": "en-US-AndrewNeural",
    "en-US-AndrewNeural": "en-US-ChristopherNeural",
    "en-US-AriaNeural": "en-US-JennyNeural",
    "en-US-JennyNeural": "en-US-AriaNeural",
}


def clean_narration(raw_text):
    value = re.sub(r"[*_#`~<>]", "", str(raw_text or ""))
    return re.sub(r"\s+", " ", value).strip()


async def synthesize_voiceover(text, output_path):
    cleaned = clean_narration(text)
    if not cleaned:
        raise RuntimeError("Narration was empty after text cleanup.")
    fallback_voice = VOICE_FALLBACKS.get(selected_voice, "en-US-ChristopherNeural")

    for attempt in range(1, 5):
        voice = selected_voice if attempt <= 2 else fallback_voice
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            print(f"Synthesizing voiceover ({voice}), attempt {attempt}/4...")
            communicator = edge_tts.Communicate(cleaned, voice, rate="-4%")
            await communicator.save(output_path)
            if os.path.isfile(output_path) and os.path.getsize(output_path) > 1000:
                return
            raise RuntimeError("TTS returned an empty or unusually small audio file.")
        except Exception as exc:
            print(f"TTS attempt {attempt}/4 failed: {exc}")
            if attempt < 4:
                await asyncio.sleep(attempt * 3)
    raise RuntimeError("Edge TTS failed after four attempts.")


VOICE_PATH = "voiceover.mp3"
asyncio.run(synthesize_voiceover(narration, VOICE_PATH))


# --- 7. BUILD A SINGLE-SLIDE VIDEO CARD ---
PALETTES = {
    "navy_gold": ((20, 30, 48), "#F4EAD4", "#FFFFFF"),
    "forest_cream": ((24, 42, 34), "#E9DFC8", "#F8F9FA"),
    "burgundy_warm": ((48, 22, 28), "#F5E6CC", "#FFFFFF"),
    "midnight_amber": ((15, 23, 42), "#FBBF24", "#F8FAFC"),
    "royal_purple": ((36, 20, 54), "#F3E8FF", "#FFFFFF"),
    "olive_parchment": ((42, 48, 32), "#FAF3E0", "#FFFFFF"),
    "slate_silver": ((30, 41, 59), "#E2E8F0", "#FFFFFF"),
    "crimson_gold": ((69, 10, 10), "#FDE68A", "#FFFFFF"),
    "teal_sand": ((17, 48, 52), "#FEF3C7", "#FFFFFF"),
    "charcoal_copper": ((28, 25, 23), "#FED7AA", "#FAFAF9"),
    "indigo_pearl": ((30, 27, 75), "#E0E7FF", "#FFFFFF"),
    "cedar_linen": ((54, 34, 24), "#F5EBE0", "#FFFFFF"),
    "ocean_mist": ((12, 45, 72), "#BAE6FD", "#FFFFFF"),
}
bg_rgb, heading_color, body_color = PALETTES.get(
    visual_theme, PALETTES["navy_gold"]
)


def load_font(size, bold=False):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ] if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def wrap_to_width(draw, text, font, max_width):
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def make_video_card(output_path, background_path=""):
    width, height = (1080, 1920) if is_short else (1920, 1080)
    base_color = tuple(int(channel) for channel in bg_rgb)

    if background_path:
        try:
            with Image.open(background_path) as source:
                canvas = ImageOps.fit(source.convert("RGB"), (width, height))
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 125))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        except Exception as exc:
            print(f"WARNING: Background image could not be used: {exc}")
            canvas = Image.new("RGB", (width, height), base_color)
    else:
        canvas = Image.new("RGB", (width, height), base_color)

    draw = ImageDraw.Draw(canvas)
    margin = int(width * 0.10)
    max_text_width = width - 2 * margin

    # Subtle frame and accent line.
    accent = heading_color
    draw.rounded_rectangle(
        (int(width * 0.045), int(height * 0.045),
         int(width * 0.955), int(height * 0.955)),
        radius=max(18, int(width * 0.018)),
        outline=accent,
        width=max(2, int(width * 0.0025)),
    )
    draw.rounded_rectangle(
        (margin, int(height * 0.27), margin + int(width * 0.12),
         int(height * 0.277)),
        radius=4,
        fill=accent,
    )

    title_font = load_font(int(width * (0.047 if is_short else 0.050)), bold=True)
    subtitle_font = load_font(int(width * (0.035 if is_short else 0.032)))
    scripture_font = load_font(int(width * 0.022))
    footer_font = load_font(int(width * 0.018))

    title_lines = wrap_to_width(draw, study_title, title_font, max_text_width)
    subtitle_lines = wrap_to_width(draw, card_subtitle, subtitle_font, max_text_width)
    scripture_text = study_scripture or "A Bible Study"

    title_line_height = int(title_font.size * 1.25)
    subtitle_line_height = int(subtitle_font.size * 1.45)
    title_block_height = title_line_height * len(title_lines)
    subtitle_block_height = subtitle_line_height * len(subtitle_lines)
    combined_height = title_block_height + int(height * 0.045) + subtitle_block_height
    y = int((height - combined_height) / 2) - int(height * 0.015)

    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=heading_color)
        y += title_line_height

    y += int(height * 0.045)
    for line in subtitle_lines:
        draw.text((margin, y), line, font=subtitle_font, fill=body_color)
        y += subtitle_line_height

    scripture_y = int(height * 0.79)
    for line in wrap_to_width(draw, scripture_text, scripture_font, max_text_width)[:2]:
        draw.text((margin, scripture_y), line, font=scripture_font, fill=heading_color)
        scripture_y += int(scripture_font.size * 1.4)

    footer = "BIBLE STUDY  •  DEVOTIONAL"
    draw.text(
        (margin, int(height * 0.90)),
        footer,
        font=footer_font,
        fill=body_color,
    )
    canvas.save(output_path, format="PNG", optimize=True)


CARD_PATH = "video_card.png"
make_video_card(CARD_PATH, local_background)

# --- 8. ASSEMBLE VIDEO AND AUDIO ---
VIDEO_PATH = "bible_study_video.mp4"
voice_clip = None
music_clip = None
final_audio = None
video_clip = None

try:
    voice_clip = AudioFileClip(VOICE_PATH)
    duration = max(voice_clip.duration + 1.5, 5.0)
    audio_layers = [voice_clip]

    if local_music and os.path.isfile(local_music):
        try:
            music_clip = AudioFileClip(local_music)
            music_loop = audio_loop(music_clip, duration=duration).volumex(0.12)
            audio_layers.append(music_loop)
        except Exception as exc:
            print(f"WARNING: Background music could not be added: {exc}")

    final_audio = CompositeAudioClip(audio_layers).set_duration(duration)
    video_clip = (
        ImageClip(np.array(Image.open(CARD_PATH).convert("RGB")))
        .set_duration(duration)
        .set_audio(final_audio)
    )

    print(f"Rendering {VIDEO_PATH} ({duration:.1f} seconds)...")
    video_clip.write_videofile(
        VIDEO_PATH,
        fps=30,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=2,
        temp_audiofile="video_temp_audio.m4a",
        remove_temp=True,
        logger="bar",
    )
finally:
    if video_clip is not None:
        video_clip.close()
    if final_audio is not None:
        final_audio.close()
    if music_clip is not None:
        music_clip.close()
    if voice_clip is not None:
        voice_clip.close()

if not os.path.isfile(VIDEO_PATH) or os.path.getsize(VIDEO_PATH) < 10_000:
    raise RuntimeError("Video render did not produce a valid MP4 file.")


# --- 9. UPLOAD TO YOUTUBE AS UNLISTED FOR REVIEW ---
video_body = {
    "snippet": {
        "title": youtube_title,
        "description": youtube_description,
        "categoryId": "22",
        "defaultLanguage": "en",
        "tags": ["Bible Study", "Christian Devotional", "Scripture", "Prayer"],
    },
    "status": {
        "privacyStatus": "unlisted",
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
    },
}
media = MediaFileUpload(
    VIDEO_PATH,
    mimetype="video/mp4",
    resumable=True,
    chunksize=8 * 1024 * 1024,
)
upload_request = youtube.videos().insert(
    part="snippet,status",
    body=video_body,
    media_body=media,
)
upload_response = None
while upload_response is None:
    upload_status, upload_response = upload_request.next_chunk(num_retries=5)
    if upload_status is not None:
        print(f"YouTube upload progress: {int(upload_status.progress() * 100)}%")

video_id = upload_response.get("id") if upload_response else None
if not video_id:
    raise RuntimeError(f"YouTube upload returned no video ID: {upload_response}")

youtube_url = f"https://www.youtube.com/watch?v={video_id}"
sheets_call_with_retry(
    sheet.batch_update,
    [
        {"range": gspread.utils.rowcol_to_a1(target_row, YOUTUBE_URL_COL), "values": [[youtube_url]]},
        {"range": gspread.utils.rowcol_to_a1(target_row, STATUS_COL), "values": [["Pending Review"]]},
    ],
)
print(f"Uploaded video as Unlisted (Pending Review): {youtube_url}")

try:
    send_review_notification(target_row, study_title, youtube_url)
except Exception as exc:
    # The video and sheet update have succeeded; don't mark the whole run failed
    # solely because the optional email notification could not be delivered.
    print(f"WARNING: Video uploaded, but review email failed: {exc}")

print(f"Completed successfully for spreadsheet row {target_row}.")
