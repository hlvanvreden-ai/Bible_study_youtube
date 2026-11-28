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
BATCH_SIZE = int(os.environ.get("FETCH_BATCH_SIZE", "3"))


def sheets_call_with_retry(fn, *args, **kwargs):
    """Retry Google Sheets API calls on 429 quota or transient 5xx errors."""
    for attempt in range(1, 7):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("429", "Quota exceeded", "500", "502", "503", "504")):
                delay = min(15 * attempt, 75)
                print(
                    f"Sheets API rate/server limit ({msg[:120]}); "
                    f"retrying in {delay}s (attempt {attempt}/6)..."
                )
                time.sleep(delay)
                continue
            raise
    return fn(*args, **kwargs)


# --- 1. CONNECT TO GOOGLE SHEETS ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)
gc = gspread.authorize(creds)
sheet = sheets_call_with_retry(gc.open, "Bible study list").sheet1

headers = [h.strip().casefold() for h in sheets_call_with_retry(sheet.row_values, 1)]
records = sheets_call_with_retry(sheet.get_all_records)

existing_titles = {
    str(row.get("title", "")).strip().casefold()
    for row in records
    if str(row.get("title", "")).strip()
}

print(f"Spreadsheet: {sheet.title}")
print(f"Existing studies: {len(existing_titles)}")
print(f"Spreadsheet headings: {headers}")


# --- 2. FETCH SOURCES & NOTES FROM NOTEBOOKLM ---
async def get_notebook_items():
    """Return a list of (label, content_block) tuples for sources and saved notes."""
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
        items = []

        # Fetch sources
        sources = await client.sources.list(target.id)
        for src in sources:
            src_title = str(getattr(src, "title", "Untitled Source")).strip()
            try:
                full_text = await client.sources.get_fulltext(target.id, src.id)
                if full_text:
                    actual_text = getattr(full_text, "content", None) or str(full_text)
                    if not isinstance(actual_text, str):
                        actual_text = str(actual_text)

                    if actual_text.strip():
                        items.append((src_title, f"=== SOURCE: {src_title} ===\n{actual_text}"))
                        print(
                            f"Retrieved source: {src_title} "
                            f"({len(actual_text)} characters)"
                        )
            except Exception as e:
                print(f"WARNING: Could not retrieve source '{src_title}': {e}")

        # Fetch saved notes
        try:
            notes = await client.notes.list(target.id)
            for note in notes:
                note_title = str(getattr(note, "title", "Untitled Note")).strip()
                note_content = getattr(note, "content", "") or getattr(note, "text", "")
                if note_content and str(note_content).strip():
                    items.append((note_title, f"=== NOTE: {note_title} ===\n{note_content}"))
                    print(
                        f"Retrieved note: {note_title} "
                        f"({len(str(note_content))} characters)"
                    )
        except Exception as exc:
            print(f"Warning: could not list notes: {exc}")

        return items


notebook_items = asyncio.run(get_notebook_items())
total_chars = sum(len(block) for _, block in notebook_items)
print(f"Total NotebookLM items: {len(notebook_items)} ({total_chars} characters)")

if not notebook_items:
    print("ERROR: No readable notebook content found.")
    raise SystemExit(1)

# Skip single-study sources/notes whose exact title already exists in the sheet,
# while still inspecting multi-study compilations or new items.
candidate_items = [
    (title, block)
    for title, block in notebook_items
    if title.casefold() not in existing_titles or len(block) > 6000
]
print(f"Candidate items to inspect for missing studies: {len(candidate_items)}")


# --- 3. EXTRACT NEW STUDIES VIA GEMINI (BATCHED) ---
DEFAULT_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash",
    "gemini-3.5-flash",
]
MODEL_NAMES = [
    name.strip()
    for name in os.environ.get("GEMINI_MODEL_FALLBACKS", ",".join(DEFAULT_MODELS)).split(",")
    if name.strip()
]


def discover_available_flash_models(client):
    """Query the Gemini API for available Flash models supporting generateContent."""
    discovered = []
    try:
        for model in client.models.list():
            name = getattr(model, "name", "") or ""
            methods = (
                getattr(model, "supported_actions", None)
                or getattr(model, "supported_generation_methods", None)
                or []
            )
            if methods and "generateContent" not in methods:
                continue
            clean_name = name.removeprefix("models/")
            if (
                "flash" in clean_name.lower()
                and "image" not in clean_name.lower()
                and "audio" not in clean_name.lower()
            ):
                discovered.append(clean_name)
    except Exception as exc:
        print(f"Warning: could not list Gemini models dynamically: {exc}")
    return discovered


