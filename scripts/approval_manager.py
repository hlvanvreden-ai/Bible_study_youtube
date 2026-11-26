import argparse
import functools
import json
import os
import smtplib
import time
from email.message import EmailMessage

import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

# Ensure logs flush immediately in CI/CD environments
print = functools.partial(print, flush=True)

# --- 1. CONFIGURATION & SECRETS ---
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
REVIEW_WEBAPP_URL = os.environ.get("REVIEW_WEBAPP_URL", "").strip()
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").strip()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()

SPREADSHEET_ID = "1laiol-0gmUJS7yHennQXa6w8bWjfbqzLqRc0lWReOL0"
VIDEO_STATUS_COL = 7   # Column G ("Video Status")
YOUTUBE_URL_COL = 10   # Column J ("YouTube URL")

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# --- 2. GOOGLE SHEETS HELPERS ---
def sheets_call_with_retry(fn, *args, **kwargs):
    """Retry Google Sheets API calls on 429 quota or transient 5xx errors."""
    for attempt in range(1, 7):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("429", "Quota exceeded", "500", "502", "503", "504")):
                delay = min(15 * attempt, 75)
                print(f"Sheets API limit ({msg[:100]}); retrying in {delay}s (attempt {attempt}/6)...")
                time.sleep(delay)
                continue
            raise
    return fn(*args, **kwargs)


def connect_to_sheet():
    """Authenticate and return Sheet 1 after verifying Column G and Column J headers."""
    creds = ServiceAccountCredentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    workbook = sheets_call_with_retry(gc.open_by_key, SPREADSHEET_ID)
    sheet = workbook.sheet1

    header_row = sheets_call_with_retry(sheet.row_values, 1)

    def get_header(col_idx):
        return header_row[col_idx - 1].strip() if len(header_row) >= col_idx else ""

    col_g_header = get_header(VIDEO_STATUS_COL)
    if col_g_header.casefold() != "video status":
        raise RuntimeError(
            f"Expected Column G (col {VIDEO_STATUS_COL}) to be 'Video Status', found '{col_g_header}'."
        )

    col_j_header = get_header(YOUTUBE_URL_COL)
    if col_j_header.casefold() != "youtube url":
        raise RuntimeError(
            f"Expected Column J (col {YOUTUBE_URL_COL}) to be 'YouTube URL', found '{col_j_header}'."
        )

    return sheet


def cell_value(row_values, col_idx):
    """Safely read a 1-based column from a raw row list."""
    return str(row_values[col_idx - 1]).strip() if len(row_values) >= col_idx else ""


# --- 3. APPROVAL WORKFLOW ACTIONS ---
def send_review_notification(row_idx, study_title, youtube_url):
    """Send a Gmail notification with a direct link to the HTML approval page."""
    if REVIEW_WEBAPP_URL:
        separator = "&" if "?" in REVIEW_WEBAPP_URL else "?"
        review_link = f"{REVIEW_WEBAPP_URL}{separator}row={row_idx}"
    else:
        review_link = youtube_url

    print(f"Approval page link: {review_link}")

    if not (NOTIFY_EMAIL and SMTP_USER and SMTP_PASS):
        print("SMTP secrets not configured; skipping email notification.")
        return False

    message = EmailMessage()
    message["Subject"] = f"🎬 Review Ready: {study_title} (Row {row_idx})"
    message["From"] = SMTP_USER
    message["To"] = NOTIFY_EMAIL
    message.set_content(
        f"Your Bible study video '{study_title}' (row {row_idx}) is prepared and ready for review.\n\n"
        f"Approve or review in the HTML portal:\n{review_link}\n\n"
        f"Unlisted YouTube video:\n{youtube_url}\n"
    )

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(message)

    print(f"Review notification sent to {NOTIFY_EMAIL}.")
    return True


def notify_prepared_videos(sheet):
    """Scan rows where Column G is 'Prepared', read YouTube URL from Column J, email the review link, and set Column G to 'Pending Review'."""
    all_rows = sheets_call_with_retry(sheet.get_all_values)
    notified_count = 0

    for row_idx, row_values in enumerate(all_rows[1:], start=2):
        status = cell_value(row_values, VIDEO_STATUS_COL).casefold()
        if status != "prepared":
            continue

        title = cell_value(row_values, 2) or "Untitled Study"  # Column B ("Title")
        youtube_url = cell_value(row_values, YOUTUBE_URL_COL)  # Column J ("YouTube URL")

        if not youtube_url:
            print(f"Row {row_idx} ('{title}') is marked Prepared in G{row_idx} but J{row_idx} (YouTube URL) is empty; skipping.")
            continue

        sent = send_review_notification(row_idx, title, youtube_url)
        if sent:
            sheets_call_with_retry(sheet.update_cell, row_idx, VIDEO_STATUS_COL, "Pending Review")
            print(f"Updated cell G{row_idx} to 'Pending Review'.")
            notified_count += 1

    print(f"Notification run complete. Sent {notified_count} approval email(s).")


def mark_row_approved(sheet, row_idx):
    """Mark Column G ('Video Status') for the given row as 'Approved'."""
    if row_idx < 2:
        raise ValueError("Row index must be 2 or greater (row 1 is headers).")

    youtube_url = sheets_call_with_retry(sheet.cell, row_idx, YOUTUBE_URL_COL).value or ""
    if not str(youtube_url).strip():
        raise RuntimeError(f"Cannot approve row {row_idx}: cell J{row_idx} (YouTube URL) is empty.")

    sheets_call_with_retry(sheet.update_cell, row_idx, VIDEO_STATUS_COL, "Approved")
    print(f"Cell G{row_idx} marked as 'Approved' for video {youtube_url.strip()}.")


# --- 4. CLI ENTRYPOINT ---
def main():
    parser = argparse.ArgumentParser(
        description="Send approval links for prepared videos (Column J) and mark Column G ('Video Status') as Approved."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "notify",
        help="Scan Column G for 'Prepared', read YouTube URL from Column J, email approval link, and set G to 'Pending Review'.",
    )

    approve_parser = subparsers.add_parser(
        "approve",
        help="Verify Column J has a YouTube URL and update Column G ('Video Status') to 'Approved'.",
    )
    approve_parser.add_argument(
        "--row",
        type=int,
        required=True,
        help="1-based row number to mark as Approved (e.g. 2).",
    )

    args = parser.parse_args()
    sheet = connect_to_sheet()

    if args.command == "notify":
        notify_prepared_videos(sheet)
    elif args.command == "approve":
        mark_row_approved(sheet, args.row)


if __name__ == "__main__":
    main()
