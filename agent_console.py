import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, render_template, request


agent_console = Blueprint("agent_console", __name__)

REPO_DEFAULT = "swyxio/reclip"
ALLOWED_PATHS = (
    "app.py",
    "agent_console.py",
    "agent_worker.mjs",
    "templates/",
    "static/",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "Dockerfile",
    "README.md",
)
DENIED_PATH_PARTS = (".env", ".pem", ".key", "downloads/", "__pycache__/")
MAX_DIFF_BYTES = 500_000
JOBS = {}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def admin_token():
    return os.environ.get("ADMIN_TOKEN", "").strip()


def token_from_request():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return request.headers.get("X-Admin-Token", "").strip() or request.form.get("admin_token", "").strip()


def require_admin():
    expected = admin_token()
    if not expected:
        return jsonify({"error": "ADMIN_TOKEN is not configured"}), 503
    if token_from_request() != expected:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def add_log(job, message, kind="log"):
    entry = {"time": now(), "type": kind, "message": redact(message)}
    job["events"].append(entry)


def redact(text):
    text = str(text)
    for name in ("GITHUB_TOKEN", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "ADMIN_TOKEN"):
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, f"<redacted:{name}>")
    return text


def run_cmd(job, args, cwd=None, env=None, timeout=300, capture=True):
    add_log(job, f"$ {' '.join(args)}", "cmd")
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    if capture:
        if result.stdout.strip():
            add_log(job, result.stdout.strip(), "stdout")
        if result.stderr.strip():
            add_log(job, result.stderr.strip(), "stderr")
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}")
    return result.stdout if capture else ""


def git_env():
    token = required_env("GITHUB_TOKEN")
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: bearer {token}",
    })
    return env


def codex_env(job_home):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": job_home,
        "CODEX_HOME": os.path.join(job_home, ".codex"),
    }
    for name in ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def workspace_root(job):
    return Path(job["workspace"]) / "repo"


def is_allowed_path(path):
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.startswith("../") or "/../" in normalized:
        return False
    if any(part in normalized for part in DENIED_PATH_PARTS):
        return False
    return any(normalized == item.rstrip("/") or normalized.startswith(item) for item in ALLOWED_PATHS)


def collect_diff(job):
    root = workspace_root(job)
    names = run_cmd(job, ["git", "diff", "--name-only"], cwd=root).splitlines()
    changed = [name.strip() for name in names if name.strip()]
    if not changed:
        raise RuntimeError("Codex completed without producing file changes")
    rejected = [name for name in changed if not is_allowed_path(name)]
    if rejected:
        raise RuntimeError(f"Rejected changes outside allowlist: {', '.join(rejected)}")
    diff = run_cmd(job, ["git", "diff", "--no-ext-diff", "--"], cwd=root)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise RuntimeError("Diff is too large for admin review")
    job["changed_files"] = changed
    job["diff"] = diff
    add_log(job, f"Prepared diff for {len(changed)} changed file(s)", "log")


def github_api(method, path, **kwargs):
    repo = os.environ.get("GITHUB_REPO", REPO_DEFAULT)
    token = required_env("GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub API failed {response.status_code}: {response.text[:500]}")
    return response.json()


def create_pull_request(job):
    root = workspace_root(job)
    run_cmd(job, ["git", "config", "user.email", "reclip-agent@users.noreply.github.com"], cwd=root)
    run_cmd(job, ["git", "config", "user.name", "ReClip Agent"], cwd=root)
    run_cmd(job, ["git", "add", "--"] + job["changed_files"], cwd=root)
    run_cmd(job, ["git", "commit", "-m", job["commit_message"]], cwd=root)
    run_cmd(job, ["git", "push", "origin", job["branch"]], cwd=root, env=git_env(), timeout=300)
    pr = github_api("POST", "/pulls", json={
        "title": job["commit_message"],
        "head": job["branch"],
        "base": os.environ.get("GITHUB_BASE_BRANCH", "main"),
        "body": f"Created by ReClip admin agent job `{job['id']}`.\n\nPrompt:\n\n{job['prompt']}",
    })
    job["pr_url"] = pr["html_url"]
    job["status"] = "pr_created"
    add_log(job, f"Created PR: {job['pr_url']}", "pr")


