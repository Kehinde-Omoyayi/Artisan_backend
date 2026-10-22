"""Bounded, local media validation. No AI, transcription or network decoder input."""

import json, math, subprocess, tempfile
from pathlib import Path
from django.conf import settings
from rest_framework.exceptions import ValidationError

EXTENSIONS = {".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4"}


def validate_recording(upload, suffix):
    if getattr(settings, "ENVIRONMENT", "") == "production":
        from .readiness import require_production_readiness

        require_production_readiness()
    if not getattr(settings, "CONVERSATION_RECORDINGS_ENABLED", False):
        raise ValidationError(
            "Voice and video uploads are not enabled by this deployment."
        )
    from .purge_ledger import ledger_path

    try:
        path = ledger_path()
        if path is not None and not path.is_file():
            raise OSError("Missing ledger")
    except Exception:
        raise ValidationError(
            "Recording deployment is not ready: restore protection is unavailable."
        ) from None
    if upload.size > 10 * 1024 * 1024:
        raise ValidationError(
            "Video must be 10 MB or smaller; audio must be 5 MB or smaller."
        )
    upload.seek(0)
    header = upload.read(32)
    upload.seek(0)
    valid = (
        suffix == ".webm"
        and header.startswith(b"\x1aE\xdf\xa3")
        or suffix == ".ogg"
        and header.startswith(b"OggS")
        or suffix == ".wav"
        and header.startswith(b"RIFF")
        and header[8:12] == b"WAVE"
        or suffix in (".m4a", ".mp4")
        and header[4:8] == b"ftyp"
        or suffix == ".mp3"
        and (header.startswith(b"ID3") or header[:1] == b"\xff")
    )
    if not valid:
        raise ValidationError("Recording signature does not match its extension.")
    try:
        with tempfile.TemporaryDirectory(prefix="artisan-media-") as directory:
            path = Path(directory) / ("recording" + suffix)
            with path.open("wb") as f:
                for chunk in upload.chunks():
                    f.write(chunk)
            probe = subprocess.run(
                [
                    getattr(settings, "FFPROBE_BINARY", "ffprobe"),
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-show_entries",
                    "format=duration,format_name:stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=8,
                check=True,
            )
            info = json.loads(probe.stdout)
            types = [s.get("codec_type") for s in info.get("streams", [])]
            if not types or any(t not in ("audio", "video") for t in types):
                raise ValueError("Invalid streams")
            video = "video" in types
            if video and suffix not in (".mp4", ".webm"):
                raise ValueError("Invalid video container")
            if not video and upload.size > 5 * 1024 * 1024:
                raise ValidationError("Voice notes and audio must be 5 MB or smaller.")
            maximum = 60 if video else 90
            # Decode to validate content AND live WebM recordings lacking duration metadata.
            decode = subprocess.run(
                [
                    getattr(settings, "FFMPEG_BINARY", "ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                    "-t",
                    str(maximum + 1),
                    "-progress",
                    "pipe:1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            times = [
                float(line.split("=", 1)[1]) / 1000000
                for line in decode.stdout.decode().splitlines()
                if line.startswith("out_time_us=") and line.split("=", 1)[1] != "N/A"
            ]
            duration = max(times, default=0)
            if (
                not math.isfinite(duration)
                or duration <= 0
                or duration > maximum + 0.05
            ):
                raise ValidationError(f"Recording must be at most {maximum} seconds.")
            mime = (".webm" if suffix == ".webm" else ".mp4") if video else suffix
            return (
                {".webm": "video/webm", ".mp4": "video/mp4"}
                if video
                else {
                    ".webm": "audio/webm",
                    ".ogg": "audio/ogg",
                    ".wav": "audio/wav",
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".mp4": "audio/mp4",
                }
            )[mime]
    except ValidationError:
        raise
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise ValidationError(
            "Recording could not be validated. Check format and deployment FFmpeg configuration."
        ) from None
    finally:
        upload.seek(0)
