#!/usr/bin/env python3
"""
render_bundle.py — cloud/local render half of the video compilation engine.

Runs on a GitHub Actions ubuntu runner (or anywhere with ffmpeg/ffprobe).
Self-contained: Python stdlib + ffmpeg/ffprobe via subprocess only.

=========================================================================
BUNDLE FORMAT v3 — THE STANDARD TEMPLATE (TEMPLATE.md v3, owner 2026-11-20)
=========================================================================
Bundles are SEGMENT-based. v2 bundles (flat clip list + per-clip voice)
are NO LONGER supported. Layout:

    bundle/
      meta.json                  v3 description (below)
      clips/001.mp4, 002.mp4...  trick clips in FINAL play order.
                                 Audio is KEPT (it becomes the ASMR bed).
      voice/hook.wav             hook voice block (Seed, pitch-chained,
      voice/001.wav, 002.wav...  one per trick, ALREADY fitted by the
      voice/cta.wav              bundle builder: silence-trimmed and
                                 atempo-fitted to its slot, see below)
      chars/hook.png             character PNGs, transparent, ~120px tall
      chars/001.png, 002.png...  (hook.png is reused on the end card)
      sfx/pop.wav                SFX assets (copied in by the builder;
      sfx/whoosh.wav             renderer synthesizes a fallback for any
      sfx/sparkle.wav            missing one, but builders SHOULD ship
      sfx/chime.wav              the real files)

meta.json:
    {
      "version": 3,
      "width": 1080, "height": 1920, "fps": 30,
      "title": "Caption Ready Title - 7 Genius Tricks Pros Hide",
      "hook": {
        "screen_text": ["99% OF PEOPLE", "DON'T KNOW THIS!"],
        "voice": "voice/hook.wav",
        "character": "chars/hook.png",
        "shots": [
          {"clip": 5, "in": 2.0, "dur": 1.1},   // clip = 1-based index
          {"clip": 2, "in": 4.0, "dur": 1.1}    // into clips/; "in" is
        ]                                       // seconds into that clip
      },
      "tricks": [
        {
          "clip": "clips/001.mp4",
          "in": 0.0, "out": 6.0,        // OPTIONAL trim window inside
                                        // the clip (default: whole clip)
          "voice": "voice/001.wav",
          "character": "chars/001.png",
          "pad": "clone",               // how to stretch video to the
                                        // slot when the window is short:
                                        // "clone" = freeze last frame,
                                        // "slow"  = slow down (<=1.5x)
                                        // then clone the remainder
          "payoff_at": 6.9,             // SLOT time (seconds from this
                                        // segment's start IN THE OUTPUT)
                                        // when the payoff completes:
                                        // sparkle lands here
          "zoom": {"at": 6.3, "dur": 1.2},  // OPTIONAL punch-in window
                                        // (SLOT time): video scales to
                                        // 125% centre-crop; a whoosh
                                        // plays at "at"
          "whoosh_at": [3.0]            // OPTIONAL extra whoosh points
        }                               // (SLOT time, inside tricks only)
      ],
      "end_card": {"dur": 2.5, "voice": "voice/cta.wav",
                   "character": "chars/hook.png"}
    }

TIMELINE RULES (the voice defines the timeline — no dead silence, ever):
  - hook slot  = duration of the hook voice block
  - trick slot = duration of that trick's voice block
  - end slot   = max(end_card.dur, duration of the CTA voice block)
  - A missing/empty voice block for ANY segment aborts the render.
  - Voice blocks are placed back-to-back, trimmed/padded to their slot
    exactly; the builder pre-fits them (atempo 0.90-1.12, text rewritten
    instead of padding), so renderer padding is a rounding residue only.
  - ALL marker times (payoff_at, zoom, whoosh_at) are SLOT times: seconds
    from the segment's start in the finished video. The builder converts
    clip times to slot times (identity when the window is trimmed at the
    end only; scaled by the slow-down factor when pad="slow").

RENDER BEHAVIOUR:
  - Every video piece conformed to meta width/height/fps (scale + pad,
    setsar=1) and enhanced: eq contrast 1.08, saturation 1.14,
    brightness 0.02 + unsharp 5:5:0.7.
  - HOOK: montage of the listed shots (each conformed, hard cuts), hook
    screen text via drawtext for the WHOLE hook (DejaVuSans-Bold, white,
    black border, top-center; expansion=none so % is literal), hook
    character overlaid top-right from frame 0. Audio = hook voice + each
    shot's OWN audio at gain 0.15. NO WHOOSH IN THE HOOK, ever.
  - TRICKS: window conformed to the slot (trim / slow / clone per "pad"),
    punch-in zoom at the marked window, character top-right. Audio =
    trick voice + the clip's OWN audio under it at gain 0.10. Pop (0.9)
    at the segment start; whoosh (0.30) at the zoom start and at any
    marked whoosh_at points — INSIDE the trick only; sparkle (0.5) at
    payoff_at.
  - END CARD: solid card, title + "FOLLOW FOR MORE" (drawtext), hook
    character top-right, CTA voice, chime (0.8) once at the card start.
  - Final mix loudnormed to I=-16, TP=-1.5 (two-pass, measured values
    fed into the encode pass). Output: H.265 libx265 CRF 19 preset fast,
    native (meta) resolution, AAC 256k, final.mp4 in the bundle dir.
  - Self-verification at the end: per-stream durations within 1s of each
    other and ~total, full decode pass, silence scan (any dead air
    >= 1.0s at -42dB fails the render). Exits non-zero on any failure.
"""
import glob
import json
import os
import re
import subprocess
import sys

