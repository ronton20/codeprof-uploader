# YouTube upload script — The Code Professor

Uploads and schedules the channel's Shorts through the YouTube Data API v3. The videos and their metadata stay on the PC (in `SHORTS_DIR`), never in this repo.

## Setup (once)

1. Install Python 3.10+ (python.org, tick "Add to PATH"), then in this folder:
   `python -m pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in the client ID, client secret and refresh token (from the Google Cloud project and the OAuth Playground). Set `SHORTS_DIR` if the videos aren't in `~/Documents/Shorts`.
3. `python upload.py check` should print `Channel: The Code Professor`.
4. When Google approves the API audit, set `YT_AUDIT_APPROVED=true`.

## Folder layout it expects

```
SHORTS_DIR/
  <Topic>/<lesson>/
    <lesson>.mp4
    <lesson> - thumbnail.png      (any case; > 2 MB is shrunk to a JPEG automatically)
    upload.json                   (see upload.json.example; written when the video is saved)
  _plan/upload-schedule.md        (written by the script, a readable overview)
```

## Use

- `python upload.py plan`: what would be uploaded and on which day, plus any problems with a lesson. No API calls.
- `python upload.py upload`: uploads every `pending` lesson in `order`, one per day at `RELEASE_TIME`, starting tomorrow and filling free days. Use `--limit N` or `--only "<lesson folder>"` to do fewer.
- `python upload.py report`: rewrites `_plan/upload-schedule.md`.

Each upload sets the title, description, tags, thumbnail and playlist (the playlist is created if it's missing). It also sets Education, English, not made for kids, no altered/synthetic content and the standard license, schedules the video private until release, and notifies subscribers. The result goes back into `upload.json` (`status`, `video_id`, `publish_at_local`).

- **Hold a lesson:** `"hold": true`.
- **Fix the date:** `"publish_at_local": "2026-12-01 19:00"`.
- **Re-upload:** delete the video in Studio, then set `"upload": {"status": "pending"}`.

## Demo upload (for the API audit screencast)

`SHORTS_DIR/Demo/Demo/` holds a 10-second test clip whose `upload.json` has `"demo": true`. Normal runs skip it. To upload it, even before the audit is approved, run `python upload.py upload --only Demo`. Before approval YouTube keeps it private. Delete the video and the "API Test" playlist in Studio afterwards.

The API can't set a Short's related-video link, so the script prints the suggested one (`related_lesson`) for you to set in Studio.

The default API quota allows roughly 6 uploads a day. When it runs out, the script stops cleanly and the next run continues where it left off. To run it daily, schedule `python upload.py upload` in Windows Task Scheduler (start in this folder).
