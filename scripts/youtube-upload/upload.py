#!/usr/bin/env python3
"""Upload and schedule The Code Professor Shorts with the YouTube Data API v3.

Each lesson folder under SHORTS_DIR (<Topic>/<lesson>/) holds:
  <lesson>.mp4, "<lesson> - thumbnail.png" (any case), and upload.json (see upload.json.example).

The script uploads every lesson whose upload.json says status "pending" (and not "hold"),
in "order", one per day at RELEASE_TIME, and writes the result back into upload.json.

A lesson with "demo": true (e.g. SHORTS_DIR/Demo/Demo) is skipped by normal runs and is
uploaded only with --only "<folder>", even before the audit is approved (it stays private).

Usage (from this folder, with a .env next to this file):
  python upload.py check            test credentials and channel (read-only, ~1 quota unit)
  python upload.py plan             show what would be uploaded and when (offline, no API calls)
  python upload.py upload [--limit N] [--only "<lesson>"]
  python upload.py report           rewrite SHORTS_DIR/_plan/upload-schedule.md from the upload.json files

Setup: python -m pip install -r requirements.txt   (tzdata, pillow)
"""
import argparse, datetime as dt, glob, json, os, sys, tempfile, time
import http.client, urllib.error, urllib.parse, urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
CHUNK = 8 * 1024 * 1024
STOP_REASONS = {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"}


# ---------------------------------------------------------------- config

def load_env(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    cfg = {
        "client_id": os.environ.get("YT_CLIENT_ID", ""),
        "client_secret": os.environ.get("YT_CLIENT_SECRET", ""),
        "refresh_token": os.environ.get("YT_REFRESH_TOKEN", ""),
        "shorts_dir": Path(os.path.expandvars(os.path.expanduser(
            os.environ.get("SHORTS_DIR", "~/Documents/Shorts")))),
        "audit_approved": os.environ.get("YT_AUDIT_APPROVED", "false").lower() in ("1", "true", "yes"),
        "release_time": os.environ.get("RELEASE_TIME", "19:00"),
        "tz": os.environ.get("TIMEZONE", "Asia/Jerusalem"),
        "channel": os.environ.get("EXPECTED_CHANNEL", "The Code Professor"),
    }
    try:
        from zoneinfo import ZoneInfo
        cfg["zone"] = ZoneInfo(cfg["tz"])
    except Exception:
        sys.exit("Time zone data missing: run  python -m pip install -r requirements.txt")
    return cfg


# ---------------------------------------------------------------- HTTP

class ApiError(Exception):
    def __init__(self, status, reason, message):
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason, self.message = status, reason, message


def req(method, url, token=None, body=None, headers=None, raw=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json; charset=UTF-8"
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            txt = resp.read().decode() or "{}"
            return resp.status, dict(resp.headers), json.loads(txt) if txt.strip().startswith("{") else {}
    except urllib.error.HTTPError as e:
        if e.code == 308:  # resumable upload: "Resume Incomplete"
            return 308, dict(e.headers), {}
        txt = e.read().decode(errors="replace")
        reason, msg = "", txt[:500]
        try:
            err = json.loads(txt)
            if isinstance(err.get("error"), dict):
                msg = err["error"].get("message", msg)
                reason = (err["error"].get("errors") or [{}])[0].get("reason", "") or err["error"].get("status", "")
            else:
                reason, msg = str(err.get("error", "")), err.get("error_description", msg)
        except Exception:
            pass
        raise ApiError(e.code, reason, msg)
    except urllib.error.URLError as e:
        raise ApiError(0, "network", str(e.reason))
    except (OSError, http.client.HTTPException) as e:
        raise ApiError(0, "network", f"{type(e).__name__}: {e}")


def access_token(cfg):
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not cfg[k]]
    if missing:
        sys.exit("Missing in .env: " + ", ".join("YT_" + m.upper() for m in missing))
    form = urllib.parse.urlencode({"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                   "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token"}).encode()
    try:
        return req("POST", "https://oauth2.googleapis.com/token", raw=form,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})[2]["access_token"]
    except ApiError as e:
        hint = (" -> the refresh token expired or was revoked; make a new one in the OAuth Playground"
                if e.reason == "invalid_grant" else "")
        sys.exit(f"Could not get an access token: {e}{hint}")


class Token:
    """Access tokens last an hour; refresh before long runs run past it."""
    def __init__(self, cfg):
        self.cfg, self.value, self.at = cfg, None, 0

    def __str__(self):
        if not self.value or time.time() - self.at > 45 * 60:
            self.value, self.at = access_token(self.cfg), time.time()
        return self.value


# ---------------------------------------------------------------- lessons

class Lesson:
    def __init__(self, path):
        self.json_path = path
        self.folder = path.parent
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.data.setdefault("upload", {"status": "pending"})

    @property
    def name(self):
        return self.folder.name

    @property
    def status(self):
        return self.data["upload"].get("status", "pending")

    def save(self):
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.json_path)

    def video(self):
        v = sorted(self.folder.glob("*.mp4"))
        return v[0] if len(v) == 1 else None

    def thumbnail(self):
        c = [p for p in self.folder.iterdir()
             if p.suffix.lower() in (".png", ".jpg", ".jpeg") and " - thumbnail" in p.stem.lower()]
        return max(c, key=lambda p: p.stat().st_mtime) if c else None

    def problems(self):
        d, p = self.data, []
        for k in ("order", "series", "title", "description", "tags", "playlist"):
            if d.get(k) in (None, "", []):
                p.append(f"missing {k}")
        t, desc = d.get("title", ""), d.get("description", "")
        if len(t) > 100:
            p.append(f"title is {len(t)} chars (max 100)")
        if "<" in t + desc or ">" in t + desc:
            p.append("title/description contains < or >")
        if len(desc.encode()) > 5000:
            p.append("description over 5000 bytes")
        if sum(len(x) + (2 if " " in x else 0) for x in d.get("tags", [])) + len(d.get("tags", [])) > 500:
            p.append("tags over 500 chars")
        if not self.video():
            p.append("no single .mp4 in folder")
        if not self.thumbnail():
            p.append("no '<lesson> - thumbnail' image")
        return p


def load_lessons(cfg):
    root = cfg["shorts_dir"]
    if not root.is_dir():
        sys.exit(f"SHORTS_DIR not found: {root}")
    out = []
    for f in glob.glob(str(root / "*" / "*" / "upload.json")):
        try:
            out.append(Lesson(Path(f)))
        except Exception as e:
            print(f"! skipping {f}: {e}")
    return sorted(out, key=lambda l: (l.data.get("order", 9999), l.name))


def local_dt(cfg, s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=cfg["zone"])


def assign_slots(cfg, lessons, include_demo=False):
    """Pending lessons, in order, each get the earliest free day from tomorrow on
    (after the slot given to the lesson before it). A fixed publish_at_local in
    upload.json is used as is if it is still at least 2 hours away."""
    taken = {local_dt(cfg, l.data["upload"]["publish_at_local"]).date()
             for l in lessons if l.status in ("uploaded", "scheduled") and l.data["upload"].get("publish_at_local")}
    hh, mm = map(int, cfg["release_time"].split(":"))
    now = dt.datetime.now(cfg["zone"])
    day = now.date() + dt.timedelta(days=1)
    plan = []
    for l in lessons:
        if l.status != "pending" or l.data.get("hold") or (l.data.get("demo") and not include_demo):
            continue
        fixed = l.data.get("publish_at_local")
        if fixed and local_dt(cfg, fixed) > now + dt.timedelta(hours=2):
            when = local_dt(cfg, fixed)
        else:
            while day in taken:
                day += dt.timedelta(days=1)
            when = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=cfg["zone"])
        taken.add(when.date())
        day = max(day, when.date() + dt.timedelta(days=1))
        plan.append((l, when))
    return plan


# ---------------------------------------------------------------- API steps

def channel(cfg, tok):
    j = req("GET", f"{API}/channels?part=snippet&mine=true", str(tok))[2]
    items = j.get("items") or []
    return (items[0]["snippet"]["title"], items[0]["id"]) if items else (None, None)


def upload_video(tok, lesson, when):
    d = lesson.data
    body = {
        "snippet": {"title": d["title"], "description": d["description"], "tags": d["tags"],
                    "categoryId": "27", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": "private",
                   "publishAt": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                   "selfDeclaredMadeForKids": False, "license": "youtube", "embeddable": True,
                   "publicStatsViewable": True, "containsSyntheticMedia": False},
    }
    path = lesson.video()
    size = path.stat().st_size
    _, hdrs, _ = req("POST", f"{UPLOAD}/videos?uploadType=resumable&part=snippet,status&notifySubscribers=true",
                     str(tok), body=body, headers={"X-Upload-Content-Type": "video/mp4",
                                                   "X-Upload-Content-Length": str(size)})
    loc = hdrs.get("Location") or hdrs.get("location")
    sent = 0
    with open(path, "rb") as f:
        while True:
            f.seek(sent)
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            for attempt in range(5):
                try:
                    st, hdrs, j = req("PUT", loc, str(tok), raw=chunk, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}"})
                    break
                except ApiError as e:
                    if e.status not in (0, 500, 502, 503, 504) or attempt == 4:
                        raise
                    time.sleep(3 * 2 ** attempt)
                    st, hdrs, j = req("PUT", loc, str(tok), raw=b"",
                                      headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})
                    if st in (200, 201):
                        break
                    rng = hdrs.get("Range") or hdrs.get("range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    f.seek(sent)
                    chunk = f.read(CHUNK)
                    end = sent + len(chunk) - 1
            if st in (200, 201):
                if "id" not in j:
                    raise ApiError(st, "no_id", "upload finished but no video id came back")
                return j["id"]
            rng = hdrs.get("Range") or hdrs.get("range")
            sent = int(rng.split("-")[1]) + 1 if rng else end + 1


def thumbnail_file(lesson):
    p = lesson.thumbnail()
    if p.stat().st_size <= 2 * 1024 * 1024:
        return p
    try:
        from PIL import Image
    except ImportError:
        raise ApiError(0, "pillow", "thumbnail over 2 MB and Pillow isn't installed (pip install -r requirements.txt)")
    im = Image.open(p).convert("RGB")
    dst = Path(tempfile.gettempdir()) / f"yt-thumb-{os.getpid()}.jpg"
    for q in range(92, 40, -4):
        im.save(dst, "JPEG", quality=q, optimize=True)
        if dst.stat().st_size < 1.9 * 1024 * 1024:
            return dst
    raise ApiError(0, "thumb_size", "couldn't get the thumbnail under 2 MB")


def set_thumbnail(tok, vid, path):
    ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    req("POST", f"{UPLOAD}/thumbnails/set?videoId={vid}", str(tok), raw=path.read_bytes(),
        headers={"Content-Type": ctype})


_playlists = {}


def add_to_playlist(tok, vid, name, series):
    key = name.strip().lower()
    if not _playlists:
        page = ""
        while True:
            j = req("GET", f"{API}/playlists?part=snippet&mine=true&maxResults=50"
                           + (f"&pageToken={page}" if page else ""), str(tok))[2]
            for p in j.get("items", []):
                _playlists[p["snippet"]["title"].strip().lower()] = p["id"]
            page = j.get("nextPageToken")
            if not page:
                break
        _playlists.setdefault("__loaded__", "")
    if key not in _playlists:
        j = req("POST", f"{API}/playlists?part=snippet,status", str(tok), body={
            "snippet": {"title": name, "defaultLanguage": "en",
                        "description": f"{series} — short hand-drawn lessons by The Code Professor."},
            "status": {"privacyStatus": "public"}})[2]
        _playlists[key] = j["id"]
    req("POST", f"{API}/playlistItems?part=snippet", str(tok), body={
        "snippet": {"playlistId": _playlists[key], "resourceId": {"kind": "youtube#video", "videoId": vid}}})
    return _playlists[key]


def video_status(tok, vid):
    j = req("GET", f"{API}/videos?part=status&id={vid}", str(tok))[2]
    return (j.get("items") or [{}])[0].get("status", {})


# ---------------------------------------------------------------- commands

def cmd_check(cfg, a):
    tok = Token(cfg)
    try:
        title, cid = channel(cfg, tok)
    except ApiError as e:
        sys.exit(f"API call failed: {e}")
    if not title:
        sys.exit("The token works, but no YouTube channel is attached to it.")
    print(f"Token OK. Channel: {title} ({cid})")
    if title != cfg["channel"]:
        sys.exit(f"Wrong channel: expected '{cfg['channel']}'. Redo the OAuth Playground step and pick it.")
    print("Audit approved in .env: " + ("yes" if cfg["audit_approved"] else "NO (uploads are blocked)"))


def cmd_plan(cfg, a):
    lessons = load_lessons(cfg)
    plan = assign_slots(cfg, lessons)
    print(f"{len(lessons)} lessons with upload.json, {len(plan)} to upload:")
    for l, when in plan:
        p = l.problems()
        print(f"  #{l.data.get('order', 0):03d}  {when:%a %Y-%m-%d %H:%M}  {l.name}" + (f"   !! {'; '.join(p)}" if p else ""))
    demos = [l.name for l in lessons if l.data.get("demo") and l.status == "pending"]
    if demos:
        print("Demo lessons (uploaded only with --only): " + ", ".join(demos))
    held = [l.name for l in lessons if l.status == "pending" and l.data.get("hold")]
    if held:
        print("On hold: " + ", ".join(held))
    write_report(cfg, lessons, plan)


def cmd_upload(cfg, a):
    lessons = load_lessons(cfg)
    target = [l for l in lessons if a.only and l.name.lower() == a.only.lower()]
    demo_run = bool(target) and bool(target[0].data.get("demo"))
    if a.only and not target:
        sys.exit(f"No lesson folder named '{a.only}' with an upload.json.")
    if demo_run:
        print("Demo upload: a throwaway test video. Before the audit is approved it will stay private "
              "(locked); delete it in YouTube Studio afterwards.")
    elif not cfg["audit_approved"]:
        sys.exit("YT_AUDIT_APPROVED isn't true in .env. Videos uploaded before Google approves the audit "
                 "are locked private for good, so nothing was uploaded.")
    tok = Token(cfg)
    title, _ = channel(cfg, tok)
    if title != cfg["channel"]:
        sys.exit(f"Signed in as '{title}', expected '{cfg['channel']}'. Nothing was uploaded.")

    # finish anything a previous run left half-done (uploaded but no thumbnail/playlist yet)
    todo = [(l, local_dt(cfg, l.data["upload"]["publish_at_local"])) for l in lessons if l.status == "uploaded"]
    todo += assign_slots(cfg, lessons, include_demo=demo_run)
    if a.only:
        todo = [(l, w) for l, w in todo if l.name.lower() == a.only.lower()]
    if a.limit:
        todo = todo[:a.limit]
    done, failed, stopped = [], [], None
    for l, when in todo:
        p = l.problems()
        if p:
            failed.append((l, "; ".join(p)))
            print(f"x {l.name}: {'; '.join(p)}")
            continue
        u = l.data["upload"]
        try:
            if u.get("status") != "uploaded":
                print(f"> {l.name}: uploading for {when:%Y-%m-%d %H:%M} ...", flush=True)
                vid = upload_video(tok, l, when)
                u.update({"status": "uploaded", "video_id": vid, "publish_at_local": f"{when:%Y-%m-%d %H:%M}",
                          "uploaded_at": dt.datetime.now(cfg["zone"]).isoformat(timespec="seconds")})
                u.pop("error", None)
                l.save()
            vid = u["video_id"]
            notes = []
            try:
                set_thumbnail(tok, vid, thumbnail_file(l))
            except ApiError as e:
                if e.reason in STOP_REASONS:
                    raise
                notes.append(f"thumbnail not set ({e.reason or e.status}: {e.message})")
            u["playlist_id"] = add_to_playlist(tok, vid, l.data["playlist"], l.data["series"])
            s = video_status(tok, vid)
            if demo_run and not s.get("publishAt") and not s.get("rejectionReason") and not s.get("failureReason"):
                u.update({"status": "scheduled", "note": f"demo: YouTube kept it {s.get('privacyStatus')} (expected before the audit)"})
                l.save()
                done.append(l)
                print(f"+ {l.name}: uploaded, {s.get('privacyStatus')} (expected before the audit approves "
                      f"public releases)  https://youtube.com/shorts/{vid}")
                continue
            if not s.get("publishAt") or s.get("rejectionReason") or s.get("failureReason"):
                u["error"] = f"not scheduled as expected: {s}"
                l.save()
                stopped = (f"{l.name} uploaded but isn't scheduled ({s}). It may be locked private "
                           f"(unaudited project). Check it in YouTube Studio before running again.")
                break
            u["status"] = "scheduled"
            if notes:
                u["notes"] = notes
            l.save()
            done.append(l)
            print(f"+ {l.name}: scheduled {u['publish_at_local']}  https://youtube.com/shorts/{vid}"
                  + (f"  ({'; '.join(notes)})" if notes else ""))
        except ApiError as e:
            if e.reason in STOP_REASONS:
                stopped = f"YouTube's daily limit reached ({e.reason}). Run again tomorrow; it picks up where it stopped."
                break
            u["error"] = str(e)
            l.save()
            failed.append((l, str(e)))
            print(f"x {l.name}: {e}")
            if e.status in (0, 401, 403) and e.reason not in ("forbidden",):
                stopped = f"Stopped after an access/network error: {e}"
                break
    write_report(cfg, load_lessons(cfg), assign_slots(cfg, load_lessons(cfg)))
    print(f"\nDone: {len(done)} scheduled, {len(failed)} failed.")
    for l in done:
        rel = related_hint(cfg, l)
        if rel:
            print(f"  related video for {l.name}: {rel}")
    if stopped:
        print("STOPPED: " + stopped)
    sys.exit(1 if failed or stopped else 0)


def related_hint(cfg, lesson):
    want = (lesson.data.get("related_lesson") or "").strip().lower()
    if not want:
        return ""
    for l in load_lessons(cfg):
        if l.name.lower() == want or l.data.get("title", "").lower().startswith(want):
            u = l.data["upload"]
            if u.get("video_id"):
                when = u.get("publish_at_local")
                live = when and local_dt(cfg, when) <= dt.datetime.now(cfg["zone"])
                return f"{l.name} ({u['video_id']}{'' if live or not when else ', public from ' + when})"
            return f"{l.name} (not on YouTube yet)"
    return ""


def write_report(cfg, lessons, plan):
    planned = {id(l): w for l, w in plan}
    rows = []
    for l in lessons:
        if l.data.get("demo"):
            continue
        u, n = l.data["upload"], l.data.get("order", 0)
        if u.get("status") in ("scheduled", "uploaded", "manual"):
            when = u.get("publish_at_local") or "—"
            vid = u.get("video_id", "?")
            mark = "x" if u["status"] != "uploaded" else "~"
            rows.append(f"- [{mark}] #{n:03d} | {when} | {l.data.get('series','')} | {l.name} | video: {vid}"
                        + (f" | related: {l.data['related_lesson']}" if l.data.get("related_lesson") else ""))
        elif l.data.get("hold"):
            rows.append(f"- [ ] #{n:03d} | on hold | {l.data.get('series','')} | {l.name}")
        else:
            w = planned.get(id(l))
            err = f" | last error: {u['error']}" if u.get("error") else ""
            rows.append(f"- [ ] #{n:03d} | {w:%Y-%m-%d %H:%M} (planned) | {l.data.get('series','')} | {l.name}{err}"
                        if w else f"- [ ] #{n:03d} | — | {l.data.get('series','')} | {l.name}{err}")
    out = cfg["shorts_dir"] / "_plan" / "upload-schedule.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("# Upload schedule — generated by upload.py, don't edit by hand\n\n"
                   f"Updated {dt.datetime.now(cfg['zone']):%Y-%m-%d %H:%M}. "
                   "[x] scheduled/public, [~] uploaded but unfinished, [ ] waiting. "
                   "Edit upload.json in a lesson folder to change anything.\n\n" + "\n".join(rows) + "\n",
                   encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check")
    sp.add_parser("plan")
    sp.add_parser("report")
    u = sp.add_parser("upload")
    u.add_argument("--limit", type=int, help="upload at most N lessons")
    u.add_argument("--only", help="upload just this lesson (folder name)")
    a = p.parse_args()
    cfg = load_env()
    if a.cmd == "report":
        ls = load_lessons(cfg)
        write_report(cfg, ls, assign_slots(cfg, ls))
        print("Wrote _plan/upload-schedule.md")
    else:
        {"check": cmd_check, "plan": cmd_plan, "upload": cmd_upload}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