MAX_SLOW = 1.50           # max video slow-down before clone-extending
HOOK_ASMR = 0.15          # hook shots' own audio gain
TRICK_ASMR = 0.10         # trick clip audio gain under the voice
POP_VOL, WHOOSH_VOL, SPARKLE_VOL, CHIME_VOL = 0.9, 0.30, 0.5, 0.8
ZOOM_SCALE = 1.25         # punch-in zoom factor at payoffs
CHAR_H = 120              # character overlay height, px (at 720-wide
                          # reference it is exactly 120; scaled by W/720)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
END_BG = "0x14202B"


def run(cmd, what="ffmpeg"):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"ERROR: {what} failed:\n{r.stderr[-2000:]}\n")
        raise SystemExit(1)
    return r


def dur(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path])
    return float(json.loads(out.decode())["format"]["duration"])


def has_audio(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "json", path])
    return any(s.get("codec_type") == "audio"
               for s in json.loads(out.decode()).get("streams", []))


def find_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


# ---------------------------------------------------------------- SFX --
def make_whoosh(path):
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "anoisesrc=color=white:duration=0.28:sample_rate=44100",
         "-af", "highpass=f=500,lowpass=f=3200,"
                "afade=t=in:st=0:d=0.08,afade=t=out:st=0.10:d=0.18,volume=0.6",
         "-codec:a", "pcm_s16le", path], "whoosh synthesis")


def make_pop(path):
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=320:duration=0.09:sample_rate=44100",
         "-af", "afade=t=out:st=0.01:d=0.08,volume=0.9",
         "-codec:a", "pcm_s16le", path], "pop synthesis")


def make_sparkle(path):
    # three-note rising sine arpeggio with decays (~0.45s)
    fc = ("sine=frequency=1568:duration=0.45:sample_rate=44100[a];"
          "sine=frequency=2093:duration=0.45:sample_rate=44100[b];"
          "sine=frequency=2637:duration=0.45:sample_rate=44100[c];"
          "[b]adelay=90:all=1[bd];[c]adelay=180:all=1[cd];"
          "[a][bd][cd]amix=inputs=3:normalize=0,"
          "afade=t=out:st=0.15:d=0.30,volume=0.5")
    run(["ffmpeg", "-y", "-v", "error", "-filter_complex", fc,
         "-codec:a", "pcm_s16le", path], "sparkle synthesis")


def make_chime(path):
    fc = ("sine=frequency=1046:duration=0.9:sample_rate=44100[a];"
          "sine=frequency=1568:duration=0.9:sample_rate=44100[b];"
          "[b]adelay=140:all=1[bd];"
          "[a][bd]amix=inputs=2:normalize=0,"
          "afade=t=out:st=0.25:d=0.65,volume=0.6")
    run(["ffmpeg", "-y", "-v", "error", "-filter_complex", fc,
         "-codec:a", "pcm_s16le", path], "chime synthesis")


