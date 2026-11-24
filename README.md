Bible_study_youtube
Automation to retrieve Bible studies from NotebookLM, sync them to Google Sheets,
generate a spoken devotional, render a single-slide video, and upload it to YouTube
as Unlisted for review.
GitHub Actions secrets
Configure these under Settings → Secrets and variables → Actions:
- GEMINI_API_KEY
- GOOGLE_SERVICE_ACCOUNT_JSON
- YOUTUBE_TOKEN_JSON
- NOTEBOOKLM_STORAGE_JSON (needed for fetch modes)
- REVIEW_WEBAPP_URL (optional)
- NOTIFY_EMAIL (optional)
- SMTP_USER (optional)
- SMTP_PASS (optional)
The Google Sheet must be named Bible study list. The first row must include at
least Title, Scripture, Summary, Details, Video Status, Scheduled Date
and Video Format. The renderer creates YouTube URL if it is absent. Other
creative-control columns are optional: Creative Brief, Visual Theme,
Voice Style, Music Track, and Background Image.
Workflow modes
- fetch_and_build: fetch new studies, then render the earliest Ready row.
- fetch_only: only sync NotebookLM studies to the spreadsheet.
- build_only: only render a Ready row or schedule an Approved video.
A newly rendered video is uploaded as Unlisted and the spreadsheet status is
set to Review. After the review process changes the status to Approved, the
next build run schedules it using the spreadsheet's Scheduled Date.
Important
The renderer currently creates a single-slide video card with voiceover and
optional background music. It does not generate scene-by-scene footage or a
custom thumbnail. Verify the first generated video and review the sheet column
names/status values before enabling unattended daily publishing.
