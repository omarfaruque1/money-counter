#!/usr/bin/env python3
"""Download a video, extract frames, and transcribe its audio.

Usage:
    process_video.py <url-or-path> [--out DIR] [--frames N] [--model SIZE]

Output layout (in --out, default ./video-work/<slug>):
    video.<ext>        downloaded video
    frames/frame_XXX_<mm-ss>.jpg   evenly spaced frames, 1280px wide max
    transcript.txt     "[mm:ss] text" lines
    info.json          title, uploader, duration, url, description, frame list

Transcript sources, in order: uploaded/auto captions (via yt-dlp) ->
local faster-whisper -> OpenAI Whisper API (needs OPENAI_API_KEY).
Missing Python tools (yt-dlp, faster-whisper) are pip-installed on demand.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys


def log(msg):
    print(f"[video-to-skill] {msg}", file=sys.stderr, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd[:4])} ...\n{r.stderr.strip()[-1500:]}")
    return r


def pip_install(pkg):
    log(f"installing {pkg} ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", pkg], check=True)


def ensure_ytdlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        pip_install("yt-dlp")
    return [sys.executable, "-m", "yt_dlp"]


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "video"


def ts(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- download
def download(src, out_dir):
    if os.path.exists(src):
        dst = os.path.join(out_dir, "video" + os.path.splitext(src)[1])
        shutil.copy(src, dst)
        return dst, {"title": os.path.basename(src), "url": os.path.abspath(src)}

    ytdlp = ensure_ytdlp()
    info = json.loads(run(ytdlp + ["-J", "--no-playlist", src]).stdout)
    run(ytdlp + [
        "--no-playlist",
        "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
        "--sub-format", "vtt", "--convert-subs", "vtt",
        "-o", os.path.join(out_dir, "video.%(ext)s"),
        src,
    ])
    vids = [p for p in glob.glob(os.path.join(out_dir, "video.*"))
            if not p.endswith((".vtt", ".json", ".part"))]
    if not vids:
        sys.exit("download failed: no video file produced")
    meta = {k: info.get(k) for k in
            ("title", "uploader", "duration", "webpage_url", "description", "upload_date")}
    meta["url"] = info.get("webpage_url") or src
    return vids[0], meta


def duration_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path]).stdout.strip()
    return float(out or 0)


# ------------------------------------------------------------------ frames
def extract_frames(video, out_dir, n, duration):
    fdir = os.path.join(out_dir, "frames")
    os.makedirs(fdir, exist_ok=True)
    frames = []
    if duration <= 0:
        n = 1
    for i in range(n):
        t = duration * (i + 0.5) / n if duration > 0 else 0
        name = f"frame_{i:03d}_{ts(t).replace(':', '-')}.jpg"
        path = os.path.join(fdir, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", video,
                        "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                        "-q:v", "3", path], check=False)
        if os.path.exists(path):
            frames.append({"file": f"frames/{name}", "time": ts(t)})
    return frames


# -------------------------------------------------------------- transcript
def parse_vtt(path):
    lines, last = [], None
    cur = None
    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            m = re.match(r"(\d+):(\d+):(\d+)[.,]\d+\s+-->", raw) or \
                re.match(r"()(\d+):(\d+)[.,]\d+\s+-->", raw)
            if m:
                h = int(m.group(1) or 0)
                cur = h * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                continue
            if not raw or cur is None or raw.isdigit() or raw.startswith(("WEBVTT", "Kind:", "Language:")):
                continue
            text = re.sub(r"<[^>]+>", "", raw).strip()
            # auto-captions repeat each line across cues; drop exact repeats
            if text and text != last:
                lines.append(f"[{ts(cur)}] {text}")
                last = text
    return lines


def transcript_from_captions(out_dir):
    vtts = sorted(glob.glob(os.path.join(out_dir, "*.vtt")),
                  key=lambda p: (".auto" in p or "-orig" not in p, p))
    # prefer manual subs (yt-dlp names auto subs the same, so just take first non-empty)
    for v in vtts:
        lines = parse_vtt(v)
        if lines:
            return lines, f"captions ({os.path.basename(v)})"
    return None, None


def extract_audio(video, out_dir):
    audio = os.path.join(out_dir, "audio.mp3")
    run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", "-ac", "1",
         "-ar", "16000", "-b:a", "48k", audio])
    return audio


def transcript_local_whisper(audio, model):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        try:
            pip_install("faster-whisper")
            from faster_whisper import WhisperModel
        except Exception as e:  # noqa: BLE001
            log(f"faster-whisper unavailable: {e}")
            return None
    log(f"transcribing locally with faster-whisper ({model}) ...")
    wm = WhisperModel(model, device="auto", compute_type="int8")
    segs, _ = wm.transcribe(audio, vad_filter=True)
    return [f"[{ts(s.start)}] {s.text.strip()}" for s in segs if s.text.strip()]


def transcript_openai(audio):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    log("transcribing with OpenAI Whisper API ...")
    out = run(["curl", "-sS", "https://api.openai.com/v1/audio/transcriptions",
               "-H", f"Authorization: Bearer {key}",
               "-F", f"file=@{audio}", "-F", "model=whisper-1",
               "-F", "response_format=verbose_json"]).stdout
    data = json.loads(out)
    if "segments" not in data:
        log(f"OpenAI error: {out[:300]}")
        return None
    return [f"[{ts(s['start'])}] {s['text'].strip()}" for s in data["segments"]]


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="video URL (anything yt-dlp supports) or local file")
    ap.add_argument("--out", help="output directory (default ./video-work/<slug>)")
    ap.add_argument("--frames", type=int, default=0,
                    help="number of frames (default: auto, ~1 per 20s, 8..40)")
    ap.add_argument("--model", default="small", help="faster-whisper model size")
    ap.add_argument("--no-whisper", action="store_true",
                    help="captions only; skip speech-to-text fallback")
    a = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required (apt install ffmpeg / brew install ffmpeg)")

    tmp = a.out or os.path.join("video-work", "_incoming")
    os.makedirs(tmp, exist_ok=True)
    log(f"downloading {a.source} ...")
    video, meta = download(a.source, tmp)

    out_dir = a.out
    if not out_dir:
        out_dir = os.path.join("video-work", slugify(meta.get("title") or "video"))
        if os.path.abspath(out_dir) != os.path.abspath(tmp):
            if os.path.exists(out_dir):
                shutil.rmtree(out_dir)
            shutil.move(tmp, out_dir)
            video = os.path.join(out_dir, os.path.basename(video))

    duration = duration_of(video)
    meta["duration"] = meta.get("duration") or round(duration)
    n = a.frames or max(8, min(40, int(duration // 20) or 8))
    log(f"extracting {n} frames from {ts(duration)} of video ...")
    meta["frames"] = extract_frames(video, out_dir, n, duration)

    lines, source = transcript_from_captions(out_dir)
    if not lines and not a.no_whisper:
        audio = extract_audio(video, out_dir)
        lines = transcript_local_whisper(audio, a.model)
        source = f"faster-whisper ({a.model})" if lines else None
        if not lines:
            lines = transcript_openai(audio)
            source = "openai whisper-1" if lines else None
    meta["transcript_source"] = source or "none"
    with open(os.path.join(out_dir, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines or ["(no transcript available)"]) + "\n")

    with open(os.path.join(out_dir, "info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(json.dumps({
        "out_dir": os.path.abspath(out_dir),
        "title": meta.get("title"),
        "duration": ts(duration),
        "frames": len(meta["frames"]),
        "transcript_lines": len(lines or []),
        "transcript_source": meta["transcript_source"],
    }, indent=2))


if __name__ == "__main__":
    main()
