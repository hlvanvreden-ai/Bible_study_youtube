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
    VideoFileClip,
    concatenate_videoclips,
)
from moviepy.audio.fx.all import audio_loop

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
    """
    Ensures Scheduled Date is a valid future ISO-8601 timestamp in SAST (+02:00).
    If blank or already in the past when approved, bumps to now + 30 minutes SAST.
    """
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
    """Sends an HTML/text review alert via Gmail STARTTLS (port 587)."""
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
    """
    Accepts a Google Drive File ID, share URL, or exact filename in Drive,
    downloads it to the GitHub Actions VM, and returns the local file path.
    """
    val = str(file_ref or "").strip()
    if not val or val.lower() == "none":
        return ""

    # If already a valid local repo file, return as-is
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
print(f"Rendering Stage 1 for Row {target_row_index}: {study_title}")

# --- 5. READ CREATIVE CONTROL COLUMNS (K–P) & DOWNLOAD DRIVE MEDIA ---
video_format = str(study.get("Video Format", "Devotional")).strip()
is_short = video_format.lower() == "short"

creative_brief = str(study.get("Creative Brief", "")).strip()
slide_breakdown = str(study.get("Slide Breakdown", "")).strip()
visual_theme = str(study.get("Visual Theme", "navy_gold")).strip().lower()
voice_choice = str(study.get("Voice Style", "Luke")).strip().lower()
music_track_ref = str(study.get("Music Track", "")).strip()
bg_image_ref = str(study.get("Background Image", "")).strip()

print(
    f"Creative Config -> Theme: '{visual_theme}' | Voice: '{voice_choice}' | "
    f"Music Ref: '{music_track_ref}' | BgImage Ref: '{bg_image_ref}'"
)

local_bg_image = download_drive_asset(bg_image_ref, "drive_bg_image")
local_music_path = download_drive_asset(music_track_ref, "drive_music_track")

# Fallback to repo assets/music/ if a local filename was supplied
if not local_music_path and music_track_ref:
    repo_music = os.path.join("assets", "music", music_track_ref)
    if os.path.isfile(repo_music):
        local_music_path = repo_music

VOICE_MAP = {
    "luke": "en-ZA-LukeNeural",                # South African male
    "leah": "en-ZA-LeahNeural",                # South African female
    "christopher": "en-US-ChristopherNeural",  # Deep US male
    "andrew": "en-US-AndrewNeural",            # Warm US male
    "aria": "en-US-AriaNeural",                # Gentle US female
    "jenny": "en-US-JennyNeural",              # Clear US female
}
selected_voice = VOICE_MAP.get(voice_choice, "en-ZA-LukeNeural")

# --- 6. GENERATE MULTI-SLIDE STORYBOARD WITH GEMINI (WITH RETRY) ---
client = genai.Client(api_key=GEMINI_API_KEY)

length_guidance = (
    "45 to 55 seconds total (roughly 115-135 words across 3 slides)."
    if is_short
    else "3 to 4 minutes total (roughly 450-550 words across 5 distinct slides)."
)

prompt = f"""
You are a reverent, Christ-centered Bible teacher and video director creating a {video_format} video.
Convert the study below into a multi-slide storyboard with spoken narration for each slide.

Creative Director Instructions: {creative_brief if creative_brief else "Warm, reverent, clear teaching."}
Requested Slide Structure: {slide_breakdown if slide_breakdown else "Slide 1: Hook & Title | Slide 2: Scripture Anchor | Slide 3: Core Teaching | Slide 4: Daily Application | Slide 5: Closing Prayer"}
Target Length: {length_guidance}

Rules:
- Each slide must have 'heading' (short uppercase banner), 'on_screen_text' (key verse or 2-3 short bullet points for the screen), and 'narration' (natural spoken prose ONLY—no stage directions, no markdown).
- Keep on_screen_text concise and readable (under 25 words per slide).

Title: {study_title}
Scripture: {study.get('Scripture') or study.get('scripture') or ''}
Summary: {study.get('Summary') or study.get('summary') or ''}
Detailed Notes: {study.get('Details') or study.get('details') or ''}

Return ONLY valid JSON matching this schema:
{{
  "youtube_title": "Compelling Title | Scripture Study",
  "youtube_description": "Full description with scripture references, timestamps, and prayer...",
  "slides": [
    {{
      "heading": "SLIDE HEADING",
      "on_screen_text": "Key scripture quote or crisp takeaway points",
      "narration": "Spoken voiceover for this specific slide..."
    }}
  ]
}}
"""

models_to_try = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
response = None

for attempt in range(1, 7):
    model_name = models_to_try[(attempt - 1) % len(models_to_try)]
    try:
        print(f"Calling Gemini ({model_name}), attempt {attempt}/6...")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        break
    except Exception as e:
        print(f"Attempt {attempt} ({model_name}) failed: {e}")
        if any(code in str(e) for code in ("503", "UNAVAILABLE", "429", "404", "NOT_FOUND")):
            wait_sec = min(attempt * 12, 45)
            time.sleep(wait_sec)
        else:
            raise

