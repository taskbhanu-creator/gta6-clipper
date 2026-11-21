# GTA 6 Automated Live-Stream Clipper — Project Handoff

This document summarizes everything set up so far so you can continue debugging and running the pipeline seamlessly.

## 1. Goal & Architecture

Build a **$0/month, unattended, cloud-hosted automation** that monitors whitelisted YouTube GTA 6 streamers, captures live highlight clips, burns word-level captions + 9:16 vertical framing, and cross-posts to **YouTube Shorts** and **Instagram Reels**.

- **Compute & Scheduler:** Public GitHub repository running **GitHub Actions** (`ubuntu-latest`) on a 30-minute `cron` schedule (`*/30 * * * *`) plus manual `workflow_dispatch`.
- **Stream Discovery:** YouTube Data API v3 (`search.list` with `eventType="live"`, `q="GTA 6"`) polling `WHITELIST_CHANNELS`.
- **Live Capture & Highlight Slicing:** `yt-dlp` (resolving a single combined stream URL via `-g`) + `ffmpeg` (capturing a 180s sample, scanning audio RMS levels via `astats`, and slicing a 45s clip around the loudest peak).
- **Vertical Rendering & Captions:** `ffmpeg` reframes 16:9 to 9:16 (`1080x1920`) with a blurred background, adds a 3.5s top hook banner, and burns 3-word uppercase `.ass` captions generated via **Groq's Whisper API** (`whisper-large-v3-turbo` with `timestamp_granularities[]=word`).
- **Publishing & Cleanup:**
  - **YouTube Shorts:** `videos.insert` via `google-api-python-client` using a saved OAuth `refresh_token` (`YOUTUBE_TOKEN_JSON`). Uploads default to `privacyStatus="private"` until Google OAuth compliance audit passes.
  - **Instagram Reels:** Uploads the rendered MP4 temporarily to **Cloudflare R2** (`boto3` S3-compatible API) to get a public HTTPS URL, publishes via **Instagram Graph API v21.0** (`/{IG_USER_ID}/media` → poll `FINISHED` → `media_publish`), and deletes the R2 object afterward.

## 2. Repository Files

- `gta6_clipper.py` — Main Python script implementing all 4 stages (`--auto` flag for GitHub Actions).
- `.github/workflows/clipper.yml` — GitHub Actions workflow installing `ffmpeg` + Python deps (`yt-dlp`, `google-api-python-client`, `google-auth-oauthlib`, `requests`, `boto3`) and running `python gta6_clipper.py --auto`.

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
2. **Current Blocker — YouTube Bot Check / Datacenter IP:** The latest run installed dependencies successfully, then `yt-dlp` failed resolving video `7ZG7PEGpxno` with `Sign in to confirm you're not a bot`. The script now relies on yt-dlp's default player clients instead of forcing a fixed client list, reports whether the cookie secret is configured (never its value), and tries other live-stream candidates before failing with guidance. If every candidate gets the same rejection, verify the `YOUTUBE_COOKIES` secret and, if it is valid but still rejected, run on a runner whose network YouTube allows. This restriction is imposed by YouTube and cannot be fixed by changing the workflow alone.

## 6. Next Steps to Continue

1. If cookies are needed, export a fresh YouTube cookies file in Netscape format and replace the `YOUTUBE_COOKIES` Actions secret. Do not paste cookies into issues or logs.
2. Trigger `workflow_dispatch` and inspect the **Run unattended GTA 6 clipping pipeline** step. It reports whether cookies were configured. If YouTube still rejects every candidate, configure a self-hosted runner with network access YouTube permits; a GitHub-hosted runner cannot guarantee playback access.
3. Once stream capture succeeds, verify Groq transcription, 9:16 rendering, and the YouTube/Instagram uploads independently.
4. Add a deduplication/state check (e.g., tracking clipped stream IDs or capping at 3–5 uploads/day) and transformative commentary/effects to comply with YouTube's Shorts Original Content & Reused Content policies.
