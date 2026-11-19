#!/usr/bin/env python3
"""
drive.py — Google Drive stages of the AI Video Compiler engine (TOOL SPEC v3).

Runs on a GitHub Actions runner (see .github/workflows/drive.yml). Drive is
reached keylessly: the workflow exchanges its GitHub identity for a Google
access token (Workload Identity Federation) and exports it as DRIVE_TOKEN.

Stages (env MODE = cutter | merger | both, MAX_SOURCES = int, default 2):
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
    MAX_SOURCES = max(0, int(os.environ.get("MAX_SOURCES") or 2))
except ValueError:
    MAX_SOURCES = 2
REPO = "ifthakar9078-web/video-engine"
API = "https://generativelanguage.googleapis.com/v1beta"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
ANALYSIS_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
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


def post_source(record):
    """Upsert ONE source record. POST /sources replaces the whole list, so
    re-read the board and merge by id, keeping everyone else's records."""
    try:
        fresh = get_board().get("sources") or []
        merged = [s for s in fresh if s.get("id") != record["id"]] + [record]
        qapi("POST", "/sources", {"sources": merged})
    except Exception as e:
        log(f"source report failed (ignored): {e}")


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
            last = e
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
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


# ---------------- gemini ----------------
def gpost(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


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
{"category": one of ["fruit-food","tools-home","woodworking","crafts","beauty","christmas","other"] — or a NEW short lowercase-hyphen label (e.g. "car-repair") if none of these fits,
 "has_captions": true if the video already has burned-in text captions, else false,
 "segments": [{"start": <seconds>, "end": <seconds>, "desc": "short description of the trick shown", "reveal": <1-10, how strong the finished-result reveal is>}]}
Rules: one segment per individual trick, each 3 to 25 seconds, in time order, no overlaps."""


def clean_category(c):
    c = re.sub(r"[^a-z0-9-]+", "-", str(c or "other").lower()).strip("-")
    return c or "other"


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
                data = wrapped if wrapped is not None else {"category": "other", "has_captions": False, "segments": data}
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
            return {"category": clean_category(data.get("category", "other")),
                    "has_captions": bool(data.get("has_captions", False)),
                    "segments": segs}
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"analysis failed: {last}")


SCRIPT_PROMPT = """You write viral hack-compilation narration. Follow this reference blueprint EXACTLY (taken from a top-performing painting-hacks compilation):
- HOOK first, a curiosity gap that makes the viewer feel a secret was kept from them. Reference hook: "Why did nobody tell me these painting tricks sooner?" Write the same form for THIS niche.
- Then one line per trick. Each line: open with a pain-point question or curiosity line, give the step, end with the payoff/benefit. Reference lines: "Hate cleaning paint trays?" / "Still bending over and sticking down floor protection little by little?" / "It works even better than using tape."
- Fast, energetic, authoritative. Narration must fill the clip: words ~= slot_seconds x 2.5. Zero filler, zero dead air.
- Keep an OPEN LOOP: somewhere in the middle lines, tease that the best/last trick is still coming, so viewers hold to the final second.
- Do NOT number the lines (numbering is added by the renderer). No repeated openings.
- CTA at the end, save-form: "Save these tricks for later, and follow for more." (adapt the wording to the niche).
Return ONLY JSON: {"hook_line": "...", "lines": ["...", ...], "cta": "..."} with exactly one line per clip, in the order given.
Clips (final order, slot seconds, what is shown):
"""


def write_script(clips):
    listing = "\n".join(f"{i+1}. ({c['dur']:.1f}s) {c['desc']}" for i, c in enumerate(clips))
    last = None
    for model in ANALYSIS_MODELS:
        try:
            txt = gemini_generate(model, [{"text": SCRIPT_PROMPT + listing}])
            data = json.loads(txt)
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict) and "lines" in d), {})
            if len(data.get("lines", [])) != len(clips):
                raise RuntimeError("line count mismatch")
            return data
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"script failed: {last}")


# ---------------- voice ----------------
_kokoro = {"pipe": None, "tried": False}


def kokoro_wav(text, out_wav):
    try:
        import numpy as np, wave
        if not _kokoro["tried"]:
            _kokoro["tried"] = True
            from kokoro import KPipeline
            _kokoro["pipe"] = KPipeline(lang_code='a')
        if _kokoro["pipe"] is None:
            return False
        chunks = [a for _, _, a in _kokoro["pipe"](text, voice=VOICE, speed=1.1)]
        if not chunks:
            return False
        pcm = np.concatenate(chunks)
        data = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(data.tobytes())
        return True
    except Exception as e:
        log(f"kokoro failed: {e}")
        _kokoro["pipe"] = None
        return False


def gemini_tts_wav(text, out_wav):
    import base64, wave
    for model in TTS_MODELS:
        try:
            out = gpost(f"{API}/models/{model}:generateContent", {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}}}})
            rawb = base64.b64decode(out["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
            if rawb[:4] == b"RIFF":
                Path(out_wav).write_bytes(rawb)
            else:
                with wave.open(str(out_wav), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                    w.writeframes(rawb)
            return True
        except Exception:
            continue
    return False


def synth_block(text, out_mp3):
    wav = Path(str(out_mp3) + ".wav")
    if not kokoro_wav(text, wav) and not gemini_tts_wav(text, wav):
        raise RuntimeError("voice synthesis failed (Kokoro and Gemini TTS)")
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-ar", "44100",
             "-codec:a", "libmp3lame", "-b:a", "192k", str(out_mp3)])
    if r.returncode != 0:
        raise RuntimeError("voice convert failed")


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
def cutter(board_sources):
    done_ids = {s.get("id") for s in board_sources
                if s.get("stage") in ("pooled", "failed")}
    inbox = [f for f in drive_list(INBOX_ID)
             if str(f.get("mimeType", "")).startswith("video/")
             and int(f.get("size") or 0) > 0]
    inbox.sort(key=lambda f: f.get("createdTime", ""))
    todo = [f for f in inbox if f"drive-{f['id']}" not in done_ids][:MAX_SOURCES]
    log(f"cutter: {len(inbox)} inbox video(s), {len(todo)} to process (max {MAX_SOURCES})")
    processed = 0
    for f in todo:
        fid, name = f["id"], f.get("name", "")
        rec = {"id": f"drive-{fid}", "name": name, "category": "",
               "stage": "analysing", "duration": 0,
               "thumb": f.get("thumbnailLink", ""), "note": ""}
        post_source(rec)
        try:
            src = WORK / f"src-{fid[:8]}.mp4"
            log(f"cutter: downloading {name}")
            drive_download(fid, src)
            dur = ffprobe_dur(src)
            rec["duration"] = round(dur, 1)
            mime = f.get("mimeType") or "video/mp4"
            log(f"cutter: analysing {name} ({dur:.0f}s)")
            info = analyse(src, mime)
            category = info["category"]
            rec["category"] = category
            rec["stage"] = "cutting"
            rec["note"] = f"{len(info['segments'])} scene(s) found"
            post_source(rec)
            cat = drive_find(POOLS_ID, category, FOLDER_MIME)
            if not cat:
                cat = drive_create_folder(category, POOLS_ID)
                log(f"cutter: created pool folder '{category}' ({cat['id']})")
            manifest, pool_file_id = load_pool_manifest(cat["id"])
            known = {c.get("name") for c in manifest["clips"]}
            new_clips = []
            for i, seg in enumerate(info["segments"]):
                cname = f"{fid[:8]}-{i + 1}.mp4"
                if cname in known:
                    continue
                cpath = WORK / cname
                cut_lossless(src, seg["start"], seg["end"], cpath)
                cdur = ffprobe_dur(cpath)
                if cdur < 2.5:
                    log(f"cutter: clip {cname} too short after cut ({cdur:.1f}s) — skipped")
                    cpath.unlink(missing_ok=True)
                    continue
                up = drive_upload(cname, cat["id"], cpath, "video/mp4")
                new_clips.append({"drive_id": up["id"], "name": cname,
                                  "dur": round(cdur, 2), "used": False,
                                  "desc": seg["desc"], "reveal": seg["reveal"],
                                  "has_captions": info["has_captions"],
                                  "source_id": fid, "source_name": name})
                cpath.unlink(missing_ok=True)
            manifest["clips"].extend(new_clips)
            save_pool_manifest(cat["id"], pool_file_id, manifest)
            total = sum(c["dur"] for c in new_clips)
            rec["stage"] = "pooled"
            rec["note"] = f"{len(new_clips)} clips, {total:.0f}s pooled"
            post_source(rec)
            log(f"cutter: pooled {name} -> {category}: {len(new_clips)} clips, {total:.0f}s")
            src.unlink(missing_ok=True)
            processed += 1
        except Exception as e:
            rec["stage"] = "failed"
            rec["note"] = str(e)[:200]
            post_source(rec)
            log(f"cutter: FAILED {name}: {e}")
    return processed


# ---------------- MERGER ----------------
def merger():
    global VOICE_COUNTER
    folders = [f for f in drive_list(POOLS_ID) if f.get("mimeType") == FOLDER_MIME]
    log(f"merger: {len(folders)} category pool(s)")
    for cat in folders:
        category = cat["name"]
        try:
            manifest, pool_file_id = load_pool_manifest(cat["id"])
        except Exception as e:
            log(f"merger: pool {category} manifest unreadable: {e}")
            continue
        unused = [c for c in manifest["clips"] if not c.get("used")]
        free_secs = sum(c["dur"] for c in unused)
        log(f"merger: {category}: {len(unused)} unused clip(s), {free_secs:.0f}s free")
        if free_secs < 70:
            continue
        # choose the compilation: longest clip first (hook), rest shuffled, 61-90s
        hook = max(unused, key=lambda c: c["dur"])
        rest = [c for c in unused if c is not hook]
        random.shuffle(rest)
        chosen, total = [hook], hook["dur"]
        for c in rest:
            if total + c["dur"] <= 90.0:
                chosen.append(c); total += c["dur"]
            if total >= 75:
                break
        if total < 61:
            for c in rest:
                if c in chosen:
                    continue
                if total + c["dur"] <= 90.0:
                    chosen.append(c); total += c["dur"]
                if total >= 61:
                    break
        if total < 61:
            log(f"merger: {category}: could not reach 61s (got {total:.0f}s) — skipped")
            continue
        # number the output: compilation-<category>-<n>.mp4
        existing = [f for f in drive_list(FINISHED_ID)
                    if f.get("name", "").startswith(f"compilation-{category}-")]
        n = len(existing) + 1
        names = {f.get("name") for f in existing}
        while f"compilation-{category}-{n}.mp4" in names:
            n += 1
        title = f"compilation-{category}-{n}"
        jid = None
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
                              f"{len(chosen)} clips from pool {category}, {total:.0f}s")
            local = []
            for c in chosen:
                p = WORK / f"pool-{c['name']}"
                drive_download(c["drive_id"], p)
                w, h = probe_wh(p)
                local.append({**c, "file": str(p), "w": w, "h": h,
                              "dur": ffprobe_dur(p) or c["dur"]})
            total = sum(c["dur"] for c in local)
            script = write_script(local)
            blocks = []
            for i, c in enumerate(local):
                num = NUMWORDS[i] if i < len(NUMWORDS) else str(i + 1)
                text = f"{num} — {script['lines'][i]}"
                if i == 0:
                    text = f"{script['hook_line']} {text}"
                if i == len(local) - 1:
                    text = f"{text} {script['cta']}"
                blocks.append(text)
            if jid:
                set_job_stage(jid, "voicing", f"voice {VOICE}")
            bundle = WORK / "bundle"
            if bundle.exists():
                shutil.rmtree(bundle)
            (bundle / "clips").mkdir(parents=True)
            (bundle / "voice").mkdir(parents=True)
            starts, t = [], 0.0
            for i, c in enumerate(local):
                starts.append(t); t += c["dur"]
                r = run(["ffmpeg", "-y", "-v", "error", "-i", c["file"], "-an",
                         "-c:v", "copy", str(bundle / "clips" / f"{i+1:03d}.mp4")])
                if r.returncode != 0:
                    r = run(["ffmpeg", "-y", "-v", "error", "-i", c["file"], "-an",
                             "-c:v", "libx264", "-crf", "10", "-preset", "veryfast",
                             str(bundle / "clips" / f"{i+1:03d}.mp4")])
                    if r.returncode != 0:
                        raise RuntimeError("bundle clip copy failed")
                synth_block(blocks[i], bundle / "voice" / f"{i+1:03d}.mp3")
            dims = {}
            for c in local:
                dims[(c["w"], c["h"])] = dims.get((c["w"], c["h"]), 0) + 1
            (W, H) = max(dims.items(), key=lambda kv: kv[1])[0]
            any_captions = any(c.get("has_captions") for c in local)
            captions = None
            srt_path = None
            if not any_captions:
                captions = [{"start": round(starts[i], 2),
                             "end": round(starts[i] + local[i]["dur"], 2),
                             "text": blocks[i]} for i in range(len(local))]
            else:
                srt_path = WORK / f"{title}.srt"
                srt_path.write_text("\n".join(
                    f"{i+1}\n{fmt_t(starts[i])} --> {fmt_t(starts[i] + local[i]['dur'])}\n{blocks[i]}\n"
                    for i in range(len(local))))
            (bundle / "meta.json").write_text(json.dumps(
                {"title": title, "width": W, "height": H, "fps": 30,
                 "hook_text": None, "captions": captions}, indent=2))
            if jid:
                set_job_stage(jid, "rendering", title)
            r = run(["python3", "render_bundle.py", str(bundle)])
            if r.returncode != 0:
                raise RuntimeError("render failed: " + (r.stderr or "")[-300:])
            final = WORK / f"{title}.mp4"
            shutil.copy(bundle / "final.mp4", final)
            d = verify_final(final)
            log(f"merger: verified {title}: video {d['video']:.2f}s / audio {d['audio']:.2f}s")
            up = drive_upload(f"{title}.mp4", FINISHED_ID, final, "video/mp4")
            log(f"merger: uploaded to Video Finished: {up.get('id')}")
            if srt_path:
                drive_upload(f"{title}.srt", FINISHED_ID, srt_path, "text/plain")
            tag = f"drive-{title}"
            files = [str(final)] + ([str(srt_path)] if srt_path else [])
            r = run(["gh", "release", "create", tag, *files, "--title", title,
                     "--notes", f"Produced by the Drive engine from the {category} pool "
                                f"({len(local)} clips, {total:.0f}s). Also in Drive Video Finished."])
            if r.returncode != 0:
                r = run(["gh", "release", "upload", tag, *files, "--clobber"])
                if r.returncode != 0:
                    raise RuntimeError("release publish failed: " + (r.stderr or "")[-250:])
            url = f"https://github.com/{REPO}/releases/download/{tag}/{title}.mp4"
            for c in manifest["clips"]:
                if c["name"] in {x["name"] for x in chosen}:
                    c["used"] = True
            save_pool_manifest(cat["id"], pool_file_id, manifest)
            if jid:
                set_job_stage(jid, "done",
                              f"{title} — {len(local)} clips, {total:.0f}s, in Video Finished",
                              output=url)
            try:
                qapi("POST", "/settings", {"voice_counter": VOICE_COUNTER + 1})
            except Exception as e:
                log(f"voice rotation save failed (ignored): {e}")
            log(f"merger: DONE {title} ({total:.0f}s) -> {url}")
            return 1
        except Exception as e:
            log(f"merger: FAILED {category}: {e}")
            if jid:
                set_job_stage(jid, "failed", "", error=str(e)[:280])
            continue
    return 0


# ---------------- main ----------------
def main():
    global VOICE, VOICES, VOICE_COUNTER, INBOX_ID, FINISHED_ID, POOLS_ID
    log(f"drive.py starting: mode={MODE} max_sources={MAX_SOURCES}")
    if not DRIVE_TOKEN:
        log("ERROR: DRIVE_TOKEN is empty — the WIF auth step must run first. Nothing can run.")
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
    did = []
    if MODE in ("cutter", "both"):
        n = cutter(board.get("sources") or [])
        did.append(f"cut {n} new source(s)")
    if MODE in ("merger", "both"):
        n = merger()
        did.append(f"built {n} compilation(s)")
    engine_status("idle", "Drive cycle finished: " + ", ".join(did) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
