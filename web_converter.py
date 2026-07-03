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
import os
import re
import shutil
import signal
import subprocess
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


def convert_world(upload_name: str, upload_bytes: bytes, output_format: str) -> tuple[Path, str, str]:
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

    mcworld_name = f"{world_name} Bedrock.mcworld"
    mcworld_path = job_dir / mcworld_name
    zip_dir_to_mcworld(output_dir, mcworld_path)
    return mcworld_path, mcworld_name, log


class ConverterHandler(BaseHTTPRequestHandler):
    server_version = "ChunkerWeb/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.send_html(HTTPStatus.OK, page())
        elif path == "/healthz":
            self.send_text(HTTPStatus.OK, "ok\n")
        else:
            self.send_html(HTTPStatus.NOT_FOUND, page("Not found"))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
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
    h1 {{ margin-bottom: .25rem; }}
    form {{ margin-top: 1.5rem; padding: 1.25rem; border: 1px solid #8885; border-radius: 14px; }}
    label {{ display: block; font-weight: 700; margin-top: 1rem; }}
    input, select, button {{ font: inherit; margin-top: .35rem; }}
    input[type=file], select {{ width: 100%; box-sizing: border-box; }}
    button {{ padding: .7rem 1rem; border: 0; border-radius: 10px; cursor: pointer; font-weight: 700; }}
    .hint {{ color: #777; }}
    .error {{ margin-top: 1rem; border-left: 4px solid #c00; padding: .75rem; background: #c002; }}
    .notice {{ margin-top: 1rem; border-left: 4px solid #06c; padding: .75rem; background: #06c2; }}
    pre {{ white-space: pre-wrap; margin: 0; }}
  </style>
</head>
<body>
  <h1>Minecraft Java → Bedrock Converter</h1>
  <p class="hint">Upload a zipped Java world. The server converts it with Chunker and downloads a <code>.mcworld</code> file.</p>
  {banner}
  <form method="post" action="/convert" enctype="multipart/form-data">
    <label for="world">Zipped Java world</label>
    <input id="world" name="world" type="file" accept=".zip,application/zip" required>

    <label for="format">Bedrock output format</label>
    <select id="format" name="format">{options}</select>

    <p class="hint">Leave the default unless you need an older Bedrock version. Conversion may take a minute for large maps.</p>
    <button type="submit">Convert to .mcworld</button>
  </form>
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
