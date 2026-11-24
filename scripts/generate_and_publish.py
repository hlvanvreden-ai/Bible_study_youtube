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
sheet.update_cell(target_row, YOUTUBE_URL_COL, youtube_url)
sheet.update_cell(target_row, STATUS_COL, "Review")
print(f"Uploaded video as Unlisted: {youtube_url}")

try:
    send_review_notification(target_row, study_title, youtube_url)
except Exception as exc:
    # The video and sheet update have succeeded; don't mark the whole run failed
