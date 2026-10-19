from io import BytesIO
from pathlib import Path

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 500 * 1024

def optimize_profile_photo(uploaded_file):
    if uploaded_file.size > MAX_SOURCE_BYTES:
        raise ValidationError("Profile photo must not exceed 5 MB.")

    try:
        with Image.open(uploaded_file) as image:
            image.verify()

        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            if image.width * image.height > 20_000_000:
                raise ValidationError("Profile photo dimensions are too large.")
            image = ImageOps.exif_transpose(image)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")

            max_dimension = 1600
            while max_dimension >= 512:
                candidate = image.copy()
                candidate.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                for quality in (82, 78, 74, 70, 66, 62):
                    buffer = BytesIO()
                    candidate.save(buffer, format="WEBP", quality=quality, method=6)
                    if buffer.tell() <= MAX_OUTPUT_BYTES:
                        filename = f"{Path(uploaded_file.name).stem}.webp"
                        return ContentFile(buffer.getvalue(), name=filename)
                max_dimension = int(max_dimension * 0.8)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("The uploaded profile photo is not a valid image.") from exc

    raise ValidationError("Unable to optimize the profile photo below 500 KB.")
