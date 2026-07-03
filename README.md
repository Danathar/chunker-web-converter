# Chunker Web Converter

A small web UI for converting zipped Minecraft Java worlds into Bedrock `.mcworld` files using [Chunker](https://github.com/HiveGamesOSS/Chunker).

## Local prototype

```sh
python3 web_converter.py
```

Then open locally or from another device on the same network:

```text
http://localhost:8080/
http://<this-computer-ip>:8080/
```

## Container image

Build locally:

```sh
podman build -t chunker-web:local .
```

Run locally/LAN-accessible:

```sh
podman run --rm \
  --name chunker-web \
  -p 8090:8080 \
  -v chunker-web-conversions:/app/conversions:Z \
  chunker-web:local
```

Open:

```text
http://<server-ip>:8090/
```

## GHCR image from GitHub Actions

The included workflow publishes to GitHub Container Registry:

```text
ghcr.io/danathar/chunker-web-converter:latest
ghcr.io/danathar/chunker-web-converter:chunker-<version>
```

Example run from GHCR:

```sh
podman pull ghcr.io/danathar/chunker-web-converter:latest
podman run -d \
  --name chunker-web \
  --restart unless-stopped \
  -p 8090:8080 \
  -v chunker-web-conversions:/app/conversions:Z \
  ghcr.io/danathar/chunker-web-converter:latest
```

Update later:

```sh
podman pull ghcr.io/danathar/chunker-web-converter:latest
podman stop chunker-web
podman rm chunker-web
podman run -d \
  --name chunker-web \
  --restart unless-stopped \
  -p 8090:8080 \
  -v chunker-web-conversions:/app/conversions:Z \
  ghcr.io/danathar/chunker-web-converter:latest
```


## Podman Quadlet service

A template is included at:

```text
deploy/chunker-web.container
```

On your Podman server:

```sh
mkdir -p ~/.config/containers/systemd
cp deploy/chunker-web.container ~/.config/containers/systemd/chunker-web.container
# edit Image=ghcr.io/danathar/chunker-web-converter:latest
systemctl --user daemon-reload
systemctl --user enable --now chunker-web.service
```

Optional auto-update timer:

```sh
systemctl --user enable --now podman-auto-update.timer
```

## GitHub Actions behavior

`.github/workflows/container.yml` does this:

- Runs on pushes to `main` that touch the app/container files.
- Runs daily on a schedule.
- Checks `https://api.github.com/repos/HiveGamesOSS/Chunker/releases/latest`.
- If the `ghcr.io/...:chunker-<latest-version>` tag already exists during a scheduled run, it skips the build.
- If Chunker has a new release, it builds a new image and tags it as:
  - `latest`
  - `chunker-<version>`
  - `<git-sha>`

## Settings

Environment variables:

- `HOST` default `0.0.0.0` in the container/all interfaces
- `PORT` default `8080` in the container
- `CHUNKER_BIN` default `/opt/chunker-cli/bin/chunker-cli` in the container
- `CHUNKER_OUTPUT_FORMAT` default `BEDROCK_R26_20`
- `MAX_UPLOAD_BYTES` default `536870912` (512 MB)
- `CHUNKER_TIMEOUT_SECONDS` default `1200`
- `CLEANUP_SUCCESSFUL_JOBS` default `true`; deletes a successful job directory after the `.mcworld` download is sent
- `FAILED_JOB_RETENTION_SECONDS` default `86400`; keeps failed/debug job directories for 24 hours before pruning. Set to `0` to remove them on the next request/startup, or `-1` to disable failed-job pruning.

## Disk cleanup

Successful conversions are removed from `/app/conversions` immediately after the download response is sent. Failed conversions are retained briefly for troubleshooting and pruned by `FAILED_JOB_RETENTION_SECONDS`.

If you do not want any conversion files to survive container restarts, do not bind-mount `/app/conversions` to host storage.

## Notes

- The app rejects zip entries with absolute paths or `..` path traversal.
- The current app is synchronous: one browser request waits while the conversion runs.
