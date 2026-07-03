#!/usr/bin/env python3
"""Tiny dependency-free web UI for converting zipped Java worlds to .mcworld.

Prototype goals:
- Accept a Java world .zip from a browser.
- Safely extract it into a per-conversion workspace.
- Find the world folder containing level.dat.
- Run Chunker to convert it to Bedrock.
- Return an importable .mcworld file for download.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import sys
import time
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
CONVERSIONS_DIR = BASE_DIR / "conversions"
DEFAULT_OUTPUT_FORMAT = os.environ.get("CHUNKER_OUTPUT_FORMAT", "BEDROCK_R26_20")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
CHUNKER_TIMEOUT_SECONDS = int(os.environ.get("CHUNKER_TIMEOUT_SECONDS", str(20 * 60)))
CLEANUP_SUCCESSFUL_JOBS = os.environ.get("CLEANUP_SUCCESSFUL_JOBS", "true").lower() not in {"0", "false", "no"}
FAILED_JOB_RETENTION_SECONDS = int(os.environ.get("FAILED_JOB_RETENTION_SECONDS", str(24 * 60 * 60)))
ASYNC_JOB_RETENTION_SECONDS = int(os.environ.get("ASYNC_JOB_RETENTION_SECONDS", str(60 * 60)))
CHUNKER_VERSION_LABEL = os.environ.get("CHUNKER_VERSION", "unknown")
CHUNKER_RELEASE_DATE = os.environ.get("CHUNKER_RELEASE_DATE", "unknown")
IMAGE_BUILD_DATE = os.environ.get("IMAGE_BUILD_DATE", "unknown")

JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()

FORMATS = [
    "BEDROCK_R26_20",
    "BEDROCK_R26_10",
    "BEDROCK_R26",
    "BEDROCK_R21_130",
    "BEDROCK_R21_120",
    "BEDROCK_R21_110",
    "BEDROCK_R21_100",
]


def chunker_command() -> list[str]:
    """Return the best available Chunker command for this checkout/container."""
    configured = os.environ.get("CHUNKER_BIN")
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            configured_path.chmod(configured_path.stat().st_mode | 0o111)
        return [configured]

    native = BASE_DIR / "tools" / "chunker-cli-linux-amd64" / "chunker-cli" / "bin" / "chunker-cli"
    if native.exists():
        native.chmod(native.stat().st_mode | 0o111)
        return [str(native)]

    jar = BASE_DIR / "tools" / "chunker-cli-1.17.0.jar"
    java = shutil.which("java")
    if java and jar.exists():
        return [java, "-jar", str(jar)]

    wrapper = BASE_DIR / "tools" / "chunker"
    if wrapper.exists():
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
        return [str(wrapper)]

    raise RuntimeError("Could not find Chunker. Set CHUNKER_BIN or provide tools/chunker-cli-linux-amd64 or tools/chunker-cli-1.17.0.jar.")


def safe_name(name: str, fallback: str = "converted-world") -> str:
    name = name.strip().replace("\x00", "")
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or fallback


def is_safe_zip_member(member_name: str) -> bool:
    # Prevent zip-slip paths and absolute paths.
    if not member_name or member_name.startswith(("/", "\\")):
        return False
    parts = Path(member_name.replace("\\", "/")).parts
    return ".." not in parts


def extract_zip_safely(zip_path: Path, extract_dir: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        bad = [zi.filename for zi in zf.infolist() if not is_safe_zip_member(zi.filename)]
        if bad:
            raise ValueError(f"Zip contains unsafe path: {bad[0]!r}")
        zf.extractall(extract_dir)


def find_world_dir(extract_dir: Path) -> Path:
    candidates = [p.parent for p in extract_dir.rglob("level.dat") if p.is_file()]
    if not candidates:
        raise ValueError("Could not find level.dat in the uploaded zip. Is this a zipped Java world folder?")

    # Prefer the shallowest world folder, then alphabetically for deterministic behavior.
    candidates.sort(key=lambda p: (len(p.relative_to(extract_dir).parts), str(p).lower()))
    return candidates[0]


def zip_dir_to_mcworld(source_dir: Path, dest_file: Path) -> None:
    with zipfile.ZipFile(dest_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir).as_posix())


def cleanup_old_jobs() -> None:
    """Remove old conversion job directories left behind for failed-job debugging."""
    if FAILED_JOB_RETENTION_SECONDS < 0 or not CONVERSIONS_DIR.exists():
        return

    cutoff = time.time() - FAILED_JOB_RETENTION_SECONDS
    for job_dir in CONVERSIONS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            if job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            # Best-effort cleanup only; never fail a conversion because pruning failed.
            pass


def remove_job_dir(path: Path | None) -> None:
    if path is None:
        return
    try:
        job_dir = path if path.is_dir() else path.parent
        if job_dir.is_relative_to(CONVERSIONS_DIR):
            shutil.rmtree(job_dir, ignore_errors=True)
    except OSError:
        pass


def update_async_job(job_id: str, **updates: object) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def public_job(job: dict[str, object]) -> dict[str, object]:
    result = {
        "id": job["id"],
        "status": job["status"],
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "filename": job.get("filename", ""),
    }
    if job.get("status") == "complete":
        result["download_url"] = f"/jobs/{job['id']}/download"
        result["download_name"] = job.get("mcworld_name", "converted.mcworld")
    if job.get("status") == "error":
        result["error"] = job.get("error", "Conversion failed")
    return result


def cleanup_async_jobs() -> None:
    if ASYNC_JOB_RETENTION_SECONDS < 0:
        return
    cutoff = time.time() - ASYNC_JOB_RETENTION_SECONDS
    remove_ids: list[str] = []
    remove_paths: list[Path] = []
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            if job.get("status") not in {"complete", "error"}:
                continue
            if float(job.get("updated_at", 0)) >= cutoff:
                continue
            path_value = job.get("mcworld_path")
            if isinstance(path_value, str):
                remove_paths.append(Path(path_value))
            remove_ids.append(job_id)
        for job_id in remove_ids:
            JOBS.pop(job_id, None)
    for path in remove_paths:
        remove_job_dir(path)


def run_chunker(cmd: list[str], job_dir: Path, progress_cb: object | None = None) -> str:
    """Run Chunker and optionally report progress percentages from its output."""
    if progress_cb is None:
        completed = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=CHUNKER_TIMEOUT_SECONDS,
            check=False,
        )
        log = completed.stdout or ""
        (job_dir / "chunker.log").write_text(log)
        if completed.returncode != 0:
            raise RuntimeError(f"Chunker failed with exit code {completed.returncode}.\n\n{log[-4000:]}")
        return log

    lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    def read_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            stripped = line.strip()
            if stripped:
                match = list(re.finditer(r"(\d+(?:\.\d+)?)%", stripped))
                if match:
                    pct = min(99.0, float(match[-1].group(1)))
                    progress_cb(pct, stripped)
                elif "Compacting world" in stripped:
                    progress_cb(90.0, stripped)
                elif "Finished compacting" in stripped:
                    progress_cb(96.0, stripped)
                elif "Conversion complete" in stripped:
                    progress_cb(100.0, stripped)
                elif stripped.startswith("Converting from"):
                    progress_cb(1.0, stripped)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        returncode = proc.wait(timeout=CHUNKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        reader.join(timeout=5)
        log = "".join(lines)
        (job_dir / "chunker.log").write_text(log)
        raise RuntimeError(f"Chunker timed out after {CHUNKER_TIMEOUT_SECONDS} seconds.\n\n{log[-4000:]}") from exc
    reader.join(timeout=5)
    log = "".join(lines)
    (job_dir / "chunker.log").write_text(log)
    if returncode != 0:
        raise RuntimeError(f"Chunker failed with exit code {returncode}.\n\n{log[-4000:]}")
    return log


def convert_world(upload_name: str, upload_bytes: bytes, output_format: str, progress_cb: object | None = None) -> tuple[Path, str, str]:
    if not upload_name.lower().endswith(".zip"):
        raise ValueError("Please upload a .zip file containing a Java world.")
    if output_format not in FORMATS:
        raise ValueError(f"Unsupported output format: {output_format}")

    CONVERSIONS_DIR.mkdir(exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    job_dir = CONVERSIONS_DIR / job_id
    upload_dir = job_dir / "upload"
    extract_dir = job_dir / "extracted"
    output_dir = job_dir / "bedrock-world"
    job_dir.mkdir(parents=True)
    upload_dir.mkdir()
    extract_dir.mkdir()

    upload_path = upload_dir / safe_name(upload_name, "world.zip")
    upload_path.write_bytes(upload_bytes)

    if not zipfile.is_zipfile(upload_path):
        raise ValueError("The uploaded file is not a valid zip file.")

    extract_zip_safely(upload_path, extract_dir)
    world_dir = find_world_dir(extract_dir)
    world_name = safe_name(world_dir.name, Path(upload_name).stem or "world")

    cmd = chunker_command() + ["-i", str(world_dir), "-f", output_format, "-o", str(output_dir)]
    log = run_chunker(cmd, job_dir, progress_cb)

    mcworld_name = f"{world_name} Bedrock.mcworld"
    mcworld_path = job_dir / mcworld_name
    zip_dir_to_mcworld(output_dir, mcworld_path)
    return mcworld_path, mcworld_name, log


def start_async_conversion(upload_name: str, upload_bytes: bytes, output_format: str) -> str:
    cleanup_old_jobs()
    cleanup_async_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0.0,
            "message": "Queued",
            "filename": safe_name(upload_name, "world.zip"),
            "created_at": now,
            "updated_at": now,
        }

    def worker() -> None:
        def progress_cb(progress: float, message: str) -> None:
            update_async_job(job_id, progress=round(progress, 2), message=message)

        try:
            update_async_job(job_id, status="running", progress=0.0, message="Preparing upload")
            mcworld_path, mcworld_name, _log = convert_world(upload_name, upload_bytes, output_format, progress_cb)
            update_async_job(
                job_id,
                status="complete",
                progress=100.0,
                message="Conversion complete. Click the download link to save your .mcworld.",
                mcworld_path=str(mcworld_path),
                mcworld_name=mcworld_name,
            )
        except Exception as exc:
            update_async_job(job_id, status="error", progress=100.0, message="Conversion failed", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return job_id


class ConverterHandler(BaseHTTPRequestHandler):
    server_version = "ChunkerWeb/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        cleanup_async_jobs()
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.send_html(HTTPStatus.OK, page())
        elif path == "/healthz":
            self.send_text(HTTPStatus.OK, "ok\n")
        elif path.startswith("/jobs/") and path.endswith("/download"):
            self.handle_job_download(path)
        elif path.startswith("/jobs/"):
            self.handle_job_status(path)
        else:
            self.send_html(HTTPStatus.NOT_FOUND, page("Not found"))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/jobs":
            self.handle_job_create()
            return
        if path != "/convert":
            self.send_html(HTTPStatus.NOT_FOUND, page("Not found"))
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_html(HTTPStatus.BAD_REQUEST, page("Missing or invalid Content-Length."))
            return

        if content_length <= 0:
            self.send_html(HTTPStatus.BAD_REQUEST, page("No upload received."))
            return
        if content_length > MAX_UPLOAD_BYTES:
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            self.send_html(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, page(f"Upload is too large. Limit is {mb} MB."))
            return

        body = self.rfile.read(content_length)
        mcworld_path: Path | None = None
        try:
            cleanup_old_jobs()
            fields = parse_multipart(self.headers.get("Content-Type", ""), body)
            file_info = fields.get("world")
            if not isinstance(file_info, UploadedFile):
                raise ValueError("Choose a zipped Java world file before converting.")
            output_format = str(fields.get("format") or DEFAULT_OUTPUT_FORMAT)
            mcworld_path, mcworld_name, log = convert_world(file_info.filename, file_info.content, output_format)
        except Exception as exc:  # Prototype: return friendly diagnostics in browser.
            self.send_html(HTTPStatus.BAD_REQUEST, page(str(exc), is_error=True))
            return

        try:
            self.send_download(mcworld_path, mcworld_name)
        finally:
            if CLEANUP_SUCCESSFUL_JOBS:
                remove_job_dir(mcworld_path)

    def handle_job_create(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing or invalid Content-Length."})
            return

        if content_length <= 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "No upload received."})
            return
        if content_length > MAX_UPLOAD_BYTES:
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"Upload is too large. Limit is {mb} MB."})
            return

        body = self.rfile.read(content_length)
        try:
            fields = parse_multipart(self.headers.get("Content-Type", ""), body)
            file_info = fields.get("world")
            if not isinstance(file_info, UploadedFile):
                raise ValueError("Choose a zipped Java world file before converting.")
            output_format = str(fields.get("format") or DEFAULT_OUTPUT_FORMAT)
            if output_format not in FORMATS:
                raise ValueError(f"Unsupported output format: {output_format}")
            job_id = start_async_conversion(file_info.filename, file_info.content, output_format)
            with JOBS_LOCK:
                job = dict(JOBS[job_id])
            self.send_json(HTTPStatus.ACCEPTED, public_job(job))
        except Exception as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def handle_job_status(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 2:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        job_id = parts[1]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job and job.get("status") == "complete":
                path_value = job.get("mcworld_path")
                if not isinstance(path_value, str) or not Path(path_value).exists():
                    JOBS.pop(job_id, None)
                    job = None
            payload = public_job(dict(job)) if job else None
        if payload is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found or already cleaned up."})
            return
        self.send_json(HTTPStatus.OK, payload)

    def handle_job_download(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self.send_html(HTTPStatus.NOT_FOUND, page("Not found"))
            return
        job_id = parts[1]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                self.send_html(HTTPStatus.NOT_FOUND, page("Job not found or already cleaned up.", is_error=True))
                return
            if job.get("status") != "complete":
                self.send_html(HTTPStatus.BAD_REQUEST, page("That conversion is not complete yet.", is_error=True))
                return
            path_value = job.get("mcworld_path")
            mcworld_name = str(job.get("mcworld_name") or "converted.mcworld")

        if not isinstance(path_value, str):
            self.send_html(HTTPStatus.NOT_FOUND, page("Converted file is missing.", is_error=True))
            return
        mcworld_path = Path(path_value)
        try:
            self.send_download(mcworld_path, mcworld_name)
        finally:
            if CLEANUP_SUCCESSFUL_JOBS:
                remove_job_dir(mcworld_path)
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, status: HTTPStatus, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, status: HTTPStatus, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, path: Path, filename: str) -> None:
        data = path.read_bytes()
        safe_filename = safe_name(filename, "converted.mcworld")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.end_headers()
        self.wfile.write(data)


class UploadedFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.content = content


def parse_multipart(content_type: str, body: bytes) -> dict[str, object]:
    if "multipart/form-data" not in content_type:
        raise ValueError("Expected multipart/form-data upload.")

    headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=email_policy).parsebytes(headers + body)
    fields: dict[str, object] = {}

    for part in msg.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="Content-Disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            fields[name] = UploadedFile(filename, payload)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
    return fields


def page(message: str = "", is_error: bool = False) -> str:
    options = "\n".join(
        f'<option value="{html.escape(fmt)}" {"selected" if fmt == DEFAULT_OUTPUT_FORMAT else ""}>{html.escape(fmt)}</option>'
        for fmt in FORMATS
    )
    banner = ""
    if message:
        klass = "error" if is_error else "notice"
        banner = f'<div class="{klass}"><pre>{html.escape(message)}</pre></div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Minecraft Java → Bedrock Converter</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ max-width: 760px; margin: 3rem auto; padding: 0 1rem; line-height: 1.45; }}
    .meta-badge {{ position: fixed; top: .75rem; right: .75rem; font-size: .82rem; color: #666; background: Canvas; border: 1px solid #8885; border-radius: 10px; padding: .45rem .6rem; box-shadow: 0 2px 10px #0001; }}
    @media (max-width: 700px) {{ .meta-badge {{ position: static; margin-bottom: 1rem; display: inline-block; }} }}
    h1 {{ margin-bottom: .25rem; }}
    form, #progress-panel {{ margin-top: 1.5rem; padding: 1.25rem; border: 1px solid #8885; border-radius: 14px; }}
    label {{ display: block; font-weight: 700; margin-top: 1rem; }}
    input, select, button {{ font: inherit; margin-top: .35rem; }}
    input[type=file], select {{ width: 100%; box-sizing: border-box; }}
    button {{ padding: .7rem 1rem; border: 0; border-radius: 10px; cursor: pointer; font-weight: 700; }}
    button:disabled {{ cursor: wait; opacity: .65; }}
    progress {{ width: 100%; height: 1.4rem; margin-top: .75rem; }}
    .hint {{ color: #777; }}
    .error {{ margin-top: 1rem; border-left: 4px solid #c00; padding: .75rem; background: #c002; }}
    .notice {{ margin-top: 1rem; border-left: 4px solid #06c; padding: .75rem; background: #06c2; }}
    .hidden {{ display: none; }}
    .status-line {{ font-weight: 700; margin: .5rem 0 0; }}
    .small {{ font-size: .92rem; }}
    pre {{ white-space: pre-wrap; margin: 0; }}
  </style>
</head>
<body>
  <div class="meta-badge">Chunker {html.escape(CHUNKER_VERSION_LABEL)}<br>Released {html.escape(CHUNKER_RELEASE_DATE)}<br>Image updated {html.escape(IMAGE_BUILD_DATE)}</div>
  <h1>Minecraft Java → Bedrock Converter</h1>
  <p class="hint">Upload a zipped Java world. The server converts it with Chunker and downloads a <code>.mcworld</code> file.</p>
  {banner}
  <form id="convert-form" method="post" action="/convert" enctype="multipart/form-data">
    <label for="world">Zipped Java world</label>
    <input id="world" name="world" type="file" accept=".zip,application/zip" required>

    <label for="format">Bedrock output format</label>
    <select id="format" name="format">{options}</select>

    <p class="hint">Leave the default unless you need an older Bedrock version. Large maps may take a while; keep this page open.</p>
    <button id="convert-button" type="submit">Convert to .mcworld</button>
  </form>

  <section id="progress-panel" class="hidden" aria-live="polite">
    <strong id="progress-title">Starting conversion…</strong>
    <progress id="progress-bar" max="100"></progress>
    <p id="progress-percent" class="status-line">Preparing…</p>
    <p id="progress-message" class="hint small"></p>
    <p id="download-line" class="hidden"><a id="download-link" href="#">Download converted world</a></p>
  </section>

  <script>
    const form = document.getElementById('convert-form');
    const button = document.getElementById('convert-button');
    const panel = document.getElementById('progress-panel');
    const title = document.getElementById('progress-title');
    const bar = document.getElementById('progress-bar');
    const percent = document.getElementById('progress-percent');
    const message = document.getElementById('progress-message');
    const downloadLine = document.getElementById('download-line');
    const downloadLink = document.getElementById('download-link');
    let pollTimer = null;

    function setProgress(value, msg) {{
      if (typeof value === 'number' && !Number.isNaN(value)) {{
        bar.value = Math.max(0, Math.min(100, value));
        percent.textContent = `${{Math.round(bar.value)}}%`;
      }} else {{
        bar.removeAttribute('value');
        percent.textContent = 'Working…';
      }}
      if (msg) message.textContent = msg;
    }}

    async function pollJob(jobId) {{
      const response = await fetch(`/jobs/${{jobId}}`, {{ cache: 'no-store' }});
      if (!response.ok) throw new Error(`Unable to get job status (${{response.status}})`);
      const job = await response.json();
      title.textContent = job.status === 'complete' ? 'Conversion complete' :
                          job.status === 'error' ? 'Conversion failed' :
                          'Converting world…';
      setProgress(Number(job.progress), job.message || '');

      if (job.status === 'complete') {{
        clearInterval(pollTimer);
        pollTimer = null;
        button.disabled = false;
        button.textContent = 'Convert another map';
        downloadLink.href = job.download_url;
        downloadLink.download = job.download_name || 'converted.mcworld';
        downloadLine.classList.remove('hidden');
        message.textContent = 'Your converted .mcworld is ready. Click the download link below.';
      }} else if (job.status === 'error') {{
        clearInterval(pollTimer);
        pollTimer = null;
        button.disabled = false;
        button.textContent = 'Try again';
        title.textContent = 'Conversion failed';
        message.textContent = job.error || job.message || 'Conversion failed.';
        panel.classList.add('error');
      }}
    }}

    form.addEventListener('submit', async (event) => {{
      event.preventDefault();
      clearInterval(pollTimer);
      downloadLine.classList.add('hidden');
      panel.classList.remove('hidden', 'error');
      title.textContent = 'Uploading world…';
      message.textContent = 'Uploading zip to the server. Conversion will start after upload completes.';
      bar.removeAttribute('value');
      percent.textContent = 'Uploading…';
      button.disabled = true;
      button.textContent = 'Converting…';

      try {{
        const response = await fetch('/jobs', {{ method: 'POST', body: new FormData(form) }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `Upload failed (${{response.status}})`);
        title.textContent = 'Queued…';
        setProgress(Number(payload.progress), payload.message || 'Queued');
        pollTimer = setInterval(() => pollJob(payload.id).catch(showError), 1000);
        await pollJob(payload.id);
      }} catch (error) {{
        showError(error);
      }}
    }});

    function showError(error) {{
      clearInterval(pollTimer);
      pollTimer = null;
      panel.classList.remove('hidden');
      panel.classList.add('error');
      title.textContent = 'Conversion failed';
      bar.value = 100;
      percent.textContent = 'Error';
      message.textContent = error && error.message ? error.message : String(error);
      button.disabled = false;
      button.textContent = 'Try again';
    }}
  </script>
</body>
</html>"""


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    # Fail fast with a helpful error if Chunker is missing.
    cleanup_old_jobs()
    cmd = chunker_command()
    print(f"Using Chunker: {' '.join(cmd)}")
    display_host = "localhost" if host in {"0.0.0.0", "::"} else host
    print(f"Listening on {host}:{port}")
    print(f"Open http://{display_host}:{port}/ locally, or http://<this-computer-ip>:{port}/ from another device")
    server = ThreadingHTTPServer((host, port), ConverterHandler)

    def request_shutdown(signum: int, _frame: object) -> None:
        print(f"\nReceived signal {signum}; shutting down...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
