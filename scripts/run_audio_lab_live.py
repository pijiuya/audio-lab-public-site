#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, parse_qs, urlparse
from urllib.request import Request, urlopen


ROOT = Path(os.environ.get("AUDIO_LAB_ROOT", Path(__file__).resolve().parents[1])).resolve()
GENERATOR = ROOT / "scripts" / "generate_multiband_audio_lab.py"
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "audio-lab-live"
CURATED_BUNDLES = ("ruin_epic", "night_broadcast", "artifact_dance", "chamber_signal")
RUNTIME_PROFILES: dict[str, dict[str, object]] = {
    "realism-cycle": {
        "description": "Rotate through showcase bundles with a realism-heavy chamber pass and exported stems.",
        "sequence": (
            {"preset_bundle": "chamber_signal", "duration": 14.0, "export_stems": True, "request_label": "Chamber realism pass"},
            {"preset_bundle": "ruin_epic", "duration": 12.0, "request_label": "Ruin long-tail pass"},
            {"preset_bundle": "night_broadcast", "duration": 12.0, "request_label": "Broadcast pressure pass"},
            {"preset_bundle": "artifact_dance", "duration": 11.0, "request_label": "Artifact floor pass"},
        ),
    },
    "broadcast-floor": {
        "description": "Alternate the most direct public-facing bundles for a gallery-floor runtime.",
        "sequence": (
            {"preset_bundle": "night_broadcast", "duration": 10.0, "request_label": "Broadcast floor cycle"},
            {"preset_bundle": "artifact_dance", "duration": 10.0, "request_label": "Artifact floor cycle"},
            {"preset_bundle": "chamber_signal", "duration": 12.0, "request_label": "Breathing reset cycle"},
        ),
    },
    "fixed": {
        "description": "Do not rotate automatically beyond the default pinned request.",
        "sequence": (),
    },
}
STATUS_PATH = "/api/audio-lab/status"
LATEST_PATH = "/api/audio-lab/latest"
LATEST_AUDIO_PATH = "/api/audio-lab/latest/audio"
LATEST_SUMMARY_PATH = "/api/audio-lab/latest/summary"
LATEST_MANIFEST_PATH = "/api/audio-lab/latest/manifest"
LATEST_STYLE_DISCOVERY_PATH = "/api/audio-lab/latest/style-discovery"
LATEST_LOG_PATH = "/api/audio-lab/latest/log"
LATEST_REVIEW_PATH = "/api/audio-lab/latest/review"
LATEST_CREDIBILITY_PATH = "/api/audio-lab/latest/credibility"
LATEST_PROFESSIONAL_PATH = "/api/audio-lab/latest/professional"
LATEST_CATALOG_CANDIDATES_PATH = "/api/audio-lab/latest/catalog-candidates"
LATEST_CATALOG_SEEDS_PATH = "/api/audio-lab/latest/catalog-seeds"
LATEST_CATALOG_ENTRIES_PATH = "/api/audio-lab/latest/catalog-entries"
LATEST_CATALOG_ENTRIES_LITE_PATH = "/api/audio-lab/latest/catalog-entries-lite"
LATEST_CATALOG_ENTRY_PATH = "/api/audio-lab/latest/catalog-entry"
LATEST_CATALOG_ENTRIES_PAGE_PATH = "/api/audio-lab/latest/catalog-entries-page"
LATEST_CATALOG_LIBRARY_PATH = "/api/audio-lab/latest/catalog-library"
LATEST_PROFESSIONAL_DIGEST_PATH = "/api/audio-lab/latest/professional-digest"
LATEST_CATALOG_ROLLUP_STATUS_PATH = "/api/audio-lab/latest/catalog-rollup-status"
INTEGRATIONS_STATUS_PATH = "/api/audio-lab/integrations/status"
SPOTIFY_CONNECT_PATH = "/api/audio-lab/integrations/spotify/connect"
SPOTIFY_CALLBACK_PATH = "/api/audio-lab/integrations/spotify/callback"
SPOTIFY_EXPORT_PATH = "/api/audio-lab/integrations/spotify/export"
NETEASE_IMPORT_PATH = "/api/audio-lab/integrations/netease/import"
CONTRACT_PATH = "/api/audio-lab/contract"
CONTROL_START_PATH = "/api/audio-lab/control/start"
CONTROL_STOP_PATH = "/api/audio-lab/control/stop"
RENDER_PATH = "/api/audio-lab/render"
LIVE_PAGE_PATH = "/audio-lab/live"
ARCHIVE_PAGE_PATH = "/audio-lab/archive"
ENTRY_PAGE_PATH = "/audio-lab/entry"
ARTIST_PAGE_PATH = "/audio-lab/artist"


def load_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    if not path.exists():
        return payload
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def slugify(value: object, default: str = "playlist") -> str:
    text = "".join(ch.lower() if str(ch).isalnum() else "-" for ch in str(value or "").strip())
    collapsed = "-".join(part for part in text.split("-") if part)
    return collapsed or default


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict[str, object]:
    request = Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc}") from exc


def now_label() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding=encoding)
    temp_path.replace(path)


def write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def atomic_copy(source: Path, destination: Path) -> None:
    ensure_dir(destination.parent)
    temp_path = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temp_path)
    temp_path.replace(destination)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def clamp_score(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)


