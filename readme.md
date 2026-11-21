# GTA 6 Automated Live-Stream Clipper — Project Handoff

This document summarizes everything set up so far so you can continue debugging and running the pipeline seamlessly.

## 1. Goal & Architecture

Build a **$0/month, unattended, cloud-hosted automation** that monitors whitelisted YouTube GTA 6 streamers, captures live highlight clips, burns word-level captions + 9:16 vertical framing, and cross-posts to **YouTube Shorts** and **Instagram Reels**.

- **Compute & Scheduler:** Public GitHub repository running **GitHub Actions** (`ubuntu-latest`) hourly (`0 * * * *`) plus manual `workflow_dispatch`. Hourly polling keeps three per-channel YouTube searches within the default daily search quota.
- **Stream Discovery:** YouTube Data API v3 searches `WHITELIST_CHANNELS` for live GTA 6 streams. If none are available, it scans each channel's uploads playlist for unprocessed GTA 6 recordings.
- **Capture & Highlight Slicing:** `yt-dlp` resolves a stream URL and `ffmpeg` captures a 180s sample. For recorded videos, a stable video-specific offset chooses a source segment; the pipeline scans audio RMS levels via `astats` and slices a 45s clip around the loudest peak.
- **Vertical Rendering & Captions:** `ffmpeg` reframes 16:9 to 9:16 (`1080x1920`) with a blurred background, adds a 3.5s top hook banner, and burns 3-word uppercase `.ass` captions generated via **Groq's Whisper API** (`whisper-large-v3-turbo` with `timestamp_granularities[]=word`).
- **Publishing & Cleanup:**
  - **YouTube Shorts:** `videos.insert` via `google-api-python-client` using a saved OAuth `refresh_token` (`YOUTUBE_TOKEN_JSON`). Uploads default to `privacyStatus="private"` until Google OAuth compliance audit passes.
  - **Instagram Reels:** Uploads the rendered MP4 temporarily to **Cloudflare R2** (`boto3` S3-compatible API) to get a public HTTPS URL, publishes via **Instagram Graph API v21.0** (`/{IG_USER_ID}/media` → poll `FINISHED` → `media_publish`), and deletes the R2 object afterward.

## 2. Repository Files

- `gta6_clipper.py` — Main Python script implementing all 4 stages (`--auto` flag for GitHub Actions).
- `.github/workflows/clipper.yml` — GitHub Actions workflow installing Node.js 22, `ffmpeg`, and Python deps (including yt-dlp's EJS solver scripts), running regression tests, restoring/saving processed-video history, and running `python gta6_clipper.py --auto`.
- `tests/test_gta6_clipper.py` — Focused regression tests for video selection and capture behavior.

## 3. Configured Whitelist Channels (`WHITELIST_CHANNELS`)

| Streamer | Channel ID | Handle |
| --- | --- | --- |
| Unq Gamer | `UCabjixiIIRDY6-dmaQVbPlA` | `@UnqGamer` |
| Techno Gamerz | `UCX8pnu3DYUnx8qy8V_c6oHg` | `@TechnoGamerzOfficial` |
| IShowSpeed | `UCWsDFcIhY2DBi3GB5uykGXA` | `@IShowSpeed` |

## 4. GitHub Actions Secrets Configured

All credentials are stored in **Settings → Secrets and variables → Actions**:
- `YOUTUBE_API_KEY` (Google Cloud YouTube Data API v3 key)
- `YOUTUBE_TOKEN_JSON` (One-time OAuth Desktop App `token.json` with `refresh_token` for `youtube.upload` scope)
- `YOUTUBE_COOKIES` (Netscape-format `cookies.txt` contents for `yt-dlp`; needed when YouTube challenges the runner)
- `GROQ_API_KEY` (Groq Whisper API key)
- `IG_USER_ID` & `IG_ACCESS_TOKEN` (Linked Instagram Business/Creator account ID + 60-day Meta Graph API token)
- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL` (Cloudflare R2 bucket & public `r2.dev` URL)

The cookie secret must contain the complete, current Netscape cookie file, including its header and actual cookie rows. Do not store a base64-encoded file or literal `\n` sequences. Treat this secret like a password; never commit it or print it in workflow logs. YouTube rotates cookies, and using account cookies with yt-dlp carries a risk of account restrictions. Cookies may still not overcome GitHub-hosted runner IP blocks.

## 5. Issues Encountered & Current Status

1. **Resolved — `yt-dlp` stdout pipe failure:** Initial run piped `bestvideo+bestaudio` to `stdout`, causing `live_sample.mp4` not to be created (`ffmpeg` exit 254). Fixed by switching to `yt-dlp -g` with a combined format (`best[height<=1080][ext=mp4]/best[height<=1080]/best`) and passing the resolved URL to `ffmpeg`.
2. **Recorded-video fallback:** The latest manual run found no live streams and exited without producing a clip. The pipeline now checks the channels' uploads playlists for unprocessed GTA 6 recordings, samples a stable segment, and tracks successful source video IDs in a GitHub Actions cache to avoid repeat uploads. It processes one source per run.
3. **YouTube bot check / runner access:** A previous run failed resolving a live video with `Sign in to confirm you're not a bot`. The script uses yt-dlp's default player clients, reports whether the cookie secret is configured (never its value), and tries other candidates before failing. If YouTube rejects recorded videos too, verify the cookie secret or use a runner with network access YouTube permits.
4. **YouTube JavaScript challenge:** A later run reported `n challenge solving failed` and only image formats. The workflow now installs Node.js 22 and `yt-dlp[default]` (which includes the matching EJS solver package), and yt-dlp explicitly uses Node for challenge solving.
5. **Instagram configuration:** The supplied run log showed `IG_USER_ID` was empty, so Instagram publishing is skipped. Set this Actions secret if Reels publishing is required; YouTube publishing can still run independently.

## 6. Next Steps to Continue

1. Trigger `workflow_dispatch` and inspect which live or recorded video was selected. Successful source IDs are cached so they are not published repeatedly.
2. If YouTube still reports a challenge-solving error, verify the Actions log shows Node.js 22 and the yt-dlp default extras installed. If it reports a bot check instead, verify the `YOUTUBE_COOKIES` secret or configure a runner with network access YouTube permits.
3. Set `IG_USER_ID` if Instagram publishing is required, then verify Groq transcription, 9:16 rendering, and each upload destination.
4. Add transformative commentary/effects to comply with YouTube's Shorts Original Content & Reused Content policies.
