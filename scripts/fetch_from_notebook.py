import os
import re
import json
import uuid
import time
import asyncio
import gspread
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from notebooklm import NotebookLMClient

# --- 1. LOAD SECRETS ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
NOTEBOOK_TITLE = os.environ.get("NOTEBOOK_TITLE", "Bible studies").strip()

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
existing_records = sheet.get_all_records()

existing_titles = {
    str(r.get("title", "")).strip().lower()
    for r in existing_records
    if str(r.get("title", "")).strip()
}
print(f"Loaded {len(existing_titles)} existing studies from 'Bible study list'.")


async def fetch_notebook_sources():
    """Connects to NotebookLM and retrieves only the most recent Bible studies without exceeding RPC size caps."""
    async with await NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
        target_nb = next(
            (
                nb
                for nb in notebooks
                if "bible study" in nb.title.lower() and "youtube" not in nb.title.lower()
            ),
            None,
        )
        if not target_nb:
            raise RuntimeError(
                f"Notebook '{NOTEBOOK_TITLE}' not found. Available: {[nb.title for nb in notebooks]}"
            )

        print(f"Connected to notebook: '{target_nb.title}' (ID: {target_nb.id})")

        # Skip client.sources.list() to avoid the 268 MB RPC payload limit on large notebooks;
        # query only the 3 most recent studies via chat.ask() instead.
        ask_resp = await client.chat.ask(
            target_nb.id,
            "Provide ONLY the 3 most recent Bible studies created in this notebook "
            "(including yesterday's study 'He First Loved Us...'). "
            "For each of those 3 studies, provide: exact Title, Category, primary Scripture references, "
            "a concise Summary, and the complete structured HTML teaching notes. "
            "Do NOT include older studies beyond the 3 most recent.",
        )
        sources = []
        return sources, ask_resp.answer


sources, notebook_digest = asyncio.run(fetch_notebook_sources())

# --- 3. EXTRACT & STRUCTURE MISSING STUDIES WITH GEMINI ---
genai_client = genai.Client(api_key=GEMINI_API_KEY)
existing_list_str = "\n".join(f"- {t}" for t in sorted(existing_titles)) or "None"

extraction_prompt = f"""
You are a theological data archivist syncing Bible studies from NotebookLM into a structured Google Sheet.

Below are the study titles ALREADY present in the Google Sheet (DO NOT include these):
{existing_list_str}

Below is the content retrieved from the '{NOTEBOOK_TITLE}' NotebookLM notebook:
{notebook_digest}

Task:
Identify every Bible study in the notebook content whose title is NOT already in the existing list above.
For each NEW study, extract and format:
- title: Exact study title
- category: One of [Christian Walk, Spiritual Warfare, Theology & Covenant, Psalms & Prayer, Identity & Rebirth, Word Studies, Living Water & Spirit]
- scripture: Comma-separated scripture references (e.g. "Psalm 103:20, Hebrews 1:14")
- summary: 1-2 sentence summary of the study
- details: Clean, structured HTML teaching notes using <h3>, <p>, <ul>, <li> tags

Return ONLY a valid JSON object matching this exact schema:
{{
  "new_studies": [
    {{
      "title": "Study Title",
      "category": "Category Name",
      "scripture": "Book Chapter:Verse",
      "summary": "Short summary...",
      "details": "<h3>Section</h3><p>Teaching content...</p>"
    }}
  ]
}}
If all studies are already in the sheet, return {{"new_studies": []}}.
"""

models_to_try = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
response = None

for attempt in range(1, 7):
    model_name = models_to_try[(attempt - 1) % len(models_to_try)]
    try:
        print(f"Extracting new studies via {model_name} (attempt {attempt}/6)...")
        response = genai_client.models.generate_content(
            model=model_name,
            contents=extraction_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        break
    except Exception as e:
        print(f"Attempt {attempt} ({model_name}) failed: {e}")
        if any(code in str(e) for code in ("503", "UNAVAILABLE", "429", "404", "NOT_FOUND")):
            time.sleep(min(attempt * 10, 45))
        else:
            raise

if response is None:
    raise RuntimeError("Gemini API remained unavailable while extracting NotebookLM studies.")

parsed = json.loads(response.text)
new_studies = parsed.get("new_studies", [])

added_count = 0
for item in new_studies:
    title = str(item.get("title", "")).strip()
    if not title or title.lower() in existing_titles:
        continue

    study_id = str(uuid.uuid4())
    # Columns A-I: id, title, category, scripture, summary, details, Video Status (blank), Scheduled Date (blank), Video Format
    row_values = [
        study_id,
        title,
        str(item.get("category", "Christian Walk")).strip(),
        str(item.get("scripture", "")).strip(),
        str(item.get("summary", "")).strip(),
        str(item.get("details", "")).strip(),
        "",            # Col G: Left blank so Apps Script applies rotating template & marks 'Ready'
        "",            # Col H: Left blank so Apps Script assigns SAST timestamp
        "Devotional",  # Col I: Default format
    ]
    sheet.append_row(row_values, value_input_option="USER_ENTERED")
    existing_titles.add(title.lower())
    added_count += 1
    print(f"Added new study to sheet: {title} ({study_id})")

print(f"Sync complete. Added {added_count} new study(ies) to 'Bible study list'.")
