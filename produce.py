#!/usr/bin/env python3
"""
produce.py — the pasted-link engine of the AI Video Compiler.

Runs on a GitHub Actions runner (see .github/workflows/produce.yml).
Claims queued link jobs from the board and gives them FULL parity with
the Drive path: download -> Gemini analysis -> cut into the SAME Drive
category pools -> build with the SAME v3 builder (builder.py) -> deliver
to Video Finished and report done on the board.

Downloads:
  - Google Drive file/folder links go through the owner's OAuth token
    (DRIVE_TOKEN), so a NON-PUBLIC file shared with the account works —
    yt-dlp's anonymous download (HTTP 403) is never used for Drive.
  - Direct media URLs: curl.  - Other platform links: yt-dlp.

Failure discipline (owner's rule: a failure wastes a whole cycle):
  - Gemini quota exhaustion -> the job is REQUEUED and the run stops
    gracefully; a later run picks it up. Never failed, never burned.
  - Transient errors retry with backoff inside the run.
  - Analyses are cached in the pool manifests (source_id), so a link is
    never downloaded or analysed twice.
  - A job is marked failed only for real, permanent problems (dead
    link, file not shared with the account, no usable clips in it, or a
    build that still fails after the builder's automatic repairs).
Pacing: at most MAX_JOBS jobs per run; the workflows run every 30 min,
so a bulk queue drains steadily without bursting any platform limit.
"""
import hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

import drive as drv
import builder

JOB_ID = (os.environ.get("JOB_ID") or "").strip()
try:
    MAX_JOBS = max(1, int(os.environ.get("MAX_JOBS") or 2))
except ValueError:
    MAX_JOBS = 2
WORK = Path("work-produce"); WORK.mkdir(exist_ok=True)
log = drv.log
run = drv.run


def set_stage(jid, stage, note="", error=None, output=None):
    drv.set_job_stage(jid, stage, note, error=error, output=output)


def engine_status(state, note):
    drv.engine_status(state, note)


# ---------------- downloads ----------------
def parse_drive_link(link):
    m = re.search(r"drive\.google\.com/file/d/([\w-]{10,})", link)
    if m:
        return ("file", m.group(1))
    m = re.search(r"drive\.google\.com/drive/folders/([\w-]{10,})", link)
    if m:
        return ("folder", m.group(1))
    m = re.search(r"drive\.google\.com/open\?id=([\w-]{10,})", link)
    if m:
        return ("file", m.group(1))
    return None


def download_link(link, dest):
    """Download any supported link to dest (mp4). Raises RuntimeError
    with a plain reason after retries."""
    kind = parse_drive_link(link)
    if kind and kind[0] == "file":
        drv.drive_download(kind[1], dest)  # OAuth — non-public OK
        return [kind[1]]
    if kind and kind[0] == "folder":
        raise RuntimeError("folder link handled by caller")
    last = None
    for attempt in range(3):
        try:
            if re.search(r"\.(mp4|mov|mkv|webm)(\?|$)", link, re.I):
                r = run(["curl", "-fL", "--retry", "2", "--max-time", "1200",
                         "-o", str(dest), link])
                if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    return []
                last = RuntimeError("direct download failed")
            else:
                r = run(["yt-dlp", "-f", "bv*[height<=2160]+ba/b",
                         "--merge-output-format", "mp4", "--no-playlist",
                         "--retries", "3", "--socket-timeout", "30",
                         "-o", str(dest), link])
                if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    return []
                last = RuntimeError("download failed: " + (r.stderr or "")[-250:])
        except Exception as e:
            last = e
        if attempt < 2:
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"download failed after retries: {last}")