SFX_MAKERS = {"pop": make_pop, "whoosh": make_whoosh,
              "sparkle": make_sparkle, "chime": make_chime}


def load_sfx(bundle, workdir):
    """Return {name: path} for the four SFX, synthesizing any missing."""
    out = {}
    for name, maker in SFX_MAKERS.items():
        src = None
        for cand in (os.path.join(bundle, "sfx", f"{name}.wav"),
                     os.path.join(bundle, "sfx", f"{name}_soft.wav"),
                     os.path.join(bundle, f"{name}.wav")):
            if os.path.exists(cand):
                src = cand
                break
        if src is None:
            src = os.path.join(workdir, f"sfx-{name}.wav")
            maker(src)
        out[name] = src
    return out


# ------------------------------------------------------------- helpers --
def voice_dur(bundle, rel, label):
    p = os.path.join(bundle, rel)
    if not (os.path.exists(p) and os.path.getsize(p) > 500):
        sys.stderr.write(
            f"ERROR: voice block missing for {label} ({rel}) — refusing "
            "to render a silent slot\n")
        raise SystemExit(1)
    return dur(p)


def conform_vf(W, H, fps):
    return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1,"
            "eq=contrast=1.08:saturation=1.14:brightness=0.02,"
            "unsharp=5:5:0.7,format=yuv420p")


def fit_font(lines, W, base_fs):
    """Shrink base_fs until the longest line fits ~94% of the width
    (DejaVu Bold averages ~0.60 em per char)."""
    longest = max((len(x) for x in lines), default=1)
    fs = base_fs
    while fs > 14 and 0.60 * fs * longest > W * 0.94:
        fs -= 2
    return fs


def write_textfile(workdir, name, text):
    p = os.path.join(workdir, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def char_h(W):
    return max(60, int(round(CHAR_H * W / 720.0)))


# ------------------------------------------------------- segment video --
def build_hook_video(bundle, meta, hook, slot, workdir, font):
    """Montage of shots -> text + character. Returns video-only mp4."""
    W, H, fps = meta["width"], meta["height"], meta["fps"]
    clips = sorted(glob.glob(os.path.join(bundle, "clips", "*.mp4")))
    shots = hook["shots"]
    inputs, fc = [], []
    # one input per shot (same file may repeat with different windows)
    for j, sh in enumerate(shots):
        src = clips[int(sh["clip"]) - 1]
        inputs += ["-ss", f"{float(sh.get('in', 0)):.3f}",
                   "-t", f"{float(sh['dur']):.3f}", "-i", src]
        fc.append(f"[{j}:v]{conform_vf(W, H, fps)},"
                  f"trim=0:{float(sh['dur']):.3f},setpts=PTS-STARTPTS[s{j}]")
    fc.append("".join(f"[s{j}]" for j in range(len(shots))) +
              f"concat=n={len(shots)}:v=1:a=0,"
              f"tpad=stop_mode=clone:stop_duration={slot:.3f},"
              f"trim=0:{slot:.3f},setpts=PTS-STARTPTS[mont]")
    cur = "mont"
    if font and hook.get("screen_text"):
        lines = [str(x) for x in hook["screen_text"]]
        fs = fit_font(lines, W, int(round(52 * W / 720.0)))
        y = H * 0.045
        for li, ltext in enumerate(lines):
            tf = write_textfile(workdir, f"hook_text_{li}.txt", ltext)
            fc.append(
                f"[{cur}]drawtext=fontfile={font}:textfile='{tf}'"
                f":expansion=none:fontsize={fs}:fontcolor=white"
                f":borderw={max(3, fs // 14)}:bordercolor=black"
                f":x=(w-text_w)/2:y={y:.0f}[ht{li}]")
            cur = f"ht{li}"
            y += fs * 1.30
    char = os.path.join(bundle, hook["character"])
    inputs += ["-loop", "1", "-t", f"{slot:.3f}", "-i", char]
    fc.append(f"[{len(shots)}:v]scale=-1:{char_h(W)},format=rgba[ci];"
              f"[{cur}][ci]overlay=W-w-12:12[vout]")
    out = os.path.join(workdir, "seg_hook.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[vout]",
         "-c:v", "libx264", "-crf", "8", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out], "hook video build")
    return out