def sentence_case(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def truncate_text(value: object, limit: int = 260) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact_entry_payload(entry: dict[str, object]) -> dict[str, object]:
    artist_context = entry.get("artist_context") or {}
    listener_context = artist_context.get("listener_context") or {}
    site_score = entry.get("site_score") or {}
    credibility = entry.get("credibility") or {}
    professional = entry.get("professional_evidence") or {}
    discovery = entry.get("discovery_signals") or {}
    detail = entry.get("detail") or {}
    return {
        "entry_id": entry.get("entry_id"),
        "artist": entry.get("artist"),
        "album": entry.get("album"),
        "year": entry.get("year"),
        "era_tag": entry.get("era_tag"),
        "status": entry.get("status"),
        "scene_tags": entry.get("scene_tags") or [],
        "cover_url": entry.get("cover_url") or entry.get("cover_art_url"),
        "classification": entry.get("classification") or {},
        "site_score": site_score,
        "credibility": {
            "score_0_to_100": credibility.get("score_0_to_100"),
            "coverage_0_to_1": credibility.get("coverage_0_to_1"),
            "judgment": credibility.get("judgment"),
            "families": credibility.get("families") or [],
        },
        "artist_context": {
            "origin": artist_context.get("origin") or {},
            "chronology": artist_context.get("chronology") or {},
            "musicbrainz_profile": {
                "tags": ((artist_context.get("musicbrainz_profile") or {}).get("tags") or [])[:8],
            },
            "listener_context": {
                "tags": (listener_context.get("tags") or [])[:8],
                "similar_artists": (listener_context.get("similar_artists") or [])[:8],
                "top_tracks": (listener_context.get("top_tracks") or [])[:6],
                "top_albums": (listener_context.get("top_albums") or [])[:6],
                "bio_summary": truncate_text(listener_context.get("bio_summary"), 320),
                "image_url": listener_context.get("image_url"),
                "url": listener_context.get("url"),
            },
        },
        "professional_evidence": {
            "summary": professional.get("summary"),
            "match_count": professional.get("match_count", 0),
            "items": (professional.get("items") or [])[:4],
        },
        "discovery_signals": {
            "platform_kind": discovery.get("platform_kind"),
            "source_format": discovery.get("source_format"),
            "attention_value": discovery.get("attention_value"),
            "duration_seconds": discovery.get("duration_seconds"),
            "originality_score": discovery.get("originality_score"),
            "indie_creator_likely": discovery.get("indie_creator_likely"),
            "flags": (discovery.get("flags") or [])[:6],
            "summary": discovery.get("summary"),
        },
        "detail": {
            "headline": detail.get("headline"),
            "dek": detail.get("dek"),
            "review": truncate_text(detail.get("review"), 420),
            "evidence": (detail.get("evidence") or [])[:8],
            "matched_sources": (detail.get("matched_sources") or [])[:6],
        },
        "listening_links": {
            "summary": ((entry.get("listening_links") or {}).get("summary")),
            "items": (((entry.get("listening_links") or {}).get("items") or [])[:8]),
        },
        "generated_at": entry.get("generated_at"),
    }


def derive_review_from_summary(summary: dict[str, object], latest_record: dict[str, object] | None = None) -> dict[str, object]:
    if not summary:
        return {}

    style_components = [str(item) for item in summary.get("style_components") or [] if str(item).strip()]
    style_label = summary.get("style") or (" + ".join(style_components) if style_components else "untitled mix")
    bundle = str(summary.get("preset_bundle") or "custom")
    segment_count = int(summary.get("segment_count") or 0)
    target_length = float(summary.get("target_length_seconds") or 0.0)
    voice_mode = str(summary.get("voice_mode") or "instrumental")
    master_bus = str(summary.get("master_bus") or "default")
    quality_notes = [str(item) for item in summary.get("quality_notes") or [] if str(item).strip()]
    next_experiments = [str(item) for item in summary.get("next_experiments") or [] if str(item).strip()]
    active_traits = [str(item) for item in (summary.get("style_discovery") or {}).get("active_traits") or [] if str(item).strip()]
    arrangement_sections = [section for section in summary.get("arrangement_sections") or [] if isinstance(section, dict)]

    mood_tags = unique_strings(
        style_components[:3]
        + [str(section.get("narrative_focus") or "") for section in arrangement_sections[:3]]
        + [trait.replace(" ", "-") for trait in active_traits[:2]]
    )
    strengths = unique_strings(
        active_traits[:3]
        + [f"{len(arrangement_sections)}-section arrangement" if arrangement_sections else ""]
        + [f"{voice_mode} voice treatment" if voice_mode and voice_mode != "instrumental" else ""]
        + [f"{master_bus} master bus" if master_bus and master_bus != "default" else ""]
    )

    weaknesses: list[str] = []
    if summary.get("downloaded_asset_count") in {0, None}:
        weaknesses.append("leans heavily on internal generation, so source variety is still narrow")
    if summary.get("instrument_asset_count") in {0, None}:
        weaknesses.append("instrument realism is more implied than fully grounded in sampled performance")
    if quality_notes and quality_notes[0] != "No major quality issues detected in this render pass.":
        weaknesses.extend(quality_notes[:2])
    elif not weaknesses:
        weaknesses.append("editorial identity is promising, but the review voice still needs stronger contrast between tracks")

    cohesion = clamp_score(6.4 + min(segment_count, 4) * 0.35 + (0.6 if arrangement_sections else 0.0))
    texture = clamp_score(6.2 + min(len(active_traits), 4) * 0.45 + (0.4 if summary.get("export_stems") else 0.0))
    originality = clamp_score(6.0 + min(len(style_components), 4) * 0.5 + (0.5 if bundle != "custom" else 0.0))
    replay_value = clamp_score(5.8 + (0.7 if target_length >= 60 else 0.2) + (0.4 if voice_mode not in {"", "none", "instrumental"} else 0.0))
    overall = clamp_score((cohesion + texture + originality + replay_value) / 4)

    verdict_bits = [
        f"{style_label} builds a {bundle.replace('_', ' ')}-leaning world",
        f"with {voice_mode} vocals" if voice_mode not in {"", "none", "instrumental"} else "with an instrumental-first focus",
        f"across {segment_count or 1} movement{'s' if (segment_count or 1) != 1 else ''}",
    ]
    one_line_verdict = sentence_case(", ".join(verdict_bits) + ".")

    review_paragraphs = [
        f"This pass frames {style_label} as a composed listening experience rather than a raw generator output. The arrangement pushes through {segment_count or 1} segments with a clear {bundle.replace('_', ' ')} identity and keeps the production language legible enough for a listener-facing review surface.",
        f"Its strongest signals are {', '.join(strengths[:3]) if strengths else 'cohesion and atmosphere'}. The current weak spot is that {weaknesses[0]}.",
    ]

    return {
        "run_id": latest_record.get("run_id") if latest_record else None,
        "title": sentence_case(str(style_label)),
        "one_line_verdict": one_line_verdict,
        "editorial_review": " ".join(review_paragraphs),
        "scores": {
            "overall": overall,
            "cohesion": cohesion,
            "texture": texture,
            "originality": originality,
            "replay_value": replay_value,
        },
        "mood_tags": mood_tags,
        "strengths": strengths[:4],
        "weaknesses": unique_strings(weaknesses)[:4],
        "for_fans_of": unique_strings(style_components[:3] + [bundle.replace("_", " ")])[:4],
        "source_notes": quality_notes[:3],
        "next_angles": next_experiments[:3],
        "review_basis": {
            "preset_bundle": bundle,
            "style_components": style_components,
            "segment_count": segment_count,
            "target_length_seconds": target_length or None,
            "voice_mode": voice_mode,
            "master_bus": master_bus,
        },
    }


def normalize_listish(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def normalize_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    return int(value)


def normalize_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    return float(value)


def normalize_string(value: object, default: str | None = None) -> str | None:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def relative_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


class AudioLabLiveCoordinator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.output_root = ensure_dir(Path(args.output_root).expanduser().resolve())
        self.runs_dir = ensure_dir(self.output_root / "runs")
        self.current_dir = ensure_dir(self.output_root / "current")
        self.integrations_dir = ensure_dir(self.current_dir / "integrations")
        self.state_path = self.output_root / "service_state.json"
        self.condition = threading.Condition()
        self.queue: deque[dict[str, object]] = deque()
        self.recent_events: deque[dict[str, object]] = deque(maxlen=24)
        self.worker = threading.Thread(target=self._worker_loop, name="audio-lab-live-worker", daemon=True)
        self.should_exit = False
        self.auto_loop_enabled = args.autostart
        self.render_active = False
        self.completed_runs = 0
        self.manual_runs = 0
        self.failed_runs = 0
        self.latest_run: dict[str, object] = self._recover_latest_run()
        self.last_error: str | None = None
        self.active_request: dict[str, object] | None = None
        self.active_process_id: int | None = None
        self.started_at = now_label()
        self.default_request = {
            "preset_bundle": args.preset_bundle,
            "style": list(args.style or []),
            "randomness_preset": args.randomness_preset,
            "duration": args.duration,
            "target_length": args.target_length,
            "sample_rate": args.sample_rate,
            "seed": args.seed,
            "download_count": args.download_count,
            "instrument_download_count": args.instrument_download_count,
            "query": list(args.query or []),
            "instrument_query": list(args.instrument_query or []),
            "spoken_text": list(args.spoken_text or []),
            "voice_name": list(args.voice_name or []),
            "voice_mode": args.voice_mode,
            "voice_effect": args.voice_effect,
            "density_curve": args.density_curve,
            "groove_template": args.groove_template,
            "master_bus": args.master_bus,
            "export_stems": bool(args.export_stems),
            "with_downloads": bool(args.with_downloads),
            "no_voice": bool(args.no_voice),
            "ignore_bundle_target_length": bool(args.ignore_bundle_target_length),
            "client_name": None,
            "request_label": None,
            "note": None,
        }
        self._record_event("service-started", {"runtime_profile": args.runtime_profile, "autostart": args.autostart})
        self._persist_state()
        self.worker.start()

    def _integration_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(load_env_file(ROOT / ".music-catalog.env"))
        return env

    def _integration_file(self, name: str) -> Path:
        return self.integrations_dir / name

    def _integration_status(self) -> dict[str, object]:
        env = self._integration_env()
        spotify_ready = bool(env.get("SPOTIFY_CLIENT_ID")) and bool(env.get("SPOTIFY_CLIENT_SECRET"))
        spotify_token = load_json(self._integration_file("spotify_token.json"))
        latest_export = load_json(self._integration_file("spotify_export_latest.json"))
        latest_import = load_json(self._integration_file("netease_import_latest.json"))
        return {
            "generated_at": now_label(),
            "spotify": {
                "configured": spotify_ready,
                "connected": bool(spotify_token.get("access_token")),
                "client_id_present": bool(env.get("SPOTIFY_CLIENT_ID")),
                "client_secret_present": bool(env.get("SPOTIFY_CLIENT_SECRET")),
                "mode": "playlist_export" if spotify_ready else "draft_only",
                "connect_path": SPOTIFY_CONNECT_PATH,
                "callback_path": SPOTIFY_CALLBACK_PATH,
                "latest_export": latest_export or None,
            },
            "netease": {
                "import_supported": True,
                "mode": "link_draft",
                "latest_import": latest_import or None,
            },
        }

    def _spotify_redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.args.port}{SPOTIFY_CALLBACK_PATH}"

    def _spotify_token_payload(self) -> dict[str, object]:
        return load_json(self._integration_file("spotify_token.json"))

    def _write_spotify_token_payload(self, payload: dict[str, object]) -> None:
        write_json(self._integration_file("spotify_token.json"), payload)

    def _spotify_authorize_url(self) -> str:
        env = self._integration_env()
        client_id = env.get("SPOTIFY_CLIENT_ID") or ""
        redirect_uri = quote(self._spotify_redirect_uri(), safe="")
        scope = quote("playlist-modify-private playlist-modify-public user-read-email user-read-private", safe="")
        return (
            "https://accounts.spotify.com/authorize"
            f"?client_id={quote(client_id, safe='')}"
            "&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&scope={scope}"
            "&show_dialog=true"
        )

    def _spotify_exchange_code(self, code: str) -> dict[str, object]:
        env = self._integration_env()
        client_id = env.get("SPOTIFY_CLIENT_ID") or ""
        client_secret = env.get("SPOTIFY_CLIENT_SECRET") or ""
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        body = (
            f"grant_type=authorization_code&code={quote(code, safe='')}"
            f"&redirect_uri={quote(self._spotify_redirect_uri(), safe='')}"
        ).encode("utf-8")
        token = http_json(
            "https://accounts.spotify.com/api/token",
            method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        token["saved_at"] = now_label()
        return token

    def _spotify_connected_headers(self) -> dict[str, str]:
        token = self._spotify_token_payload()
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Spotify is not connected yet.")
        return {"Authorization": f"Bearer {access_token}"}

    def _spotify_connect_payload(self) -> tuple[dict[str, object], int]:
        env = self._integration_env()
        if not env.get("SPOTIFY_CLIENT_ID") or not env.get("SPOTIFY_CLIENT_SECRET"):
            return {
                "error": "Spotify credentials are missing.",
                "redirect_uri": self._spotify_redirect_uri(),
            }, HTTPStatus.BAD_REQUEST
        return {
            "authorize_url": self._spotify_authorize_url(),
            "redirect_uri": self._spotify_redirect_uri(),
        }, HTTPStatus.OK

    def _spotify_callback_payload(self, query: dict[str, list[str]]) -> tuple[str, int, str]:
        code = str((query.get("code") or [""])[0]).strip()
        error = str((query.get("error") or [""])[0]).strip()
        if error:
            body = f"<html><body><h1>Spotify connection failed</h1><p>{error}</p></body></html>"
            return body, HTTPStatus.BAD_REQUEST, "text/html; charset=utf-8"
        if not code:
            body = "<html><body><h1>Spotify connection failed</h1><p>Missing authorization code.</p></body></html>"
            return body, HTTPStatus.BAD_REQUEST, "text/html; charset=utf-8"
        token = self._spotify_exchange_code(code)
        profile = http_json("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {token['access_token']}"})
        token["profile"] = {
            "id": profile.get("id"),
            "display_name": profile.get("display_name"),
            "email": profile.get("email"),
        }
        self._write_spotify_token_payload(token)
        body = (
            "<html><body><h1>Spotify connected</h1>"
            "<p>You can close this tab and go back to Audio Lab.</p>"
            "</body></html>"
        )
        return body, HTTPStatus.OK, "text/html; charset=utf-8"

    def _spotify_export(self, payload: dict[str, object]) -> tuple[dict[str, object], int]:
        try:
            requested_ids = [str(item).strip() for item in (payload.get("entry_ids") or []) if str(item).strip()]
        except Exception:
            requested_ids = []
        entries = self._latest_catalog_entries().get("entries") or []
        if requested_ids:
            selected = [entry for entry in entries if str(entry.get("entry_id") or "") in requested_ids]
        else:
            selected = entries[:12]
        if not selected:
            return {"error": "No catalog entries were selected for Spotify export."}, HTTPStatus.BAD_REQUEST
        env = self._integration_env()
        spotify_ready = bool(env.get("SPOTIFY_CLIENT_ID")) and bool(env.get("SPOTIFY_CLIENT_SECRET"))
        playlist_name = str(payload.get("playlist_name") or f"Audio Lab Sampler {datetime.now().strftime('%Y-%m-%d')}")
        export_items: list[dict[str, object]] = []
        for entry in selected:
            artist = str(entry.get("artist") or "").strip()
            album = str(entry.get("album") or "").strip()
            query = " ".join(part for part in (artist, album) if part)
            export_items.append(
                {
                    "entry_id": entry.get("entry_id"),
                    "artist": artist,
                    "album": album,
                    "search_query": query,
                    "spotify_search_url": f"https://open.spotify.com/search/{quote(query)}",
                    "cover_url": entry.get("cover_url"),
                    "site_score": ((entry.get("site_score") or {}).get("score_0_to_10")),
                }
            )
        export_record = {
            "generated_at": now_label(),
            "mode": "playlist_export" if spotify_ready else "draft_only",
            "configured": spotify_ready,
            "playlist_name": playlist_name,
            "description": str(
                payload.get("description")
                or "Prepared from the Audio Lab archive. Search links are included for track-by-track matching."
            ),
            "entry_count": len(export_items),
            "items": export_items,
        }
        if spotify_ready:
            token = self._spotify_token_payload()
            if not token.get("access_token"):
                export_record["authorize_url"] = self._spotify_authorize_url()
                export_record["note"] = "Spotify is configured but not connected yet. Authorize first, then export again."
                write_json(self._integration_file("spotify_export_latest.json"), export_record)
                return export_record, HTTPStatus.ACCEPTED
        write_json(self._integration_file("spotify_export_latest.json"), export_record)
        status = HTTPStatus.OK if spotify_ready else HTTPStatus.ACCEPTED
        if not spotify_ready:
            export_record["note"] = "Spotify credentials are missing, so this export is a prepared draft with search links."
            return export_record, status
        try:
            me = http_json("https://api.spotify.com/v1/me", headers=self._spotify_connected_headers())
            user_id = str(me.get("id") or "").strip()
            if not user_id:
                raise RuntimeError("Spotify account profile did not include a user id.")
            playlist = http_json(
                f"https://api.spotify.com/v1/users/{quote(user_id, safe='')}/playlists",
                method="POST",
                headers={**self._spotify_connected_headers(), "Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "name": playlist_name,
                        "description": export_record["description"],
                        "public": False,
                    }
                ).encode("utf-8"),
            )
            playlist_id = str(playlist.get("id") or "").strip()
            playlist_url = (((playlist.get("external_urls") or {}).get("spotify")) or "")
            uris: list[str] = []
            matched_items: list[dict[str, object]] = []
            for item in export_items:
                query = quote(str(item["search_query"]), safe="")
                search = http_json(
                    f"https://api.spotify.com/v1/search?q={query}&type=album&limit=1",
                    headers=self._spotify_connected_headers(),
                )
                album_items = (((search.get("albums") or {}).get("items")) or [])
                if not album_items:
                    matched_items.append({**item, "matched": False})
                    continue
                album = album_items[0]
                album_id = str(album.get("id") or "").strip()
                tracks = http_json(
                    f"https://api.spotify.com/v1/albums/{quote(album_id, safe='')}/tracks?limit=10",
                    headers=self._spotify_connected_headers(),
                )
                track_uris = [str(track.get("uri") or "").strip() for track in (tracks.get("items") or []) if str(track.get("uri") or "").strip()]
                uris.extend(track_uris[: min(3, len(track_uris))])
                matched_items.append(
                    {
                        **item,
                        "matched": True,
                        "spotify_album_name": album.get("name"),
                        "spotify_album_url": ((album.get("external_urls") or {}).get("spotify")),
                        "added_track_count": min(3, len(track_uris)),
                    }
                )
            if uris and playlist_id:
                http_json(
                    f"https://api.spotify.com/v1/playlists/{quote(playlist_id, safe='')}/tracks",
                    method="POST",
                    headers={**self._spotify_connected_headers(), "Content-Type": "application/json"},
                    body=json.dumps({"uris": uris}).encode("utf-8"),
                )
            export_record.update(
                {
                    "mode": "playlist_created",
                    "playlist_id": playlist_id,
                    "playlist_url": playlist_url,
                    "matched_items": matched_items,
                    "added_track_count": len(uris),
                }
            )
            write_json(self._integration_file("spotify_export_latest.json"), export_record)
            return export_record, HTTPStatus.OK
        except Exception as exc:  # noqa: BLE001
            export_record["note"] = f"Spotify playlist creation failed: {exc}"
            write_json(self._integration_file("spotify_export_latest.json"), export_record)
            return export_record, HTTPStatus.ACCEPTED

    def _netease_import(self, payload: dict[str, object]) -> tuple[dict[str, object], int]:
        raw_url = str(payload.get("url") or "").strip()
        if not raw_url:
            return {"error": "A public NetEase Music URL is required."}, HTTPStatus.BAD_REQUEST
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query or "")
        fragment_query = parse_qs((parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""))
        identifier = str((query.get("id") or fragment_query.get("id") or [""])[0]).strip()
        kind = "unknown"
        text = f"{parsed.path} {parsed.fragment}".lower()
        if "playlist" in text:
            kind = "playlist"
        elif "album" in text:
            kind = "album"
        elif "song" in text:
            kind = "song"
        draft = {
            "generated_at": now_label(),
            "input_url": raw_url,
            "parsed": {
                "host": parsed.netloc,
                "path": parsed.path,
                "fragment": parsed.fragment,
                "kind": kind,
                "id": identifier or None,
            },
            "status": "drafted" if identifier else "needs_review",
            "note": "This first version stores the public link and parsed target so it can be matched into the archive or exported later.",
        }
        write_json(self._integration_file("netease_import_latest.json"), draft)
        return draft, HTTPStatus.ACCEPTED

    def shutdown(self) -> None:
        with self.condition:
            self.should_exit = True
            self.condition.notify_all()
        self.worker.join(timeout=5.0)

    def _persist_state(self) -> None:
        payload = self.snapshot()
        write_json(self.state_path, payload)

    def snapshot(self) -> dict[str, object]:
        next_auto_request = self._preview_auto_request()
        return {
            "service": {
                "started_at": self.started_at,
                "output_root": str(self.output_root),
                "current_dir": str(self.current_dir),
                "auto_loop_enabled": self.auto_loop_enabled,
                "render_active": self.render_active,
                "queued_manual_requests": len(self.queue),
                "completed_runs": self.completed_runs,
                "manual_runs": self.manual_runs,
                "failed_runs": self.failed_runs,
                "max_runs": self.args.max_runs,
                "run_cooldown_seconds": self.args.run_cooldown,
                "runtime_profile": self.args.runtime_profile,
                "runtime_profile_description": str(RUNTIME_PROFILES[self.args.runtime_profile]["description"]),
                "available_runtime_profiles": list(RUNTIME_PROFILES.keys()),
                "available_bundles": list(CURATED_BUNDLES),
                "next_auto_request": next_auto_request,
                "default_request": self.default_request,
                "default_rotation": self._rotation_mode_label(),
                "paths": {
                    "live_page": LIVE_PAGE_PATH,
                    "archive_page": ARCHIVE_PAGE_PATH,
                    "entry_page": ENTRY_PAGE_PATH,
                    "artist_page": ARTIST_PAGE_PATH,
                    "status": STATUS_PATH,
                    "latest": LATEST_PATH,
                    "latest_audio": LATEST_AUDIO_PATH,
                    "latest_summary": LATEST_SUMMARY_PATH,
                    "latest_manifest": LATEST_MANIFEST_PATH,
                    "latest_style_discovery": LATEST_STYLE_DISCOVERY_PATH,
                    "latest_log": LATEST_LOG_PATH,
                    "latest_review": LATEST_REVIEW_PATH,
                    "latest_credibility": LATEST_CREDIBILITY_PATH,
                    "latest_professional": LATEST_PROFESSIONAL_PATH,
                    "latest_catalog_candidates": LATEST_CATALOG_CANDIDATES_PATH,
                    "latest_catalog_seeds": LATEST_CATALOG_SEEDS_PATH,
                    "latest_catalog_entries": LATEST_CATALOG_ENTRIES_PATH,
                "latest_catalog_library": LATEST_CATALOG_LIBRARY_PATH,
                "latest_professional_digest": LATEST_PROFESSIONAL_DIGEST_PATH,
                "latest_catalog_rollup_status": LATEST_CATALOG_ROLLUP_STATUS_PATH,
                "integrations_status": INTEGRATIONS_STATUS_PATH,
                "spotify_export": SPOTIFY_EXPORT_PATH,
                "netease_import": NETEASE_IMPORT_PATH,
                "contract": CONTRACT_PATH,
                "render": RENDER_PATH,
                "start": CONTROL_START_PATH,
                    "stop": CONTROL_STOP_PATH,
                },
            },
            "active_request": self.active_request,
            "active_process_id": self.active_process_id,
            "queue_preview": list(self.queue)[:5],
            "recent_events": list(self.recent_events),
            "latest_run": self.latest_run,
            "last_error": self.last_error,
        }

    def _rotation_mode_label(self) -> str:
        if self.default_request.get("preset_bundle") or self.default_request.get("style"):
            return "fixed"
        return self.args.runtime_profile

    def _current_artifact_path(self, name: str) -> Path:
        return self.current_dir / name

    def _latest_run_dirs(self) -> list[Path]:
        try:
            return sorted(
                [path for path in self.runs_dir.iterdir() if path.is_dir()],
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
        except OSError:
            return []

    def _discover_latest_session_dir(self) -> Path | None:
        for session_dir in self._latest_run_dirs():
            if (session_dir / "summary.json").exists() or (session_dir / "latest_mix.wav").exists():
                return session_dir
        return None

    def _resolve_latest_record(self, latest: dict[str, object] | None = None) -> dict[str, object]:
        record = dict(latest or {})
        session_dir_value = record.get("session_dir")
        session_dir = Path(str(session_dir_value)) if session_dir_value else None
        if (session_dir is None or not session_dir.exists()) and record.get("run_id"):
            candidate = self.runs_dir / str(record["run_id"])
            if candidate.exists():
                session_dir = candidate
        if session_dir is None or not session_dir.exists():
            session_dir = self._discover_latest_session_dir()

        current_defaults = {
            "summary_path": self._current_artifact_path("summary.json"),
            "manifest_path": self._current_artifact_path("session_manifest.json"),
            "style_discovery_path": self._current_artifact_path("style_discovery.json"),
            "log_path": self._current_artifact_path("run.log"),
            "latest_mix_path": self._current_artifact_path("latest_mix.wav"),
        }
        session_defaults = {
            "summary_path": "summary.json",
            "manifest_path": "session_manifest.json",
            "style_discovery_path": "style_discovery.json",
            "log_path": "run.log",
            "latest_mix_path": "latest_mix.wav",
        }

        for key, current_path in current_defaults.items():
            path_value = record.get(key)
            path = Path(str(path_value)) if path_value else None
            if path and path.exists():
                continue
            if current_path.exists():
                record[key] = str(current_path)
                continue
            if session_dir is not None:
                session_path = session_dir / session_defaults[key]
                if session_path.exists():
                    record[key] = str(session_path)

        if session_dir is not None:
            record.setdefault("run_id", session_dir.name)
            record.setdefault("session_dir", str(session_dir))
            record.setdefault("session_dir_relative", relative_to_root(session_dir))
            request_path = session_dir / "request.json"
            if request_path.exists():
                record.setdefault("request_path", str(request_path))
                request = load_json(request_path)
                if request:
                    record.setdefault("request", request)
                    record.setdefault("request_id", request.get("request_id"))
                    record.setdefault("request_kind", request.get("request_kind"))
                    record.setdefault("client_name", request.get("client_name"))
                    record.setdefault("request_label", request.get("request_label"))
                    record.setdefault("note", request.get("note"))

        record.setdefault("published_current_dir", str(self.current_dir))
        record.setdefault(
            "api_paths",
            {
                "latest": LATEST_PATH,
                "audio": LATEST_AUDIO_PATH,
                "summary": LATEST_SUMMARY_PATH,
                "manifest": LATEST_MANIFEST_PATH,
                "review": LATEST_REVIEW_PATH,
                "credibility": LATEST_CREDIBILITY_PATH,
                "catalog_candidates": LATEST_CATALOG_CANDIDATES_PATH,
                "catalog_seeds": LATEST_CATALOG_SEEDS_PATH,
                "catalog_entries": LATEST_CATALOG_ENTRIES_PATH,
                "catalog_entries_lite": LATEST_CATALOG_ENTRIES_LITE_PATH,
                "catalog_entry": LATEST_CATALOG_ENTRY_PATH,
                "catalog_library": LATEST_CATALOG_LIBRARY_PATH,
                "catalog_rollup_status": LATEST_CATALOG_ROLLUP_STATUS_PATH,
                "artist_page": ARTIST_PAGE_PATH,
            },
        )
        return record

    def _recover_latest_run(self) -> dict[str, object]:
        latest = self._resolve_latest_record(load_json(self.current_dir / "latest_run.json"))
        summary = self._latest_summary(latest)
        if summary:
            latest.setdefault("style", summary.get("style"))
            latest.setdefault("style_components", summary.get("style_components"))
            latest.setdefault("preset_bundle", summary.get("preset_bundle"))
            latest.setdefault("segment_count", summary.get("segment_count"))
            latest.setdefault("quality_notes", summary.get("quality_notes"))
            latest.setdefault("next_experiments", summary.get("next_experiments"))
            latest.setdefault(
                "summary_excerpt",
                {
                    "style": summary.get("style"),
                    "preset_bundle": summary.get("preset_bundle"),
                    "segment_count": summary.get("segment_count"),
                    "latest_mix": summary.get("latest_mix"),
                    "voice_mode": summary.get("voice_mode"),
                    "master_bus": summary.get("master_bus"),
                },
            )
        return latest

    def _latest_summary(self, latest: dict[str, object] | None = None) -> dict[str, object]:
        record = self._resolve_latest_record(latest or self.latest_run)
        if record.get("summary_path"):
            return load_json(Path(str(record["summary_path"])))
        return {}

    def _latest_manifest(self, latest: dict[str, object] | None = None) -> dict[str, object]:
        record = self._resolve_latest_record(latest or self.latest_run)
        if record.get("manifest_path"):
            return load_json(Path(str(record["manifest_path"])))
        return {}

    def _latest_review(self, latest: dict[str, object] | None = None, summary: dict[str, object] | None = None) -> dict[str, object]:
        record = self._resolve_latest_record(latest or self.latest_run)
        return derive_review_from_summary(summary or self._latest_summary(record), record)

    def _latest_credibility(self) -> dict[str, object]:
        return load_json(self.current_dir / "credibility_snapshot.json")

    def _latest_professional_bundle(self) -> dict[str, object]:
        return load_json(self.current_dir / "professional_reviews.json")

    def _latest_catalog_candidates(self) -> dict[str, object]:
        return load_json(self.current_dir / "catalog_candidates.json")

    def _latest_catalog_seeds(self) -> dict[str, object]:
        return load_json(self.current_dir / "catalog_seeds.json")

    def _latest_catalog_entries(self) -> dict[str, object]:
        return load_json(self.current_dir / "catalog_entries.json")

    def _latest_catalog_entries_lite(self) -> dict[str, object]:
        payload = self._latest_catalog_entries()
        payload["entries"] = [compact_entry_payload(entry) for entry in (payload.get("entries") or [])]
        return payload

    def _latest_catalog_entries_page(
        self,
        *,
        offset: int = 0,
        limit: int = 24,
        q: str = "",
        classification: str = "",
        era: str = "",
        scene: str = "",
        artist: str = "",
        discovery: str = "",
    ) -> dict[str, object]:
        payload = self._latest_catalog_entries()
        entries = payload.get("entries") or []
        query = str(q or "").strip().lower()
        classification = str(classification or "").strip().lower()
        era = str(era or "").strip()
        scene = str(scene or "").strip().lower()
        artist = str(artist or "").strip().lower()
        discovery = str(discovery or "").strip().lower()

        def matches(entry: dict[str, object]) -> bool:
            if classification and classification != "all":
                if str(((entry.get("classification") or {}).get("key") or "")).strip().lower() != classification:
                    return False
            if era and era != "all":
                if str(entry.get("era_tag") or "").strip() != era:
                    return False
            if scene and scene != "all":
                scene_tags = [str(item or "").strip().lower() for item in (entry.get("scene_tags") or [])]
                if scene not in scene_tags:
                    return False
            if artist:
                if str(entry.get("artist") or "").strip().lower() != artist:
                    return False
            if discovery:
                seed_sources = [str(item or "").strip().lower() for item in (entry.get("seed_sources") or [])]
                discovery_signals = entry.get("discovery_signals") or {}
                if discovery == "social":
                    if "social-experimental-discovery" not in seed_sources:
                        return False
                elif discovery == "indie-social":
                    if "social-experimental-discovery" not in seed_sources:
                        return False
                    if not bool(discovery_signals.get("indie_creator_likely")):
                        return False
            if query:
                listener_tags = (((entry.get("artist_context") or {}).get("listener_context") or {}).get("tags") or [])
                haystack = " ".join(
                    [
                        str(entry.get("artist") or ""),
                        str(entry.get("album") or ""),
                        str(entry.get("year") or ""),
                        str(entry.get("era_tag") or ""),
                        " ".join(str(item or "") for item in (entry.get("scene_tags") or [])),
                        " ".join(str(item or "") for item in listener_tags),
                    ]
                ).lower()
                if query not in haystack:
                    return False
            return True

        filtered = [entry for entry in entries if matches(entry)]
        total_count = len(filtered)
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 200))
        page_entries = filtered[safe_offset : safe_offset + safe_limit]
        return {
            "generated_at": now_label(),
            "built_count": payload.get("built_count", len(entries)),
            "offset": safe_offset,
            "limit": safe_limit,
            "returned_count": len(page_entries),
            "total_count": total_count,
            "entries": [compact_entry_payload(entry) for entry in page_entries],
        }

    def _latest_catalog_entry(self, entry_id: str) -> dict[str, object]:
        for entry in (self._latest_catalog_entries().get("entries") or []):
            if str(entry.get("entry_id") or "") == entry_id:
                return entry
        return {}

    def _latest_catalog_library(self) -> dict[str, object]:
        return load_json(self.current_dir / "catalog_library.json")

    def _latest_professional_digest(self) -> dict[str, object]:
        return load_json(self.current_dir / "professional_source_digest.json")

    def _latest_catalog_rollup_status(self) -> dict[str, object]:
        return load_json(self.current_dir / "catalog_rollup_status.json")

    def _record_event(self, event: str, details: dict[str, object] | None = None) -> None:
        payload = {"time": now_label(), "event": event}
        if details:
            payload["details"] = details
        self.recent_events.appendleft(payload)

    def _preview_auto_request(self) -> dict[str, object] | None:
        if not self.auto_loop_enabled and not self.render_active and self.args.max_runs is not None and self.completed_runs >= self.args.max_runs:
            return None
        return self._build_auto_request(preview_only=True)

    def set_auto_loop(self, enabled: bool) -> dict[str, object]:
        with self.condition:
            self.auto_loop_enabled = enabled
            self._record_event("auto-loop-enabled" if enabled else "auto-loop-disabled")
            self.condition.notify_all()
            self._persist_state()
            return self.snapshot()

    def enqueue_render(self, payload: dict[str, object] | None) -> dict[str, object]:
        request = self._normalize_request(payload or {})
        request["request_id"] = f"manual-{int(time.time() * 1000)}"
        request["request_kind"] = "manual"
        request["queued_at"] = now_label()
        with self.condition:
            self.queue.append(request)
            self._record_event(
                "manual-request-queued",
                {
                    "request_id": request["request_id"],
                    "preset_bundle": request.get("preset_bundle"),
                    "style": request.get("style"),
                    "client_name": request.get("client_name"),
                },
            )
            self.condition.notify_all()
            self._persist_state()
            return {
                "accepted": True,
                "request_id": request["request_id"],
                "queue_length": len(self.queue),
                "request": request,
            }

    def latest_payload(self) -> dict[str, object]:
        latest = self._resolve_latest_record(self.latest_run)
        if latest:
            summary = self._latest_summary(latest)
            latest["summary"] = summary
            latest["manifest"] = self._latest_manifest(latest)
            latest["review"] = self._latest_review(latest, summary)
            latest["credibility"] = self._latest_credibility()
            latest["catalog_candidates"] = self._latest_catalog_candidates()
            latest["catalog_seeds"] = self._latest_catalog_seeds()
            latest["catalog_entries"] = self._latest_catalog_entries()
            latest["catalog_library"] = self._latest_catalog_library()
            latest["professional_digest"] = self._latest_professional_digest()
            latest["catalog_rollup_status"] = self._latest_catalog_rollup_status()
            latest["integrations"] = self._integration_status()
        return {
            "status": self.snapshot(),
            "latest": latest,
        }

    def contract_payload(self) -> dict[str, object]:
        return {
            "service": {
                "runtime_profile": self.args.runtime_profile,
                "runtime_profile_description": str(RUNTIME_PROFILES[self.args.runtime_profile]["description"]),
                "default_request": self.default_request,
                "next_auto_request": self._preview_auto_request(),
            },
            "request_schema": {
                "optional_fields": [
                    "preset_bundle",
                    "style",
                    "duration",
                    "target_length",
                    "sample_rate",
                    "seed",
                    "download_count",
                    "instrument_download_count",
                    "query",
                    "instrument_query",
                    "spoken_text",
                    "voice_name",
                    "voice_mode",
                    "voice_effect",
                    "density_curve",
                    "groove_template",
                    "master_bus",
                    "export_stems",
                    "with_downloads",
                    "no_voice",
                    "ignore_bundle_target_length",
                    "client_name",
                    "request_label",
                    "note",
                ],
                "enums": {
                    "preset_bundle": list(CURATED_BUNDLES),
                    "target_length": ["60", "90", "120", "180"],
                    "randomness_preset": ["controlled", "balanced", "chaotic"],
                },
            },
            "endpoints": {
                "status": STATUS_PATH,
                "latest": LATEST_PATH,
                "latest_audio": LATEST_AUDIO_PATH,
                "latest_summary": LATEST_SUMMARY_PATH,
                "latest_manifest": LATEST_MANIFEST_PATH,
                "latest_style_discovery": LATEST_STYLE_DISCOVERY_PATH,
                "latest_log": LATEST_LOG_PATH,
                "latest_review": LATEST_REVIEW_PATH,
                "latest_credibility": LATEST_CREDIBILITY_PATH,
                "latest_professional": LATEST_PROFESSIONAL_PATH,
                "latest_catalog_candidates": LATEST_CATALOG_CANDIDATES_PATH,
                "latest_catalog_seeds": LATEST_CATALOG_SEEDS_PATH,
                "latest_catalog_entries": LATEST_CATALOG_ENTRIES_PATH,
                "latest_professional_digest": LATEST_PROFESSIONAL_DIGEST_PATH,
                "integrations_status": INTEGRATIONS_STATUS_PATH,
                "spotify_export": SPOTIFY_EXPORT_PATH,
                "netease_import": NETEASE_IMPORT_PATH,
                "render": RENDER_PATH,
                "start": CONTROL_START_PATH,
                "stop": CONTROL_STOP_PATH,
                "live_page": LIVE_PAGE_PATH,
                "archive_page": ARCHIVE_PAGE_PATH,
                "entry_page": ENTRY_PAGE_PATH,
            },
            "artifacts": {
                "current_dir": str(self.current_dir),
                "latest_run": self._resolve_latest_record(self.latest_run),
            },
        }

    def latest_file(self, kind: str) -> tuple[Path | None, str]:
        latest = self._resolve_latest_record(self.latest_run)
        if kind == "audio":
            return (Path(str(latest["latest_mix_path"])), "audio/wav") if latest.get("latest_mix_path") else (None, "audio/wav")
        if kind == "summary":
            return (Path(str(latest["summary_path"])), "application/json; charset=utf-8") if latest.get("summary_path") else (None, "application/json; charset=utf-8")
        if kind == "manifest":
            return (Path(str(latest["manifest_path"])), "application/json; charset=utf-8") if latest.get("manifest_path") else (None, "application/json; charset=utf-8")
        if kind == "style_discovery":
            return (
                (Path(str(latest["style_discovery_path"])), "application/json; charset=utf-8")
                if latest.get("style_discovery_path")
                else (None, "application/json; charset=utf-8")
            )
        if kind == "log":
            return (Path(str(latest["log_path"])), "text/plain; charset=utf-8") if latest.get("log_path") else (None, "text/plain; charset=utf-8")
        return None, "application/octet-stream"

    def _worker_loop(self) -> None:
        while True:
            with self.condition:
                while not self.should_exit and not self.queue and not self._auto_loop_ready():
                    self.condition.wait(timeout=1.0)
                if self.should_exit:
                    return
                if self.queue:
                    request = self.queue.popleft()
                else:
                    request = self._build_auto_request()
                self.render_active = True
                self.active_request = request
                self.active_process_id = None
                self._persist_state()

            try:
                self._execute_request(request)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.failed_runs += 1
                self._record_event(
                    "render-failed",
                    {
                        "request_id": request.get("request_id"),
                        "request_kind": request.get("request_kind"),
                        "error": self.last_error,
                    },
                )
            finally:
                with self.condition:
                    self.render_active = False
                    self.active_request = None
                    self.active_process_id = None
                    self._persist_state()

            if self.should_exit:
                return
            if self.args.run_cooldown > 0:
                time.sleep(self.args.run_cooldown)

    def _auto_loop_ready(self) -> bool:
        if not self.auto_loop_enabled:
            return False
        if self.args.max_runs is None:
            return True
        return self.completed_runs < self.args.max_runs

    def _build_auto_request(self, preview_only: bool = False) -> dict[str, object]:
        request = self._normalize_request({})
        request["request_id"] = f"auto-{int(time.time() * 1000)}"
        request["request_kind"] = "auto"
        request["queued_at"] = now_label()
        if not request.get("preset_bundle") and not request.get("style"):
            profile = RUNTIME_PROFILES[self.args.runtime_profile]
            sequence = profile.get("sequence") or ()
            if sequence:
                rotation = dict(sequence[self.completed_runs % len(sequence)])
            else:
                bundle_name = CURATED_BUNDLES[self.completed_runs % len(CURATED_BUNDLES)]
                rotation = {"preset_bundle": bundle_name}
            request.update(rotation)
            request["rotation_note"] = (
                f"No bundle or style was pinned, so the live service used runtime profile '{self.args.runtime_profile}'."
            )
        if preview_only:
            request["request_id"] = "next-auto-preview"
        return request

    def _normalize_request(self, payload: dict[str, object]) -> dict[str, object]:
        request = dict(self.default_request)
        if "preset_bundle" in payload:
            request["preset_bundle"] = payload.get("preset_bundle") or None
        if "style" in payload:
            request["style"] = normalize_listish(payload.get("style"))
        if "randomness_preset" in payload:
            request["randomness_preset"] = payload.get("randomness_preset") or None
        if "duration" in payload:
            request["duration"] = normalize_float(payload.get("duration"), request.get("duration"))
        if "target_length" in payload:
            target_length = payload.get("target_length")
            request["target_length"] = None if target_length in (None, "", 0, "0") else str(int(float(target_length)))
        if "sample_rate" in payload:
            request["sample_rate"] = normalize_int(payload.get("sample_rate"), request.get("sample_rate"))
        if "seed" in payload:
            request["seed"] = normalize_int(payload.get("seed"), request.get("seed"))
        if "download_count" in payload:
            request["download_count"] = max(0, normalize_int(payload.get("download_count"), request.get("download_count")) or 0)
        if "instrument_download_count" in payload:
            request["instrument_download_count"] = max(
                0,
                normalize_int(payload.get("instrument_download_count"), request.get("instrument_download_count")) or 0,
            )
        if "query" in payload:
            request["query"] = normalize_listish(payload.get("query"))
        if "instrument_query" in payload:
            request["instrument_query"] = normalize_listish(payload.get("instrument_query"))
        if "spoken_text" in payload:
            request["spoken_text"] = normalize_listish(payload.get("spoken_text"))
        if "voice_name" in payload:
            request["voice_name"] = normalize_listish(payload.get("voice_name"))
        if "voice_mode" in payload:
            request["voice_mode"] = payload.get("voice_mode") or None
        if "voice_effect" in payload:
            request["voice_effect"] = payload.get("voice_effect") or None
        if "density_curve" in payload:
            request["density_curve"] = payload.get("density_curve") or None
        if "groove_template" in payload:
            request["groove_template"] = payload.get("groove_template") or None
        if "master_bus" in payload:
            request["master_bus"] = payload.get("master_bus") or None
        if "export_stems" in payload:
            request["export_stems"] = normalize_bool(payload.get("export_stems"), bool(request.get("export_stems")))
        if "with_downloads" in payload:
            request["with_downloads"] = normalize_bool(payload.get("with_downloads"), bool(request.get("with_downloads")))
        if "no_voice" in payload:
            request["no_voice"] = normalize_bool(payload.get("no_voice"), bool(request.get("no_voice")))
        if "ignore_bundle_target_length" in payload:
            request["ignore_bundle_target_length"] = normalize_bool(
                payload.get("ignore_bundle_target_length"),
                bool(request.get("ignore_bundle_target_length")),
            )
        if "client_name" in payload:
            request["client_name"] = normalize_string(payload.get("client_name"))
        if "request_label" in payload:
            request["request_label"] = normalize_string(payload.get("request_label"))
        if "note" in payload:
            request["note"] = normalize_string(payload.get("note"))
        return request

    def _build_command(self, request: dict[str, object], session_dir: Path) -> list[str]:
        run_number = self.completed_runs + self.failed_runs + 1
        seed = int(request.get("seed") or 71) + run_number * 1009
        command = [
            "python3",
            str(GENERATOR),
            "--output-dir",
            str(session_dir),
            "--duration",
            str(float(request.get("duration") or 16.0)),
            "--sample-rate",
            str(int(request.get("sample_rate") or 16_000)),
            "--seed",
            str(seed),
            "--download-count",
            str(int(request.get("download_count") or 0)),
            "--instrument-download-count",
            str(int(request.get("instrument_download_count") or 0)),
        ]
        if request.get("preset_bundle"):
            command.extend(["--preset-bundle", str(request["preset_bundle"])])
        if request.get("target_length"):
            command.extend(["--target-length", str(request["target_length"])])
        for value in normalize_listish(request.get("style")):
            command.extend(["--style", value])
        for value in normalize_listish(request.get("query")):
            command.extend(["--query", value])
        for value in normalize_listish(request.get("instrument_query")):
            command.extend(["--instrument-query", value])
        for value in normalize_listish(request.get("spoken_text")):
            command.extend(["--spoken-text", value])
        for value in normalize_listish(request.get("voice_name")):
            command.extend(["--voice-name", value])
        for flag_name in ("randomness_preset", "voice_mode", "voice_effect", "density_curve", "groove_template", "master_bus"):
            flag_value = request.get(flag_name)
            if flag_value:
                command.extend([f"--{flag_name.replace('_', '-')}", str(flag_value)])
        if normalize_bool(request.get("export_stems")):
            command.append("--export-stems")
        if not normalize_bool(request.get("with_downloads")):
            command.append("--no-downloads")
        if normalize_bool(request.get("no_voice")):
            command.append("--no-voice")
        if normalize_bool(request.get("ignore_bundle_target_length")):
            command.append("--ignore-bundle-target-length")
        return command

    def _execute_request(self, request: dict[str, object]) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_number = self.completed_runs + self.failed_runs + 1
        session_dir = ensure_dir(self.runs_dir / f"run-{run_number:04d}-{stamp}")
        request_path = session_dir / "request.json"
        write_json(request_path, request)
        command = self._build_command(request, session_dir)
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        with self.condition:
            self.active_process_id = process.pid
            self._record_event(
                "render-started",
                {
                    "request_id": request.get("request_id"),
                    "request_kind": request.get("request_kind"),
                    "preset_bundle": request.get("preset_bundle"),
                    "client_name": request.get("client_name"),
                },
            )
            self._persist_state()
        output_chunks: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output_chunks.append(line)
        return_code = process.wait()
        log_path = session_dir / "run.log"
        log_path.write_text("".join(output_chunks), encoding="utf-8")
        if return_code != 0:
            raise RuntimeError(f"Audio render failed with exit code {return_code}. See {log_path}")

        summary_path = session_dir / "summary.json"
        manifest_path = session_dir / "session_manifest.json"
        style_discovery_path = session_dir / "style_discovery.json"
        latest_mix_path = session_dir / "latest_mix.wav"
        summary = load_json(summary_path)
        manifest = load_json(manifest_path)

        self._publish_current_artifacts(
            latest_mix_path=latest_mix_path,
            summary_path=summary_path,
            manifest_path=manifest_path,
            style_discovery_path=style_discovery_path,
            log_path=log_path,
            stems_dir=session_dir / "stems",
        )

        latest_record = {
            "run_id": session_dir.name,
            "request_id": request.get("request_id"),
            "request_kind": request.get("request_kind"),
            "completed_at": now_label(),
            "session_dir": str(session_dir),
            "session_dir_relative": relative_to_root(session_dir),
            "request_path": str(request_path),
            "log_path": str(log_path),
            "summary_path": str(summary_path),
            "manifest_path": str(manifest_path),
            "style_discovery_path": str(style_discovery_path),
            "latest_mix_path": str(latest_mix_path),
            "published_current_dir": str(self.current_dir),
            "api_paths": {
                "latest": LATEST_PATH,
                "audio": LATEST_AUDIO_PATH,
                "summary": LATEST_SUMMARY_PATH,
                "manifest": LATEST_MANIFEST_PATH,
                "review": LATEST_REVIEW_PATH,
                "live_page": LIVE_PAGE_PATH,
                "archive_page": ARCHIVE_PAGE_PATH,
                "entry_page": ENTRY_PAGE_PATH,
                "artist_page": ARTIST_PAGE_PATH,
                "catalog_rollup_status": LATEST_CATALOG_ROLLUP_STATUS_PATH,
            },
            "style": summary.get("style"),
            "style_components": summary.get("style_components"),
            "preset_bundle": summary.get("preset_bundle"),
            "segment_count": summary.get("segment_count"),
            "quality_notes": summary.get("quality_notes"),
            "next_experiments": summary.get("next_experiments"),
            "summary_excerpt": {
                "style": summary.get("style"),
                "preset_bundle": summary.get("preset_bundle"),
                "segment_count": summary.get("segment_count"),
                "latest_mix": summary.get("latest_mix"),
                "voice_mode": summary.get("voice_mode"),
                "master_bus": summary.get("master_bus"),
            },
            "client_name": request.get("client_name"),
            "request_label": request.get("request_label"),
            "note": request.get("note"),
            "request": request,
        }
        write_json(self.current_dir / "latest_run.json", latest_record)
        self.latest_run = latest_record
        self.last_error = None
        self.completed_runs += 1
        if request.get("request_kind") == "manual":
            self.manual_runs += 1
        self._record_event(
            "render-completed",
            {
                "run_id": session_dir.name,
                "preset_bundle": summary.get("preset_bundle"),
                "style": summary.get("style"),
                "request_kind": request.get("request_kind"),
            },
        )
        if self.args.max_runs is not None and self.completed_runs >= self.args.max_runs:
            self.auto_loop_enabled = False
        self._persist_state()

    def _publish_current_artifacts(
        self,
        *,
        latest_mix_path: Path,
        summary_path: Path,
        manifest_path: Path,
        style_discovery_path: Path,
        log_path: Path,
        stems_dir: Path,
    ) -> None:
        if latest_mix_path.exists():
            atomic_copy(latest_mix_path, self.current_dir / "latest_mix.wav")
        if summary_path.exists():
            atomic_copy(summary_path, self.current_dir / "summary.json")
        if manifest_path.exists():
            atomic_copy(manifest_path, self.current_dir / "session_manifest.json")
        if style_discovery_path.exists():
            atomic_copy(style_discovery_path, self.current_dir / "style_discovery.json")
        if log_path.exists():
            atomic_copy(log_path, self.current_dir / "run.log")
        current_stems_dir = self.current_dir / "stems"
        ensure_dir(current_stems_dir)
        for path in current_stems_dir.glob("*.wav"):
            path.unlink()
        if stems_dir.exists():
            for stem_path in stems_dir.glob("latest_*.wav"):
                atomic_copy(stem_path, current_stems_dir / stem_path.name)