def fetch_sources(job):
    """Return [{file_id_or_None, link_sid, local_path, name}] for a job:
    downloads everything the link points at (a Drive folder yields one
    source per video in it, up to 4)."""
    link = job["link"]
    kind = parse_drive_link(link)
    out = []
    if kind and kind[0] == "folder":
        files = [f for f in drv.drive_list(kind[1])
                 if str(f.get("mimeType", "")).startswith("video/")
                 and int(f.get("size") or 0) > 0][:4]
        if not files:
            raise RuntimeError("no videos found in that Drive folder")
        for f in files:
            dest = WORK / f"src-{f['id'][:8]}.mp4"
            drv.drive_download(f["id"], dest)
            out.append({"sid": f["id"], "board_id": f"drive-{f['id']}",
                        "local": dest, "name": f.get("name", "")})
        return out
    if kind and kind[0] == "file":
        fid = kind[1]
        dest = WORK / f"src-{fid[:8]}.mp4"
        drv.drive_download(fid, dest)
        meta = {}
        try:
            meta = drv.drive_get_json(
                f"{drv.DRIVE}/files/{fid}?fields=name")
        except Exception:
            pass
        out.append({"sid": fid, "board_id": f"drive-{fid}", "local": dest,
                    "name": meta.get("name", "")})
        return out
    sid = "link-" + hashlib.sha1(link.encode()).hexdigest()[:12]
    dest = WORK / f"src-{sid}.mp4"
    download_link(link, dest)
    out.append({"sid": sid, "board_id": sid, "local": dest,
                "name": job.get("name") or link})
    return out


# ---------------- pooling (identical to the Drive cutter) ----------------
def pool_source(src, info):
    """Cut one analysed source and upload its clips into its category
    pool. Returns (category, [manifest clip dicts])."""
    category = info["category"]
    cat = drv.drive_find(drv.POOLS_ID, category, drv.FOLDER_MIME)
    if not cat:
        cat = drv.drive_create_folder(category, drv.POOLS_ID)
        log(f"produce: created pool folder '{category}'")
    manifest, pool_file_id = drv.load_pool_manifest(cat["id"])
    known = {c.get("name") for c in manifest["clips"]}
    new_clips = []
    for i, seg in enumerate(info["segments"]):
        cname = f"{src['sid'][-8:]}-{i + 1}.mp4"
        if cname in known:
            continue
        cpath = WORK / cname
        drv.cut_lossless(src["local"], seg["start"], seg["end"], cpath)
        cdur = drv.ffprobe_dur(cpath)
        if cdur < 2.5:
            cpath.unlink(missing_ok=True)
            continue
        up = drv.drive_upload(cname, cat["id"], cpath, "video/mp4")
        new_clips.append({"drive_id": up["id"], "name": cname,
                          "dur": round(cdur, 2), "used": False,
                          "desc": seg["desc"], "reveal": seg["reveal"],
                          "has_captions": info["has_captions"],
                          "source_id": src["sid"],
                          "source_name": src["name"]})
        cpath.unlink(missing_ok=True)
    manifest["clips"].extend(new_clips)
    drv.save_pool_manifest(cat["id"], pool_file_id, manifest)
    drv.post_source({"id": src["board_id"], "name": src["name"],
                     "category": category, "stage": "pooled",
                     "duration": 0, "thumb": "",
                     "note": f"{len(new_clips)} clips pooled from a pasted link",
                     "clips": len(new_clips), "clips_used": 0})
    return category, new_clips


def find_pooled_source(sid):
    """Analysis cache: if this source already lives in a pool manifest,
    return (category, its clips) without any download or Gemini call."""
    for cat in drv.drive_list(drv.POOLS_ID):
        if cat.get("mimeType") != drv.FOLDER_MIME:
            continue
        manifest, _ = drv.load_pool_manifest(cat["id"])
        mine = [c for c in manifest["clips"] if c.get("source_id") == sid]
        if mine:
            return cat["name"], mine
    return None, []


