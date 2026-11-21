"""Unattended GTA 6 Automated Live-Stream Clipper & Cross-Poster.

Designed for GitHub Actions (cron) or any Linux runner:
  1. Monitor whitelisted YouTube channels for active GTA 6 live streams, then recent recordings.
  2. Capture a 3-minute sample via yt-dlp + ffmpeg and slice the loudest 45s peak.
  3. Generate word-level timestamps via Groq's free Whisper API and burn styled
     .ass captions + title hook into a 9:16 (1080x1920) MP4.
  4. Upload to YouTube Shorts (via saved refresh token) and Instagram Reels
     (via temporary Cloudflare R2 public URL, then auto-delete).
"""

import argparse
import hashlib
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
PROCESSED_VIDEOS_FILE = WORK_DIR / "processed_videos.json"

# Only monitor streamers who explicitly allow clipping & re-uploads with credit.
WHITELIST_CHANNELS = [
    {"name": "Unq Gamer", "channel_id": "UCabjixiIIRDY6-dmaQVbPlA", "handle": "@UnqGamer"},
    {"name": "Techno Gamerz", "channel_id": "UCX8pnu3DYUnx8qy8V_c6oHg", "handle": "@TechnoGamerzOfficial"},
    {"name": "IShowSpeed", "channel_id": "UCWsDFcIhY2DBi3GB5uykGXA", "handle": "@IShowSpeed"},
]

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_TOKEN_JSON = os.getenv("YOUTUBE_TOKEN_JSON", "")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")

R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")


class StreamResolutionError(RuntimeError):
    """Raised when yt-dlp cannot resolve a live stream URL."""


def ensure_dirs() -> None:
    for d in (CHUNKS_DIR, CLIPS_DIR, READY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_processed_video_ids() -> list[str]:
    if not PROCESSED_VIDEOS_FILE.exists():
        return []
    try:
        state = json.loads(PROCESSED_VIDEOS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read processed-video state: {exc}") from exc
    ids = state.get("processed_video_ids") if isinstance(state, dict) else None
    if not isinstance(ids, list) or any(not isinstance(video_id, str) for video_id in ids):
        raise RuntimeError(f"Invalid processed-video state in {PROCESSED_VIDEOS_FILE}.")
    return ids


def _mark_video_processed(video_id: str) -> None:
    processed_ids = _load_processed_video_ids()
    if video_id not in processed_ids:
        processed_ids.append(video_id)
    PROCESSED_VIDEOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_VIDEOS_FILE.write_text(
        json.dumps({"processed_video_ids": processed_ids}, indent=2) + "\n",
        encoding="utf-8",
    )


def _cookies_args() -> list[str]:
    """Write optional Netscape cookies.txt from YOUTUBE_COOKIES secret for datacenter IPs."""
    if not YOUTUBE_COOKIES.strip():
        return []
    cookie_file = WORK_DIR / "cookies.txt"
    cookie_file.write_text(YOUTUBE_COOKIES, encoding="utf-8")
    return ["--cookies", str(cookie_file)]


# ---------------------------------------------------------------------------
# Stage 1: Discover Active Live Streams on Whitelisted Channels
# ---------------------------------------------------------------------------
def find_live_streams(channels: list[dict]) -> list[dict]:
    """Return active GTA 6 live stream URLs for whitelisted channels."""
    live = []
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is required to discover YouTube videos.")

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


def _parse_video_duration(duration: str) -> int:
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        duration,
    )
    if not match:
        raise RuntimeError(f"Unexpected YouTube video duration: {duration!r}")
    days, hours, minutes, seconds = match.groups()
    return (
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + int(float(seconds or 0))
    )


def _sample_start(video_id: str, duration: int, sample_duration: int = 180) -> int:
    latest_start = max(0, duration - sample_duration)
    if latest_start == 0:
        return 0
    digest = hashlib.sha256(video_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (latest_start + 1)


def find_recorded_videos(channels: list[dict], processed_ids: set[str]) -> list[dict]:
    """Find recent, unprocessed GTA 6 recordings in the whitelisted channels' uploads."""
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is required to discover recorded videos.")

    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    channel_response = (
        youtube.channels()
        .list(
            part="contentDetails",
            id=",".join(ch["channel_id"] for ch in channels),
            maxResults=len(channels),
        )
        .execute()
    )
    uploads_by_channel = {
        item["id"]: item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        for item in channel_response.get("items", [])
    }

    candidates = []
    for channel in channels:
        uploads_id = uploads_by_channel.get(channel["channel_id"])
        if not uploads_id:
            continue

        page_token = None
        channel_matches = 0
        for _ in range(10):
            request = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_id,
                maxResults=50,
                **({"pageToken": page_token} if page_token else {}),
            )
            response = request.execute()
            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                video_id = snippet.get("resourceId", {}).get("videoId")
                if not video_id or video_id in processed_ids:
                    continue
                searchable_text = f"{snippet.get('title', '')} {snippet.get('description', '')}"
                if not re.search(
                    r"\b(?:GTA\s*(?:6|VI)|Grand Theft Auto VI)\b",
                    searchable_text,
                    re.I,
                ):
                    continue
                candidates.append(
                    {
                        "video_id": video_id,
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": snippet.get("title", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "streamer": channel["name"],
                        "handle": channel["handle"],
                    }
                )
                channel_matches += 1
                if channel_matches >= 5:
                    break

            if channel_matches >= 5:
                break
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    if not candidates:
        return []

    video_response = (
        youtube.videos()
        .list(
            part="snippet,contentDetails",
            id=",".join(candidate["video_id"] for candidate in candidates),
        )
        .execute()
    )
    videos_by_id = {video["id"]: video for video in video_response.get("items", [])}

    recorded = []
    for candidate in candidates:
        video = videos_by_id.get(candidate["video_id"])
        if not video:
            continue
        snippet = video.get("snippet", {})
        if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
            continue
        duration_value = video.get("contentDetails", {}).get("duration")
        if not duration_value:
            continue
        duration = _parse_video_duration(duration_value)
        if duration < 45:
            continue
        candidate["duration_sec"] = duration
        candidate["start_sec"] = _sample_start(candidate["video_id"], duration)
        candidate["source"] = "recorded"
        recorded.append(candidate)

    return sorted(recorded, key=lambda video: video["published_at"], reverse=True)


# ---------------------------------------------------------------------------
# Stage 2: Capture Live Segment & Detect Audio Peak
# ---------------------------------------------------------------------------
def record_video_sample(
    stream_url: str, duration_sec: int = 180, start_sec: int = 0
) -> Path:
    """Resolve a stream URL with yt-dlp and record a sample via ffmpeg."""
    ensure_dirs()
    out_path = CHUNKS_DIR / "live_sample.mp4"
    if out_path.exists():
        out_path.unlink()

    print(f"[Stage 2] Resolving stream URL for {stream_url} ...")
    cookies_args = _cookies_args()
    print(f"[Stage 2] YouTube cookies: {'configured' if cookies_args else 'not configured'}.")
    yt_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--js-runtimes",
        "node",
        "-f",
        "best[height<=1080][ext=mp4]/best[height<=1080]/best",
        "-g",
        *cookies_args,
        stream_url,
    ]
    url_res = subprocess.run(yt_cmd, capture_output=True, text=True, check=False)
    if url_res.returncode != 0 or not url_res.stdout.strip():
        raise StreamResolutionError(
            f"yt-dlp failed to resolve stream URL (exit {url_res.returncode}):\n{url_res.stderr}"
        )

    direct_url = url_res.stdout.strip().splitlines()[0]
    print(f"[Stage 2] Capturing {duration_sec}s sample from {start_sec}s via ffmpeg ...")
    ff_cmd = [
        "ffmpeg",
        "-y",
    ]
    if start_sec > 0:
        ff_cmd.extend(["-ss", str(start_sec)])
    ff_cmd.extend(
        [
            "-i",
            direct_url,
            "-t",
            str(duration_sec),
            "-c",
            "copy",
            str(out_path),
        ]
    )
    ff_res = subprocess.run(ff_cmd, capture_output=True, text=True, check=False)
    if ff_res.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg capture failed (exit {ff_res.returncode}):\n{ff_res.stderr}")

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
            "description": f"GTA 6 highlight from {streamer_handle}.\n\n#GTA6 #GrandTheftAutoVI #Shorts",
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


