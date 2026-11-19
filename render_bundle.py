#!/usr/bin/env python3
"""
render_bundle.py — cloud render half of the video compilation engine.

Runs on a GitHub Actions ubuntu runner (or anywhere with ffmpeg/ffprobe).
Self-contained: Python stdlib + ffmpeg/ffprobe via subprocess only.

Bundle layout (input dir):
    bundle/
      clips/001.mp4, 002.mp4, ...     video clips in FINAL order (audio ignored)
      voice/001.mp3, 002.mp3, ...     TTS block per clip (a missing file = silent slot)
      meta.json                       {width, height, fps, title?,
                                       hook_text?, captions: [{start,end,text}] | null}
      whoosh.wav                      OPTIONAL whoosh asset; if absent one is
                                      synthesized with ffmpeg lavfi
Output:
    bundle/final.mp4                  H.265 CRF 19 preset fast, loudnorm I=-14 TP=-1.5

Behaviour (mirrors app/produce_final6.py):
  - every clip conformed to meta width/height/fps (scale + pad, setsar=1)
  - THE VOICE DEFINES THE TIMELINE (no dead silence, ever): each clip's slot
    is fitted to its voice block — a shorter voice trims the clip (at most
    0.30s of padding may remain), a longer voice is sped up to atempo 1.20
    and beyond that the VIDEO is slowed/freeze-extended so narration is
    never cut mid-word. A missing voice block is a hard error: a silent
    slot must never ship. The clip's own audio is always discarded.
  - whoosh at every cut, HARD cuts (concat, no transitions)
  - optional burned captions (small, white bold, black outline, bottom-centre;
    timings are remapped onto the fitted timeline) and optional hook_text
    overlay for the first 3 seconds
  - self-verification at the end: per-stream durations within 1s, a full
    decode pass, and a silence scan (any dead air >= 1.0s fails the render);
    exits non-zero with a clear message if verification fails
"""
import glob
import json
import os
import re
import subprocess
import sys

MAX_TEMPO = 1.20          # hard cap for voice fit-tempo (template rule)
TAIL_PAD = 0.30           # max trailing pause allowed at the end of a slot
MAX_STRETCH = 1.50        # max video slow-down before freeze-extending
WHOOSH_VOL = 0.15
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


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


def find_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def make_whoosh(path):
    """Synthesize a ~0.5s whoosh: white-noise swell through band shaping
    with fast fade-in / fade-out (no asset file needed)."""
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "anoisesrc=color=white:duration=0.28:sample_rate=44100",
         "-af", "highpass=f=500,lowpass=f=3200,"
                "afade=t=in:st=0:d=0.08,afade=t=out:st=0.10:d=0.18,volume=0.6",
         "-codec:a", "pcm_s16le", path], "whoosh synthesis")


def fit_slots(bundle, clip_durs):
    """Fit every slot to its voice block. Returns (slots, tempos, stretches):
    slot = final on-screen seconds for the clip, tempo = voice speed-up
    applied (1.0 = none), stretch = video slow-down factor (1.0 = none).
    A missing/empty voice block aborts the render — silent slots never ship."""
    slots, tempos, stretches = [], [], []
    for i, cd in enumerate(clip_durs):
        vf = os.path.join(bundle, "voice", f"{i+1:03d}.mp3")
        if not (os.path.exists(vf) and os.path.getsize(vf) > 500):
            sys.stderr.write(
                f"ERROR: voice block missing for clip {i+1} — refusing to "
                "render a silent slot\n")
            raise SystemExit(1)
        vd = dur(vf)
        tempo = min(MAX_TEMPO, vd / cd) if vd > cd else 1.0
        vd_fit = vd / tempo
        if vd_fit > cd:
            slot, stretch = vd_fit, vd_fit / cd
        else:
            slot, stretch = min(cd, vd_fit + TAIL_PAD), 1.0
        slots.append(round(slot, 3))
        tempos.append(round(tempo, 3))
        stretches.append(round(stretch, 4))
    return slots, tempos, stretches


