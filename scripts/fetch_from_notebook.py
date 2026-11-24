import asyncio
import functools
import json
import os
import time
import uuid

import gspread
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from notebooklm import NotebookLMClient

# GitHub Actions logs should appear immediately.
print = functools.partial(print, flush=True)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
NOTEBOOK_TITLE = os.environ.get("NOTEBOOK_TITLE", "Bible study").strip()

# --- 1. CONNECT TO GOOGLE SHEETS ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)
gc = gspread.authorize(creds)
sheet = gc.open("Bible study list").sheet1

records = sheet.get_all_records()
existing_titles = {
    str(row.get("Title", "")).strip().casefold()
    for row in records
    if str(row.get("Title", "")).strip()
}

# --- 2. FETCH SOURCES & NOTES FROM NOTEBOOKLM ---
async def get_notebook_content():
    async with await NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
        target = next(
            (
                nb
                for nb in notebooks
                if str(getattr(nb, "title", "")).strip().casefold() == NOTEBOOK_TITLE.casefold()
            ),
            None,
        )
        if not target:
            available = [getattr(nb, "title", "") for nb in notebooks]
            raise RuntimeError(
                f"Notebook '{NOTEBOOK_TITLE}' not found. Available notebooks: {available}"
            )

        print(f"Found NotebookLM notebook: {target.title} ({target.id})")
        parts = []

        # Fetch sources
        sources = await client.sources.list(target.id)
        for src in sources:
            src_title = getattr(src, "title", "Untitled Source")
            try:
                full_text = await client.sources.get_fulltext(target.id, src.id)
                if full_text:
                    parts.append(f"=== SOURCE: {src_title} ===\n{full_text}")
            except Exception as exc:
                print(f"Warning: could not fetch fulltext for source '{src_title}': {exc}")

        # Fetch saved notes
        try:
            notes = await client.notes.list(target.id)
            for note in notes:
                note_title = getattr(note, "title", "Untitled Note")
                note_content = getattr(note, "content", "") or getattr(note, "text", "")
                if note_content:
                    parts.append(f"=== NOTE: {note_title} ===\n{note_content}")
        except Exception as exc:
            print(f"Warning: could not list notes: {exc}")

        return "\n\n".join(parts)


notebook_content = asyncio.run(get_notebook_content())
if not notebook_content.strip():
    print("Notebook returned no readable sources or notes. Nothing to sync.")
    raise SystemExit(0)

# --- 3. EXTRACT NEW STUDIES VIA GEMINI ---
existing_titles_list = sorted(existing_titles)
extraction_prompt = f"""You are extracting structured Bible study entries from NotebookLM content so they can be added to a Google Sheet.

Existing study titles already in the spreadsheet (DO NOT include duplicates of these):
{json.dumps(existing_titles_list, ensure_ascii=False)}

NotebookLM content:
{notebook_content}

Extract ONLY new Bible studies not already listed above. For each new study, provide:
- title: exact study title
- category: one of [Christian Walk, Spiritual Warfare, Theology & Covenant,
  Psalms & Prayer, Identity & Rebirth, Word Studies, Living Water & Spirit]
- scripture: comma-separated Scripture references
- summary: 1-2 sentence summary
- details: structured HTML teaching notes using only h3, p, ul and li tags

Return ONLY valid JSON in this format:
{{
  "new_studies": [
    {{
      "title": "Study Title",
      "category": "Category Name",
      "scripture": "Book Chapter:Verse",
      "summary": "Short summary",
      "details": "<h3>Section</h3><p>Teaching content</p>"
    }}
  ]
}}
If no new studies are present, return {{"new_studies": []}}.
"""

MODEL_NAMES = [
    name.strip()
    for name in os.environ.get(
        "GEMINI_MODEL_FALLBACKS",
        "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.5-flash,gemini-3-flash",
    ).split(",")
    if name.strip()
]


def generate_with_fallback(prompt, stage_name):
    """Try configured Gemini models; back off on temporary 429/503 errors."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_error = None

    for attempt in range(1, 9):
        model_name = MODEL_NAMES[(attempt - 1) % len(MODEL_NAMES)]
        try:
            print(f"{stage_name}: calling {model_name}, attempt {attempt}/8...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            if not getattr(response, "text", None):
                raise RuntimeError(f"{model_name} returned an empty response.")
            return response
        except Exception as exc:
            last_error = exc
            message = str(exc)
            print(f"{stage_name}: {model_name} failed: {message}")

            if "404" in message or "NOT_FOUND" in message:
                continue

            if any(code in message for code in (
                "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                "500", "INTERNAL", "502", "BAD_GATEWAY",
            )):
                delay = min(5 * (2 ** (attempt - 1)), 60)
                print(f"Temporary Gemini error; retrying after {delay} seconds.")
                time.sleep(delay)
                continue

            raise

    raise RuntimeError(
        f"{stage_name}: all 8 Gemini attempts failed. Last error: {last_error}"
    )


def parse_json_response(response_text):
    """Parse JSON, tolerating a surrounding Markdown code fence."""
    text = str(response_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


response = generate_with_fallback(extraction_prompt, "Study extraction")
parsed = parse_json_response(response.text)
new_studies = parsed.get("new_studies", [])

if not isinstance(new_studies, list):
    raise RuntimeError("Gemini response did not contain a 'new_studies' list.")

added_count = 0
for item in new_studies:
    if not isinstance(item, dict):
        continue

    title = str(item.get("title", "")).strip()
    title_key = title.casefold()
    if not title or title_key in existing_titles:
        continue

    study_id = str(uuid.uuid4())
    # Existing sheet layout: A-I = id, title, category, scripture, summary,
    # details, Video Status, Scheduled Date, Video Format.
    row_values = [
        study_id,
        title,
        str(item.get("category", "Christian Walk")).strip(),
        str(item.get("scripture", "")).strip(),
        str(item.get("summary", "")).strip(),
        str(item.get("details", "")).strip(),
        "",
        "",
        "Devotional",
    ]
    sheet.append_row(row_values, value_input_option="USER_ENTERED")
    existing_titles.add(title_key)
    added_count += 1
    print(f"Added new study: {title} ({study_id})")

print(f"Sync complete. Added {added_count} new study/studies.")