def generate_with_fallback(prompt, stage_name):
    """Try configured Gemini models; dynamically discover models if needed."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    candidates = list(dict.fromkeys(MODEL_NAMES))
    last_error = None
    discovered_checked = False

    attempt = 0
    while attempt < 10:
        if not candidates and not discovered_checked:
            discovered_checked = True
            discovered = discover_available_flash_models(client)
            if discovered:
                print(f"{stage_name}: discovered available models: {discovered}")
                candidates.extend(discovered)

        if not candidates:
            break

        model_name = candidates.pop(0)
        attempt += 1
        try:
            print(f"{stage_name}: calling {model_name}, attempt {attempt}...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=16384,
                ),
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

            if any(
                code in message
                for code in (
                    "503",
                    "UNAVAILABLE",
                    "429",
                    "RESOURCE_EXHAUSTED",
                    "500",
                    "INTERNAL",
                    "502",
                    "BAD_GATEWAY",
                )
            ):
                delay = min(5 * (2 ** min(attempt - 1, 4)), 60)
                print(f"Temporary Gemini error; retrying after {delay} seconds.")
                time.sleep(delay)
                candidates.append(model_name)
                continue

            raise

    raise RuntimeError(
        f"{stage_name}: all Gemini model attempts failed. Last error: {last_error}"
    )


def parse_json_response(response_text, stage_name=""):
    """Parse JSON, tolerating Markdown fences and salvaging complete items if truncated."""
    text = str(response_text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        print(
            f"WARNING ({stage_name}): JSONDecodeError ({err}). "
            f"Length={len(text)}, tail={text[-250:]!r}"
        )
        # Salvage any complete study objects before a truncated trailing object
        last_obj_end = text.rfind("},")
        if last_obj_end != -1:
            repaired = text[: last_obj_end + 1] + "\n  ]\n}"
            try:
                salvaged = json.loads(repaired)
                count = len(salvaged.get("new_studies", []))
                print(f"Salvaged {count} complete study object(s) from truncated JSON.")
                return salvaged
            except json.JSONDecodeError:
                pass
        raise


def build_extraction_prompt(batch_content, known_titles):
    existing_json = json.dumps(sorted(known_titles), ensure_ascii=False)
    return f"""You are extracting NEW structured Bible study entries from NotebookLM content so they can be added to a Google Sheet.

Already-existing study titles in the Google Sheet (DO NOT extract or return any study matching these titles):
{existing_json}

NotebookLM content batch:
{batch_content}

Extract ONLY distinct Bible studies found in the NotebookLM content batch whose title is NOT in the existing list above.
Do not return an empty array if new complete Bible studies are clearly present in the supplied batch.

Fields for each NEW study:
- title: exact study title
- category: one of [Christian Walk, Spiritual Warfare, Theology & Covenant, Psalms & Prayer, Identity & Rebirth, Word Studies, Living Water & Spirit]
- scripture: comma-separated Scripture references
- summary: 1-2 sentence summary
- details: concise, structured HTML teaching notes (max 1,200 characters) using only h3, p, ul and li tags

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
If no new studies are present in this batch, return {{"new_studies": []}}.
"""


all_extracted_studies = []

for start in range(0, len(candidate_items), BATCH_SIZE):
    batch = candidate_items[start : start + BATCH_SIZE]
    batch_num = (start // BATCH_SIZE) + 1
    total_batches = (len(candidate_items) + BATCH_SIZE - 1) // BATCH_SIZE
    stage_label = f"Study extraction (batch {batch_num}/{total_batches})"

    batch_content = "\n\n".join(block for _, block in batch)
    prompt = build_extraction_prompt(batch_content, existing_titles)
    response = generate_with_fallback(prompt, stage_label)

    parsed = parse_json_response(response.text, stage_label)
    batch_studies = parsed.get("new_studies", [])

    if not isinstance(batch_studies, list):
        raise RuntimeError(f"{stage_label}: Gemini response did not contain a 'new_studies' list.")

    print(f"{stage_label}: extracted {len(batch_studies)} candidate study/studies.")
    all_extracted_studies.extend(batch_studies)

print(f"Total new candidate studies extracted across all batches: {len(all_extracted_studies)}")

rows_to_append = []
for item in all_extracted_studies:
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
    rows_to_append.append(row_values)
    existing_titles.add(title_key)
    print(f"Prepared new study: {title} ({study_id})")

if rows_to_append:
    sheets_call_with_retry(
        sheet.append_rows,
        rows_to_append,
        value_input_option="USER_ENTERED",
    )

print(f"Sync complete. Added {len(rows_to_append)} new study/studies.")