def build_trick_video(bundle, meta, tr, slot, workdir, idx):
    """One trick: window conformed to slot, zoom, character. Video-only."""
    W, H, fps = meta["width"], meta["height"], meta["fps"]
    src = os.path.join(bundle, tr["clip"])
    tin = float(tr.get("in", 0.0))
    tout = float(tr["out"]) if tr.get("out") else None
    win = (tout - tin) if tout else dur(src) - tin
    # --- conform the window to the slot on the SLOT timeline ---
    pre = ""
    if win < slot - 0.01:
        if (tr.get("pad") or "clone") == "slow":
            f = min(MAX_SLOW, slot / win)
            pre = f"setpts={f:.4f}*(PTS-STARTPTS),"
            rem = slot - win * f
            if rem > 0.01:
                pre += f"tpad=stop_mode=clone:stop_duration={rem:.3f},"
        else:
            pre = f"tpad=stop_mode=clone:stop_duration={slot - win:.3f},"
    base = (f"{pre}trim=0:{slot:.3f},setpts=PTS-STARTPTS,"
            + conform_vf(W, H, fps))
    inputs = ["-ss", f"{tin:.3f}", "-t", f"{win:.3f}", "-i", src]
    fc = []
    zoom = tr.get("zoom")
    if zoom:
        z0 = float(zoom["at"]); z1 = z0 + float(zoom["dur"])
        z0 = max(0.0, min(z0, slot)); z1 = max(z0, min(z1, slot))
        fc.append(f"[0:v]{base},split=3[za][zb][zc]")
        fc.append(f"[za]trim=0:{z0:.3f},setpts=PTS-STARTPTS[pa]")
        fc.append(f"[zb]trim={z0:.3f}:{z1:.3f},setpts=PTS-STARTPTS,"
                  f"scale=iw*{ZOOM_SCALE}:ih*{ZOOM_SCALE},"
                  f"crop={W}:{H}[pb]")
        fc.append(f"[zc]trim={z1:.3f}:{slot:.3f},setpts=PTS-STARTPTS[pc]")
        fc.append(f"[pa][pb][pc]concat=n=3:v=1:a=0[zv]")
        cur = "zv"
    else:
        fc.append(f"[0:v]{base}[zv]")
        cur = "zv"
    char = os.path.join(bundle, tr["character"])
    inputs += ["-loop", "1", "-t", f"{slot:.3f}", "-i", char]
    fc.append(f"[1:v]scale=-1:{char_h(W)},format=rgba[ci];"
              f"[{cur}][ci]overlay=W-w-12:12[vout]")
    out = os.path.join(workdir, f"seg_trick_{idx:02d}.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[vout]",
         "-c:v", "libx264", "-crf", "8", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out], f"trick video build {tr['clip']}")
    return out


def build_end_video(bundle, meta, slot, workdir, font):
    """End card: solid bg + title + FOLLOW FOR MORE + character."""
    W, H, fps = meta["width"], meta["height"], meta["fps"]
    ec = meta["end_card"]
    title = str(meta.get("title") or "")
    words, lines, line = title.split(), [], ""
    for wd in words:
        if line and len(line) + 1 + len(wd) > 22:
            lines.append(line); line = wd
        else:
            line = (line + " " + wd).strip()
    if line:
        lines.append(line)
    lines = lines[:3]
    inputs = ["-f", "lavfi", "-i",
              f"color=c={END_BG}:size={W}x{H}:rate={fps}:duration={slot:.3f}"]
    fc = ["[0:v]format=yuv420p[bg]"]
    cur = "bg"
    if font and lines:
        fs = fit_font(lines, W, int(round(60 * W / 720.0)))
        total_h = fs * 1.3 * len(lines)
        y = H * 0.40 - total_h / 2
        for li, ltext in enumerate(lines):
            tf = write_textfile(workdir, f"end_title_{li}.txt", ltext)
            fc.append(
                f"[{cur}]drawtext=fontfile={font}:textfile='{tf}'"
                f":expansion=none:fontsize={fs}:fontcolor=white"
                f":borderw={max(3, fs // 14)}:bordercolor=black"
                f":x=(w-text_w)/2:y={y:.0f}[et{li}]")
            cur = f"et{li}"
            y += fs * 1.3
        ffs = fit_font(["FOLLOW FOR MORE"], W, int(round(40 * W / 720.0)))
        tf = write_textfile(workdir, "end_follow.txt", "FOLLOW FOR MORE")
        fc.append(
            f"[{cur}]drawtext=fontfile={font}:textfile='{tf}'"
            f":expansion=none:fontsize={ffs}:fontcolor=0xFFD93B"
            f":borderw={max(3, ffs // 14)}:bordercolor=black"
            f":x=(w-text_w)/2:y={H * 0.60:.0f}[ef]")
        cur = "ef"
    char = os.path.join(bundle, ec["character"])
    inputs += ["-loop", "1", "-t", f"{slot:.3f}", "-i", char]
    fc.append(f"[1:v]scale=-1:{char_h(W)},format=rgba[ci];"
              f"[{cur}][ci]overlay=W-w-12:12[vout]")
    out = os.path.join(workdir, "seg_end.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[vout]",
         "-c:v", "libx264", "-crf", "8", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out], "end card build")
    return out