class AudioLabLiveHandler(SimpleHTTPRequestHandler):
    coordinator: AudioLabLiveCoordinator

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == STATUS_PATH:
            self._send_json(self.coordinator.snapshot())
            return
        if parsed.path == LATEST_PATH:
            self._send_json(self.coordinator.latest_payload())
            return
        if parsed.path == LATEST_AUDIO_PATH:
            path, content_type = self.coordinator.latest_file("audio")
            if path is None or not path.exists():
                self._send_json({"error": "No rendered audio is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(path, content_type)
            return
        if parsed.path == LATEST_SUMMARY_PATH:
            path, content_type = self.coordinator.latest_file("summary")
            if path is None or not path.exists():
                self._send_json({"error": "No summary is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(path, content_type)
            return
        if parsed.path == LATEST_MANIFEST_PATH:
            path, content_type = self.coordinator.latest_file("manifest")
            if path is None or not path.exists():
                self._send_json({"error": "No manifest is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(path, content_type)
            return
        if parsed.path == LATEST_STYLE_DISCOVERY_PATH:
            path, content_type = self.coordinator.latest_file("style_discovery")
            if path is None or not path.exists():
                self._send_json({"error": "No style discovery is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(path, content_type)
            return
        if parsed.path == LATEST_LOG_PATH:
            path, content_type = self.coordinator.latest_file("log")
            if path is None or not path.exists():
                self._send_json({"error": "No run log is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(path, content_type)
            return
        if parsed.path == LATEST_REVIEW_PATH:
            review = self.coordinator._latest_review()
            if not review:
                self._send_json({"error": "No review is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(review)
            return
        if parsed.path == LATEST_CREDIBILITY_PATH:
            credibility = self.coordinator._latest_credibility()
            if not credibility:
                self._send_json({"error": "No credibility snapshot is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(credibility)
            return
        if parsed.path == LATEST_PROFESSIONAL_PATH:
            professional = self.coordinator._latest_professional_bundle()
            if not professional:
                self._send_json({"error": "No professional review bundle is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(professional)
            return
        if parsed.path == LATEST_CATALOG_CANDIDATES_PATH:
            candidates = self.coordinator._latest_catalog_candidates()
            if not candidates:
                self._send_json({"error": "No catalog candidates are available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(candidates)
            return
        if parsed.path == LATEST_CATALOG_SEEDS_PATH:
            seeds = self.coordinator._latest_catalog_seeds()
            if not seeds:
                self._send_json({"error": "No catalog seeds are available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(seeds)
            return
        if parsed.path == LATEST_CATALOG_ENTRIES_PATH:
            entries = self.coordinator._latest_catalog_entries()
            if not entries:
                self._send_json({"error": "No catalog entries are available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(entries)
            return
        if parsed.path == LATEST_CATALOG_ENTRIES_LITE_PATH:
            entries = self.coordinator._latest_catalog_entries_lite()
            if not entries:
                self._send_json({"error": "No catalog entries are available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(entries)
            return
        if parsed.path == LATEST_CATALOG_ENTRY_PATH:
            query = parse_qs(parsed.query or "")
            entry_id = str((query.get("entry_id") or [""])[0]).strip()
            if not entry_id:
                self._send_json({"error": "entry_id is required."}, status=HTTPStatus.BAD_REQUEST)
                return
            entry = self.coordinator._latest_catalog_entry(entry_id)
            if not entry:
                self._send_json({"error": "Catalog entry not found."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(entry)
            return
        if parsed.path == LATEST_CATALOG_ENTRIES_PAGE_PATH:
            query = parse_qs(parsed.query or "")
            try:
                offset = int(str((query.get("offset") or ["0"])[0]).strip() or "0")
            except ValueError:
                offset = 0
            try:
                limit = int(str((query.get("limit") or ["24"])[0]).strip() or "24")
            except ValueError:
                limit = 24
            self._send_json(
                self.coordinator._latest_catalog_entries_page(
                    offset=offset,
                    limit=limit,
                    q=str((query.get("q") or [""])[0]),
                    classification=str((query.get("classification") or [""])[0]),
                    era=str((query.get("era") or [""])[0]),
                    scene=str((query.get("scene") or [""])[0]),
                    artist=str((query.get("artist") or [""])[0]),
                    discovery=str((query.get("discovery") or [""])[0]),
                )
            )
            return
        if parsed.path == LATEST_CATALOG_LIBRARY_PATH:
            library = self.coordinator._latest_catalog_library()
            if not library:
                self._send_json({"error": "No catalog library is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(library)
            return
        if parsed.path == LATEST_PROFESSIONAL_DIGEST_PATH:
            digest = self.coordinator._latest_professional_digest()
            if not digest:
                self._send_json({"error": "No professional source digest is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(digest)
            return
        if parsed.path == LATEST_CATALOG_ROLLUP_STATUS_PATH:
            rollup = self.coordinator._latest_catalog_rollup_status()
            if not rollup:
                self._send_json({"error": "No catalog rollup status is available yet."}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(rollup)
            return
        if parsed.path == INTEGRATIONS_STATUS_PATH:
            self._send_json(self.coordinator._integration_status())
            return
        if parsed.path == SPOTIFY_CONNECT_PATH:
            response, status = self.coordinator._spotify_connect_payload()
            self._send_json(response, status=status)
            return
        if parsed.path == SPOTIFY_CALLBACK_PATH:
            body, status, content_type = self.coordinator._spotify_callback_payload(parse_qs(parsed.query or ""))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        if parsed.path == CONTRACT_PATH:
            self._send_json(self.coordinator.contract_payload())
            return
        if parsed.path == LIVE_PAGE_PATH:
            self.path = "/dashboard/audio-lab-live.html"
        if parsed.path == ARCHIVE_PAGE_PATH:
            self.path = "/dashboard/audio-lab-archive.html"
        if parsed.path == ENTRY_PAGE_PATH:
            self.path = "/dashboard/audio-lab-entry.html"
        if parsed.path == ARTIST_PAGE_PATH:
            self.path = "/dashboard/audio-lab-artist.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == CONTROL_START_PATH:
            self._send_json(self.coordinator.set_auto_loop(True))
            return
        if parsed.path == CONTROL_STOP_PATH:
            self._send_json(self.coordinator.set_auto_loop(False))
            return
        if parsed.path == RENDER_PATH:
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"Invalid JSON body: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(self.coordinator.enqueue_render(payload), status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == SPOTIFY_EXPORT_PATH:
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"Invalid JSON body: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            response, status = self.coordinator._spotify_export(payload)
            self._send_json(response, status=status)
            return
        if parsed.path == NETEASE_IMPORT_PATH:
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"Invalid JSON body: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            response, status = self.coordinator._netease_import(payload)
            self._send_json(response, status=status)
            return
        self._send_json({"error": f"Unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        if os.environ.get("AUDIO_LAB_LIVE_SILENT") == "1":
            return
        super().log_message(format, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a live audio-lab service with continuous generation and an HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--duration", type=float, default=12.0, help="Per-run duration in seconds when target-length is not set.")
    parser.add_argument("--target-length", choices=("60", "90", "120", "180"), default=None)
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--preset-bundle", choices=CURATED_BUNDLES, default=None)
    parser.add_argument("--runtime-profile", choices=tuple(RUNTIME_PROFILES.keys()), default="realism-cycle")
    parser.add_argument("--style", action="append", default=[])
    parser.add_argument("--randomness-preset", choices=("controlled", "balanced", "chaotic"), default="balanced")
    parser.add_argument("--download-count", type=int, default=0)
    parser.add_argument("--instrument-download-count", type=int, default=0)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--instrument-query", action="append", default=[])
    parser.add_argument("--spoken-text", action="append", default=[])
    parser.add_argument("--voice-name", action="append", default=[])
    parser.add_argument("--voice-mode", default=None)
    parser.add_argument("--voice-effect", default=None)
    parser.add_argument("--density-curve", default=None)
    parser.add_argument("--groove-template", default=None)
    parser.add_argument("--master-bus", default=None)
    parser.add_argument("--export-stems", action="store_true")
    parser.add_argument("--with-downloads", action="store_true")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--ignore-bundle-target-length", action="store_true")
    parser.add_argument("--max-runs", type=int, default=None, help="Optional cap for automatic live runs. Manual render requests still work.")
    parser.add_argument("--run-cooldown", type=float, default=1.0, help="Seconds to wait between completed runs in live mode.")
    parser.add_argument("--open-browser", action="store_true", help="Open the live page in the default browser on startup.")
    parser.set_defaults(autostart=True)
    parser.add_argument("--autostart", dest="autostart", action="store_true")
    parser.add_argument("--no-autostart", dest="autostart", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    coordinator = AudioLabLiveCoordinator(args)
    handler_class = type("ConfiguredAudioLabLiveHandler", (AudioLabLiveHandler,), {"coordinator": coordinator})
    server = ThreadingHTTPServer((args.host, args.port), handler_class)
    live_url = f"http://{args.host}:{args.port}{LIVE_PAGE_PATH}"
    print(f"Audio Lab Live: {live_url}")
    print(f"Status API: http://{args.host}:{args.port}{STATUS_PATH}")
    print(f"Latest API: http://{args.host}:{args.port}{LATEST_PATH}")
    if args.open_browser:
        webbrowser.open(live_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        coordinator.shutdown()


if __name__ == "__main__":
    main()
