"""Unattended GTA 6 Automated Live-Stream Clipper & Cross-Poster.

Designed for GitHub Actions (cron) or any Linux runner:
  1. Monitor whitelisted YouTube channels for active GTA 6 live streams.
  2. Capture a 3-minute live segment via yt-dlp + ffmpeg and slice the loudest 45s peak.
  3. Generate word-level timestamps via Groq's free Whisper API (or local faster-whisper)
     and burn styled .ass captions + title hook into a 9:16 (1080x1920) MP4.
  4. Upload to YouTube Shorts (via saved refresh token) and Instagram Reels
     (via temporary Cloudflare R2 public URL, then auto-delete).
"""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

import boto3
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WORK_DIR = Path(os.getenv("CLIPPER_WORK_DIR", "./clipper_work"))
CHUNKS_DIR = WORK_DIR / "chunks"
CLIPS_DIR = WORK_DIR / "clips"
READY_DIR = WORK_DIR / "ready"

# Only monitor streamers who explicitly allow clipping & re-uploads with credit.
WHITELIST_CHANNELS = [
    {"name": "ExampleStreamer", "channel_id": "UC_REPLACE_WITH_CHANNEL_ID", "handle": "@ExampleStreamer"},
]

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_TOKEN_JSON = os.getenv("YOUTUBE_TOKEN_JSON", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")

R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")


def ensure_dirs() -> None:
    for d in (CHUNKS_DIR, CLIPS_DIR, READY_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Stage 1: Discover Active Live Streams on Whitelisted Channels
# ---------------------------------------------------------------------------
def find_live_streams(channels: list[dict]) -> list[dict]:
    """Return active GTA 6 live stream URLs for whitelisted channels."""
    live = []
    if not YOUTUBE_API_KEY:
        print("[Stage 1] YOUTUBE_API_KEY not set; skipping live check.")
        return live

    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    for ch in channels:
        if ch["channel_id"].startswith("UC_REPLACE"):
            continue
        resp = (
            youtube.search()
            .list(
                part="snippet",
                channelId=ch["channel_id"],
                eventType="live",
                type="video",
                q="GTA 6",
                maxResults=1,
            )
            .execute()
        )
        for item in resp.get("items", []):
            vid = item["id"]["videoId"]
            live.append(
                {
                    "video_id": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": item["snippet"]["title"],
                    "streamer": ch["name"],
                    "handle": ch["handle"],
                }
            )
    return live


# ---------------------------------------------------------------------------
# Stage 2: Capture Live Segment & Detect Audio Peak
# ---------------------------------------------------------------------------
def record_live_sample(stream_url: str, duration_sec: int = 180) -> Path:
    """Capture a lossless live segment from yt-dlp piped into ffmpeg."""
    ensure_dirs()
    out_path = CHUNKS_DIR / "live_sample.mp4"
    yt_cmd = ["yt-dlp", "-f", "bestvideo[height<=1080]+bestaudio/best", "-o", "-", stream_url]
    ff_cmd = ["ffmpeg", "-y", "-i", "pipe:0", "-t", str(duration_sec), "-c", "copy", str(out_path)]
    print(f"[Stage 2] Capturing {duration_sec}s live sample from {stream_url} ...")
    yt_proc = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff_proc = subprocess.Popen(ff_cmd, stdin=yt_proc.stdout, stderr=subprocess.DEVNULL)
    try:
        ff_proc.wait(timeout=duration_sec + 30)
    finally:
        yt_proc.terminate()
    return out_path


def detect_loudest_window(chunk_path: Path) -> float:
    """Scan audio RMS levels and return the start timestamp (seconds) before the loudest peak."""
    cmd = [
        "ffmpeg",
        "-i",
        str(chunk_path),
        "-af",
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    peak_time, peak_db, current_time = 0.0, -999.0, 0.0
    for line in res.stdout.splitlines():
        t_match = re.search(r"pts_time:([0-9.]+)", line)
        if t_match:
            current_time = float(t_match.group(1))
        db_match = re.search(r"RMS_level=([-0-9.]+)", line)
        if db_match:
            db = float(db_match.group(1))
            if db > peak_db:
                peak_db = db
                peak_time = current_time
    return max(0.0, peak_time - 20.0)


def extract_clip(chunk_path: Path, start_sec: float, duration: int = 45) -> Path:
    """Slice a raw highlight clip from the captured segment."""
    ensure_dirs()
    out_path = CLIPS_DIR / f"raw_{chunk_path.stem}_{int(start_sec)}.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_sec:.2f}",
        "-i",
        str(chunk_path),
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


# ---------------------------------------------------------------------------
# Stage 3: Groq Whisper Word-Level Captions + 9:16 Render
# ---------------------------------------------------------------------------
def _ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_ass_via_groq(video_path: Path, ass_path: Path) -> Path:
    """Extract audio and transcribe word-level timestamps using Groq's free Whisper API."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required for serverless transcription.")

    audio_path = CLIPS_DIR / f"{video_path.stem}.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with audio_path.open("rb") as f:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (audio_path.name, f, "audio/mpeg")},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            timeout=60,
        )
    resp.raise_for_status()
    words = resp.json().get("words", [])

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,74,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,"
        "100,100,0,0,1,5,2,2,60,60,340,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    for i in range(0, len(words), 3):
        group = words[i : i + 3]
        if not group:
            continue
        start = _ass_time(float(group[0]["start"]))
        end = _ass_time(float(group[-1]["end"]))
        text = " ".join(w["word"].strip().upper() for w in group)
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_path


def render_vertical_short(raw_clip: Path, hook_text: str) -> Path:
    """Reframe 16:9 to 9:16 (blurred backdrop + center gameplay), burn captions & hook."""
    ensure_dirs()
    ass_path = CLIPS_DIR / f"{raw_clip.stem}.ass"
    generate_ass_via_groq(raw_clip, ass_path)

    out_path = READY_DIR / f"{raw_clip.stem}_short.mp4"
    escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")
    safe_hook = hook_text.replace("'", "\\'").replace(":", "\\:")

    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20:5[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"ass='{escaped_ass}',"
        f"drawtext=text='{safe_hook}':fontcolor=white:fontsize=56:box=1:boxcolor=black@0.75:"
        "boxborderw=18:x=(w-text_w)/2:y=220:enable='between(t,0,3.5)'[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(raw_clip),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        "4500k",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


# ---------------------------------------------------------------------------
# Stage 4: Unattended Upload to YouTube Shorts & Instagram Reels
# ---------------------------------------------------------------------------
def publish_youtube_short(video_path: Path, title: str, streamer_handle: str) -> str:
    """Upload vertical video unattended using a saved OAuth refresh token JSON."""
    if not YOUTUBE_TOKEN_JSON:
        print("[Stage 4] YOUTUBE_TOKEN_JSON not set; skipping YouTube upload.")
        return ""

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_info(json.loads(YOUTUBE_TOKEN_JSON), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": f"{title[:80]} #Shorts #GTA6",
            "description": f"GTA 6 live highlight from {streamer_handle}.\n\n#GTA6 #GrandTheftAutoVI #Shorts",
            "tags": ["GTA 6", "Grand Theft Auto VI", "Shorts", "Gaming"],
            "categoryId": "20",
        },
        "status": {
            # Switch to 'public' after your Google Cloud OAuth app passes YouTube's audit.
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    resp = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    return resp["id"]


def publish_instagram_reel(video_path: Path, caption: str) -> str:
    """Upload MP4 to Cloudflare R2 temporarily, publish Reel via Graph API, then delete from R2."""
    if not (IG_USER_ID and IG_ACCESS_TOKEN and R2_ENDPOINT_URL and R2_BUCKET_NAME and R2_PUBLIC_BASE_URL):
        print("[Stage 4] Instagram or R2 credentials missing; skipping Instagram upload.")
        return ""

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    )
    key = f"reels/{int(time.time())}_{video_path.name}"
    s3.upload_file(str(video_path), R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "video/mp4"})
    public_url = f"{R2_PUBLIC_BASE_URL}/{key}"

    try:
        create_resp = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
            data={"media_type": "REELS", "video_url": public_url, "caption": caption, "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        ).json()
        container_id = create_resp["id"]

        for _ in range(20):
            status = requests.get(
                f"https://graph.facebook.com/v21.0/{container_id}",
                params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
                timeout=15,
            ).json()
            if status.get("status_code") == "FINISHED":
                break
            if status.get("status_code") == "ERROR":
                raise RuntimeError(f"Instagram container error: {json.dumps(status)}")
            time.sleep(5)

        pub_resp = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        ).json()
        return pub_resp["id"]
    finally:
        s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)


def run_auto() -> None:
    """Full unattended end-to-end run for GitHub Actions cron."""
    streams = find_live_streams(WHITELIST_CHANNELS)
    if not streams:
        print("No whitelisted GTA 6 streams are live right now. Exiting cleanly.")
        return

    stream = streams[0]
    sample = record_live_sample(stream["url"], duration_sec=180)
    peak_start = detect_loudest_window(sample)
    raw = extract_clip(sample, peak_start, duration=45)
    final_mp4 = render_vertical_short(raw, hook_text=f"{stream['streamer'].upper()} GTA 6")

    yt_id = publish_youtube_short(final_mp4, stream["title"], stream["handle"])
    ig_id = publish_instagram_reel(final_mp4, f"{stream['title']} (via {stream['handle']}) #GTA6 #Reels")
    print(f"Published! YouTube ID: {yt_id or 'skipped'} | Instagram ID: {ig_id or 'skipped'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unattended GTA 6 Live Clipper")
    parser.add_argument("--auto", action="store_true", help="Run full unattended pipeline")
    parser.add_argument("--clip", type=Path, help="Render a local sample MP4")
    parser.add_argument("--hook", default="INSANE GTA 6 MOMENT", help="3.5s top hook text")
    args = parser.parse_args()

    ensure_dirs()
    if args.auto:
        run_auto()
    elif args.clip:
        print(f"Rendered: {render_vertical_short(args.clip, args.hook)}")
    else:
        print(f"Active whitelisted streams: {find_live_streams(WHITELIST_CHANNELS)}")


if __name__ == "__main__":
    main()