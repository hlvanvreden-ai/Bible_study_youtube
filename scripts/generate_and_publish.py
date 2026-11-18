import os
import time
import json
import asyncio
import smtplib
import urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from PIL import Image, ImageDraw
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


def send_review_notification(row_idx, study_title, youtube_url):
    """Sends an HTML email alert with the Apps Script Review Portal link."""
    review_link = REVIEW_WEBAPP_URL if REVIEW_WEBAPP_URL else youtube_url
    print(f"Review portal ready: {review_link}")

    if not (NOTIFY_EMAIL and SMTP_USER and SMTP_PASS):
        print("SMTP email secrets not set; skipping email dispatch.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🎬 Review Ready: {study_title} (Row {row_idx})"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    msg.set_content(
        f"Your Bible study video '{study_title}' (Row {row_idx}) is rendered and staged as Unlisted.\n"
        f"Review & Approve here: {review_link}\n"
        f"Direct Unlisted YouTube Link: {youtube_url}\n"
    )
    msg.add_alternative(
        f"""
        <div style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:auto;background:#141E30;color:#F8FAFC;padding:28px;border-radius:12px;">
          <h2 style="color:#F4EAD4;margin-top:0;">🎬 Video Ready for Review</h2>
          <p style="font-size:16px;line-height:1.5;">
            <strong>Study (Row {row_idx}):</strong> {study_title}
          </p>
          <p style="margin:24px 0;">
            <a href="{review_link}" style="background:#16A34A;color:#FFFFFF;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:bold;display:inline-block;">
              Open Review &amp; Approval Portal
            </a>
          </p>
          <p style="font-size:13px;color:#94A3B8;">
            Direct Unlisted Preview: <a href="{youtube_url}" style="color:#F4EAD4;">{youtube_url}</a>
          </p>
        </div>
        """,
        subtype="html",
    )

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    print(f"Review notification email sent to {NOTIFY_EMAIL}.")


def prepare_background_image(source_ref, idx, target_w, target_h):
    """Downloads or loads an image, center-crops to target aspect ratio, and adds a dark sanctuary vignette."""
    raw_path = f"bg_raw_{idx}.jpg"
    processed_path = f"bg_clean_{idx}.png"

    if source_ref.startswith(("http://", "https://")):
        req = urllib.request.Request(source_ref, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp, open(raw_path, "wb") as out:
            out.write(resp.read())
    elif os.path.isfile(source_ref):
        raw_path = source_ref
    else:
        return None

    # Oversize by 10% so slow Ken Burns zoom never reveals black borders
    canvas_w = int(target_w * 1.10)
    canvas_h = int(target_h * 1.10)

    img = Image.open(raw_path).convert("RGB")
    src_ratio = img.width / img.height
    dst_ratio = canvas_w / canvas_h

    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / dst_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))

    img = img.resize((canvas_w, canvas_h), Image.Resampling.LANCZOS).convert("RGBA")

    # Apply dark sanctuary tint + subtle gold-bordered center reading frame for crisp legibility
    overlay = Image.new("RGBA", (canvas_w, canvas_h), (12, 18, 30, 155))
    draw = ImageDraw.Draw(overlay)
    margin_x = int(canvas_w * 0.08)
    margin_y = int(canvas_h * 0.14)
    draw.rounded_rectangle(
        [margin_x, margin_y, canvas_w - margin_x, canvas_h - margin_y],
        radius=28,
        fill=(15, 23, 42, 115),
        outline=(244, 234, 212, 110),
        width=3,
    )

    composed = Image.alpha_composite(img, overlay).convert("RGB")
    composed.save(processed_path, "PNG")
    return processed_path


# --- 2. CONNECT TO GOOGLE SHEETS & YOUTUBE ---
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

yt_creds = UserCredentials.from_authorized_user_info(YOUTUBE_TOKEN_INFO)
youtube = build("youtube", "v3", credentials=yt_creds)

