# AI/ML Event Email Workflow

This package contains a GitHub Actions workflow that finds upcoming free online AI/ML events, groups them by source website, sends one email per source, appends the same events to a Google Sheet, and stores history so previously emailed events are skipped.

## What It Does

- Runs automatically every 3 days.
- Searches Eventbrite, Meetup, Devpost, Sessionize, AI conference calendars, and university event pages.
- Keeps only free, online, non-expired events.
- Deduplicates against `data/sent_events.json`.
- Sends one email per source website.
- Uses the event title as the subject when a source has one new event.
- Uses `[Source] X Upcoming Free Online AI/ML Events` when a source has multiple new events.
- Appends rows to Google Sheets.
- Commits updated history back to the repository.

## Setup

Copy these files into a GitHub repository, then add these GitHub repository secrets:

| Secret | Purpose |
| --- | --- |
| `SMTP_HOST` | SMTP server hostname, for example `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port, usually `587` |
| `SMTP_USERNAME` | SMTP username |
| `SMTP_PASSWORD` | SMTP password or app password |
| `EMAIL_FROM` | Sender email address |
| `EMAIL_TO` | Recipient email address, comma-separated for multiple recipients |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON credentials for a Google service account |
| `GOOGLE_SHEET_ID` | The destination Google Sheet ID |
| `GOOGLE_SHEET_TAB` | Optional. Defaults to `Events` |

For Google Sheets, share the spreadsheet with the service account email address from the credentials JSON.

## Source Configuration

Edit `config/sources.yml` to adjust search terms, search domains, and fixed calendar pages. The workflow uses web search plus JSON-LD/schema extraction where possible, then falls back to page metadata.

University and conference calendar pages vary a lot, so add high-quality pages under `seed_urls` when you know specific institutions or calendars you care about.

## Run Manually

From GitHub Actions, open **AI/ML Event Digest** and choose **Run workflow**.

Locally:

```bash
pip install -r requirements.txt
python scripts/ai_events_workflow.py
```

For a dry run that does not send email, write to Google Sheets, or update history:

```bash
python scripts/ai_events_workflow.py --dry-run
```