def run_agent_job(job_id):
    job = JOBS[job_id]
    try:
        repo = os.environ.get("GITHUB_REPO", REPO_DEFAULT)
        required_env("GITHUB_TOKEN")
        if not (os.environ.get("CODEX_ACCESS_TOKEN") or os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            raise RuntimeError("Set CODEX_ACCESS_TOKEN, CODEX_API_KEY, or OPENAI_API_KEY before running agent jobs")

        job["status"] = "running"
        add_log(job, "Creating temporary checkout")
        Path(job["workspace"]).mkdir(parents=True, exist_ok=True)
        clone_url = f"https://github.com/{repo}.git"
        run_cmd(job, ["git", "clone", "--depth", "1", clone_url, "repo"], cwd=job["workspace"], env=git_env(), timeout=300)

        root = workspace_root(job)
        run_cmd(job, ["git", "checkout", "-b", job["branch"]], cwd=root)

        add_log(job, "Starting Codex SDK worker")
        worker_input = {
            "jobId": job["id"],
            "prompt": job["prompt"],
            "workingDirectory": str(root),
            "model": os.environ.get("CODEX_MODEL", "").strip(),
            "reasoningEffort": os.environ.get("CODEX_REASONING_EFFORT", "medium"),
        }
        proc = subprocess.Popen(
            ["node", str(Path(__file__).with_name("agent_worker.mjs"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=codex_env(job["workspace"]),
        )
        stdout, stderr = proc.communicate(json.dumps(worker_input), timeout=int(os.environ.get("AGENT_TIMEOUT_SECONDS", "900")))
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
                add_log(job, event.get("message", event), event.get("type", "agent"))
            except json.JSONDecodeError:
                add_log(job, line, "agent")
        if stderr.strip():
            add_log(job, stderr.strip(), "stderr")
        if proc.returncode != 0:
            raise RuntimeError(f"Codex worker failed with exit code {proc.returncode}")

        collect_diff(job)
        job["status"] = "awaiting_approval"
        add_log(job, "Review the diff, then approve PR creation", "approval_required")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = redact(str(exc))
        add_log(job, job["error"], "error")


@agent_console.route("/admin")
def admin_page():
    configured = {
        "adminToken": bool(admin_token()),
        "githubToken": bool(os.environ.get("GITHUB_TOKEN")),
        "codexCredential": bool(os.environ.get("CODEX_ACCESS_TOKEN") or os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")),
        "githubRepo": os.environ.get("GITHUB_REPO", REPO_DEFAULT),
    }
    return render_template("admin.html", configured=configured)


@agent_console.route("/api/admin/jobs", methods=["POST"])
def create_job():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    commit_message = str(data.get("commit_message") or "").strip() or "Agent proposal from ReClip admin console"
    if len(prompt) < 10:
        return jsonify({"error": "Prompt must be at least 10 characters"}), 400
    if len(prompt) > 8000:
        return jsonify({"error": "Prompt is too long"}), 400
    if len(commit_message) > 160:
        return jsonify({"error": "Commit message is too long"}), 400

    max_active = int(os.environ.get("MAX_ACTIVE_AGENT_JOBS", "1"))
    active = [job for job in JOBS.values() if job.get("status") in {"queued", "running"}]
    if len(active) >= max_active:
        return jsonify({"error": "Another agent job is already running"}), 429

    job_id = uuid.uuid4().hex[:10]
    branch = f"codex/admin-agent-{job_id}"
    workspace_base = os.environ.get("AGENT_WORKDIR", tempfile.gettempdir())
    workspace = os.path.join(workspace_base, f"reclip-agent-{job_id}")
    job = {
        "id": job_id,
        "status": "queued",
        "prompt": prompt,
        "branch": branch,
        "workspace": workspace,
        "events": [],
        "diff": "",
        "changed_files": [],
        "commit_message": commit_message,
        "created_at": now(),
    }
    JOBS[job_id] = job
    threading.Thread(target=run_agent_job, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})


@agent_console.route("/api/admin/jobs/<job_id>")
def get_job(job_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({k: v for k, v in job.items() if k != "workspace"})


@agent_console.route("/api/admin/jobs/<job_id>/events")
def job_events(job_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def stream():
        index = 0
        while True:
            while index < len(job["events"]):
                yield f"data: {json.dumps(job['events'][index])}\n\n"
                index += 1
            if job["status"] in {"awaiting_approval", "pr_created", "error"}:
                yield f"data: {json.dumps({'type': 'status', 'message': job['status'], 'time': now()})}\n\n"
                break
            time.sleep(1)

    return Response(stream(), mimetype="text/event-stream")


@agent_console.route("/api/admin/jobs/<job_id>/approve-pr", methods=["POST"])
def approve_pr(job_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "awaiting_approval":
        return jsonify({"error": f"Job is not ready for PR approval: {job['status']}"}), 400
    try:
        create_pull_request(job)
        return jsonify({"pr_url": job["pr_url"]})
    except Exception as exc:
        job["status"] = "error"
        job["error"] = redact(str(exc))
        add_log(job, job["error"], "error")
        return jsonify({"error": job["error"]}), 500


@agent_console.route("/api/admin/jobs/<job_id>/cleanup", methods=["POST"])
def cleanup_job(job_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    shutil.rmtree(job.get("workspace", ""), ignore_errors=True)
    add_log(job, "Temporary workspace removed")
    return jsonify({"ok": True})
