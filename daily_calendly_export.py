#!/usr/bin/env python3
"""Export new Calendly routing form submissions as CSV and email them.

Usage:
  python3 daily_calendly_export.py                 # last 24 hours, then email
  python3 daily_calendly_export.py --hours 48      # wider window
  python3 daily_calendly_export.py --no-email      # write the CSV only
  python3 daily_calendly_export.py --list-forms    # show routing form URIs for your org

Settings come from environment variables or a .env file next to this script.
Standard library only, so no pip install is needed.
"""

import argparse
import csv
import io
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

API = "https://api.calendly.com"
HERE = Path(__file__).resolve().parent
EXPORT_DIR = HERE / "exports"


def load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def require(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing setting {name}. Add it to {HERE / '.env'} (see .env.example).")
    return value


def api_get(token, url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "daily-calendly-export/1.0"}
    for attempt in range(1, 5):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            # Rate limits and server errors are worth retrying; other errors (bad token, plan) are not.
            if error.code not in (429, 500, 502, 503, 504) or attempt == 4:
                sys.exit(f"Calendly returned HTTP {error.code} for {url}\n{body}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                sys.exit(f"Could not reach Calendly for {url} after 4 attempts. Try again later.")
        time.sleep(2 ** attempt)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_submissions_since(token, form_uri, since):
    """Return submissions created at or after `since`, oldest first.

    The API returns newest first, so paging stops as soon as a submission is older than `since`.
    """
    collected = []
    params = {"form": form_uri, "count": 100, "sort": "created_at:desc"}
    while True:
        page = api_get(token, f"{API}/routing_form_submissions", params)
        for submission in page["collection"]:
            if parse_time(submission["created_at"]) < since:
                return sorted(collected, key=lambda s: s["created_at"])
            collected.append(submission)
        next_token = page["pagination"].get("next_page_token")
        if not next_token:
            return sorted(collected, key=lambda s: s["created_at"])
        params["page_token"] = next_token


def add_booking_times(token, submissions):
    """Add booked_at and event_starts_at to each booked submission.

    The submission only links to the invitee. The invitee gives the booking time (created_at),
    and the invitee's event gives the start time. Events are fetched once and reused.
    """
    event_starts = {}
    for submission in submissions:
        submission["booked_at"] = ""
        submission["event_starts_at"] = ""
        if not submission.get("submitter"):
            continue
        invitee = api_get(token, submission["submitter"])["resource"]
        event_uri = invitee["event"]
        if event_uri not in event_starts:
            event_starts[event_uri] = api_get(token, event_uri)["resource"]["start_time"]
        submission["booked_at"] = invitee["created_at"]
        submission["event_starts_at"] = event_starts[event_uri]


def build_csv(submissions):
    """One row per submission. Question labels become columns, in the order they first appear."""
    questions = []
    for submission in submissions:
        for qa in submission.get("questions_and_answers", []):
            label = qa.get("question") or ""
            if label not in questions:
                questions.append(label)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Submitted at (UTC)", "Event scheduled", "Routing result", "Booked At (UTC)", "Event Starts At (UTC)", *questions,
    ])
    for submission in submissions:
        answers = {qa.get("question"): qa.get("answer") or "" for qa in submission.get("questions_and_answers", [])}
        result = submission.get("result") or {}
        writer.writerow([
            submission["created_at"],
            "Yes" if submission.get("submitter") else "No",
            result.get("type", ""),
            submission.get("booked_at", ""),
            submission.get("event_starts_at", ""),
            *[answers.get(label, "") for label in questions],
        ])
    return buffer.getvalue()


def send_email(csv_text, filename, subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = require("SMTP_USER")
    message["To"] = require("EMAIL_TO")
    message.set_content(body)
    message.add_attachment(csv_text.encode("utf-8"), maintype="text", subtype="csv", filename=filename)

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(message["From"], require("SMTP_PASSWORD"))
        smtp.send_message(message)


def list_forms(token):
    me = api_get(token, f"{API}/users/me")["resource"]
    forms = api_get(token, f"{API}/routing_forms", {"organization": me["current_organization"]})["collection"]
    for form in forms:
        print(f"{form['name']}\n  {form['uri']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=int, default=24, help="how far back to look (default: 24)")
    parser.add_argument("--no-email", action="store_true", help="write the CSV but do not send it")
    parser.add_argument("--list-forms", action="store_true", help="print routing form names and URIs, then exit")
    args = parser.parse_args()

    load_env_file(HERE / ".env")
    token = require("CALENDLY_TOKEN")

    if args.list_forms:
        list_forms(token)
        return

    form_uri = require("CALENDLY_FORM_URI")
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)

    submissions = fetch_submissions_since(token, form_uri, since)
    add_booking_times(token, submissions)
    csv_text = build_csv(submissions)

    EXPORT_DIR.mkdir(exist_ok=True)
    local_date = datetime.now().strftime("%Y-%m-%d")
    filename = f"calendly_leads_{local_date}.csv"
    (EXPORT_DIR / filename).write_text(csv_text, encoding="utf-8")
    print(f"Wrote {len(submissions)} submission(s) to {EXPORT_DIR / filename}")

    if args.no_email:
        return

    subject = f"Calendly leads {local_date}: {len(submissions)} new"
    body = "Hi,\n\nPlease find the last 24 hour submissions attached.\n\nThanks"
    send_email(csv_text, filename, subject, body)
    print(f"Emailed {filename} to {os.environ.get('EMAIL_TO')}")


if __name__ == "__main__":
    main()
