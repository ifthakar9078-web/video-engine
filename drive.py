#!/usr/bin/env python3
"""
drive.py — Google Drive stages of the AI Video Compiler engine (TOOL SPEC v3).

Runs on a GitHub Actions runner (see .github/workflows/drive.yml). Drive is
reached keylessly: the workflow exchanges its GitHub identity for a Google
access token (Workload Identity Federation) and exports it as DRIVE_TOKEN.

Stages (env MODE = cutter | merger | both, MAX_SOURCES = int, default 4):
  CUTTER — take new videos from the Drive "Video Inbox" one by one, Gemini
           analyses each (ONE category + scene segments), cut the scenes and
           save them into that category's pool folder inside "Video Pools"
           (manifest _pool.json per category folder). Progress is reported
           to the board Worker as source records (POST /sources).
  MERGER — when a category pool holds >= 70s of unused clips, build ONE
           compilation: shuffled clips (longest first as the hook), 61-90s,
           fresh Gemini script for the sequence, Kokoro voice (board-selected,
           default am_eric @ 1.1x), render_bundle.py (H.265 CRF 19), upload
           the result to "Video Finished", publish it as a GitHub Release
           asset and report a done job to the Worker.

Patterns (Gemini resumable upload, JSON normalisation, script blueprint,
Kokoro/Gemini-TTS voice, bundle layout, release publishing, Worker
reporting) mirror the proven produce.py embedded in produce.yml.
"""
import json, os, random, re, shutil, subprocess, sys, time
import urllib.request, urllib.error, urllib.parse
from pathlib import Path

QUEUE_URL = os.environ.get("QUEUE_URL", "https://video-compiler-queue.hi-rajabahmad.workers.dev").rstrip("/")
QUEUE_KEY = os.environ.get("QUEUE_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DRIVE_TOKEN = os.environ.get("DRIVE_TOKEN", "")
RUN_URL = os.environ.get("RUN_URL", "")
MODE = (os.environ.get("MODE") or "both").strip().lower()
if MODE not in ("cutter", "merger", "both"):
    MODE = "both"
try:
    MAX_SOURCES = max(0, int(os.environ.get("MAX_SOURCES") or 4))
except ValueError:
    MAX_SOURCES = 4
REPO = "ifthakar9078-web/video-engine"
API = "https://generativelanguage.googleapis.com/v1beta"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
ANALYSIS_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview",
              "gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
FOLDER_MIME = "application/vnd.google-apps.folder"
# Fallbacks — the live values come from the board settings.
INBOX_ID = os.environ.get("INBOX_ID", "1QtcFlEcXnKIDDr3NxLL3pTX51iSqDfNn")
FINISHED_ID = os.environ.get("FINISHED_ID", "1bVg1ktG65X2idppb_YuX-3H5S1GATkTI")
POOLS_ID = os.environ.get("POOLS_ID", "1Cx-EG5V7_1pe_CDYO-9rsRQYS59GSxzD")
WORK = Path("work-drive"); WORK.mkdir(exist_ok=True)
VOICE = "am_eric"
VOICES = ["am_eric"]
VOICE_COUNTER = 0
NUMWORDS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
            "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
            "Eighteen", "Nineteen", "Twenty"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_dur(p):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)])
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def probe_wh(p):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(p)])
    try:
        w, h = r.stdout.strip().split(",")
        return int(w), int(h)
    except Exception:
        return 720, 1280


# ---------------- queue worker ----------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) video-compiler-engine"


