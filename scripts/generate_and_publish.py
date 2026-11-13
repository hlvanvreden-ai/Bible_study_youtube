import os
import json
import asyncio
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
import edge_tts
from moviepy.editor import (
    AudioFileClip,
    VideoFileClip,
    TextClip,
    CompositeVideoClip,
    ColorClip
)
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- 1. SETUP & AUTHENTICATION ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDS_JSON = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
YOUTUBE_TOKEN_JSON = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])

genai.configure(api_key=GEMINI_API_KEY)

# Connect to Google Sheets
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
sheet_creds = Credentials.from_service_account_info(GOOGLE_CREDS_JSON, scopes=scope)
gc = gspread.authorize(sheet_creds)
sheet = gc.open("Bible study list").sheet1

# --- 2. FETCH NEXT "READY" STUDY ---
rows = sheet.get_all_records()
target_row_idx = None
study = None

for idx, r in enumerate(rows, start=2):  # Row 1 is header
    if str(r.get("Video Status", "")).strip().lower() == "ready":
        study = r
        target_row_idx = idx
        break

if not study:
    print("No rows marked as 'Ready'. Exiting.")
    exit(0)

print(f"Processing study: {study.get('Title', 'Untitled')}")

# --- 3. TRANSFORM NOTES TO SPOKEN SCRIPT (GEMINI) ---
prompt = f"""
Convert this Bible study into a warm, contemplative, easy-reading YouTube devotional script (2 to 3 minutes, approx 350-450 words).
Structure into:
1. Hook (relatable problem)
2. Scripture reading
3. Insight/word study
4. Practical application
5. Closing prayer

Title: {study.get('Title')}
Scripture: {study.get('Scripture References', study.get('Scripture', ''))}
Notes: {study.get('Premise', '')} {study.get('Rich Notes', '')}

Return ONLY valid JSON matching this format:
{{
  "voiceover_text": "Spoken text without stage directions...",
  "youtube_title": "Compelling Title | Scripture Devotional",
  "youtube_description": "Clean description with timestamps...",
  "featured_verse": "Quote of the key verse"
}}
"""

model = genai.GenerativeModel("gemini-3.8-flash")
response = model.generate_content(
    prompt,
    generation_config={"response_mime_type": "application/json"}
)
video_data = json.loads(response.text)

# --- 4. GENERATE AUDIO (FREE EDGE-TTS) ---
VOICE = "en-US-ChristopherNeural"  # Deep, warm, deliberate voice
audio_path = "output_audio.mp3"

async def make_audio():
    communicate = edge_tts.Communicate(video_data["voiceover_text"], VOICE, rate="-4%")
    await communicate.save(audio_path)

asyncio.run(make_audio())

# --- 5. RENDER VIDEO (MOVIEPY) ---
# Uses background video loops placed in 'assets/backgrounds/'
audio_clip = AudioFileClip(audio_path)
duration = audio_clip.duration

# Background clip (falls back to a calm dark background if no asset is present)
bg_path = "assets/backgrounds/calm_water.mp4"
if os.path.exists(bg_path):
    bg_clip = VideoFileClip(bg_path).loop(duration=duration).resize((1920, 1080))
else:
    bg_clip = ColorClip(size=(1920, 1080), color=(15, 23, 42), duration=duration)

# Title & Scripture Overlay
txt_title = TextClip(
    video_data.get("featured_verse", study.get("Title")),
    fontsize=48,
    color="white",
    font="Helvetica-Bold",
    method="caption",
    size=(1600, None)
).set_position(("center", "center")).set_duration(duration)

final_video = CompositeVideoClip([bg_clip, txt_title]).set_audio(audio_clip)
output_video_path = "final_output.mp4"
final_video.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac")

# --- 6. UPLOAD & SCHEDULE TO YOUTUBE ---
yt_creds = Credentials.from_authorized_user_info(YOUTUBE_TOKEN_JSON)
youtube = build("youtube", "v3", credentials=yt_creds)

publish_time = study.get("Scheduled Date", "")  # Format: "2026-11-12T06:00:00Z"

body = {
    "snippet": {
        "title": video_data["youtube_title"][:100],
        "description": video_data["youtube_description"],
        "tags": ["Bible Study", "Devotional", "Scripture", "Sanctuary"],
        "categoryId": "22"  # People & Blogs
    },
    "status": {
        "privacyStatus": "private",
        "publishAt": publish_time if publish_time else None
    }
}

media = MediaFileUpload(output_video_path, chunksize=-1, resumable=True)
upload_request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
video_response = upload_request.execute()
yt_url = f"https://youtu.be/{video_response['id']}"

# --- 7. UPDATE GOOGLE SHEET ---
status_col = list(study.keys()).index("Video Status") + 1
yt_col = list(study.keys()).index("YouTube URL") + 1 if "YouTube URL" in study else len(study) + 1

sheet.update_cell(target_row_idx, status_col, "Scheduled")
sheet.update_cell(target_row_idx, yt_col, yt_url)
print(f"Successfully published and scheduled: {yt_url}")
