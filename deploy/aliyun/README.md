# Aliyun ECS deployment

This folder contains a minimal deployment path for the public Audio Lab site on an Ubuntu-style ECS host.

## What it deploys

- `scripts/run_audio_lab_live.py` as a long-running local service on `127.0.0.1:8876`
- `nginx` as the public reverse proxy on port `80`
- the current archive snapshot under `outputs/audio-lab-live`

## Server assumptions

- Ubuntu or Debian based ECS image
- Python 3 installed or installable with `apt-get`
- SSH access with sudo

## Recommended target path

- `/opt/audio-lab-public-site`

## One-command bootstrap

After copying this repository to the ECS host:

```bash
cd /opt/audio-lab-public-site
bash deploy/aliyun/bootstrap_audio_lab_public.sh
```

## Manual pieces

- systemd unit: `deploy/aliyun/audio-lab-public.service`
- nginx site config: `deploy/aliyun/audio-lab-public.nginx.conf`

## Notes

- The public site runs with `--no-autostart`, so it serves the current archive snapshot and APIs without starting the heavier local generation loop.
- If you later bind a domain, replace `server_name _;` in the nginx config and add HTTPS with certbot or your preferred TLS setup.
