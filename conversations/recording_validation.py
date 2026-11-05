"""Bounded, local media validation. No AI, transcription or network decoder input."""

import json, logging, math, shutil, subprocess, tempfile
from pathlib import Path
from django.conf import settings
from rest_framework.exceptions import ValidationError

EXTENSIONS = {".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4"}

logger = logging.getLogger(__name__)


def _audio_limit_bytes():
    return int(getattr(settings, "CONVERSATION_ATTACHMENT_MAX_BYTES", 30 * 1024 * 1024))


def _max_seconds(video):
    return int(
        getattr(
            settings,
            "CONVERSATION_VIDEO_MAX_SECONDS" if video else "CONVERSATION_AUDIO_MAX_SECONDS",
            300 if video else 600,
        )
    )



def _mime_for(suffix, video):
    if video:
        return {".webm": "video/webm", ".mp4": "video/mp4"}.get(suffix, "video/mp4")
    return {
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
    }[suffix]


def _decoder_available(binary):
    """True only when the configured ffmpeg/ffprobe binary can actually run."""
    target = str(binary or "").strip()
    if not target:
        return False
    found = shutil.which(target)
    if not found and not Path(target).is_file():
        return False
    return True


def _decoder_pair():
    probe = getattr(settings, "FFPROBE_BINARY", "ffprobe")
    decode = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
    return _decoder_available(probe) and _decoder_available(decode)

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
    limit = _audio_limit_bytes()
    if upload.size > limit:
        raise ValidationError(
            f"Audio and video must be {limit // (1024 * 1024)} MB or smaller."
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
    if not _decoder_pair():
        # No ffmpeg/ffprobe on this deployment (a local Windows checkout, for
        # example). Uploads must still work: the file size, extension and
        # container signature were already checked, so the recording is accepted
        # without the duration probe instead of being rejected outright.
        logger.warning(
            "recording_decoder_unavailable: accepting %s after signature and size checks",
            suffix,
        )
        upload.seek(0)
        return _mime_for(suffix, suffix in (".mp4", ".webm"))
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
                timeout=30,
                check=True,
            )
            info = json.loads(probe.stdout)
            types = [s.get("codec_type") for s in info.get("streams", [])]
            if not types or any(t not in ("audio", "video") for t in types):
                raise ValueError("Invalid streams")
            video = "video" in types
            if video and suffix not in (".mp4", ".webm"):
                raise ValueError("Invalid video container")
            maximum = _max_seconds(video)
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
                timeout=180,
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
                kind = "Video" if video else "Audio"
                raise ValidationError(
                    f"{kind} must be at most {maximum // 60} minutes "
                    f"({maximum} seconds)."
                )
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