# ---------------- one job ----------------
def process_job(job, state):
    """Returns 'done' | 'requeue' | 'failed'."""
    jid = job["id"]
    link = job["link"]
    kind = parse_drive_link(link)
    # cache check first (Drive file id or link hash) — no download at all
    sid_hint = None
    if kind and kind[0] == "file":
        sid_hint = kind[1]
    elif not kind:
        sid_hint = "link-" + hashlib.sha1(link.encode()).hexdigest()[:12]
    category, my_clips = (None, [])
    my_sids = []
    if sid_hint:
        category, cached = find_pooled_source(sid_hint)
        if cached:
            log(f"produce: {jid} source already pooled in {category} — reusing")
            my_clips = cached
            my_sids = [sid_hint]
    if not my_clips:
        set_stage(jid, "downloading", link[:80])
        sources = fetch_sources(job)
        set_stage(jid, "analysing")
        for src in sources:
            my_sids.append(src["sid"])
            _cat, cached = find_pooled_source(src["sid"])
            if cached:
                category = category or _cat
                my_clips.extend(cached)
                src["local"].unlink(missing_ok=True)
                continue
            info = drv.analyse(src["local"],
                               "video/mp4")
            set_stage(jid, "cutting", f"category: {info['category']}")
            category, clips = pool_source(src, info)
            my_clips.extend(clips)
            src["local"].unlink(missing_ok=True)
    if not my_clips:
        set_stage(jid, "failed", "",
                  error="no usable clips could be cut from this video")
        return "failed"
    # ---- build attempt from the (now richer) category pool ----
    folders = [f for f in drv.drive_list(drv.POOLS_ID)
               if f.get("mimeType") == drv.FOLDER_MIME]
    cat_folder = next((f for f in folders if f["name"] == category), None)
    if cat_folder is None:
        set_stage(jid, "done",
                  f"{len(my_clips)} clips pooled into {category}")
        return "done"
    manifest, _pf = drv.load_pool_manifest(cat_folder["id"])
    unused = [c for c in manifest["clips"]
              if not c.get("used") and not c.get("held")]
    chosen = builder.select_clips(unused, prefer_sources=set(my_sids),
                                  allow_single=True)
    if not chosen:
        set_stage(jid, "done",
                  f"{len(my_clips)} clips pooled into {category} — a video "
                  f"builds automatically when the pool reaches 80s "
                  f"(pool now {sum(c['dur'] for c in unused):.0f}s unused)")
        return "done"
    token = f"run-{int(time.time())}-{jid[-4:]}"
    if drv.reserve_clips(cat_folder["id"], chosen, token) is None:
        set_stage(jid, "done",
                  f"{len(my_clips)} clips pooled into {category} — another "
                  "build is using the pool right now; yours builds next cycle")
        return "done"
    fin_folder = drv._finished_folder(category)
    title = drv._next_title(category, fin_folder)
    try:
        set_stage(jid, "scripting",
                  f"{len(chosen)} clips from pool {category} (v3 builder)")
        drv.post_now(f"Making a {category.replace('-', ' ')} video",
                     category, 2)
        local = []
        for c in chosen:
            p = WORK / f"pool-{c['name']}"
            drv.drive_download(c["drive_id"], p)
            w, h = drv.probe_wh(p)
            local.append({**c, "file": str(p), "w": w, "h": h,
                          "dur": drv.ffprobe_dur(p) or c["dur"]})
        res = builder.build_compilation(local, category,
                                        WORK / f"build-{title}", state,
                                        drv.VOICE)
        final = Path(res["final"])
        drv.drive_upload(f"{title}.mp4", fin_folder, final, "video/mp4")
        tag = f"drive-{title}"
        r = run(["gh", "release", "create", tag, str(final), "--title", title,
                 "--notes", f"Produced by the independent engine (TEMPLATE v3) "
                            f"from a pasted link ({res['n_clips']} clips, "
                            f"{res['duration']:.0f}s). Voice: {res['voice_label']}. "
                            f"Also in Drive Video Finished."])
        if r.returncode != 0:
            r = run(["gh", "release", "upload", tag, str(final), "--clobber"])
            if r.returncode != 0:
                raise RuntimeError("release publish failed: " + (r.stderr or "")[-250:])
        url = f"https://github.com/{drv.REPO}/releases/download/{tag}/{title}.mp4"
        drv.confirm_clips(cat_folder["id"], token)
        drv.save_engine_state()
        note = (f"{title} — {res['n_clips']} clips, {res['duration']:.0f}s, "
                f"in Video Finished · voice: {res['voice_label']}")
        if res.get("char_note"):
            note += f" · {res['char_note']}"
        set_stage(jid, "done", note, output=url)
        log(f"produce: DONE {title} for job {jid}")
        return "done"
    except drv.QuotaExhausted as e:
        drv.release_clips(cat_folder["id"], token)
        log(f"produce: {jid} deferred — {e}")
        set_stage(jid, "queued",
                  "Waiting for Gemini quota — automatic retry next cycle")
        return "requeue"
    except Exception as e:
        drv.release_clips(cat_folder["id"], token)
        set_stage(jid, "failed", "", error=str(e)[:280])
        log(f"produce: FAILED {jid}: {e}")
        return "failed"