def _capture_first_available(
    candidates: list[dict],
) -> tuple[Path | None, dict | None, list[str]]:
    errors = []
    for candidate in candidates:
        start_sec = candidate.get("start_sec", 0)
        try:
            sample = record_video_sample(
                candidate["url"], duration_sec=180, start_sec=start_sec
            )
            return sample, candidate, errors
        except StreamResolutionError as exc:
            errors.append(f"{candidate['video_id']}: {exc}")
            print(
                f"[Stage 2] Could not resolve {candidate['video_id']}; "
                "trying the next video."
            )
    return None, None, errors


def run_auto() -> None:
    """Full unattended end-to-end run for GitHub Actions cron."""
    processed_ids = set(_load_processed_video_ids())
    live_candidates = [
        video
        for video in find_live_streams(WHITELIST_CHANNELS)
        if video["video_id"] not in processed_ids
    ]
    if not live_candidates:
        print("[Stage 1] No new whitelisted live streams; checking recent recorded videos.")

    sample, stream, resolution_errors = _capture_first_available(live_candidates)
    recorded_candidates = []
    if sample is None:
        recorded_candidates = find_recorded_videos(WHITELIST_CHANNELS, processed_ids)
        if recorded_candidates:
            print(f"[Stage 1] Found {len(recorded_candidates)} unprocessed GTA 6 recordings.")
            sample, stream, recorded_errors = _capture_first_available(recorded_candidates)
            resolution_errors.extend(recorded_errors)

    if sample is None or stream is None:
        if not live_candidates and not recorded_candidates:
            print("[Stage 1] No unprocessed GTA 6 videos are available. Exiting cleanly.")
            return
        detail = "\n".join(resolution_errors)
        if any("not a bot" in error.lower() for error in resolution_errors):
            detail += (
                "\nYouTube rejected the runner. Check that YOUTUBE_COOKIES is a fresh, "
                "valid Netscape-format cookies.txt value; GitHub-hosted runner IPs may "
                "still be blocked, in which case use a runner with YouTube access."
            )
        raise RuntimeError(f"Could not resolve any candidate video:\n{detail}")

    print(f"[Stage 2] Selected {stream.get('source', 'live')} video: {stream['title']}")
    peak_start = detect_loudest_window(sample)
    raw = extract_clip(sample, peak_start, duration=45)
    final_mp4 = render_vertical_short(raw, hook_text=f"{stream['streamer'].upper()} GTA 6")

    yt_id = publish_youtube_short(final_mp4, stream["title"], stream["handle"])
    ig_id = publish_instagram_reel(final_mp4, f"{stream['title']} (via {stream['handle']}) #GTA6 #Reels")
    if not yt_id and not ig_id:
        raise RuntimeError(
            "No upload destination is configured. Set YOUTUBE_TOKEN_JSON or the Instagram/R2 secrets."
        )
    _mark_video_processed(stream["video_id"])
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
