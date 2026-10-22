from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from rest_framework.exceptions import ValidationError

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_IMAGE_PIXELS = 20_000_000
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
IMAGE_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def validate_attachment(upload):
    if not upload or not getattr(upload, "name", None):
        raise ValidationError("Attachment is invalid.")
    if upload.size > MAX_ATTACHMENT_SIZE:
        raise ValidationError("Each attachment must be 10 MB or smaller.")
    name = str(upload.name).replace("\\", "/")
    base = Path(name).name
    if base != name or "\x00" in name or base in {".", ".."} or "/" in name:
        raise ValidationError("Invalid attachment filename.")
    suffix = Path(base).suffix.lower()
    from .recording_validation import EXTENSIONS, validate_recording

    if suffix in EXTENSIONS:
        return validate_recording(upload, suffix)
    if suffix not in ALLOWED_EXTENSIONS or base.count(".") != 1:
        raise ValidationError("Unsupported attachment filename or extension.")

    upload.seek(0)
    header = upload.read(16)
    upload.seek(0)
    if suffix == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValidationError("The uploaded PDF is invalid.")
        return "application/pdf"

    try:
        image = Image.open(upload)
        detected = image.format
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValidationError("The uploaded image is too large to process safely.")
        image.verify()
        upload.seek(0)
    except (UnidentifiedImageError, DecompressionBombError, OSError, ValueError) as exc:
        upload.seek(0)
        raise ValidationError("The uploaded image is invalid.") from exc

    mime = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }.get(detected)
    if mime is None or mime != IMAGE_MIME_BY_EXT[suffix]:
        raise ValidationError("The attachment content does not match its extension.")
    return mime
