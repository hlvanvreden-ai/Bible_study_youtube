import functools
import io
import os
import re
import time
import json
import asyncio
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import gspread
import edge_tts
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
)
from moviepy.audio.fx.all import audio_loop

print = functools.partial(print, flush=True)

# --- 1. LOAD ENVIRONMENT SECRETS ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
YOUTUBE_TOKEN_INFO = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])

REVIEW_WEBAPP_URL = os.environ.get("REVIEW_WEBAPP_URL", "").strip()
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").strip()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()

SAST_TZ = timezone(timedelta(hours=2))


def ensure_future_sast_iso(raw_sched_str):
    """Ensures Scheduled Date is a valid future ISO-8601 timestamp in SAST (+02:00)."""
    now_sast = datetime.now(SAST_TZ)
    min_future = now_sast + timedelta(minutes=15)

    if not raw_sched_str:
        return (now_sast + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+02:00")

    try:
        parsed = datetime.fromisoformat(raw_sched_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SAST_TZ)
        if parsed <= min_future:
            bumped = (now_sast + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+02:00")
            print(f"Scheduled Date '{raw_sched_str}' was in the past; auto-bumped to {bumped}.")
            return bumped
        return raw_sched_str
    except ValueError:
        return (now_sast + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+02:00")


def send_review_notification(row_idx, study_title, youtube_url):
    """Sends a review alert via Gmail STARTTLS (port 587)."""
    review_link = f"{REVIEW_WEBAPP_URL}?row={row_idx}" if REVIEW_WEBAPP_URL else youtube_url
    print(f"Review portal ready: {review_link}")

    if not (NOTIFY_EMAIL and SMTP_USER and SMTP_PASS):
        print("SMTP email secrets not set; skipping email dispatch.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🎬 Review Ready: {study_title} (Row {row_idx})"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    msg.set_content(
        f"Your Bible study video '{study_title}' (Row {row_idx}) has been rendered and staged as Unlisted.\n\n"
        f"Open Review & Approval Portal:\n{review_link}\n\n"
        f"Direct Unlisted YouTube Link:\n{youtube_url}\n"
    )

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    print(f"Review notification email sent to {NOTIFY_EMAIL}.")


# --- 2. CONNECT TO GOOGLE SHEETS, DRIVE & YOUTUBE ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
sheets_creds = ServiceAccountCredentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)
gc = gspread.authorize(sheets_creds)
sheet = gc.open("Bible study list").sheet1
records = sheet.get_all_records()

drive_service = build("drive", "v3", credentials=sheets_creds)

yt_creds = UserCredentials.from_authorized_user_info(YOUTUBE_TOKEN_INFO)
youtube = build("youtube", "v3", credentials=yt_creds)


def download_drive_asset(file_ref, out_prefix):
    """Downloads a Google Drive File ID, share URL, or exact filename to the runner VM."""
    val = str(file_ref or "").strip()
    if not val or val.lower() == "none":
        return ""
    if os.path.isfile(val):
        return val

    file_id = None
    url_match = re.search(r"/d/([a-zA-Z0-9_-]{20,})", val) or re.search(
        r"id=([a-zA-Z0-9_-]{20,})", val
    )
    if url_match:
        file_id = url_match.group(1)
    elif re.fullmatch(r"[a-zA-Z0-9_-]{25,}", val):
        file_id = val

    try:
        if not file_id:
            safe_name = val.replace("'", "\\'")
            res = (
                drive_service.files()
                .list(
                    q=f"name = '{safe_name}' and trashed = false",
                    fields="files(id, name)",
                    pageSize=1,
                )
                .execute()
            )
            found = res.get("files", [])
            if not found:
                print(f"WARNING: Drive asset '{val}' not found.")
                return ""
            file_id = found[0]["id"]
            orig_name = found[0]["name"]
        else:
            meta = drive_service.files().get(fileId=file_id, fields="name").execute()
            orig_name = meta.get("name", "asset.bin")

        ext = os.path.splitext(orig_name)[1] or ".bin"
        local_path = f"{out_prefix}{ext}"

        request = drive_service.files().get_media(fileId=file_id)
        with io.FileIO(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        print(f"Downloaded Drive asset '{orig_name}' ({file_id}) -> {local_path}")
        return local_path
    except Exception as e:
        print(f"WARNING: Failed to download Drive asset '{val}': {e}")
        return ""


# --- 3. STAGE 2 CHECK: SCHEDULE ANY 'APPROVED' VIDEOS FIRST ---
for idx, row in enumerate(records, start=2):
    if str(row.get("Video Status", "")).strip().lower() == "approved":
        yt_url = str(row.get("YouTube URL", "")).strip()
        raw_sched = str(row.get("Scheduled Date", "")).strip()

        if not yt_url:
            print(f"Row {idx} is marked 'Approved' but has no YouTube URL. Skipping.")
            continue

        scheduled_date = ensure_future_sast_iso(raw_sched)
        if scheduled_date != raw_sched:
            sheet.update_cell(idx, 8, scheduled_date)

        video_id = yt_url.rstrip("/").split("/")[-1].split("?")[0]
        print(f"Scheduling Approved Row {idx} (Video ID: {video_id}) for {scheduled_date}...")

        youtube.videos().update(
            part="status",
            body={
                "id": video_id,
                "status": {
                    "privacyStatus": "private",
                    "publishAt": scheduled_date,
                    "selfDeclaredMadeForKids": False,
                },
            },
        ).execute()

        sheet.update_cell(idx, 7, "Scheduled")
        print(f"Row {idx} successfully scheduled on YouTube for {scheduled_date}!")
        exit(0)

# --- 4. STAGE 1: PICK EARLIEST 'READY' ROW FOR RENDERING ---
ready_rows = []
for idx, row in enumerate(records, start=2):
    if str(row.get("Video Status", "")).strip().lower() == "ready":
        sched_str = str(row.get("Scheduled Date", "")).strip()
        ready_rows.append((sched_str, idx, row))

if not ready_rows:
    print("No rows marked as 'Approved' or 'Ready'. Exiting cleanly.")
    exit(0)

ready_rows.sort(key=lambda x: x[0] if x[0] else "9999-12-31T23:59:59+02:00")
_, target_row_index, study = ready_rows[0]
study_title = str(study.get("Title") or study.get("title") or "Untitled Study").strip()
study_scripture = str(study.get("Scripture") or study.get("scripture") or "").strip()
print(f"Rendering Single-Slide Stage 1 for Row {target_row_index}: {study_title}")

# --- 5. READ CREATIVE CONTROLS & DOWNLOAD ROTATED DRIVE MEDIA ---
video_format = str(study.get("Video Format", "Devotional")).strip()
is_short = video_format.lower() == "short"

creative_brief = str(study.get("Creative Brief", "")).strip()
visual_theme = str(study.get("Visual Theme", "navy_gold")).strip().lower()
voice_choice = str(study.get("Voice Style", "Luke")).strip().lower()
music_track_ref = str(study.get("Music Track", "")).strip()
bg_image_ref = str(study.get("Background Image", "")).strip()

local_bg_image = download_drive_asset(bg_image_ref, "drive_bg_image")
local_music_path = download_drive_asset(music_track_ref, "drive_music_track")

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

# --- 6. GENERATE SPOKEN DEVOTIONAL SCRIPT WITH GEMINI (WITH RETRY) ---
client = genai.Client(api_key=GEMINI_API_KEY)

length_guidance = (
    "45 to 55 seconds (roughly 115-135 words)."
    if is_short
    else "3 to 4 minutes (roughly 450-550 words)."
)

prompt = f"""
You are a reverent, Christ-centered Bible teacher creating a {video_format} audio devotional.
Write a cohesive, warm spoken script based on the study below.

Creative Director Instructions: {creative_brief if creative_brief else "Warm, reverent, clear teaching ending with a short prayer."}
Target Length: {length_guidance}

Rules:
- 'narration' must be natural spoken prose ONLY—no stage directions, no bullet symbols, no markdown formatting.
- 'card_subtitle' must be a concise 1-to-2 sentence summary or anchor verse (under 28 words) to display cleanly on the video card.

Title: {study_title}
Scripture: {study_scripture}
Summary: {study.get('Summary') or study.get('summary') or ''}
Detailed Notes: {study.get('Details') or study.get('details') or ''}

Return ONLY valid JSON matching this schema:
{{
  "youtube_title": "Compelling Title | Scripture Study",
  "youtube_description": "Full YouTube description with scripture references and prayer...",
  "card_subtitle": "Short anchor scripture or takeaway for the screen",
  "narration": "Complete spoken devotional script..."
}}
"""

# Prioritize high-availability GA models first to avoid 503 preview spikes
models_to_try = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3-flash",
]
response = None

for attempt in range(1, 9):
    model_name = models_to_try[(attempt - 1) % len(models_to_try)]
    try:
        print(f"Calling Gemini ({model_name}), attempt {attempt}/8...")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        break
    except Exception as e:
        err_str = str(e)
        print(f"Attempt {attempt} ({model_name}) failed: {e}")
        if "404" in err_str or "NOT_FOUND" in err_str:
            # Skip immediately to next model without sleeping
            continue
        if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")):
            time.sleep(min(attempt * 8, 30))
        else:
            raise

if response is None:
    raise RuntimeError("All Gemini 3 models remained temporarily unavailable after 8 attempts.")


script_data = json.loads(response.text)
narration_text = script_data.get("narration", "")
card_subtitle = script_data.get("card_subtitle", study_scripture)

# --- 7. SYNTHESIZE SINGLE AUDIO TRACK & RENDER SINGLE-SLIDE VIDEO ---
def clean_narration_for_tts(raw_text):
    text = re.sub(r"[*_#`~<>]", "", str(raw_text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else "Let us reflect on God's Word together."


async def synthesize_full_audio(text, out_file):
    cleaned = clean_narration_for_tts(text)
    fallback_voice = VOICE_FALLBACKS.get(selected_voice, "en-US-ChristopherNeural")

    for attempt in range(1, 5):
        voice_to_use = selected_voice if attempt <= 2 else fallback_voice
        try:
            print(f"Synthesizing voiceover ({voice_to_use}), attempt {attempt}/4...")
            comm = edge_tts.Communicate(cleaned, voice_to_use, rate="-4%")
            await comm.save(out_file)
            if os.path.isfile(out_file) and os.path.getsize(out_file) > 0:
                return
        except Exception as e:
            print(f"TTS attempt {attempt}/4 ({voice_to_use}) failed: {e}")
            await asyncio.sleep(attempt * 2)

    raise RuntimeError("edge-tts failed after 4 attempts.")


voice_path = "voiceover.mp3"
asyncio.run(synthesize_full_audio(narration_text, voice_path))

voice_clip = AudioFileClip(voice_path)
total_duration = voice_clip.duration + 1.5

width, height = (1080, 1920) if is_short else (1920, 1080)

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
bg_rgb, heading_color, body_color = PALETTES.get(visual_theme, PALETTES["navy_gold"])

# Build single static background layer (Rotated Drive Image + dark readability overlay)
if local_bg_image and os.path.isfile(local_bg_image):
    base_img = ImageClip(local_bg_image).resize((width, height)).set_duration(total_duration)
    dimmer = (
        ColorClip(size=(width, height), color=(12, 18, 30))
        .set_opacity(0.50)
        .set_duration(total_duration)
    )
    bg_layer = CompositeVideoClip([base_img.set_position("center"), dimmer], size=(width, height))
else:
    bg_layer = ColorClip(size=(width, height), color=bg_rgb).set_duration(total_duration)

title_clip = (
    TextClip(
        study_title.upper(),
        fontsize=60 if not is_short else 52,
        color=heading_color,
        font="DejaVu-Sans-Bold",
        method="caption",
        size=(int(width * 0.84), None),
        align="center",
    )
    .set_position(("center", int(height * 0.24)))
    .set_duration(total_duration)
)

scripture_banner = (
    TextClip(
        study_scripture,
        fontsize=34 if not is_short else 32,
        color=heading_color,
        font="DejaVu-Sans",
        method="caption",
        size=(int(width * 0.80), None),
        align="center",
    )
    .set_position(("center", int(height * 0.43)))
    .set_duration(total_duration)
)

subtitle_clip = (
    TextClip(
        card_subtitle,
        fontsize=40 if not is_short else 36,
        color=body_color,
        font="DejaVu-Sans",
        method="caption",
        size=(int(width * 0.78), None),
        align="center",
    )
    .set_position(("center", int(height * 0.56)))
    .set_duration(total_duration)
)

final_video = CompositeVideoClip(
    [bg_layer, title_clip, scripture_banner, subtitle_clip], size=(width, height)
).set_audio(voice_clip.set_start(0.5))

# Mix rotated Google Drive ambient soundtrack (Column O)
if local_music_path and os.path.isfile(local_music_path):
    print(f"Mixing background soundtrack: {local_music_path}")
    bg_music = AudioFileClip(local_music_path)
    bg_music = audio_loop(bg_music, duration=total_duration).volumex(0.12)
    mixed_audio = CompositeAudioClip([final_video.audio, bg_music])
    final_video = final_video.set_audio(mixed_audio)

output_video_path = "final_output.mp4"
print("Encoding single-slide MP4 video...")
final_video.write_videofile(
    output_video_path,
    fps=24,
    codec="libx264",
    audio_codec="aac",
    threads=4,
    preset="ultrafast",
)
print("Single-slide video rendering complete.")

# --- 8. UPLOAD TO YOUTUBE AS 'UNLISTED' FOR REVIEW ---
category_val = str(study.get("Category") or study.get("category") or "Faith").strip()
body = {
    "snippet": {
        "title": script_data["youtube_title"][:100],
        "description": script_data["youtube_description"],
        "tags": [
            "Bible Study",
            "Christian Devotional",
            "Scripture",
            category_val,
        ],
        "categoryId": "22",
    },
    "status": {
        "privacyStatus": "unlisted",
        "selfDeclaredMadeForKids": False,
    },
}

media = MediaFileUpload(
    output_video_path, chunksize=-1, resumable=True, mimetype="video/mp4"
)
request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

response_upload = None
while response_upload is None:
    status, response_upload = request.next_chunk()
    if status:
        print(f"Upload progress: {int(status.progress() * 100)}%")

video_id = response_upload["id"]
youtube_url = f"https://youtu.be/{video_id}"
print(f"Uploaded unlisted draft for review: {youtube_url}")

# --- 9. UPDATE SHEET TO 'PENDING REVIEW' & NOTIFY ---
sheet.update_cell(target_row_index, 7, "Pending Review")
sheet.update_cell(target_row_index, 10, youtube_url)
send_review_notification(target_row_index, study_title, youtube_url)
print(f"Row {target_row_index} updated to 'Pending Review'.")