def build_voice_track(bundle, slots, tempos, workdir):
    """One fitted block per slot, concatenated -> voice.wav of exactly total
    length. Slots were fitted to the voice, so padding is <= TAIL_PAD."""
    inputs, fc = [], []
    for i, slot in enumerate(slots):
        vf = os.path.join(bundle, "voice", f"{i+1:03d}.mp3")
        chain = f"[{i}:a]aresample=44100"
        if tempos[i] > 1.0:
            chain += f",atempo={tempos[i]:.3f}"
        chain += (f",atrim=0:{slot:.3f},apad=whole_dur={slot:.3f},"
                  f"atrim=0:{slot:.3f}[s{i}]")
        inputs += ["-i", vf]
        fc.append(chain)
    fc.append("".join(f"[s{i}]" for i in range(len(slots))) +
              f"concat=n={len(slots)}:v=0:a=1[voice]")
    out = os.path.join(workdir, "voice.wav")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[voice]",
         "-codec:a", "pcm_s16le", out], "voice track build")
    return out


def build_whoosh_track(bundle, cuts, total, workdir):
    src = None
    for cand in (os.path.join(bundle, "whoosh.wav"),
                 os.path.join(bundle, "assets", "whoosh.wav")):
        if os.path.exists(cand):
            src = cand
            break
    if src is None:
        src = os.path.join(workdir, "whoosh-synth.wav")
        make_whoosh(src)
    inputs, fc = [], []
    for j, t0 in enumerate(cuts):
        inputs += ["-i", src]
        ms = int(max(0.0, t0 - 0.15) * 1000)
        fc.append(f"[{j}:a]aresample=44100,adelay={ms}:all=1,apad,"
                  f"atrim=0:{total:.3f}[w{j}]")
    fc.append("".join(f"[w{j}]" for j in range(len(cuts))) +
              f"amix=inputs={len(cuts)}:normalize=0,volume={WHOOSH_VOL}[wh]")
    out = os.path.join(workdir, "whoosh.wav")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[wh]",
         "-codec:a", "pcm_s16le", out], "whoosh track build")
    return out


def write_ass(captions, width, height, path):
    def ts(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"
    fs = max(18, int(height * 0.030))
    mv = max(10, int(height * 0.025))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}",
        f"PlayResY: {height}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,DejaVu Sans,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,3,0,2,20,20,{mv},1", "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text",
    ]
    for c in captions:
        text = str(c["text"]).replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{ts(float(c['start']))},{ts(float(c['end']))},"
                     f"Cap,,0,0,0,,{text}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


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
    # Dead-air scan: no silence of 1.0s or longer may exist anywhere.
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


