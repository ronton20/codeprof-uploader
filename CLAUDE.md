# codeprof-uploader

GitHub Pages site for the "Code Professor Uploader" Google OAuth app (index, privacy, terms) plus the script that uploads The Code Professor's YouTube Shorts.

## Uploading Shorts

When the user asks to upload, schedule or check YouTube uploads, use `scripts/youtube-upload/upload.py` (see its README). Run it from `scripts/youtube-upload` with `python` (or `py`). Never read, print, edit or commit `.env`.

1. `python upload.py plan` (offline). Mention any lessons it flags with problems; the upload step skips them and does the rest. If nothing is pending, say so and stop.
2. `python upload.py upload` (add `--limit N` or `--only "<lesson folder>"` if asked). Allow up to 30 minutes.
   - "YT_AUDIT_APPROVED isn't true": stop and tell the user. Never change it yourself; only the user sets it, after Google approves the audit.
   - "Missing in .env", "Could not get an access token" or "Wrong channel": stop and pass the message on.
   - "STOPPED: YouTube's daily limit": report what was done; the next run continues.
   - A video "isn't scheduled": stop and tell the user to check it in YouTube Studio.
3. Reply with a short table (lesson, release date/time, `youtube.com/shorts/<id>`), then the related videos to set by hand in Studio, and any failures with their reason.

The videos and their `upload.json` files live in `~/Documents/Shorts` and must never be added to this repo.
