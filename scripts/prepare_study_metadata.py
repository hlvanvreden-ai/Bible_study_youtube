import functools
import json
import os
import time

import gspread
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

# Ensure GitHub Actions logs appear immediately
print = functools.partial(print, flush=True)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

DEFAULT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
]
MODEL_NAMES = [
    name.strip()
    for name in os.environ.get("GEMINI_MODEL_FALLBACKS", ",".join(DEFAULT_MODELS)).split(",")
    if name.strip()
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)
gc = gspread.authorize(creds)


def sheets_call_with_retry(fn, *args, **kwargs):
    """Retry Google Sheets API calls on 429 quota or transient 5xx errors."""
    for attempt in range(1, 7):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("429", "Quota exceeded", "500", "502", "503", "504")):
                delay = min(15 * attempt, 75)
                print(f"Sheets API limit ({msg[:80]}); retrying in {delay}s...")
                time.sleep(delay)
                continue
            raise
    return fn(*args, **kwargs)


sheet = sheets_call_with_retry(gc.open, "Bible study list").sheet1

raw_headers = sheets_call_with_retry(sheet.row_values, 1)
headers = [h.strip().casefold() for h in raw_headers]
records = sheets_call_with_retry(sheet.get_all_records)

print(f"Spreadsheet: {sheet.title}")
print(f"Total rows in sheet: {len(records)}")

# Map header names to their 1-based index for precise column updates
header_map = {str(h).strip().casefold(): idx + 1 for idx, h in enumerate(raw_headers)}

# Required columns to populate during preparation
TARGET_COLUMNS = [
    "Creative Brief",
    "Slide Breakdown",
    "Visual Theme",
    "Voice Style",
    "Music Track",
    "Background Image",
]

client = genai.Client(api_key=GEMINI_API_KEY)


def discover_available_flash_models():
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
                and "tts" not in clean_name.lower()
                and "live" not in clean_name.lower()
            ):
                discovered.append(clean_name)
    except Exception as exc:
        print(f"Warning: could not list Gemini models dynamically: {exc}")
    return discovered


def generate_content_with_fallback(prompt, stage_name):
    """Try active Gemini 3 models; skip 404s immediately and retry transient 503/429 errors."""
    candidates = list(dict.fromkeys(MODEL_NAMES))
    last_error = None
    discovered_checked = False

    attempt = 0
    while attempt < 10:
        if not candidates and not discovered_checked:
            discovered_checked = True
            discovered = discover_available_flash_models()
            if discovered:
                print(f"{stage_name}: discovered available models: {discovered}")
                candidates.extend(discovered)

        if not candidates:
            break

        model_name = candidates.pop(0)
        attempt += 1
        try:
            print(f"{stage_name}: calling {model_name} (attempt {attempt})...")
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

            # Skip deprecated/missing models immediately
            if "404" in message or "NOT_FOUND" in message:
                continue

            # Backoff & re-queue on temporary overload or rate limits
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
                delay = min(5 * (2 ** min(attempt - 1, 4)), 45)
                print(f"Temporary Gemini error; retrying after {delay}s...")
                time.sleep(delay)
                candidates.append(model_name)
                continue

            raise

    raise RuntimeError(
        f"{stage_name}: all Gemini model attempts failed. Last error: {last_error}"
    )


def generate_metadata_for_study(title, scripture, summary, details):
    prompt = f"""You are a senior Christian media producer preparing production metadata for a daily YouTube video.
Study Title: {title}
Scripture: {scripture}
Summary: {summary}
Details: {details}

Provide production metadata for this study to prepare it for rendering. Return ONLY valid JSON with these exact keys:
- creative_brief: 1 paragraph production overview and core message.
- slide_breakdown: JSON string or HTML bulleted list breaking down 4-5 slides (Slide 1: Title, Slide 2: Context, Slide 3: Core Truth, Slide 4: Application).
- visual_theme: Cinematic visual style description (e.g., "Warm cinematic lighting, golden hour coastal nature, peaceful textures").
- voice_style: Narration tone and pace (e.g., "Warm, reflective, measured pacing, authoritative yet comforting baritone").
- music_track: Background ambient style (e.g., "Soft cinematic piano with subtle ambient pad strings, meditative").
- background_image: Visual prompt/description for background artwork (e.g., "Serene rolling hills at sunrise with soft golden rays breaking through morning mist").

JSON format:
{{
  "creative_brief": "...",
  "slide_breakdown": "...",
  "visual_theme": "...",
  "voice_style": "...",
  "music_track": "...",
  "background_image": "..."
}}
"""
    response = generate_content_with_fallback(prompt, f"Metadata '{title}'")
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# Iterate through rows and populate missing preparation fields
updated_count = 0
for idx, row in enumerate(records, start=2):  # Row 1 is header
    title = str(row.get("title", "")).strip()
    if not title:
        continue

    # Check if any of the target render-preparation columns are empty
    missing_fields = [
        col for col in TARGET_COLUMNS
        if not str(row.get(col, "")).strip()
    ]

    if missing_fields:
        print(f"Preparing metadata for row {idx}: '{title}' (Missing: {missing_fields})")
        scripture = str(row.get("scripture", ""))
        summary = str(row.get("summary", ""))
        details = str(row.get("details", ""))

        try:
            meta = generate_metadata_for_study(title, scripture, summary, details)

            # Map generated keys to sheet columns
            mapping = {
                "Creative Brief": meta.get("creative_brief", ""),
                "Slide Breakdown": meta.get("slide_breakdown", ""),
                "Visual Theme": meta.get("visual_theme", ""),
                "Voice Style": meta.get("voice_style", ""),
                "Music Track": meta.get("music_track", ""),
                "Background Image": meta.get("background_image", ""),
            }

            for col_name, val in mapping.items():
                col_idx = header_map.get(col_name.casefold())
                if col_idx and val:
                    # Serialize list/dict slide breakdowns cleanly if Gemini returns structured JSON
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    sheets_call_with_retry(sheet.update_cell, idx, col_idx, val)

            updated_count += 1
            print(f"Successfully prepared & populated row {idx}: {title}")
            time.sleep(1)  # Gentle rate limit pause for Gemini & Sheets
        except Exception as e:
            print(f"ERROR generating metadata for '{title}': {e}")

print(f"Preparation phase complete. Updated {updated_count} study/studies with render metadata.")
