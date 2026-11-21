#!/usr/bin/env python3
"""
builder.py — THE single v3 compilation builder for the independent engine.

Used by BOTH production paths (drive.py merger for Drive pools and
produce.py for pasted-link jobs) so a video is built exactly the same way
no matter where its clips came from. Implements TEMPLATE.md v3:

  - Part 3 script via Gemini (robust: retries + deterministic line-count
    repair — a script can never hard-fail a job for its shape)
  - Hook: rotating bank line + rotating screen text, 5-shot montage from
    the video's own clips, NO whoosh in the hook
  - Voice per slot: Gemini TTS (rich, free tier, paced) as PRIMARY with a
    circuit breaker, Kokoro (board-selected voice) as the fallback —
    voice can never fail a video. Every block is pause-compressed (no
    internal gap > ~0.28s) and fitted: atempo to the clip window when the
    ratio is within [0.90, 1.12], otherwise the voice defines the slot
    (video tail-trims or slows/clones). Digital silence is never padded.
  - Characters: fresh PNGs via the Gemini image API + PIL/scipy
    flood-fill cutout; ANY failure -> transparent placeholders and the
    build continues (a character never fails a video).
  - SFX from the repo's sfx/ folder (renderer synthesizes fallbacks).
  - Render via render_bundle.py (H.265 CRF 19, -16 LUFS), then an
    RMS gap scan of the final mix (<= 0.40s low-energy run). The voice
    track is ALSO scanned before rendering, so a silent mix is caught
    before a render is ever burned; a post-render failure gets one
    automatic repair + re-render before the job may fail.

drive.py provides the platform layer (board API, Drive API, paced Gemini
with daily-quota state) and is imported as `drv`.
"""
import json, math, os, random, re, shutil, subprocess, sys, time
from pathlib import Path

import drive as drv

HERE = Path(__file__).resolve().parent
SFX_DIR = HERE / "sfx"
NUMWORDS = drv.NUMWORDS

HOOK_BANK = [
    "99% of smart people know these life hacks, how many did you know?",
    "Which genius came up with these life hacks? I bet you never seen the last one",
    "These tricks my grandpa never showed everyone",
]
HOOK_BANK_SUFFIX = {0: " The secret opens at the end."}
SCREEN_HOOKS = [
    "STOP WASTING MONEY!", "YOU'VE BEEN DOING IT WRONG!",
    "THE LAST ONE IS GENIUS!", "99% OF PEOPLE DON'T KNOW THIS!",
    "WAIT FOR THE LAST ONE!", "NO ONE TELLS YOU THIS!",
    "THIS WILL SAVE YOU $100!", "YOUR GRANDPA KNEW THIS!",
    "STOP THROWING IT AWAY!", "ONE TRICK CHANGES EVERYTHING!",
    "WHY DIDN'T I KNOW THIS?", "THE SECRET IS AT THE END!",
]
OPENING_STYLES = ["action-first demo", "mistake callout", "personal story",
                  "secret reveal", "money shock", "challenge",
                  "relatable problem"]
# Gemini TTS voices (male DIY narrator range), rotated per video.
GEMINI_TTS_VOICES = ["Charon", "Puck", "Fenrir"]
IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-2.5-flash-image-preview"]
CHAR_POSES = [
    "pointing up with an excited grin",
    "holding a wrench and giving a thumbs up",
    "holding a cordless drill, looking impressed",
    "wiping his forehead, amazed expression",
    "carrying a wooden plank over one shoulder",
    "holding a paint roller, smiling at the camera",
    "celebrating with both arms raised",
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] builder: {m}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_dur(p):
    return drv.ffprobe_dur(p)