def main():
    engine_status("running", "Link production run started on GitHub")
    if not drv.DRIVE_TOKEN:
        log("ERROR: DRIVE_TOKEN is empty — the OAuth token step must run first.")
        return 1
    if not drv.GEMINI_KEY:
        engine_status("waiting-gemini-key",
                      "Add the GEMINI_API_KEY secret, then production runs on its own.")
        return 0
    try:
        settings = drv.qapi("GET", "/settings")
    except Exception as e:
        log(f"settings fetch failed: {e}")
        return 1
    if settings.get("paused"):
        engine_status("paused", "Paused from the board — no work started.")
        return 0
    _vs = [v for v in (settings.get("voices") or []) if isinstance(v, str)]
    if _vs:
        drv.VOICES = _vs
    drv.VOICE_COUNTER = int(settings.get("voice_counter") or 0)
    drv.VOICE = drv.VOICES[drv.VOICE_COUNTER % len(drv.VOICES)]
    board = {}
    try:
        board = drv.get_board()
        bs = board.get("settings") or {}
        drv.INBOX_ID = bs.get("inbox_folder_id") or drv.INBOX_ID
        drv.FINISHED_ID = bs.get("finished_folder_id") or drv.FINISHED_ID
        drv.POOLS_ID = bs.get("pools_folder_id") or drv.POOLS_ID
    except Exception as e:
        log(f"board fetch failed (using fallback folders): {e}")
    state = drv.load_engine_state()
    # claim jobs
    jobs = []
    if JOB_ID:
        listing = drv.qapi("GET", "/jobs").get("jobs", [])
        job = next((j for j in listing
                    if j["id"] == JOB_ID and j["status"] == "queued"), None)
        if not job:
            engine_status("idle", f"Requested job {JOB_ID} is not queued.")
            return 0
        jobs = [job]
    else:
        for _ in range(MAX_JOBS):
            job = drv.qapi("GET", "/next")
            if not job:
                break
            jobs.append(job)
    if not jobs:
        engine_status("idle", "Queue is empty — nothing to produce.")
        return 0
    results = []
    for job in jobs:
        try:
            results.append(process_job(job, state))
        except drv.QuotaExhausted as e:
            log(f"produce: quota reached mid-job — requeueing {job['id']}")
            set_stage(job["id"], "queued",
                      "Waiting for Gemini quota — automatic retry next cycle")
            results.append("requeue")
            break
        except Exception as e:
            set_stage(job["id"], "failed", "", error=str(e)[:280])
            results.append("failed")
        if results[-1] == "requeue":
            break
        drv.save_engine_state()
    drv.save_engine_state()
    drv.post_drive_pools()
    drv.post_now("Waiting for the next run", "", 0)
    engine_status("idle", f"Link run finished: {', '.join(results)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