# ------------------------------------------------------- segment audio --
def build_hook_asmr(bundle, hook, slot, workdir):
    clips = sorted(glob.glob(os.path.join(bundle, "clips", "*.mp4")))
    shots = hook["shots"]
    inputs, fc = [], []
    for j, sh in enumerate(shots):
        src = clips[int(sh["clip"]) - 1]
        d = float(sh["dur"])
        if has_audio(src):
            inputs += ["-ss", f"{float(sh.get('in', 0)):.3f}",
                       "-t", f"{d:.3f}", "-i", src]
            fc.append(f"[{j}:a]aresample=44100,atrim=0:{d:.3f},"
                      f"apad=whole_dur={d:.3f},atrim=0:{d:.3f}[a{j}]")
        else:
            inputs += ["-f", "lavfi", "-i",
                       f"anullsrc=r=44100:cl=stereo:d={d:.3f}"]
            fc.append(f"[{j}:a]atrim=0:{d:.3f}[a{j}]")
    fc.append("".join(f"[a{j}]" for j in range(len(shots))) +
              f"concat=n={len(shots)}:v=0:a=1,"
              f"apad=whole_dur={slot:.3f},atrim=0:{slot:.3f},"
              f"volume={HOOK_ASMR}[asmr]")
    out = os.path.join(workdir, "asmr_hook.wav")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[asmr]",
         "-codec:a", "pcm_s16le", out], "hook asmr build")
    return out


def build_trick_asmr(bundle, meta, tr, slot, workdir, idx):
    src = os.path.join(bundle, tr["clip"])
    tin = float(tr.get("in", 0.0))
    tout = float(tr["out"]) if tr.get("out") else None
    win = (tout - tin) if tout else dur(src) - tin
    out = os.path.join(workdir, f"asmr_trick_{idx:02d}.wav")
    if not has_audio(src):
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"anullsrc=r=44100:cl=stereo:d={slot:.3f}",
             "-codec:a", "pcm_s16le", out], "trick asmr (silent)")
        return out
    chain = "aresample=44100"
    if win < slot - 0.01 and (tr.get("pad") or "clone") == "slow":
        f = min(MAX_SLOW, slot / win)
        chain += f",atempo={1.0 / f:.4f}"
    chain += f",atrim=0:{slot:.3f},apad=whole_dur={slot:.3f}," \
             f"atrim=0:{slot:.3f},volume={TRICK_ASMR}"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{tin:.3f}",
         "-t", f"{win:.3f}", "-i", src, "-af", chain,
         "-codec:a", "pcm_s16le", out], f"trick asmr {tr['clip']}")
    return out


def concat_wavs(parts, out, what):
    inputs, fc = [], []
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        fc.append(f"[{i}:a]aresample=44100[c{i}]")
    fc.append("".join(f"[c{i}]" for i in range(len(parts))) +
              f"concat=n={len(parts)}:v=0:a=1[out]")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[out]",
         "-codec:a", "pcm_s16le", out], what)
    return out