# ---------------- RMS gap scan (the v3 <=0.4s rule, never silencedetect) --
def max_low_energy_run(path, thresh_db=-48.0, win_s=0.05):
    """Decode the audio of `path` to 16kHz mono PCM and return
    (max_run_seconds, (start, end)) of the longest run whose windowed RMS
    stays below thresh_db dBFS. Pure stdlib (audioop when available)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                        "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("gap scan: audio decode failed")
    pcm = r.stdout
    sr = 16000
    win = int(sr * win_s) * 2  # bytes per window (16-bit samples)
    thresh = (32768.0 * (10 ** (thresh_db / 20.0))) ** 2  # mean-square threshold
    try:
        import audioop
        def win_ms(b):
            return audioop.rms(b, 2) ** 2
    except Exception:
        import array
        def win_ms(b):
            a = array.array("h"); a.frombytes(b)
            return sum(x * x for x in a) / max(1, len(a))
    best, best_span, cur, cur_start = 0.0, (0.0, 0.0), 0.0, 0.0
    n = len(pcm) // win
    for i in range(n):
        ms = win_ms(pcm[i * win:(i + 1) * win])
        if ms < thresh:
            if cur == 0.0:
                cur_start = i * win_s
            cur += win_s
            if cur > best:
                best, best_span = cur, (cur_start, cur_start + cur)
        else:
            cur = 0.0
    return best, best_span


# ---------------- voice block conditioning ----------------
def _pcm_of(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                        "-ac", "1", "-ar", "44100", "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("voice decode failed")
    import array
    a = array.array("h"); a.frombytes(r.stdout)
    return a


def _write_pcm(samples, path):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(samples.tobytes())


def compress_pauses(path, max_pause=0.28, keep=0.22, thresh_db=-42.0):
    """Trim every internal low-energy run longer than max_pause down to
    `keep` seconds, and trim head/tail silence to ~0.04s. This is the
    structural guarantee that a fitted voice block can never contain a
    >0.4s hole. Operates in place on a 44.1kHz wav."""
    import array
    a = _pcm_of(path)
    sr = 44100
    win = int(sr * 0.01)
    thresh = (32768.0 * (10 ** (thresh_db / 20.0))) ** 2
    try:
        import audioop
        def seg_ms(seg):
            return audioop.rms(seg.tobytes(), 2) ** 2
    except Exception:
        def seg_ms(seg):
            return sum(x * x for x in seg) / max(1, len(seg))
    flags = []
    for i in range(0, len(a) - win + 1, win):
        flags.append(seg_ms(a[i:i + win]) < thresh)
    # build keep-ranges: speech windows always kept; silence runs kept
    # only up to `keep` seconds (and only max_pause+ runs are trimmed)
    keep_ranges, i, n = [], 0, len(flags)
    while i < n:
        if not flags[i]:
            keep_ranges.append((i * win, min(len(a), (i + 1) * win)))
            i += 1
            continue
        j = i
        while j < n and flags[j]:
            j += 1
        run_s = (j - i) * 0.01
        keep_s = run_s if run_s <= max_pause else keep
        if i == 0 or j == n:
            keep_s = min(keep_s, 0.04)
        k = int(keep_s * sr)
        if k > 0:
            keep_ranges.append((i * win, min(len(a), i * win + k)))
        i = j
    out = array.array("h")
    for s, e in keep_ranges:
        out.extend(a[s:e])
    if len(out) < int(0.2 * sr):
        raise RuntimeError("voice block collapsed after pause compression")
    _write_pcm(out, path)
    return len(out) / sr


def atempo_fit(path, target_s):
    """Fit a wav to exactly target_s with a single atempo (caller checks
    the ratio is sane). Rewrites the file; returns the new duration."""
    cur = ffprobe_dur(path)
    if cur <= 0 or target_s <= 0:
        raise RuntimeError("atempo fit: bad durations")
    f = cur / target_s
    tmp = str(path) + ".fit.wav"
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
             "-af", f"atempo={f:.4f}", "-ar", "44100", tmp])
    if r.returncode != 0:
        raise RuntimeError("atempo fit failed: " + (r.stderr or "")[-200:])
    shutil.move(tmp, path)
    return ffprobe_dur(path)


def _chain(path, filters):
    tmp = str(path) + ".ch.wav"
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
             "-af", filters, "-ar", "44100", tmp])
    if r.returncode != 0:
        raise RuntimeError("voice chain failed: " + (r.stderr or "")[-200:])
    shutil.move(tmp, path)


# ---------------- voice synthesis (Gemini TTS -> Kokoro) ----------------
_tts_breaker = {"tripped": False}


def _gemini_tts(text, voice_name, out_wav):
    import base64, wave
    last = None
    quota_fails = 0
    for model in drv.TTS_MODELS:
        try:
            out = drv.gpost_tts(model, {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig":
                        {"prebuiltVoiceConfig": {"voiceName": voice_name}}}}})
            rawb = base64.b64decode(
                out["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
            if rawb[:4] == b"RIFF":
                Path(out_wav).write_bytes(rawb)
            else:
                with wave.open(str(out_wav), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                    w.writeframes(rawb)
            return True
        except drv.QuotaExhausted as e:
            last = e
            quota_fails += 1
            if quota_fails >= 2:
                raise
            log(f"gemini tts quota on {model} — trying next voice model")
            continue
        except Exception as e:
            last = e
            continue
    if isinstance(last, drv.QuotaExhausted):
        raise last
    log(f"gemini tts failed ({last}) — falling back")
    return False


_kokoro = {"pipe": None, "tried": False}


def _kokoro_wav(text, voice, out_wav):
    try:
        import numpy as np, wave
        if not _kokoro["tried"]:
            _kokoro["tried"] = True
            from kokoro import KPipeline
            _kokoro["pipe"] = KPipeline(lang_code="a")
        if _kokoro["pipe"] is None:
            return False
        chunks = [a for _, _, a in _kokoro["pipe"](text, voice=voice, speed=1.08)]
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


def synth_slot(text, out_wav, state, kokoro_voice):
    """Synthesize ONE narration slot. Returns (label, duration).
    Gemini TTS is primary; two failures trip the breaker for the rest of
    the run and Kokoro takes over — voice never fails the build."""
    raw = str(out_wav) + ".raw.wav"
    label = None
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("tts_dead_day") == today:
        _tts_breaker["tripped"] = True  # quota died earlier today — Kokoro
    if not _tts_breaker["tripped"]:
        vname = GEMINI_TTS_VOICES[state.get("voice_counter", 0) % len(GEMINI_TTS_VOICES)]
        try:
            if _gemini_tts(text, vname, raw):
                label = f"Gemini TTS {vname}"
        except drv.QuotaExhausted:
            _tts_breaker["tripped"] = True
            state["tts_dead_day"] = today
            try:
                drv.save_engine_state()
            except Exception:
                pass
            log("gemini tts quota reached — Kokoro for the rest of today")
    if label is None:
        if _kokoro_wav(text, kokoro_voice, raw):
            label = f"Kokoro {kokoro_voice}"
            _tts_breaker["tripped"] = True  # stay on Kokoro this run
    if label is None:
        raise RuntimeError("voice synthesis failed (Gemini TTS and Kokoro)")
    # conditioning: Kokoro gets the richness chain (slight pitch lift +
    # compression); Gemini TTS is already rich — compression only.
    if label.startswith("Kokoro"):
        _chain(raw, "aresample=44100,asetrate=45423,aresample=44100,"
                    "atempo=0.9709,acompressor=threshold=-18dB:ratio=3,"
                    "volume=1.05")
    else:
        _chain(raw, "aresample=44100,acompressor=threshold=-18dB:ratio=3,"
                    "volume=1.05")
    dur = compress_pauses(raw)
    shutil.move(raw, out_wav)
    return label, dur


# ---------------- script (Part 3, robust) ----------------
SCRIPT_PROMPT = """You are a top short-form creator writing narration for a DIY / life-hack compilation. You write exactly the way a real person talks on camera — never like an AI, never like an ad.