if response is None:
    raise RuntimeError("Gemini API remained unavailable after 6 retry attempts.")

storyboard = json.loads(response.text)
slides_data = storyboard.get("slides", [])
print(f"Generated {len(slides_data)}-slide storyboard.")

# --- 7. GENERATE PER-SLIDE AUDIO & RENDER MULTI-SLIDE VIDEO ---
width, height = (1080, 1920) if is_short else (1920, 1080)

theme_dir = os.path.join("assets", "backgrounds", visual_theme)
fallback_dir = os.path.join("assets", "backgrounds")
bg_pool = []

for folder in [theme_dir, fallback_dir]:
    if os.path.isdir(folder):
        bg_pool.extend(
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith((".mp4", ".mov", ".webm"))
        )

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


async def synthesize_slide_audio(text, out_file):
    comm = edge_tts.Communicate(text, selected_voice, rate="-4%")
    await comm.save(out_file)


slide_clips = []

for i, slide in enumerate(slides_data):
    slide_audio_path = f"slide_{i}.mp3"
    asyncio.run(synthesize_slide_audio(slide["narration"], slide_audio_path))

    a_clip = AudioFileClip(slide_audio_path)
    slide_dur = a_clip.duration + 0.6

    # Priority 1: Rotated Google Drive Background Image (Column P) with Ken Burns slow zoom
    if local_bg_image and os.path.isfile(local_bg_image):
        base_img = ImageClip(local_bg_image).resize((width, height)).set_duration(slide_dur)
        zoomed_img = base_img.resize(lambda t: 1.0 + 0.05 * (t / max(slide_dur, 0.1)))
        dimmer = (
            ColorClip(size=(width, height), color=(12, 18, 30))
            .set_opacity(0.50)
            .set_duration(slide_dur)
        )
        bg_layer = CompositeVideoClip(
            [zoomed_img.set_position("center"), dimmer], size=(width, height)
        )
    # Priority 2: Local themed video loop if present
    elif bg_pool:
        chosen_vid = bg_pool[i % len(bg_pool)]
        raw_vid = VideoFileClip(chosen_vid).without_audio().resize((width, height))
        loops = int(slide_dur // raw_vid.duration) + 1
        looped = concatenate_videoclips([raw_vid] * loops).subclip(0, slide_dur)
        dimmer = (
            ColorClip(size=(width, height), color=(12, 18, 30))
            .set_opacity(0.55)
            .set_duration(slide_dur)
        )
        bg_layer = CompositeVideoClip([looped, dimmer])
    # Priority 3: Solid color palette fallback
    else:
        bg_layer = ColorClip(size=(width, height), color=bg_rgb).set_duration(slide_dur)

    heading_clip = (
        TextClip(
            slide["heading"].upper(),
            fontsize=54 if not is_short else 48,
            color=heading_color,
            font="DejaVu-Sans-Bold",
            method="caption",
            size=(int(width * 0.84), None),
            align="center",
        )
        .set_position(("center", int(height * 0.22)))
        .set_duration(slide_dur)
    )

    body_clip = (
        TextClip(
            slide["on_screen_text"],
            fontsize=42 if not is_short else 38,
            color=body_color,
            font="DejaVu-Sans",
            method="caption",
            size=(int(width * 0.80), None),
            align="center",
        )
        .set_position(("center", int(height * 0.44)))
        .set_duration(slide_dur)
    )

    composed_slide = CompositeVideoClip([bg_layer, heading_clip, body_clip]).set_audio(
        a_clip.set_start(0.2)
    )
    if i > 0:
        composed_slide = composed_slide.crossfadein(0.6)

    slide_clips.append(composed_slide)

final_video = concatenate_videoclips(slide_clips, method="compose", padding=-0.6)

# Mix rotated Google Drive ambient soundtrack (Column O)
if local_music_path and os.path.isfile(local_music_path):
    print(f"Mixing background soundtrack: {local_music_path}")
    bg_music = AudioFileClip(local_music_path)
    bg_music = audio_loop(bg_music, duration=final_video.duration).volumex(0.12)
    mixed_audio = CompositeAudioClip([final_video.audio, bg_music])
    final_video = final_video.set_audio(mixed_audio)
else:
    print("No background soundtrack file resolved; rendering voiceover only.")

output_video_path = "final_output.mp4"
final_video.write_videofile(
    output_video_path,
    fps=24,
    codec="libx264",
    audio_codec="aac",
    threads=4,
    preset="medium",
)
print("Multi-slide video rendering complete.")

# --- 8. UPLOAD TO YOUTUBE AS 'UNLISTED' FOR REVIEW ---
category_val = str(study.get("Category") or study.get("category") or "Faith").strip()
body = {
    "snippet": {
        "title": storyboard["youtube_title"][:100],
        "description": storyboard["youtube_description"],
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