def build_voice_track(bundle, seg_voices, slots, workdir):
    """seg_voices: [(rel_path, label)] aligned with slots. Each block is
    trimmed/padded to exactly its slot and concatenated."""
    inputs, fc = [], []
    for i, ((rel, _label), slot) in enumerate(zip(seg_voices, slots)):
        inputs += ["-i", os.path.join(bundle, rel)]
        fc.append(f"[{i}:a]aresample=44100,atrim=0:{slot:.3f},"
                  f"apad=whole_dur={slot:.3f},atrim=0:{slot:.3f}[s{i}]")
    fc.append("".join(f"[s{i}]" for i in range(len(slots))) +
              f"concat=n={len(slots)}:v=0:a=1[voice]")
    out = os.path.join(workdir, "voice.wav")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[voice]",
         "-codec:a", "pcm_s16le", out], "voice track build")
    return out


def build_sfx_track(events, sfx, total, workdir):
    """events: [(abs_time, kind)] with kind in SFX vol map."""
    vols = {"pop": POP_VOL, "whoosh": WHOOSH_VOL,
            "sparkle": SPARKLE_VOL, "chime": CHIME_VOL}
    if not events:
        out = os.path.join(workdir, "sfx.wav")
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"anullsrc=r=44100:cl=stereo:d={total:.3f}",
             "-codec:a", "pcm_s16le", out], "sfx track (empty)")
        return out
    inputs, fc = [], []
    for j, (t0, kind) in enumerate(events):
        inputs += ["-i", sfx[kind]]
        ms = int(max(0.0, t0) * 1000)
        fc.append(f"[{j}:a]aresample=44100,volume={vols[kind]},"
                  f"adelay={ms}:all=1,apad,atrim=0:{total:.3f}[e{j}]")
    fc.append("".join(f"[e{j}]" for j in range(len(events))) +
              f"amix=inputs={len(events)}:normalize=0[sfx]")
    out = os.path.join(workdir, "sfx.wav")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[sfx]",
         "-codec:a", "pcm_s16le", out], "sfx track build")
    return out


# ------------------------------------------------------------- verify --
def verify(final, total):
    sd = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "json", final])
    d = {s["codec_type"]: float(s["duration"])
         for s in json.loads(sd.decode())["streams"]}
    if "video" not in d or "audio" not in d:
        sys.stderr.write(f"VERIFY FAILED: missing stream(s) in {final}: {d}\n")
        raise SystemExit(2)
    if abs(d["video"] - d["audio"]) >= 1.0:
        sys.stderr.write(
            f"VERIFY FAILED: video/audio duration mismatch "
            f"(video {d['video']:.2f}s vs audio {d['audio']:.2f}s)\n")
        raise SystemExit(2)
    if abs(d["video"] - total) > 1.5:
        sys.stderr.write(
            f"VERIFY FAILED: output {d['video']:.2f}s vs expected ~{total:.2f}s\n")
        raise SystemExit(2)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", final, "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode != 0 or r.stderr.strip():
        sys.stderr.write(f"VERIFY FAILED: decode errors:\n{r.stderr[-1500:]}\n")
        raise SystemExit(2)
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", final,
                        "-af", "silencedetect=noise=-42dB:d=1.0",
                        "-f", "null", "-"], capture_output=True, text=True)
    for ln in r.stderr.splitlines():
        m = re.search(r"silence_duration:\s*([\d.]+)", ln)
        if m and float(m.group(1)) >= 1.0:
            sys.stderr.write(
                f"VERIFY FAILED: dead silence of {m.group(1)}s in output\n")
            raise SystemExit(2)
    return d