def qapi(method, path, payload=None):
    req = urllib.request.Request(QUEUE_URL + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Queue-Key": QUEUE_KEY,
                 "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get_board():
    req = urllib.request.Request(QUEUE_URL + "/api/board", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def engine_status(state, note):
    log(f"engine: {state} — {note}")
    try:
        qapi("POST", "/engine-status", {"state": state, "note": note, "run_url": RUN_URL})
    except Exception as e:
        log(f"engine status failed (ignored): {e}")


def post_now(text, category="", phase=0):
    """Tell the board what this engine is doing right now (the board's
    'Working now' line). Best-effort — never blocks production."""
    try:
        qapi("POST", "/now", {"text": text, "category": category, "phase": phase})
    except Exception:
        pass


def post_source(record):
    """Upsert ONE source record. POST /sources replaces the whole list, so
    re-read the board and merge by id, keeping everyone else's records."""
    try:
        fresh = get_board().get("sources") or []
        merged = [s for s in fresh if s.get("id") != record["id"]] + [record]
        qapi("POST", "/sources", {"sources": merged})
    except Exception as e:
        log(f"source report failed (ignored): {e}")


def post_drive_pools():
    """Publish the tool's Drive pool state to the board, read from the actual
    _pool.json manifests in the Video Pools folder — so the board's pool
    numbers always match what is really in Google Drive."""
    try:
        pools = []
        per_source = {}
        for f in drive_list(POOLS_ID):
            if f.get("mimeType") != FOLDER_MIME:
                continue
            manifest, _fid = load_pool_manifest(f["id"])
            clips = manifest.get("clips", [])
            if not clips:
                # Skip empty staging folders (e.g. the leftover "other"
                # folder, which is empty and cannot be deleted — its owner
                # is the old service account). Only real pools are shown.
                continue
            pools.append({
                "category": f["name"],
                "clips": len(clips),
                "clips_used": sum(1 for c in clips if c.get("used")),
                "seconds": round(sum(float(c.get("dur", 0)) for c in clips), 1),
                "seconds_free": round(sum(float(c.get("dur", 0)) for c in clips
                                        if not c.get("used")), 1),
            })
            for c in clips:
                sid = c.get("source_id")
                if not sid:
                    continue
                st = per_source.setdefault(sid, {"clips": 0, "clips_used": 0})
                st["clips"] += 1
                if c.get("used"):
                    st["clips_used"] += 1
        qapi("POST", "/drive-pools", {"pools": pools})
        log(f"drive pools reported: {[(p['category'], p['clips']) for p in pools]}")
        # Keep each pooled source's clip stats fresh on the board, so its
        # card shows formed / merged / remaining accurately.
        try:
            current = {s.get("id"): s for s in (get_board().get("sources") or [])}
            for sid, st in per_source.items():
                rec = current.get(f"drive-{sid}")
                if rec and rec.get("stage") == "pooled" and (
                        rec.get("clips") != st["clips"]
                        or rec.get("clips_used") != st["clips_used"]):
                    post_source({**rec, "clips": st["clips"],
                                 "clips_used": st["clips_used"]})
        except Exception as e:
            log(f"source clip-stat refresh failed (ignored): {e}")
    except Exception as e:
        log(f"drive pools report failed (ignored): {e}")


def post_limits():
    """Publish today's Gemini usage (counters + soft caps) to the board's
    Limits section — one batched write at the end of a run. Best-effort."""
    try:
        q = _quota()
        snap = {"date": q.get("date", ""),
                "caps": dict(QUOTA_CAPS),
                "used": {k: int(q.get(k, 0)) for k in QUOTA_CAPS}}
        qapi("POST", "/limits", snap)
        log(f"limits reported: {snap['used']} of {snap['caps']} "
            f"({snap['date']})")
    except Exception as e:
        log(f"limits report failed (ignored): {e}")


def set_job_stage(jid, stage, note="", error=None, output=None):
    log(f"job {jid}: {stage} {note}")
    body = {"stage": stage, "note": note}
    if error is not None:
        body["error"] = error
    if output is not None:
        body["output"] = output
    try:
        qapi("POST", f"/jobs/{jid}/status", body)
    except Exception as e:
        log(f"job stage report failed (ignored): {e}")


# ---------------- google drive ----------------
def drive_req(method, url, data=None, headers=None, timeout=300):
    h = {"Authorization": f"Bearer {DRIVE_TOKEN}"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                last = e
                time.sleep(5 * (attempt + 1))
                continue
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            raise RuntimeError(f"Drive HTTP {e.code} on {method} "
                               f"{url.split('?')[0]}: {body}")
    raise last


def drive_get_json(url):
    with drive_req("GET", url, timeout=120) as r:
        return json.loads(r.read())


def drive_list(folder_id):
    files, token = [], None
    while True:
        q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
        url = (f"{DRIVE}/files?q={q}&pageSize=100&orderBy=createdTime"
               "&fields=files(id,name,mimeType,size,createdTime,thumbnailLink),nextPageToken")
        if token:
            url += f"&pageToken={urllib.parse.quote(token)}"
        d = drive_get_json(url)
        files += d.get("files", [])
        token = d.get("nextPageToken")
        if not token:
            return files


def drive_find(folder_id, name, mime=None):
    for f in drive_list(folder_id):
        if f["name"] == name and (mime is None or f.get("mimeType") == mime):
            return f
    return None


def drive_create_folder(name, parent_id):
    body = json.dumps({"name": name, "mimeType": FOLDER_MIME,
                       "parents": [parent_id]}).encode()
    with drive_req("POST", f"{DRIVE}/files", data=body,
                   headers={"Content-Type": "application/json"}) as r:
        return json.loads(r.read())


def drive_download(file_id, dest):
    url = f"{DRIVE}/files/{file_id}?alt=media"
    with drive_req("GET", url, timeout=1800) as r, open(dest, "wb") as out:
        shutil.copyfileobj(r, out, 1024 * 1024)


def drive_download_json(file_id):
    url = f"{DRIVE}/files/{file_id}?alt=media"
    with drive_req("GET", url, timeout=120) as r:
        return json.loads(r.read())


def drive_upload(name, parent_id, path, mime):
    """Resumable upload; returns the created file resource (id, webViewLink)."""
    size = os.path.getsize(path)
    meta = json.dumps({"name": name, "parents": [parent_id]}).encode()
    init_url = f"{UPLOAD}/files?uploadType=resumable&fields=id,name,webViewLink"
    with drive_req("POST", init_url, data=meta, timeout=120,
                   headers={"Content-Type": "application/json",
                            "X-Upload-Content-Type": mime,
                            "X-Upload-Content-Length": str(size)}) as r:
        loc = r.headers.get("Location")
    if not loc:
        raise RuntimeError(f"Drive upload init failed for {name}: no session URL")
    with open(path, "rb") as fh:
        data = fh.read()
    with drive_req("PUT", loc, data=data, timeout=1800,
                   headers={"Content-Type": mime,
                            "Content-Length": str(size)}) as r:
        return json.loads(r.read())


def drive_update_media(file_id, data_bytes, mime):
    url = f"{UPLOAD}/files/{file_id}?uploadType=media"
    with drive_req("PATCH", url, data=data_bytes, timeout=300,
                   headers={"Content-Type": mime}) as r:
        return json.loads(r.read())


def load_pool_manifest(cat_folder_id):
    """Return (manifest_dict, pool_file_id_or_None)."""
    pf = drive_find(cat_folder_id, "_pool.json")
    if not pf:
        return {"clips": []}, None
    try:
        data = drive_download_json(pf["id"])
        if isinstance(data, dict) and isinstance(data.get("clips"), list):
            return data, pf["id"]
        log("pool manifest had unexpected shape — starting fresh")
    except Exception as e:
        log(f"pool manifest read failed ({e}) — starting fresh")
    return {"clips": []}, pf["id"]


def save_pool_manifest(cat_folder_id, pool_file_id, manifest):
    data = json.dumps(manifest, indent=1).encode()
    if pool_file_id:
        drive_update_media(pool_file_id, data, "application/json")
    else:
        tmp = WORK / "_pool.json"
        tmp.write_bytes(data)
        drive_upload("_pool.json", cat_folder_id, tmp, "application/json")


# ---------------- gemini (paced, quota-aware) ----------------
class QuotaExhausted(Exception):
    """The Gemini free quota (or our conservative soft cap under it) is
    used up. Callers must REQUEUE their work and let a later run retry —
    never fail a job over quota."""


# Soft daily caps, deliberately BELOW the real free quotas (big Flash
# ~20/day, Flash-Lite ~500/day) so the engine can never hit a hard limit.
QUOTA_CAPS = {"lite": 400, "flash": 15, "tts": 250, "img": 40}
ENGINE_STATE = None


def _quota_class(model):
    m = (model or "").lower()
    if "tts" in m:
        return "tts"
    if "image" in m:
        return "img"
    if "lite" in m:
        return "lite"
    return "flash"


def _quota():
    st = ENGINE_STATE if ENGINE_STATE is not None else {}
    q = st.setdefault("quota", {"date": "", "lite": 0, "flash": 0,
                                "tts": 0, "img": 0})
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if q.get("date") != today:
        q.update({"date": today, "lite": 0, "flash": 0, "tts": 0, "img": 0})
    return q


def _quota_check(cls):
    q = _quota()
    if q.get(cls, 0) >= QUOTA_CAPS[cls]:
        raise QuotaExhausted(
            f"Gemini {cls} soft daily cap reached "
            f"({q.get(cls, 0)}/{QUOTA_CAPS[cls]}) — work requeues")


def _quota_bump(cls):
    q = _quota()
    q[cls] = q.get(cls, 0) + 1


def gpost(url, payload):
    """Gemini POST with exponential backoff on 429/503/network errors.
    A persistent 429 (or a reached soft cap) raises QuotaExhausted."""
    m = re.search(r"/models/([^/:]+)", url)
    cls = _quota_class(m.group(1) if m else "")
    _quota_check(cls)
    # TTS/image free tiers treat 429 as a daily wall, not a transient:
    # fail fast (2 attempts) so a dead quota costs seconds, not the
    # ~10 minutes per call the full backoff burned on 2026-11-21.
    fast = cls in ("tts", "img")
    max_att = 2 if fast else 4
    last = None
    for attempt in range(max_att):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": GEMINI_KEY})
            with urllib.request.urlopen(req, timeout=300) as r:
                out = json.loads(r.read())
            _quota_bump(cls)
            return out
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                if attempt == max_att - 1:
                    raise QuotaExhausted("Gemini HTTP 429 after retries")
                time.sleep((10 if fast else 20) * (2 ** attempt))
                continue
            if e.code in (500, 502, 503) and attempt < max_att - 1:
                time.sleep(10 * (2 ** attempt))
                continue
            raise RuntimeError(f"Gemini HTTP {e.code} on {url.split('/')[-1]}")
        except Exception as e:
            last = e
            if attempt < max_att - 1:
                time.sleep(10 * (2 ** attempt))
                continue
            raise RuntimeError(f"Gemini request failed: {e}")
    raise RuntimeError(f"Gemini request failed: {last}")


def gpost_tts(model, payload):
    return gpost(f"{API}/models/{model}:generateContent", payload)


def gpost_image(model, payload):
    return gpost(f"{API}/models/{model}:generateContent", payload)


def gemini_generate_text(parts, state=None, kind="script"):
    """Text generation, Flash-Lite first (quota-friendly), big Flash as
    fallback. QuotaExhausted propagates so callers requeue."""
    last = None
    for model in ANALYSIS_MODELS:
        try:
            return gemini_generate(model, parts)
        except QuotaExhausted:
            raise
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"gemini text generation failed: {last}")


# ---------------- engine state (Drive-persisted) ----------------
def load_engine_state():
    global ENGINE_STATE
    st = {"hook_bank_next": 1, "screen_hook_next": 0, "voice_counter": 0,
          "quota": {"date": "", "lite": 0, "flash": 0, "tts": 0, "img": 0}}
    try:
        f = drive_find(POOLS_ID, "_engine_state.json")
        if f:
            data = drive_download_json(f["id"])
            if isinstance(data, dict):
                for k, v in data.items():
                    st[k] = v
                log(f"engine state loaded (hook bank next "
                    f"{st.get('hook_bank_next')}, quota {st.get('quota')})")
    except Exception as e:
        log(f"engine state load failed ({e}) — starting fresh")
    ENGINE_STATE = st
    return st


def save_engine_state():
    if ENGINE_STATE is None:
        return
    try:
        data = json.dumps(ENGINE_STATE, indent=1).encode()
        f = drive_find(POOLS_ID, "_engine_state.json")
        if f:
            drive_update_media(f["id"], data, "application/json")
        else:
            tmp = WORK / "_engine_state.json"
            tmp.write_bytes(data)
            drive_upload("_engine_state.json", POOLS_ID, tmp,
                         "application/json")
    except Exception as e:
        log(f"engine state save failed (ignored): {e}")


def gemini_upload(path, mime="video/mp4"):
    data = Path(path).read_bytes()
    meta = json.dumps({"file": {"display_name": Path(path).name}}).encode()
    req = urllib.request.Request("https://generativelanguage.googleapis.com/upload/v1beta/files",
        data=meta, method="POST",
        headers={"x-goog-api-key": GEMINI_KEY, "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(data)),
                 "X-Goog-Upload-Header-Content-Type": mime,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        up_url = r.headers.get("x-goog-upload-url") or r.headers.get("X-Goog-Upload-URL")
    if not up_url:
        raise RuntimeError("Gemini upload: no upload URL returned")
    req = urllib.request.Request(up_url, data=data, method="POST",
        headers={"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"})
    with urllib.request.urlopen(req, timeout=900) as r:
        f = json.loads(r.read())["file"]
    for _ in range(40):
        req = urllib.request.Request(f"{API}/{f['name']}",
            headers={"x-goog-api-key": GEMINI_KEY})
        with urllib.request.urlopen(req, timeout=60) as r:
            info = json.loads(r.read())
        if info.get("state") == "ACTIVE":
            return info
        if info.get("state") == "FAILED":
            raise RuntimeError("Gemini file processing FAILED")
        time.sleep(5)
    return f


def gemini_generate(model, parts):
    out = gpost(f"{API}/models/{model}:generateContent",
                {"contents": [{"parts": parts}],
                 "generationConfig": {"responseMimeType": "application/json"}})
    return out["candidates"][0]["content"]["parts"][0]["text"]


ANALYSIS_PROMPT = """Watch this video. It is a compilation of short tricks/hacks.
Return ONLY JSON:
{"category": one of ["fruit-food","tools-home","woodworking","crafts","beauty","christmas"] — or a NEW short lowercase-hyphen label (e.g. "car-repair") if none of these fits. NEVER use "other" — every video belongs to a real, named category,
 "has_captions": true if the video already has burned-in text captions, else false,
 "segments": [{"start": <seconds>, "end": <seconds>, "desc": "short description of the trick shown", "reveal": <1-10, how strong the finished-result reveal is>}]}
Rules: one segment per individual trick, each 3 to 25 seconds, in time order, no overlaps."""


def clean_category(c):
    c = re.sub(r"[^a-z0-9-]+", "-", str(c or "life-hacks").lower()).strip("-")
    if c == "other":
        c = "life-hacks"
    return c or "life-hacks"


def analyse(src, mime="video/mp4"):
    f = gemini_upload(src, mime)
    dur = ffprobe_dur(src)
    last = None
    for model in ANALYSIS_MODELS:
        try:
            txt = gemini_generate(model, [
                {"fileData": {"fileUri": f["uri"], "mimeType": mime}},
                {"text": ANALYSIS_PROMPT}])
            data = json.loads(txt)
            if isinstance(data, list):
                wrapped = next((d for d in data if isinstance(d, dict) and "segments" in d), None)
                data = wrapped if wrapped is not None else {"category": "life-hacks", "has_captions": False, "segments": data}
            segs, last_end = [], 0.0
            for s in data.get("segments", []):
                a = max(0.0, float(s["start"]))
                b = min(float(s["end"]), dur, a + 25.0)
                if b - a < 3.0 or a < last_end - 0.3:
                    continue
                segs.append({"start": a, "end": b, "desc": str(s.get("desc", "trick"))[:90],
                             "reveal": float(s.get("reveal", 5))})
                last_end = b
            if not segs:
                raise RuntimeError("no usable segments")
            return {"category": clean_category(data.get("category", "life-hacks")),
                    "has_captions": bool(data.get("has_captions", False)),
                    "segments": segs}
        except QuotaExhausted:
            raise
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"analysis failed: {last}")


# ---------------- media helpers ----------------
def cut_lossless(src, a, b, dest):
    """Lossless scene cut (-c copy) as specified; keyframe slop is fine —
    the real duration is probed afterwards and stored in the manifest."""
    r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", str(src),
             "-t", f"{max(0.1, b - a):.3f}", "-c", "copy",
             "-avoid_negative_ts", "make_zero", str(dest)])
    if r.returncode != 0:
        raise RuntimeError("cut failed: " + (r.stderr or "")[-250:])


def fmt_t(s):
    h, m = int(s // 3600), int((s % 3600) // 60)
    return f"{h:02d}:{m:02d}:{s % 60:06.3f}".replace(".", ",")


def verify_final(p):
    """Independent check (render_bundle self-verifies too): per-stream
    durations within 1s and a full clean decode."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "json", str(p)])
    d = {s["codec_type"]: float(s["duration"])
         for s in json.loads(r.stdout)["streams"]}
    if "video" not in d or "audio" not in d:
        raise RuntimeError(f"verify failed: missing stream(s) {d}")
    if abs(d["video"] - d["audio"]) >= 1.0:
        raise RuntimeError(f"verify failed: video {d['video']:.2f}s vs audio {d['audio']:.2f}s")
    r = run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"])
    if r.returncode != 0 or r.stderr.strip():
        raise RuntimeError("verify failed: decode errors: " + r.stderr[-300:])
    return d


# ---------------- CUTTER ----------------
def _fail_stage(err):
    """Transient Gemini overload (HTTP 503) or quota exhaustion is not a
    real failure: requeue the source so the next cycle retries it
    automatically. Anything else stays failed for the board's Retry
    button."""
    if "503" in err or "Service Unavailable" in err or "quota" in err.lower():
        return "queued"
    return "failed"


def _analyse_one(f):
    """Phase-1 worker for one source: download, Gemini analysis, local
    lossless cuts. No board posts and no Drive writes — safe in parallel."""
    fid, name = f["id"], f.get("name", "")
    src = WORK / f"src-{fid[:8]}.mp4"
    try:
        log(f"cutter: downloading {name}")
        drive_download(fid, src)
        dur = ffprobe_dur(src)
        mime = f.get("mimeType") or "video/mp4"
        log(f"cutter: analysing {name} ({dur:.0f}s)")
        post_now(f"Cutting: {name}", "", 1)
        info = analyse(src, mime)
        clips = []
        for i, seg in enumerate(info["segments"]):
            cname = f"{fid[:8]}-{i + 1}.mp4"
            cpath = WORK / cname
            cut_lossless(src, seg["start"], seg["end"], cpath)
            cdur = ffprobe_dur(cpath)
            if cdur < 2.5:
                log(f"cutter: clip {cname} too short after cut ({cdur:.1f}s) — skipped")
                cpath.unlink(missing_ok=True)
                continue
            clips.append({"name": cname, "path": cpath, "dur": round(cdur, 2),
                          "desc": seg["desc"], "reveal": seg["reveal"]})
        src.unlink(missing_ok=True)
        return {"f": f, "ok": True, "duration": round(dur, 1),
                "category": info["category"],
                "has_captions": info["has_captions"],
                "segments": len(info["segments"]), "clips": clips}
    except QuotaExhausted as e:
        src.unlink(missing_ok=True)
        return {"f": f, "ok": False, "error": f"quota exhausted: {e}"[:200]}
    except Exception as e:
        src.unlink(missing_ok=True)
        return {"f": f, "ok": False, "error": str(e)[:200]}


def cutter(board_sources):
    done_ids = {s.get("id") for s in board_sources
                if s.get("stage") in ("pooled", "failed", "file-error", "duplicate", "skipped", "removed")}
    all_videos = [f for f in drive_list(INBOX_ID)
                  if str(f.get("mimeType", "")).startswith("video/")]
    # Empty uploads (0 bytes) can never be processed — flag each once on the
    # board as a file error and ignore it from then on.
    known_ids = {s.get("id") for s in board_sources}
    for f in all_videos:
        if int(f.get("size") or 0) <= 0 and f"drive-{f['id']}" not in known_ids:
            post_source({"id": f"drive-{f['id']}", "name": f.get("name", ""),
                         "category": "", "stage": "file-error", "duration": 0,
                         "thumb": f.get("thumbnailLink", ""),
                         "note": "Empty file (0 bytes) — the upload never "
                                 "completed. Ignored."})
    inbox = [f for f in all_videos if int(f.get("size") or 0) > 0]
    inbox.sort(key=lambda f: f.get("createdTime", ""))
    todo = [f for f in inbox if f"drive-{f['id']}" not in done_ids][:MAX_SOURCES]
    log(f"cutter: {len(inbox)} inbox video(s), {len(todo)} to process (max {MAX_SOURCES})")
    for f in todo:
        post_source({"id": f"drive-{f['id']}", "name": f.get("name", ""),
                     "category": "", "stage": "analysing", "duration": 0,
                     "thumb": f.get("thumbnailLink", ""), "note": ""})
    # Phase 1 (parallel): analysis is the slow step (minutes per video,
    # mostly waiting on Gemini), so run up to 3 sources at once. Workers
    # touch only their own files — no board or Drive writes here.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(_analyse_one, todo))
    # Phase 2 (sequential): Drive uploads, pool manifests, board records —
    # single-threaded so manifests and the board list never race.
    processed = 0
    for res in results:
        f = res["f"]
        fid, name = f["id"], f.get("name", "")
        rec = {"id": f"drive-{fid}", "name": name, "category": "",
               "stage": "analysing", "duration": res.get("duration", 0),
               "thumb": f.get("thumbnailLink", ""), "note": ""}
        if not res["ok"]:
            rec["stage"] = _fail_stage(res["error"])
            rec["note"] = ("Gemini busy — automatic retry next cycle. " + res["error"])[:200] \
                if rec["stage"] == "queued" else res["error"]
            post_source(rec)
            log(f"cutter: {'REQUEUED' if rec['stage'] == 'queued' else 'FAILED'} {name}: {res['error']}")
            continue
        try:
            category = res["category"]
            rec["category"] = category
            rec["stage"] = "cutting"
            rec["note"] = f"{res['segments']} scene(s) found"
            post_source(rec)
            cat = drive_find(POOLS_ID, category, FOLDER_MIME)
            if not cat:
                cat = drive_create_folder(category, POOLS_ID)
                log(f"cutter: created pool folder '{category}' ({cat['id']})")
            manifest, pool_file_id = load_pool_manifest(cat["id"])
            known = {c.get("name") for c in manifest["clips"]}
            new_clips = []
            for cl in res["clips"]:
                if cl["name"] in known:
                    cl["path"].unlink(missing_ok=True)
                    continue
                log(f"cutter: uploading clip {cl['name']} ({cl['dur']:.1f}s) to pool {category}")
                up = drive_upload(cl["name"], cat["id"], cl["path"], "video/mp4")
                new_clips.append({"drive_id": up["id"], "name": cl["name"],
                                  "dur": cl["dur"], "used": False,
                                  "desc": cl["desc"], "reveal": cl["reveal"],
                                  "has_captions": res["has_captions"],
                                  "source_id": fid, "source_name": name})
                cl["path"].unlink(missing_ok=True)
            manifest["clips"].extend(new_clips)
            save_pool_manifest(cat["id"], pool_file_id, manifest)
            mine = [c for c in manifest["clips"] if c.get("source_id") == fid]
            total = sum(c["dur"] for c in new_clips)
            rec["stage"] = "pooled"
            rec["note"] = f"{len(new_clips)} clips, {total:.0f}s pooled"
            rec["clips"] = len(mine)
            rec["clips_used"] = sum(1 for c in mine if c.get("used"))
            post_source(rec)
            log(f"cutter: pooled {name} -> {category}: {len(new_clips)} clips, {total:.0f}s")
            processed += 1
        except Exception as e:
            rec["stage"] = _fail_stage(str(e))
            rec["note"] = ("Gemini busy — automatic retry next cycle. " + str(e))[:200] \
                if rec["stage"] == "queued" else str(e)[:200]
            post_source(rec)
            log(f"cutter: {'REQUEUED' if rec['stage'] == 'queued' else 'FAILED'} {name}: {e}")
    return processed


# ---------------- MERGER (TEMPLATE v3, via builder.py) ----------------
def reserve_clips(cat_id, chosen, token):
    """Optimistically reserve chosen clips: mark them used with this
    run's token and save the manifest, then read it back — if every
    chosen clip carries OUR token, the reservation is ours. Returns the
    fresh manifest or None (another run won the race; caller skips)."""
    manifest, pfid = load_pool_manifest(cat_id)
    names = {c["name"] for c in chosen}
    for c in manifest["clips"]:
        if c["name"] in names:
            c["used"] = True
            c["reserved_by"] = token
    save_pool_manifest(cat_id, pfid, manifest)
    fresh, _ = load_pool_manifest(cat_id)
    mine = [c for c in fresh["clips"] if c["name"] in names]
    if len(mine) == len(names) and all(
            c.get("reserved_by") == token and c.get("used") for c in mine):
        return fresh
    # lost the race: release whatever carries our token and step aside
    release_clips(cat_id, token)
    return None


def release_clips(cat_id, token):
    try:
        manifest, pfid = load_pool_manifest(cat_id)
        changed = False
        for c in manifest["clips"]:
            if c.get("reserved_by") == token:
                c["used"] = False
                c.pop("reserved_by", None)
                changed = True
        if changed:
            save_pool_manifest(cat_id, pfid, manifest)
    except Exception as e:
        log(f"release clips failed (ignored): {e}")


def confirm_clips(cat_id, token):
    """After a successful delivery: clips stay used; drop the token."""
    try:
        manifest, pfid = load_pool_manifest(cat_id)
        changed = False
        for c in manifest["clips"]:
            if c.get("reserved_by") == token:
                c.pop("reserved_by", None)
                changed = True
        if changed:
            save_pool_manifest(cat_id, pfid, manifest)
    except Exception as e:
        log(f"confirm clips failed (ignored): {e}")


def _finished_folder(category):
    for f in drive_list(FINISHED_ID):
        if f.get("name") == category and f.get("mimeType") == FOLDER_MIME:
            return f["id"]
    fid = drive_create_folder(category, FINISHED_ID)["id"]
    log(f"created Video Finished/{category} folder")
    return fid


def _next_title(category, fin_folder):
    existing = [f for f in drive_list(FINISHED_ID) + drive_list(fin_folder)
                if f.get("name", "").startswith(f"compilation-{category}-")]
    names = {f.get("name") for f in existing}
    n = len(existing) + 1
    while f"compilation-{category}-{n}.mp4" in names:
        n += 1
    return f"compilation-{category}-{n}"


def _unique_title(category, fin_folder, title):
    """Re-checked at DELIVERY time: a title is chosen before a ~10-min
    build, and a parallel run can deliver the same title meanwhile
    (observed 2026-11-21: merger + link run both shipped
    compilation-tools-home-24 and one replaced the other). Bump the
    numeric suffix until the name is free in Finished."""
    try:
        names = {f.get("name") for f in drive_list(fin_folder)}
    except Exception:
        return title
    if f"{title}.mp4" not in names:
        return title
    m = re.search(r"-(\d+)$", title)
    stem = title[:m.start()] if m else title
    n = (int(m.group(1)) + 1) if m else 2
    while f"{stem}-{n}.mp4" in names:
        n += 1
    new_title = f"{stem}-{n}"
    log(f"title collision: {title} already in Finished — "
        f"delivering as {new_title}")
    return new_title


def _find_open_job(title):
    try:
        for j in qapi("GET", "/jobs").get("jobs", []):
            if j.get("name") == title and j.get("status") in ("queued", "working"):
                return j["id"]
    except Exception:
        pass
    return None


def merger(state, blocked=None):
    """Build at most ONE v3 compilation per run (pacing), from the
    richest qualifying pool: >= 80s unused, >= 2 sources (TEMPLATE v3).
    Categories in `blocked` (their own sources not all resolved yet)
    are evaluated but skipped this run."""
    blocked = blocked or {}
    import builder
    folders = [f for f in drive_list(POOLS_ID) if f.get("mimeType") == FOLDER_MIME]
    log(f"merger: {len(folders)} category pool(s)")
    cands = []
    for cat in folders:
        try:
            manifest, _pf = load_pool_manifest(cat["id"])
        except Exception as e:
            log(f"merger: pool {cat['name']} manifest unreadable: {e}")
            continue
        unused = [c for c in manifest["clips"]
                  if not c.get("used") and not c.get("held")]
        free_secs = sum(c["dur"] for c in unused)
        nsrc = len({c.get("source_id") for c in unused})
        log(f"merger: {cat['name']}: {len(unused)} unused clip(s), "
            f"{free_secs:.0f}s free, {nsrc} source(s)")
        if free_secs >= 80 and nsrc >= 2:
            if cat["name"] in blocked:
                log(f"merger: {cat['name']}: pool ready but "
                    f"{blocked[cat['name']]} of its own source(s) still "
                    f"unresolved — waiting for a later cycle")
                continue
            cands.append((free_secs, cat, manifest))
    if not cands:
        return 0
    cands.sort(key=lambda x: -x[0])
    _free, cat, manifest = cands[0]
    category = cat["name"]
    unused = [c for c in manifest["clips"]
              if not c.get("used") and not c.get("held")]
    chosen = builder.select_clips(unused)
    if not chosen:
        log(f"merger: {category}: selection failed — skipped")
        return 0
    token = f"run-{int(time.time())}-{random.randint(1000, 9999)}"
    if reserve_clips(cat["id"], chosen, token) is None:
        log(f"merger: {category}: clips were reserved by another run — skipped")
        return 0
    fin_folder = _finished_folder(category)
    title = _next_title(category, fin_folder)
    post_now(f"Making a {category.replace('-', ' ')} video", category, 2)
    jid = _find_open_job(title)
    if not jid:
        try:
            res = qapi("POST", "/jobs", {"items": [{
                "link": f"https://drive.google.com/drive/folders/{cat['id']}",
                "source": "drive", "name": title}]})
            ids = res.get("ids") or []
            jid = ids[0] if ids else None
        except Exception as e:
            log(f"merger: job create failed (continuing without board job): {e}")
    try:
        if jid:
            set_job_stage(jid, "scripting",
                          f"{len(chosen)} clips from pool {category} (v3 builder)")
        local = []
        for c in chosen:
            p = WORK / f"pool-{c['name']}"
            drive_download(c["drive_id"], p)
            w, h = probe_wh(p)
            local.append({**c, "file": str(p), "w": w, "h": h,
                          "dur": ffprobe_dur(p) or c["dur"]})
        res = builder.build_compilation(local, category,
                                        WORK / f"build-{title}", state, VOICE)
        final = Path(res["final"])
        title = _unique_title(category, fin_folder, title)
        up = drive_upload(f"{title}.mp4", fin_folder, final, "video/mp4")
        log(f"merger: uploaded to Video Finished/{category}: {up.get('id')}")
        tag = f"drive-{title}"
        r = run(["gh", "release", "create", tag, str(final), "--title", title,
                 "--notes", f"Produced by the independent engine (TEMPLATE v3) "
                            f"from the {category} pool ({res['n_clips']} clips, "
                            f"{res['duration']:.0f}s). Voice: {res['voice_label']}. "
                            f"Also in Drive Video Finished."])
        if r.returncode != 0:
            r = run(["gh", "release", "upload", tag, str(final), "--clobber"])
            if r.returncode != 0:
                raise RuntimeError("release publish failed: " + (r.stderr or "")[-250:])
        url = f"https://github.com/{REPO}/releases/download/{tag}/{title}.mp4"
        confirm_clips(cat["id"], token)
        save_engine_state()
        note = (f"{title} — {res['n_clips']} clips, {res['duration']:.0f}s, "
                f"in Video Finished · voice: {res['voice_label']}")
        if res.get("char_note"):
            note += f" · {res['char_note']}"
        if jid:
            set_job_stage(jid, "done", note, output=url)
        try:
            qapi("POST", "/settings", {"voice_counter": VOICE_COUNTER + 1})
        except Exception as e:
            log(f"voice rotation save failed (ignored): {e}")
        log(f"merger: DONE {title} ({res['duration']:.0f}s) -> {url}")
        return 1
    except QuotaExhausted as e:
        release_clips(cat["id"], token)
        log(f"merger: {category} deferred — {e}")
        if jid:
            set_job_stage(jid, "queued",
                          "Waiting for Gemini quota — automatic retry next cycle")
        return 0
    except Exception as e:
        release_clips(cat["id"], token)
        log(f"merger: FAILED {category}: {e}")
        if jid:
            set_job_stage(jid, "failed", "", error=str(e)[:280])
        return 0


# ---------------- board resolution requests ----------------
def _update_source_record(fid, **fields):
    try:
        cur = next((s for s in (get_board().get("sources") or [])
                    if s.get("id") == f"drive-{fid}"), None)
        rec = {"id": f"drive-{fid}", "name": "", "category": "",
               "stage": "queued", "duration": 0, "thumb": "", "note": ""}
        if cur:
            rec.update(cur)
        rec.update(fields)
        post_source(rec)
    except Exception as e:
        log(f"source record update failed (ignored): {e}")


def _remove_source_clips(fid):
    for cat in drive_list(POOLS_ID):
        if cat.get("mimeType") != FOLDER_MIME:
            continue
        manifest, pfid = load_pool_manifest(cat["id"])
        kept = [c for c in manifest["clips"] if c.get("source_id") != fid]
        if len(kept) != len(manifest["clips"]):
            manifest["clips"] = kept
            save_pool_manifest(cat["id"], pfid, manifest)
            log(f"requests: removed clips of source {fid[:8]} from {cat['name']}")


def _set_source_clips_held(fid, held):
    for cat in drive_list(POOLS_ID):
        if cat.get("mimeType") != FOLDER_MIME:
            continue
        manifest, pfid = load_pool_manifest(cat["id"])
        changed = False
        for c in manifest["clips"]:
            if c.get("source_id") == fid and bool(c.get("held")) != held:
                c["held"] = held
                changed = True
        if changed:
            save_pool_manifest(cat["id"], pfid, manifest)


def apply_board_requests():
    """Consume the board's retry / ignore / skip requests so the system
    honours them with NO Muse in the loop."""
    try:
        reqs = qapi("GET", "/requests")
    except Exception as e:
        log(f"requests fetch failed (ignored): {e}")
        return
    for kind in ("retry", "ignore", "skip", "remove"):
        for item in reqs.get(kind) or []:
            target = item.get("target")
            name = item.get("name", "")
            if not target:
                continue
            try:
                if kind == "retry":
                    _remove_source_clips(target)
                    _update_source_record(target, name=name, stage="queued",
                                          note="")
                elif kind == "ignore":
                    _remove_source_clips(target)
                    _update_source_record(target, name=name, stage="duplicate",
                                          note="Removed by the owner.")
                elif kind == "skip":
                    _set_source_clips_held(target, True)
                    _update_source_record(target, name=name, stage="skipped",
                                          note="Set aside by the owner — clips held.")
                elif kind == "remove":
                    # Permanent exclusion: purge any clips it already
                    # contributed and flip its record to the terminal
                    # "removed" stage — the cutter's done-set includes
                    # "removed", so it is never downloaded or analysed
                    # again even though the file stays in the Inbox.
                    _remove_source_clips(target)
                    _update_source_record(target, name=name, stage="removed",
                                          note="Removed by the owner.")
                log(f"requests: applied {kind} for {target[:8]}")
            except Exception as e:
                log(f"requests: {kind} for {target[:8]} failed: {e}")
            try:
                qapi("POST", "/requests/clear",
                     {"kind": kind, "target": target})
            except Exception:
                pass


# ---------------- main ----------------
def main():
    global VOICE, VOICES, VOICE_COUNTER, INBOX_ID, FINISHED_ID, POOLS_ID
    log(f"drive.py starting: mode={MODE} max_sources={MAX_SOURCES}")
    if not DRIVE_TOKEN:
        log("ERROR: DRIVE_TOKEN is empty — the OAuth token step must run first. Nothing can run.")
        return 1
    if not QUEUE_KEY:
        log("ERROR: QUEUE_KEY secret is not set — cannot read/write board state. Nothing can run.")
        return 1
    engine_status("running", f"Drive cycle started on GitHub (mode: {MODE})")
    if not GEMINI_KEY:
        engine_status("waiting-gemini-key",
                      "Add the GEMINI_API_KEY secret (repo Settings, Secrets and variables, Actions), then the Drive engine runs on its own.")
        log("GEMINI_API_KEY secret is not set — stopping cleanly.")
        return 0
    board = {}
    try:
        board = get_board()
    except Exception as e:
        log(f"board fetch failed: {e}")
        return 1
    settings = board.get("settings") or {}
    if settings.get("paused"):
        engine_status("paused", "Paused from the board — no Drive work started.")
        return 0
    _vs = [v for v in (settings.get("voices") or []) if isinstance(v, str)]
    if _vs:
        VOICES = _vs
    VOICE_COUNTER = int(settings.get("voice_counter") or 0)
    VOICE = VOICES[VOICE_COUNTER % len(VOICES)]
    log(f"voice for this run: {VOICE}")
    INBOX_ID = settings.get("inbox_folder_id") or INBOX_ID
    FINISHED_ID = settings.get("finished_folder_id") or FINISHED_ID
    POOLS_ID = settings.get("pools_folder_id") or POOLS_ID
    state = load_engine_state()
    # Board resolution requests (retry / ignore / skip) are honoured by
    # the engine itself, before any cutting, so they work with no Muse.
    apply_board_requests()
    try:
        board = get_board()
    except Exception:
        pass
    did = []
    if MODE in ("cutter", "both"):
        n = cutter(board.get("sources") or [])
        did.append(f"cut {n} new source(s)")
    if MODE in ("merger", "both"):
        # PER-CATEGORY PHASE GATE: a category becomes buildable as soon as
        # its OWN sources are all resolved — it no longer waits for the
        # whole Inbox to be cut. A source blocks only the category recorded
        # on its board record. A source that has not been cut yet has no
        # known category (its category comes FROM its analysis), so it
        # blocks nothing for now and simply keeps its own future category
        # waiting once it is cut. Practical effect: builds start as soon as
        # ANY category is ready, instead of waiting for the whole inbox.
        blocked = {}
        try:
            fresh = get_board()
            recs = {s.get("id"): s for s in fresh.get("sources") or []}
            resolved = ("pooled", "failed", "file-error", "duplicate",
                        "skipped", "removed")
            for f in drive_list(INBOX_ID):
                if not str(f.get("mimeType", "")).startswith("video/"):
                    continue
                if int(f.get("size") or 0) <= 0:
                    continue
                rec = recs.get(f"drive-{f['id']}") or {}
                if rec.get("stage") in resolved:
                    continue
                cat = rec.get("category") or ""
                if cat:
                    blocked[cat] = blocked.get(cat, 0) + 1
        except Exception as e:
            log(f"phase gate check failed ({e}) — merger proceeds")
        if blocked:
            log("merger: categories waiting on their own sources: " +
                ", ".join(f"{c} ({n})" for c, n in sorted(blocked.items())))
        n = merger(state, blocked)
        did.append(f"built {n} compilation(s)")
    save_engine_state()
    post_limits()
    post_drive_pools()
    post_now("Waiting for the next run", "", 0)
    engine_status("idle", "Drive cycle finished: " + ", ".join(did) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
