# Audio Lab Public Site

A deployable public build of the Audio Lab music archive.

## What is included

- Public reader-facing pages:
  - `dashboard/audio-lab-live.html`
  - `dashboard/audio-lab-archive.html`
  - `dashboard/audio-lab-entry.html`
  - `dashboard/audio-lab-artist.html`
- Public API and page server:
  - `scripts/run_audio_lab_live.py`
- Snapshot data for the archive:
  - `outputs/audio-lab-live/current/*.json`

## Run locally

```bash
python3 scripts/run_audio_lab_live.py --host 127.0.0.1 --port 8876 --output-root outputs/audio-lab-live --no-autostart
```

Then open:

- `/audio-lab/live?lang=zh`
- `/audio-lab/archive?lang=zh`

## Deploy

This repo includes a `render.yaml` blueprint for Render.

The public site runs in read-only mode with `--no-autostart`, so it serves the current catalog snapshot without trying to generate new media jobs on the host.

## Aliyun ECS

For a stable full public link on an Aliyun server, use the deployment files in:

- `deploy/aliyun/README.md`
- `deploy/aliyun/audio-lab-public.service`
- `deploy/aliyun/audio-lab-public.nginx.conf`
- `deploy/aliyun/bootstrap_audio_lab_public.sh`

The service script now supports `AUDIO_LAB_ROOT`, so the repo can run outside the original local macOS path.
