import os
import json
import asyncio
import random
import gspread
import edge_tts
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)

# --- 1. LOAD ENVIRONMENT SECRETS ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
YOUTUBE_TOKEN_INFO = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])

# --- 2. CONNECT TO GOOGLE SHEETS ---
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

target_row_index = None
study = None

for idx, row in enumerate(records, start=2):  # Row 1 is headers
    if str(row.get("Video Status", "")).strip().lower() == "ready":
        target_row_index = idx
        study = row
        break

if not study:
    print("No rows marked as 'Ready' in the sheet. Exiting cleanly.")
    exit(0)

print(f"Processing Row {target_row_index}: {study.get('title')}")

# --- 3. GENERATE SCRIPT & METADATA WITH GEMINI ---
client = genai.Client(api_key=GEMINI_API_KEY)

video_format = str(study.get("Video Format", "Devotional")).strip()
is_short = video_format.lower() == "short"

length_guidance = (
    "45 to 55 seconds spoken aloud (roughly 110-135 words), punchy and impactful."
    if is_short
    else "3 to 4 minutes spoken aloud (roughly 450-550 words), warm, reverent, and deeply encouraging."
)

prompt = f"""
You are a reverent, Christ-centered Bible teacher creating a {video_format} video script.
Write a spoken narration script based on the study notes below.

Guidelines:
- Target length: {length_guidance}
- Write natural spoken prose ONLY (no stage directions, no "[Music]", no markdown formatting, no bullet points).
- Ground every point in the provided Scripture and notes.
- End with a brief, heartfelt closing prayer.

Title: {study.get('title', '')}
Scripture: {study.get('scripture', '')}
Summary: {study.get('summary', '')}
Detailed Notes: {study.get('details', '')}

Return ONLY valid JSON matching this exact structure:
{{
  "voiceover_text": "Spoken text without stage directions...",
  "youtube_title": "Compelling Title | Scripture Devotional",
  "youtube_description": "Clean description with scripture references and prayer...",
  "featured_verse": "Short key verse quote for on-screen display"
}}
"""

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=prompt,
    config=types.GenerateContentConfig(
        response_mime_type="application/json"
    ),
)
video_data = json.loads(response.text)
print("Generated script and metadata successfully.")

# --- 4. GENERATE AUDIO (FREE EDGE-TTS) ---
VOICE = "en-US-ChristopherNeural"
audio_path = "output_audio.mp3"


async def make_audio():
    communicate = edge_tts.Communicate(
        video_data["voiceover_text"], VOICE, rate="-4%"
    )
    await communicate.save(audio_path)


asyncio.run(make_audio())
print("Generated voiceover audio.")

# --- 5. RENDER VIDEO (MOVIEPY 1.0.3) ---
audio_clip = AudioFileClip(audio_path)
duration = audio_clip.duration

width, height = (1080, 1920) if is_short else (1920, 1080)

# Look for optional background video loops in assets/backgrounds/
bg_dir = "assets/backgrounds"
bg_files = []
if os.path.isdir(bg_dir):
    bg_files = [
        os.path.join(bg_dir, f)
        for f in os.listdir(bg_dir)
        if f.lower().endswith((".mp4", ".mov", ".webm"))
    ]

if bg_files:
    chosen_bg = random.choice(bg_files)
    raw_bg = VideoFileClip(chosen_bg).without_audio()
    raw_bg = raw_bg.resize((width, height))
    loops = int(duration // raw_bg.duration) + 1
    bg_clip = concatenate_videoclips([raw_bg] * loops).subclip(0, duration)
    overlay = ColorClip(size=(width, height), color=(15, 23, 42)).set_opacity(0.55).set_duration(duration)
    base_layer = CompositeVideoClip([bg_clip, overlay])
else:
    base_layer = ColorClip(size=(width, height), color=(24, 34, 54)).set_duration(duration)

title_text = str(study.get("title", "")).upper()
verse_text = f"\"{video_data.get('featured_verse', '')}\"\n— {study.get('scripture', '')}"

title_clip = (
    TextClip(
        title_text,
        fontsize=64 if not is_short else 58,
        color="#F4EAD4",
        font="DejaVu-Sans-Bold",
        method="caption",
        size=(int(width * 0.82), None),
        align="center",
    )
    .set_position(("center", int(height * 0.28)))
    .set_duration(duration)
)

verse_clip = (
    TextClip(
        verse_text,
        fontsize=42 if not is_short else 40,
        color="#FFFFFF",
        font="DejaVu-Sans",
        method="caption",
        size=(int(width * 0.78), None),
        align="center",
    )
    .set_position(("center", int(height * 0.50)))
    .set_duration(duration)
)

final_video = CompositeVideoClip([base_layer, title_clip, verse_clip]).set_audio(audio_clip)
output_video_path = "final_output.mp4"
final_video.write_videofile(
    output_video_path,
    fps=24,
    codec="libx264",
    audio_codec="aac",
    threads=4,
    preset="medium",
)
print("Video rendering complete.")

# --- 6. UPLOAD & SCHEDULE ON YOUTUBE ---
yt_creds = UserCredentials.from_authorized_user_info(YOUTUBE_TOKEN_INFO)
youtube = build("youtube", "v3", credentials=yt_creds)

scheduled_date = str(study.get("Scheduled Date", "")).strip()

body = {
    "snippet": {
        "title": video_data["youtube_title"][:100],
        "description": video_data["youtube_description"],
        "tags": ["Bible Study", "Christian Devotional", "Scripture", study.get("category", "Faith")],
        "categoryId": "22",  # People & Blogs
    },
    "status": {
        "privacyStatus": "private",
        "publishAt": scheduled_date,
        "selfDeclaredMadeForKids": False,
    },
}

media = MediaFileUpload(output_video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

response_upload = None
while response_upload is None:
    status, response_upload = request.next_chunk()
    if status:
        print(f"Upload progress: {int(status.progress() * 100)}%")

video_id = response_upload["id"]
youtube_url = f"https://youtu.be/{video_id}"
print(f"Successfully uploaded and scheduled: {youtube_url}")

# --- 7. UPDATE GOOGLE SHEET ---
# Columns: G (7) = Video Status, J (10) = YouTube URL
sheet.update_cell(target_row_index, 7, "Scheduled")
sheet.update_cell(target_row_index, 10, youtube_url)
print(f"Updated Row {target_row_index} to 'Scheduled' with URL {youtube_url}.")
