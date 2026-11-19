# Cloud render engine (GitHub Actions) — local half

Offloads the one slow step — the final H.265 render — from this VM
(1.5 shared CPUs, ~15 min per 70s video) to GitHub Actions runners.
GitHub runs several jobs in parallel, so a bulk batch renders in a
fraction of the wall-clock time. Everything else stays here.

## Who does what

**This VM (unchanged):**
- Drive I/O — pulls source videos from *Video Inbox*, delivers finished
  videos to *Video Finished*
- Gemini analysis / segmentation and script writing
- Scene cutting into the clip pools
- Casper TTS voice blocks (`/opt/hatch/bin/tts`)
- Packing bundles (`make_bundle.py`), dispatching render jobs,
  downloading results, final verification, Drive delivery

**GitHub Actions runner (only the heavy render):**
- Downloads one `bundle.zip`, runs `render_bundle.py`, uploads
  `final.mp4` as a workflow artifact named `final-<job_id>`
- `render_bundle.py` self-verifies (stream durations within 1s + full
  decode) and fails the job if the render is bad

## Bundle format

`make_bundle.py plan.json` produces `bundles/<title>/` + `<title>.zip`:

```
clips/001.mp4 ...   clips in final order (audio stripped)
voice/001.mp3 ...   one TTS block per clip (missing file = silent slot)
meta.json           {width, height, fps, title, hook_text, captions|null}
whoosh.wav          optional; a whoosh is synthesized if absent
```

`render_bundle.py` conforms every clip to meta W/H/fps, fits each voice
block into its slot (atempo up to 1.20, else trim; silence-pad if short),
adds a whoosh at every hard cut, loudnorms to I=-14 / TP=-1.5, encodes
libx265 CRF 19 preset fast, writes `final.mp4`, then verifies it.

## Files

- `render_bundle.py` — runs on the runner (stdlib + ffmpeg only)
- `make_bundle.py` — runs here; plan JSON → bundle dir + zip
- `workflow-render.yml` — goes to `.github/workflows/render.yml` in the
  engine repo; `render_bundle.py` goes to the repo root

## The one remaining step

**The user adds a GitHub token** (repo + workflow scope) through the
secure credential flow. Nothing here logs into GitHub yet.

## Dispatch, once the token exists

1. VM builds a bundle and uploads `bundle.zip` to a URL the runner can
   fetch (e.g. a signed Drive link or a release asset).
2. VM calls the GitHub API:
   `POST /repos/<owner>/<repo>/actions/workflows/render.yml/dispatches`
   with `{"ref": "main", "inputs": {"job_id": "<id>", "bundle_url": "<url>"}}`
3. VM polls the workflow run; when it completes, downloads the
   `final-<job_id>` artifact, re-verifies it locally, and delivers it
   to the *Video Finished* Drive folder.
4. Several jobs can be dispatched back-to-back — they render in
   parallel on separate runners.

## Local test (done 2026-11-19)

A 3-clip / 27.0s test bundle was built from the grandpa-hacks sample
(`samples/tiktok-ZN8aqAG4W`) with `make_bundle.py` and rendered locally
with `render_bundle.py` — see `test/` for the plan and output.
Result: `final.mp4` 27.00s video / 27.00s audio, HEVC 720×1280,
6.9 MB, decode clean; local render took ~155–172s per run while
sharing this VM's 1.5 CPUs with the live production run (a dedicated
GitHub runner will be much faster). The test also exercised the
atempo-fit and 1.20-cap voice paths, burned captions, the synthesized
whoosh, and the hook-text overlay.