# ---------------------------------------------------------------- main --
def main():
    bundle = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    meta_path = os.path.join(bundle, "meta.json")
    if not os.path.exists(meta_path):
        sys.stderr.write(f"ERROR: meta.json not found in {bundle}\n")
        raise SystemExit(1)
    meta = json.load(open(meta_path))
    if int(meta.get("version", 0)) != 3:
        sys.stderr.write("ERROR: this renderer only accepts v3 bundles "
                         "(meta.version == 3); see header for the format\n")
        raise SystemExit(1)
    meta["width"] = int(meta["width"]); meta["height"] = int(meta["height"])
    meta["fps"] = float(meta.get("fps", 30))
    hook, tricks, ec = meta["hook"], meta["tricks"], meta["end_card"]

    # ---- slots: the voice defines the timeline ----
    hook_slot = voice_dur(bundle, hook["voice"], "hook")
    trick_slots = [voice_dur(bundle, tr["voice"], f"trick {i+1}")
                   for i, tr in enumerate(tricks)]
    cta_slot = voice_dur(bundle, ec["voice"], "cta")
    end_slot = max(float(ec.get("dur", 2.5)), cta_slot)
    slots = [hook_slot] + trick_slots + [end_slot]
    starts, acc = [], 0.0
    for s in slots:
        starts.append(acc); acc += s
    total = acc
    print(f"bundle v3: hook {hook_slot:.2f}s + {len(tricks)} tricks "
          f"({sum(trick_slots):.1f}s) + end {end_slot:.2f}s "
          f"= {total:.1f}s @ {meta['width']}x{meta['height']}", flush=True)

    workdir = os.path.join(bundle, "_work")
    os.makedirs(workdir, exist_ok=True)
    font = find_font()
    sfx = load_sfx(bundle, workdir)

    # ---- segment videos ----
    seg_videos = [build_hook_video(bundle, meta, hook, hook_slot,
                                   workdir, font)]
    for i, (tr, slot) in enumerate(zip(tricks, trick_slots)):
        seg_videos.append(build_trick_video(bundle, meta, tr, slot,
                                            workdir, i + 1))
    seg_videos.append(build_end_video(bundle, meta, end_slot, workdir, font))
    listfile = os.path.join(workdir, "concat.txt")
    with open(listfile, "w") as fh:
        for p in seg_videos:
            fh.write(f"file '{p}'\n")
    video_only = os.path.join(workdir, "video_only.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", video_only], "video concat")

    # ---- audio: voice + asmr + sfx ----
    seg_voices = [(hook["voice"], "hook")] + \
                 [(tr["voice"], f"trick {i+1}")
                  for i, tr in enumerate(tricks)] + \
                 [(ec["voice"], "cta")]
    voice_wav = build_voice_track(bundle, seg_voices, slots, workdir)
    asmr_parts = [build_hook_asmr(bundle, hook, hook_slot, workdir)]
    for i, (tr, slot) in enumerate(zip(tricks, trick_slots)):
        asmr_parts.append(build_trick_asmr(bundle, meta, tr, slot,
                                           workdir, i + 1))
    asmr_parts.append(os.path.join(workdir, "asmr_end.wav"))
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"anullsrc=r=44100:cl=stereo:d={end_slot:.3f}",
         "-codec:a", "pcm_s16le", asmr_parts[-1]], "end asmr (silent)")
    asmr_wav = concat_wavs(asmr_parts, os.path.join(workdir, "asmr.wav"),
                           "asmr concat")

    events = []
    for i, tr in enumerate(tricks):
        st = starts[1 + i]
        events.append((st, "pop"))
        if tr.get("zoom"):
            events.append((st + float(tr["zoom"]["at"]), "whoosh"))
        for t in tr.get("whoosh_at", []) or []:
            events.append((st + float(t), "whoosh"))
        if tr.get("payoff_at") is not None:
            events.append((st + float(tr["payoff_at"]), "sparkle"))
    events.append((starts[-1], "chime"))
    sfx_wav = build_sfx_track(events, sfx, total, workdir)

    mix = os.path.join(workdir, "mix.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", voice_wav, "-i", asmr_wav,
         "-i", sfx_wav, "-filter_complex",
         "[0:a][1:a][2:a]amix=inputs=3:normalize=0[mix]",
         "-map", "[mix]", "-codec:a", "pcm_s16le", mix], "mix build")

    # ---- loudnorm two-pass: measure (WITHOUT -v error), then encode ----
    r = subprocess.run(
        ["ffmpeg", "-i", mix, "-af",
         "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        sys.stderr.write("ERROR: loudnorm measurement failed\n")
        raise SystemExit(1)
    meas = json.loads(m.group(0))
    ln = (f"loudnorm=I=-16:TP=-1.5:LRA=11:"
          f"measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:"
          f"measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    final = os.path.join(bundle, "final.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", video_only, "-i", mix,
         "-af", ln, "-map", "0:v", "-map", "1:a",
         "-c:v", "libx265", "-crf", "19", "-preset", "fast", "-tag:v", "hvc1",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", final],
        "final render")

    d = verify(final, total)
    size = os.path.getsize(final)
    print(f"OK final.mp4  {d['video']:.2f}s video / {d['audio']:.2f}s audio  "
          f"{size/1e6:.1f} MB  (decode clean, streams in sync)", flush=True)


if __name__ == "__main__":
    main()
