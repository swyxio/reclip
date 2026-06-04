# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Advanced request strategies for sites that reject generic server requests
- Optional admin agent console for proposing app changes and creating GitHub PRs
- Automatic URL deduplication
- Clean, responsive UI — no frontend framework
- Small Flask backend with a Node-based Codex SDK worker for admin tasks

## Quick Start

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

## Admin Agent Console

ReClip can expose a token-protected `/admin` console that runs Codex against a temporary checkout of this repo, shows the resulting diff, and creates a same-repo GitHub pull request only after you approve it in the UI.

Required environment variables:

```bash
ADMIN_TOKEN=choose-a-long-random-token
GITHUB_TOKEN=github-token-with-contents-write-and-pull-request-access
GITHUB_REPO=swyxio/reclip
```

For Codex authentication, open `/admin` and use **Login With ChatGPT Code**. ReClip talks to `codex app-server` and persists the ChatGPT login in `CODEX_HOME` for later agent jobs. You can still use environment credentials instead if you prefer:

```bash
OPENAI_API_KEY=...
# or CODEX_API_KEY=...
# or CODEX_ACCESS_TOKEN=...
```

Optional environment variables:

```bash
GITHUB_BASE_BRANCH=main
CODEX_HOME=/tmp/reclip-codex-home
CODEX_MODEL=...
CODEX_REASONING_EFFORT=medium
AGENT_TIMEOUT_SECONDS=900
MAX_ACTIVE_AGENT_JOBS=1
```

The console does not expose a raw shell. It clones the configured GitHub repo into a temp directory, runs Codex with `workspace-write` sandboxing and no network access for the agent, allowlists changed paths, then pushes a branch and opens a PR after admin approval. Do not commit tokens to the repo; configure them in your host or Railway service variables.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

If a site returns 403/access-denied for generic server requests, open **Advanced request strategy** and try **Auto**, browser/mobile headers, same-site referer, Chrome TLS impersonation, Cloudflare/generic impersonation, or custom `Header: value` lines. You can also click **Use my browser profile** to fill headers from your current browser's user agent, language, and Client Hints. Cloudflare challenges may still require a real solved browser-session cookie, such as `Cookie: cf_clearance=...`. These options only tune request metadata; they do not bypass DRM, paywalls, private media, or account-only access.

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
