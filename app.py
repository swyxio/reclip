import os
import uuid
import glob
import json
import subprocess
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Mobile/15E148 Safari/604.1"
)
STRATEGIES = {"auto", "default", "browser", "mobile", "referer", "impersonate", "cloudflare"}
AUTO_STRATEGIES = ("default", "browser", "mobile", "referer", "cloudflare", "impersonate")


def origin_for(url):
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def error_tail(stderr):
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "yt-dlp failed without an error message"


def parse_headers(raw_headers):
    if isinstance(raw_headers, list):
        lines = [str(item) for item in raw_headers]
    else:
        lines = str(raw_headers or "").splitlines()

    headers = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(headers) >= 16:
            raise ValueError("Too many custom headers (max 16)")
        if ":" not in line:
            raise ValueError(f"Header must use 'Name: value': {line[:40]}")
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name or any(c not in "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for c in name):
            raise ValueError(f"Invalid header name: {name[:40]}")
        max_len = 4096 if name.lower() == "cookie" else 500
        if "\r" in value or "\n" in value or len(value) > max_len:
            raise ValueError(f"Invalid header value for {name}")
        headers.append((name, value))
    return headers


def request_options(data):
    strategy = str(data.get("strategy") or "default").strip().lower()
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}")
    return {
        "strategy": strategy,
        "headers": parse_headers(data.get("headers", "")),
    }


def strategy_flags(strategy, url, custom_headers):
    headers = []
    if strategy == "browser":
        headers += [
            ("User-Agent", DESKTOP_UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
    elif strategy == "mobile":
        headers += [
            ("User-Agent", MOBILE_UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
    elif strategy in {"referer", "cloudflare"}:
        headers += [
            ("User-Agent", DESKTOP_UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
        origin = origin_for(url)
        if origin:
            headers.append(("Referer", origin))

    headers += custom_headers

    flags = []
    if strategy == "impersonate":
        flags += ["--impersonate", "chrome"]
    elif strategy == "cloudflare":
        flags += ["--impersonate", "chrome", "--extractor-args", "generic:impersonate=chrome"]
    for name, value in headers:
        flags += ["--add-headers", f"{name}:{value}"]
    return flags


def strategies_to_try(options):
    strategy = options["strategy"]
    return AUTO_STRATEGIES if strategy == "auto" else (strategy,)


def build_info_cmd(url, strategy, custom_headers):
    return ["yt-dlp", "--no-playlist"] + strategy_flags(strategy, url, custom_headers) + ["-j", url]


def build_download_cmd(url, out_template, format_choice, format_id, strategy, custom_headers):
    cmd = ["yt-dlp", "--no-playlist"] + strategy_flags(strategy, url, custom_headers) + ["-o", out_template]
    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
    cmd.append(url)
    return cmd


def clean_job_files(job_id):
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        try:
            os.remove(path)
        except OSError:
            pass


def run_download(job_id, url, format_choice, format_id, options):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    attempts = []

    try:
        for strategy in strategies_to_try(options):
            clean_job_files(job_id)
            job["strategy"] = strategy
            cmd = build_download_cmd(url, out_template, format_choice, format_id, strategy, options["headers"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                break
            attempts.append(f"{strategy}: {error_tail(result.stderr)}")
        else:
            job["status"] = "error"
            job["error"] = "All strategies failed. " + " | ".join(attempts)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        options = request_options(data)
        attempts = []
        for strategy in strategies_to_try(options):
            cmd = build_info_cmd(url, strategy, options["headers"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                break
            attempts.append(f"{strategy}: {error_tail(result.stderr)}")
        else:
            return jsonify({"error": "All strategies failed. " + " | ".join(attempts)}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
            "strategy": strategy,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        options = request_options(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "strategy": options["strategy"],
    }

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, options))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id, "strategy": options["strategy"]})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "strategy": job.get("strategy"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