def main():
    bundle = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    meta_path = os.path.join(bundle, "meta.json")
    if not os.path.exists(meta_path):
        sys.stderr.write(f"ERROR: meta.json not found in {bundle}\n")
        raise SystemExit(1)
    meta = json.load(open(meta_path))
    W, H = int(meta["width"]), int(meta["height"])
    fps = float(meta.get("fps", 30))
    clips = sorted(glob.glob(os.path.join(bundle, "clips", "*.mp4")))
    if not clips:
        sys.stderr.write("ERROR: no clips in bundle/clips/\n")
        raise SystemExit(1)
    clip_durs = [dur(c) for c in clips]
    orig_starts, acc = [], 0.0
    for s in clip_durs:
        orig_starts.append(acc); acc += s
    # Fit the timeline to the voice: slots shrink/stretch so narration
    # covers every second (see fit_slots). Captions are remapped below.
    slots, tempos, stretches = fit_slots(bundle, clip_durs)
    starts, acc = [], 0.0
    for s in slots:
        starts.append(acc); acc += s
    total = acc
    print(f"bundle: {len(clips)} clips, fitted total {total:.1f}s "
          f"(raw {sum(clip_durs):.1f}s), target {W}x{H}@{fps}", flush=True)

    workdir = os.path.join(bundle, "_work")
    os.makedirs(workdir, exist_ok=True)
    voice_wav = build_voice_track(bundle, slots, tempos, workdir)
    whoosh_wav = None
    if len(clips) > 1:
        whoosh_wav = build_whoosh_track(bundle, starts[1:], total, workdir)

    # ---- final render: one pass, hard-cut concat ----
    n = len(clips)
    inputs, g = [], []
    for c in clips:
        inputs += ["-i", c]
    inputs += ["-i", voice_wav]
    if whoosh_wav:
        inputs += ["-i", whoosh_wav]
    for i in range(n):
        slot, stretch = slots[i], stretches[i]
        pre = ""
        if stretch > 1.0:
            if stretch <= MAX_STRETCH:
                pre = f"setpts={stretch:.4f}*(PTS-STARTPTS),"
            else:
                extra = slot - clip_durs[i] * MAX_STRETCH
                pre = (f"setpts={MAX_STRETCH:.2f}*(PTS-STARTPTS),"
                       f"tpad=stop_mode=clone:stop_duration={extra:.3f},")
        pre += f"trim=0:{slot:.3f},setpts=PTS-STARTPTS,"
        g.append(f"[{i}:v]{pre}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1,"
                 f"format=yuv420p[v{i}]")
    g.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")
    cur = "vcat"
    font = find_font()
    captions = meta.get("captions")
    if captions:
        # Remap caption timings from the raw clip timeline onto the fitted
        # (voice-defined) timeline, proportionally inside each clip's block.
        remapped = []
        for cap in captions:
            cs, ce = float(cap["start"]), float(cap["end"])
            k = len(clips) - 1
            for j in range(len(clips)):
                if orig_starts[j] <= cs < orig_starts[j] + clip_durs[j]:
                    k = j
                    break
            scale = slots[k] / clip_durs[k] if clip_durs[k] else 1.0
            remapped.append({**cap,
                             "start": round(starts[k] + (cs - orig_starts[k]) * scale, 3),
                             "end": round(starts[k] + (ce - orig_starts[k]) * scale, 3)})
        captions = remapped
    if captions:
        ass = os.path.join(workdir, "captions.ass")
        write_ass(captions, W, H, ass)
        esc = ass.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        g.append(f"[{cur}]subtitles='{esc}'[vcap]")
        cur = "vcap"
    hook = (meta.get("hook_text") or "").strip()
    if hook and font:
        # Wrap into <=2 lines and size the font so the longest line fits
        # the frame width (a single unwrapped line overflows narrow frames).
        words = hook.split()
        lines, line = [], ""
        for wd in words:
            if line and len(line) + 1 + len(wd) > 26:
                lines.append(line); line = wd
            else:
                line = (line + " " + wd).strip()
        if line:
            lines.append(line)
        lines = lines[:2]
        longest = max(len(x) for x in lines)
        fs = int(min(H * 0.034, (W * 0.94) / (0.58 * max(1, longest))))
        fs = max(16, fs)
        y = 0.05
        for li, ltext in enumerate(lines):
            safe = (ltext.replace("\\", "\\\\").replace(":", "\\:")
                    .replace("'", "\\'"))
            g.append(f"[{cur}]drawtext=fontfile={font}:text='{safe}'"
                     f":fontsize={fs}:fontcolor=white:borderw=3"
                     f":bordercolor=black:x=(w-text_w)/2"
                     f":y=h*{y:.3f}:enable='lt(t,3)'[vhook{li}]")
            cur = f"vhook{li}"
            y += (fs * 1.25) / H
    if whoosh_wav:
        g.append(f"[{n}:a]aresample=44100[vo]")
        g.append(f"[{n+1}:a]aresample=44100[wh]")
        g.append("[vo][wh]amix=inputs=2:normalize=0,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        g.append(f"[{n}:a]aresample=44100,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    final = os.path.join(bundle, "final.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(g), "-map", f"[{cur}]", "-map", "[aout]",
         "-c:v", "libx265", "-crf", "19", "-preset", "fast", "-tag:v", "hvc1",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", final],
        "final render")

    d = verify(final, total)
    size = os.path.getsize(final)
    print(f"OK final.mp4  {d['video']:.2f}s video / {d['audio']:.2f}s audio  "
          f"{size/1e6:.1f} MB  (decode clean, streams in sync)", flush=True)


if __name__ == "__main__":
    main()
