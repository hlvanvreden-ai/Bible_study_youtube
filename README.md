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

Full Bible Study-to-YouTube Automation Pipeline
From creating the Bible study in Gemini Notebook to publishing the finished video publicly on YouTube.
1. Create the Bible study in Gemini Notebook
   
   You prepare the Bible study, including its title, category, scripture references, summary and detailed study notes.
2. Retrieve the completed study
   
   The automation connects to NotebookLM / Gemini Notebook and retrieves the latest Bible study content.
3. Transfer the study into Google Sheets
   
   The automation adds a new row to your Google Sheet, populating:
   - ID
   - Title
   - Category
   - Scripture
   - Summary
   - Details
   It automatically sets Video Status to Ready.
4. Prepare the video instructions in Google Sheets
   
   You can specify or adjust:
   - Video Format
   - Creative Brief
   - Slide Breakdown
   - Visual Theme
   - Voice Style
   - Music Track
   - Background Image
5. Generate the video script using Gemini AI
   
   The automation sends the Bible study and creative instructions to Gemini. Gemini prepares the narration and video content.
6. Generate the voiceover
   
   Text-to-speech converts the script into spoken narration using the selected voice style.
7. Create and render the video
   
   The automation combines the study content, slides, visual theme, background images, narration and music into the finished video.
8. Upload the video to YouTube privately
   
   The rendered video is uploaded as Unlisted, so it is not yet publicly available. The YouTube URL is written back to Google Sheets.
9. Send the approval request by email
   
   An email is sent to the reviewer with the video link and a request to review and approve it.
10. Review and approve the video
    
    The reviewer watches the video and decides whether it is suitable.
    - If changes are needed, the video is sent back for correction and re-rendering.
    - If accepted, Video Status is changed to Approved.
11. Schedule the public release
    
    The automation reads the Scheduled Date from Google Sheets and schedules the approved video for publication.
12. Publish the video publicly on YouTube
    
    At the scheduled date and time, the video visibility changes to Public. The Bible study video is now available to everyone.
13. Update Google Sheets
    
    The final YouTube URL and publication status are recorded in the spreadsheet, providing a record of the completed video.