# --- 3. STAGE 2 CHECK: PUBLISH ANY 'APPROVED' VIDEOS FIRST ---
for idx, row in enumerate(records, start=2):
    if str(row.get("Video Status", "")).strip().lower() == "approved":
        yt_url = str(row.get("YouTube URL", "")).strip()
        scheduled_date = str(row.get("Scheduled Date", "")).strip()

        if not yt_url:
            print(f"Row {idx} is marked 'Approved' but has no YouTube URL. Skipping.")
            continue

        # Ensure Scheduled Date is at least 20 mins in the future (SAST +02:00)
        sast = timezone(timedelta(hours=2))
        now_sast = datetime.now(sast)
        min_valid_publish = now_sast + timedelta(minutes=20)

        try:
            parsed_dt = datetime.fromisoformat(scheduled_date)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=sast)
        except ValueError:
            parsed_dt = min_valid_publish

        if parsed_dt < min_valid_publish:
            parsed_dt = now_sast + timedelta(minutes=30)
            scheduled_date = parsed_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
            sheet.update_cell(idx, 8, scheduled_date)
            print(f"Original Scheduled Date was in the past; auto-adjusted to {scheduled_date}")

        video_id = yt_url.rstrip("/").split("/")[-1].split("?")[0]
        print(f"Publishing Approved Row {idx} (Video ID: {video_id}) for {scheduled_date}...")

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
print(f"Rendering Stage 1 for Row {target_row_index}: {study.get('title')}")

# --- 5. READ CREATIVE CONTROL COLUMNS (K–P) ---
video_format = str(study.get("Video Format", "Devotional")).strip()
is_short = video_format.lower() == "short"

creative_brief = str(study.get("Creative Brief", "")).strip()
slide_breakdown = str(study.get("Slide Breakdown", "")).strip()
visual_theme = str(study.get("Visual Theme", "navy_gold")).strip().lower()
voice_choice = str(study.get("Voice Style", "Luke")).strip().lower()
music_track = str(study.get("Music Track", "")).strip()
bg_image_raw = str(study.get("Background Image", "")).strip()

# Support single image URL/path or comma-separated list of images per slide
bg_image_list = [u.strip() for u in bg_image_raw.split(",") if u.strip()]

VOICE_MAP = {
    "luke": "en-ZA-LukeNeural",               # South African warm male
    "leah": "en-ZA-LeahNeural",               # South African expressive female
    "christopher": "en-US-ChristopherNeural", # Deep authoritative male
    "andrew": "en-US-AndrewNeural",           # Warm conversational male
    "aria": "en-US-AriaNeural",               # Gentle expressive female
    "jenny": "en-US-JennyNeural",             # Clear natural female
}
selected_voice = VOICE_MAP.get(voice_choice, "en-ZA-LukeNeural")

# --- 6. GENERATE MULTI-SLIDE STORYBOARD WITH GEMINI (5-ATTEMPT RETRY) ---
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

Title: {study.get('title', '')}
Scripture: {study.get('scripture', '')}
Summary: {study.get('summary', '')}
Detailed Notes: {study.get('details', '')}

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

models_to_try = ["gemini-3.8-flash", "gemini-2.5-flash"]
response = None
for attempt in range(1, 6):
    model_name = models_to_try[0] if attempt <= 3 else models_to_try[1]
    try:
        print(f"Calling Gemini ({model_name}), attempt {attempt}/5...")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        break
    except Exception as e:
        if any(code in str(e) for code in ("503", "UNAVAILABLE", "429")):
            wait_sec = attempt * 15
            print(f"Gemini busy ({e}). Retrying in {wait_sec}s...")
            time.sleep(wait_sec)
        else:
            raise

if response is None:
    raise RuntimeError("Gemini API remained unavailable after 5 retry attempts.")

storyboard = json.loads(response.text)
slides_data = storyboard.get("slides", [])
total_slides = len(slides_data)
print(f"Generated {total_slides}-slide storyboard.")

# --- 7. GENERATE PER-SLIDE AUDIO & CINEMATIC KEN BURNS SLIDES ---
width, height = (1080, 1920) if is_short else (1920, 1080)

theme_dir = os.path.join("assets", "backgrounds", visual_theme)
fallback_dir = os.path.join("assets", "backgrounds")
bg_video_pool = []

for folder in [theme_dir, fallback_dir]:
    if os.path.isdir(folder):
        bg_video_pool.extend(
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith((".mp4", ".mov", ".webm"))
        )