For EACH trick clip below you get an assigned OPENING STYLE — use that style for that trick's opening, and never repeat a style. Per trick write 2-3 short sentences in this order: the opening (in the assigned style) -> the step(s), in the exact order they appear on screen -> ONE concrete payoff line (the visible end result, in plain words). The LAST trick's opening must call back to the hook: it is the secret promised at the start. Lines must name what THIS clip actually shows — the object, the motion, the result. Budget: about {wps} words per second of the clip's slot (the slot seconds are given), so the narration fills its slot with no dead air.

SOUND HUMAN, NOT GENERATED: contractions everywhere, short punchy sentences mixed with longer ones, talk to the viewer directly. NEVER use these or anything like them: "in this video", "let's dive in", "game-changer", "say goodbye to", "introducing", "elevate", "unlock", "master the art", "amazing trick you won't believe", no exclamation spam, never the same sentence pattern twice in a row.

Also write:
- "title": a fresh, caption-ready video title in this form: "<hooky phrase> - <N> Genius Tricks Pros Hide" (adapt the wording to the niche; N = the number of clips).
- "cta": ONE closing line: a comments question about the tricks PLUS a follow ask, in one natural breath (e.g. "Which one will you try first? Tell me below — and follow for more.").

Return ONLY JSON: {{"title": "...", "lines": ["...", ...], "cta": "..."}} with EXACTLY {n} lines, one per clip, in the order given.
Clips (final order, slot seconds, opening style, what is shown):
"""


def _fix_lines(lines, clips):
    """Deterministically repair a line list to exactly len(clips):
    merge the shortest adjacent pair when too many; split the longest
    line at a sentence/conjunction boundary when too few; as a last
    resort synthesise a plain line from the clip description."""
    lines = [str(x).strip() for x in lines if str(x).strip()]
    n = len(clips)
    while len(lines) > n:
        i = min(range(len(lines) - 1),
                key=lambda k: len(lines[k]) + len(lines[k + 1]))
        lines[i:i + 2] = [lines[i].rstrip(". ") + ". " + lines[i + 1]]
    while len(lines) < n:
        # split the longest line at its weakest joint
        cand = max(range(len(lines)), key=lambda k: len(lines[k])) if lines else None
        split_done = False
        if cand is not None and len(lines[cand]) >= 60:
            txt = lines[cand]
            joints = [m.start() for m in re.finditer(r"(?<=[.!?])\s+|,\s+|\s+and\s+|\s+then\s+", txt)]
            joints = [j for j in joints if 25 <= j <= len(txt) - 25]
            if joints:
                j = min(joints, key=lambda x: abs(x - len(txt) / 2))
                a, b = txt[:j].strip().rstrip(", "), txt[j:].strip()
                if not a.endswith((".", "!", "?")):
                    a += "."
                lines[cand:cand + 1] = [a, b]
                split_done = True
        if not split_done:
            idx = len(lines)
            desc = clips[idx]["desc"] if idx < len(clips) else "this one"
            lines.append(f"Watch this one closely — {desc[0].lower() + desc[1:] if desc else 'it just works'}.")
    return lines[:n]


def write_script(clips, state):
    """Part 3 script with retries across models; the line count is
    repaired deterministically, so this only raises on quota exhaustion
    or total Gemini failure."""
    styles = OPENING_STYLES[:]
    random.shuffle(styles)
    listing = "\n".join(
        f"{i+1}. ({c['dur']:.1f}s) [{styles[i % len(styles)]}] {c['desc']}"
        for i, c in enumerate(clips))
    prompt = SCRIPT_PROMPT.format(wps="2.6", n=len(clips)) + listing
    last = None
    for attempt in range(3):
        try:
            txt = drv.gemini_generate_text(
                [{"text": prompt}], state, kind="script")
            data = json.loads(txt)
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict)
                             and "lines" in d), {})
            lines = _fix_lines(data.get("lines", []), clips)
            title = str(data.get("title") or "").strip()
            cta = str(data.get("cta") or "").strip() or \
                "Which one will you try first? Tell me below — and follow for more."
            return {"title": title, "lines": lines, "cta": cta}
        except drv.QuotaExhausted:
            raise
        except Exception as e:
            last = e
            log(f"script attempt {attempt + 1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"script failed after retries: {last}")


# ---------------- characters (best-effort, never fatal) ----------------
def _cutout(img_path, out_path):
    """White-background flood-fill cutout (PIL + numpy + scipy)."""
    from PIL import Image, ImageFilter
    import numpy as np
    from scipy import ndimage
    im = Image.open(img_path).convert("RGB")
    a = np.asarray(im).astype(int)
    white = (a[:, :, 0] > 242) & (a[:, :, 1] > 242) & (a[:, :, 2] > 242)
    lab, _ = ndimage.label(white)
    border = set(np.unique(np.concatenate(
        [lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]]))) - {0}
    bg = np.isin(lab, list(border)) if border else white
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    out = Image.fromarray(np.dstack([a.astype(np.uint8), alpha]), "RGBA")
    out.split()[3].filter(ImageFilter.GaussianBlur(1))
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    out.save(out_path)
    return True


def _placeholder(out_path):
    from PIL import Image
    Image.new("RGBA", (240, 240), (0, 0, 0, 0)).save(out_path)
    return out_path


def make_characters(work, count, state):
    """Return (paths, note): `count` character PNGs (hook first). Fresh
    Gemini images when possible; transparent placeholders otherwise —
    this function never raises."""
    paths = []
    try:
        if state.get("img_dead_day") == time.strftime("%Y-%m-%d",
                                                      time.gmtime()):
            raise drv.QuotaExhausted("image quota already dead today")
        want = min(count, 4)
        made = []
        for k in range(want):
            pose = CHAR_POSES[k % len(CHAR_POSES)]
            prompt = ("Sticker-style flat vector cartoon mascot of a friendly "
                      "male DIY handyman wearing a teal work shirt, " + pose +
                      ", full body, standing on a plain solid white "
                      "background, no text, no shadow")
            raw = work / f"char_raw_{k}.png"
            ok = False
            for model in IMAGE_MODELS:
                try:
                    import base64
                    out = drv.gpost_image(model, {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseModalities": ["IMAGE"]}})
                    for part in out["candidates"][0]["content"]["parts"]:
                        if "inlineData" in part:
                            raw.write_bytes(base64.b64decode(
                                part["inlineData"]["data"]))
                            ok = True
                            break
                    if ok:
                        break
                except drv.QuotaExhausted:
                    raise  # quota is run-fatal for characters: abort all
                except Exception as e:
                    log(f"character image attempt failed ({model}): {e}")
                    continue
            if not ok:
                raise RuntimeError("image generation failed")
            cut = work / f"char_{k}.png"
            _cutout(raw, cut)
            made.append(cut)
        for i in range(count):
            paths.append(made[i % len(made)])
        return paths, ""
    except drv.QuotaExhausted:
        # A character must never defer or fail a video — placeholders.
        state["img_dead_day"] = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            drv.save_engine_state()
        except Exception:
            pass
        log("characters skipped (image quota) — rendering without")
        ph = work / "char_placeholder.png"
        _placeholder(ph)
        return [ph] * count, "character skipped this run — rendered without"
    except Exception as e:
        log(f"characters unavailable ({e}) — rendering without character")
        ph = work / "char_placeholder.png"
        _placeholder(ph)
        return [ph] * count, "character unavailable this run — rendered without"


# ---------------- clip selection (v3 pool rules) ----------------
def select_clips(unused, prefer_sources=(), allow_single=False,
                 target_lo=74.0, target_hi=83.0):
    """unused: manifest clip dicts (with dur, reveal, source_id).
    Returns an ordered list (strongest mystery LAST, round-robin across
    sources) totalling ~target seconds, or None when the v3 gate fails:
    >= 80s of material and (>= 2 sources, or a single preferred source
    with >= 80s when allow_single)."""
    total = sum(c["dur"] for c in unused)
    if total < 80.0:
        return None
    by_src = {}
    for c in unused:
        by_src.setdefault(c.get("source_id"), []).append(c)
    own = [c for c in unused if c.get("source_id") in prefer_sources]
    if not (len(by_src) >= 2 or (allow_single and sum(c["dur"] for c in own) >= 80.0)):
        return None
    for lst in by_src.values():
        random.shuffle(lst)
    src_order = sorted(by_src, key=lambda s: -sum(c["dur"] for c in by_src[s]))
    if prefer_sources:  # preferred sources first in the round-robin
        src_order.sort(key=lambda s: 0 if s in prefer_sources else 1)
    chosen, total_ch = [], 0.0
    idx = {s: 0 for s in src_order}
    while total_ch < target_lo:
        progressed = False
        for s in src_order:
            if total_ch >= target_lo:
                break
            if idx[s] < len(by_src[s]):
                c = by_src[s][idx[s]]; idx[s] += 1
                if total_ch + c["dur"] <= target_hi + 6.0:
                    chosen.append(c); total_ch += c["dur"]
                    progressed = True
        if not progressed:
            break
    if total_ch < 65.0 or len(chosen) < 4:
        return None
    strongest = max(chosen, key=lambda c: float(c.get("reveal", 5)))
    chosen = [c for c in chosen if c is not strongest] + [strongest]
    return chosen


# ---------------- the build ----------------
def _screen_lines(text):
    if len(text) <= 18:
        return [text]
    words = text.split()
    best, cur = None, ""
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = abs(len(a) - len(b))
        if best is None or score < best[0]:
            best = (score, [a, b])
    return best[1]


def _fit_trick(voice_wav, clip_dur):
    """Decide (slot, in, out, pad, payoff_at) for one trick from its
    fitted voice duration vs the clip window (0.1s head-bleed trim)."""
    V = ffprobe_dur(voice_wav)
    W = max(1.0, clip_dur - 0.1)
    ratio = V / W if W else 1.0
    if 0.90 <= ratio <= 1.12:
        atempo_fit(voice_wav, W)
        return W, 0.1, 0.1 + W, "clone", max(0.4, W - 0.35)
    if V < W:
        tin = max(0.0, clip_dur - V)
        return V, tin, clip_dur, "clone", max(0.4, V - 0.35)
    f = min(1.5, V / W)
    payoff = min(V - 0.35, max(0.4, (W - 0.4) * f))
    return V, 0.1, clip_dur, "slow", payoff


def build_compilation(local_clips, category, work, state, kokoro_voice):
    """Build ONE v3 compilation from downloaded local clips (selection
    order preserved; strongest must already be LAST). Returns a dict:
    {final, duration, title, voice_labels, char_note, n_clips}.
    Raises drv.QuotaExhausted (caller requeues) or RuntimeError."""
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    (work / "bundle" / "clips").mkdir(parents=True)
    (work / "bundle" / "voice").mkdir(parents=True)
    (work / "bundle" / "chars").mkdir(parents=True)
    (work / "bundle" / "sfx").mkdir(parents=True)
    bundle = work / "bundle"
    n = len(local_clips)

    script = write_script(local_clips, state)
    title = script["title"] or f"{n} Genius Tricks Pros Hide"

    # ---- voice slots ----
    bank_idx = int(state.get("hook_bank_next", 1)) - 1
    bank_idx = bank_idx % len(HOOK_BANK)
    hook_text = HOOK_BANK[bank_idx] + HOOK_BANK_SUFFIX.get(bank_idx, "")
    screen_idx = int(state.get("screen_hook_next", 0)) % len(SCREEN_HOOKS)
    screen_text = SCREEN_HOOKS[screen_idx]

    labels = []
    hook_wav = bundle / "voice" / "hook.wav"
    lab, hook_slot = synth_slot(hook_text, hook_wav, state, kokoro_voice)
    labels.append(lab)
    trick_meta = []
    voice_files = [hook_wav]
    for i, c in enumerate(local_clips):
        text = f"{NUMWORDS[i] if i < len(NUMWORDS) else str(i + 1)} — {script['lines'][i]}"
        vw = bundle / "voice" / f"{i + 1:03d}.wav"
        lab, _ = synth_slot(text, vw, state, kokoro_voice)
        labels.append(lab)
        slot, tin, tout, pad, payoff = _fit_trick(vw, c["dur"])
        tm = {"clip": f"clips/{i + 1:03d}.mp4", "in": round(tin, 3),
              "out": round(tout, 3), "voice": f"voice/{i + 1:03d}.wav",
              "character": f"chars/{i + 1:03d}.png", "pad": pad,
              "payoff_at": round(payoff, 3)}
        if slot >= 3.5:
            tm["zoom"] = {"at": round(max(0.0, payoff - 0.7), 3), "dur": 1.1}
        trick_meta.append(tm)
        voice_files.append(vw)
    cta_wav = bundle / "voice" / "cta.wav"
    lab, cta_slot = synth_slot(script["cta"], cta_wav, state, kokoro_voice)
    labels.append(lab)
    voice_files.append(cta_wav)
    # ---- hard ceiling: the finished video must stay within 80-90s.
    # If slow speech stretched the slots past the plan, atempo the
    # longest trick voices down (voice-defined tail slots) until the
    # total fits. Reliability of the format beats a perfect fit ratio.
    trick_slots = []
    for tm in trick_meta:
        trick_slots.append(drv.ffprobe_dur(bundle / tm["voice"]))
    total_est = hook_slot + sum(trick_slots) + cta_slot
    if total_est > 89.5:
        excess = total_est - 88.5
        log(f"duration trim: total {total_est:.1f}s over target — "
            f"trimming {excess:.1f}s from the longest tricks")
        order = sorted(range(len(trick_slots)),
                       key=lambda i: -trick_slots[i])
        for i in order:
            if excess <= 0:
                break
            reducible = trick_slots[i] - 3.2
            if reducible <= 0.1:
                continue
            take = min(excess, reducible)
            new_dur = trick_slots[i] - take
            vw = bundle / trick_meta[i]["voice"]
            atempo_fit(vw, new_dur)
            c = local_clips[i]
            trick_meta[i].update({
                "in": round(max(0.0, c["dur"] - new_dur), 3),
                "out": round(c["dur"], 3), "pad": "clone",
                "payoff_at": round(max(0.4, new_dur - 0.35), 3)})
            if new_dur >= 3.5:
                trick_meta[i]["zoom"] = {
                    "at": round(max(0.0, new_dur - 1.05), 3), "dur": 1.1}
            else:
                trick_meta[i].pop("zoom", None)
            trick_slots[i] = new_dur
            excess -= take
        log(f"duration trim done: estimated total "
            f"{hook_slot + sum(trick_slots) + cta_slot:.1f}s")
    voice_label = labels[0] if len(set(labels)) == 1 else \
        f"mixed ({', '.join(sorted(set(labels)))})"

    # ---- voice-track pre-scan: catch a hole BEFORE any render ----
    track = work / "voice_track.wav"
    fc_parts = [f"[{i}:a]aresample=44100[c{i}]" for i in range(len(voice_files))]
    fc_parts.append("".join(f"[c{i}]" for i in range(len(voice_files))) +
                    f"concat=n={len(voice_files)}:v=0:a=1[out]")
    r = run(["ffmpeg", "-y", "-v", "error"] +
            sum([["-i", str(v)] for v in voice_files], []) +
            ["-filter_complex", ";".join(fc_parts),
             "-map", "[out]", "-codec:a", "pcm_s16le", str(track)])
    if r.returncode == 0:
        gap, span = max_low_energy_run(track)
        log(f"voice track pre-scan: max low-energy run {gap:.2f}s")
        if gap > 0.38:
            raise RuntimeError(
                f"voice track has a {gap:.2f}s hole at {span[0]:.1f}s — "
                "refusing to burn a render")

    # ---- characters (never fatal) ----
    char_paths, char_note = make_characters(work, n + 1, state)
    shutil.copy(char_paths[0], bundle / "chars" / "hook.png")
    for i in range(n):
        shutil.copy(char_paths[i + 1], bundle / "chars" / f"{i + 1:03d}.png")

    # ---- clips into the bundle (audio kept: it is the ASMR bed) ----
    for i, c in enumerate(local_clips):
        shutil.copy(c["file"], bundle / "clips" / f"{i + 1:03d}.mp4")

    # ---- hook montage: 5 shots from this video's own clips, never the
    # last trick's method ----
    shot_src = local_clips[:-1] if n > 1 else local_clips
    shots = []
    per = hook_slot / 5.0
    for k in range(5):
        c = shot_src[k % len(shot_src)]
        idx = local_clips.index(c) + 1
        inpt = min(max(0.0, c["dur"] * 0.2), max(0.0, c["dur"] - per - 0.05))
        shots.append({"clip": idx, "in": round(inpt, 3), "dur": round(per, 3)})

    dims = {}
    for c in local_clips:
        dims[(c["w"], c["h"])] = dims.get((c["w"], c["h"]), 0) + 1
    (W, H) = max(dims.items(), key=lambda kv: kv[1])[0]
    meta = {
        "version": 3, "width": W, "height": H, "fps": 30, "title": title,
        "hook": {"screen_text": _screen_lines(screen_text),
                 "voice": "voice/hook.wav", "character": "chars/hook.png",
                 "shots": shots},
        "tricks": trick_meta,
        "end_card": {"dur": round(cta_slot, 3), "voice": "voice/cta.wav",
                     "character": "chars/hook.png"},
    }
    (bundle / "meta.json").write_text(json.dumps(meta, indent=2))
    for name in ("pop.wav", "whoosh.wav", "sparkle.wav", "chime.wav"):
        src = SFX_DIR / name
        if src.exists():
            shutil.copy(src, bundle / "sfx" / name)

    # ---- render + post-scan + one automatic repair ----
    final = _render(bundle)
    gap, span = max_low_energy_run(final)
    log(f"final mix gap scan: max low-energy run {gap:.2f}s")
    if gap > 0.40:
        log(f"repairing dead air at {span[0]:.1f}-{span[1]:.1f}s and re-rendering")
        _repair_gap(bundle, meta, span)
        final = _render(bundle)
        gap, span = max_low_energy_run(final)
        log(f"after repair: max low-energy run {gap:.2f}s")
        if gap > 0.40:
            raise RuntimeError(
                f"dead air of {gap:.2f}s in output even after repair")
    duration = ffprobe_dur(final)

    # advance the hook rotation only now that a video truly exists
    state["hook_bank_next"] = (bank_idx + 1) % len(HOOK_BANK) + 1
    state["screen_hook_next"] = (screen_idx + 1) % len(SCREEN_HOOKS)
    state["voice_counter"] = int(state.get("voice_counter", 0)) + 1
    return {"final": str(final), "duration": duration, "title": title,
            "voice_label": voice_label, "char_note": char_note,
            "n_clips": n, "gap": gap}


def _render(bundle):
    r = run(["python3", str(HERE / "render_bundle.py"), str(bundle)])
    if r.returncode != 0:
        raise RuntimeError("render failed: " + (r.stderr or "")[-300:])
    return bundle / "final.mp4"


def _repair_gap(bundle, meta, span):
    """Re-compress the voice block whose slot contains the gap's
    midpoint (harder settings) and refresh that trick's meta timings."""
    starts, acc = [], 0.0
    slots = []
    hv = drv.ffprobe_dur(bundle / meta["hook"]["voice"])
    slots.append(hv)
    for tr in meta["tricks"]:
        slots.append(drv.ffprobe_dur(bundle / tr["voice"]))
    slots.append(drv.ffprobe_dur(bundle / meta["end_card"]["voice"]))
    for s in slots:
        starts.append(acc); acc += s
    mid = (span[0] + span[1]) / 2.0
    seg = max(0, min(len(slots) - 1,
                     next((i for i in range(len(slots))
                           if starts[i] <= mid < starts[i] + slots[i]),
                          len(slots) - 1)))
    if seg == 0:
        rel = meta["hook"]["voice"]
    elif seg == len(slots) - 1:
        rel = meta["end_card"]["voice"]
    else:
        rel = meta["tricks"][seg - 1]["voice"]
    vw = bundle / rel
    compress_pauses(vw, max_pause=0.18, keep=0.13)
    if 1 <= seg <= len(meta["tricks"]):
        tr = meta["tricks"][seg - 1]
        clip_dur = drv.ffprobe_dur(bundle / tr["clip"])
        slot, tin, tout, pad, payoff = _fit_trick(vw, clip_dur)
        tr.update({"in": round(tin, 3), "out": round(tout, 3), "pad": pad,
                   "payoff_at": round(payoff, 3)})
        if slot >= 3.5:
            tr["zoom"] = {"at": round(max(0.0, payoff - 0.7), 3), "dur": 1.1}
        else:
            tr.pop("zoom", None)
        (bundle / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"repair: re-compressed voice block {rel}")