PALETTES = {
    "navy_gold": ((20, 30, 48), "#F4EAD4", "#FFFFFF"),
    "forest_cream": ((24, 42, 34), "#E9DFC8", "#F8F9FA"),
    "burgundy_warm": ((48, 22, 28), "#F5E6CC", "#FFFFFF"),
    "desert_sand": ((58, 44, 30), "#F7E7CE", "#FFFFFF"),
    "royal_purple": ((34, 20, 52), "#F4EAD4", "#FFFFFF"),
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
    slide_dur = a_clip.duration + 0.7
    bg_layer = None

    # Priority 1: Background Image from Column P (with smooth Ken Burns zoom)
    if bg_image_list:
        chosen_img_ref = bg_image_list[i % len(bg_image_list)]
        try:
            processed_img = prepare_background_image(chosen_img_ref, i, width, height)
            if processed_img:
                base_img = ImageClip(processed_img).set_duration(slide_dur)
                zoomed_img = base_img.resize(lambda t: 1.0 + 0.08 * (t / slide_dur))
                bg_layer = CompositeVideoClip(
                    [zoomed_img.set_position("center")], size=(width, height)
                ).set_duration(slide_dur)
        except Exception as img_err:
            print(f"Warning: Could not load image '{chosen_img_ref}' ({img_err}); falling back.")

    # Priority 2: Local themed background video loop if no image was provided
    if bg_layer is None and bg_video_pool:
        chosen_vid = bg_video_pool[i % len(bg_video_pool)]
        raw_vid = VideoFileClip(chosen_vid).without_audio().resize((width, height))
        loops = int(slide_dur // raw_vid.duration) + 1
        looped = concatenate_videoclips([raw_vid] * loops).subclip(0, slide_dur)
        dimmer = (
            ColorClip(size=(width, height), color=(12, 18, 30))
            .set_opacity(0.55)
            .set_duration(slide_dur)
        )
        bg_layer = CompositeVideoClip([looped, dimmer])

    # Priority 3: Rich theme color fallback
    if bg_layer is None:
        bg_layer = ColorClip(size=(width, height), color=bg_rgb).set_duration(slide_dur)

    # Gold accent bar separating heading from body text
    accent_bar = (
        ColorClip(size=(int(width * 0.24), 4), color=(244, 234, 212))
        .set_opacity(0.85)
        .set_position(("center", int(height * 0.36)))
        .set_duration(slide_dur)
    )

    heading_clip = (
        TextClip(
            slide["heading"].upper(),
            fontsize=54 if not is_short else 48,
            color=heading_color,
            font="DejaVu-Sans-Bold",
            method="caption",
            size=(int(width * 0.80), None),
            align="center",
        )
        .set_position(("center", int(height * 0.21)))
        .set_duration(slide_dur)
    )

    body_clip = (
        TextClip(
            slide["on_screen_text"],
            fontsize=42 if not is_short else 38,
            color=body_color,
            font="DejaVu-Sans",
            method="caption",
            size=(int(width * 0.76), None),
            align="center",
        )
        .set_position(("center", int(height * 0.42)))
        .set_duration(slide_dur)
    )

    footer_clip = (
        TextClip(
            f"{i + 1} / {total_slides}",
            fontsize=24,
            color="#CBD5E1",
            font="DejaVu-Sans",
        )
        .set_position(("center", int(height * 0.81)))
        .set_duration(slide_dur)
    )

    composed_slide = CompositeVideoClip(
        [bg_layer, heading_clip, accent_bar, body_clip, footer_clip]
    ).set_audio(a_clip.set_start(0.25))

    if i > 0:
        composed_slide = composed_slide.crossfadein(0.6)

    slide_clips.append(composed_slide)

final_video = concatenate_videoclips(slide_clips, method="compose", padding=-0.6)

# Optional low-volume ambient music track
music_path = os.path.join("assets", "music", music_track) if music_track else ""
if music_path and os.path.isfile(music_path):
    bg_music = AudioFileClip(music_path)
    bg_music = audio_loop(bg_music, duration=final_video.duration).volumex(0.07)
    mixed_audio = CompositeAudioClip([final_video.audio, bg_music])
    final_video = final_video.set_audio(mixed_audio)

output_video_path = "final_output.mp4"
final_video.write_videofile(
    output_video_path,
    fps=24,
    codec="libx264",
    audio_codec="aac",
    threads=4,
    preset="medium",
)
print("Cinematic multi-slide video rendering complete.")

# --- 8. UPLOAD TO YOUTUBE AS 'UNLISTED' FOR REVIEW ---
body = {
    "snippet": {
        "title": storyboard["youtube_title"][:100],
        "description": storyboard["youtube_description"],
        "tags": [
            "Bible Study",
            "Christian Devotional",
            "Scripture",
            study.get("category", "Faith"),
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
send_review_notification(target_row_index, study.get("title", "Untitled"), youtube_url)
print(f"Row {target_row_index} updated to 'Pending Review'.")
