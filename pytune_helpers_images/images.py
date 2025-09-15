from typing import Optional
from PIL import Image
from pillow_heif import register_heif_opener
from io import BytesIO
import json
from urllib.parse import urlparse
import os
import tempfile
from pytune_data import minio_client


register_heif_opener()  # Active le support HEIC

# ✅ JSON-safe wrapper
def safe_json(obj):
    try:
        clean_str = json.dumps(obj, ensure_ascii=False).encode('utf-8').decode('utf-8')
        return json.loads(clean_str)
    except Exception as e:
        raise ValueError(f"safe_json failed: {e}")


def compress_image(image_bytes: bytes, max_side: int = 1024, quality: int = 80) -> BytesIO:
    image = Image.open(BytesIO(image_bytes))
    width, height = image.size
    ratio = max_side / max(width, height)
    new_size = (int(width * ratio), int(height * ratio))
    image = image.resize(new_size, Image.LANCZOS)

    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=quality)
    output.seek(0)
    return output


def compress_image_and_extract_metadata(image_bytes: bytes, max_side: int = 1024, quality: int = 80) -> tuple[BytesIO, dict]:
    from pytune_helpers_images.exif_gps import extract_exif_with_exifread, extract_gps_from_exifread
    image = Image.open(BytesIO(image_bytes))
    format_original = image.format
    width_original, height_original = image.size

    # 📏 Resize
    ratio = max_side / max(width_original, height_original)
    new_size = (int(width_original * ratio), int(height_original * ratio))
    image_resized = image.resize(new_size, Image.LANCZOS)

    # 🗜️ Compression JPEG
    output = BytesIO()
    image_resized.convert("RGB").save(output, format="JPEG", quality=quality)
    output.seek(0)

    # 📷 Extraction via exifread
    exif_data = extract_exif_with_exifread(image_bytes)
    location = extract_gps_from_exifread(exif_data)

    # 🎯 Données utiles si disponibles
    optical_metadata = {
        "make": exif_data.get("Image Make"),
        "model": exif_data.get("Image Model"),
        "focal_length_mm": exif_data.get("EXIF FocalLength"),
        "focal_length_35mm": exif_data.get("EXIF FocalLengthIn35mmFilm"),
        "orientation": exif_data.get("Image Orientation"),
        "exif_width": exif_data.get("EXIF ExifImageWidth"),
        "exif_height": exif_data.get("EXIF ExifImageHeight"),
    }

    metadata = {
        "format_original": format_original,
        "size_original": {"width": width_original, "height": height_original},
        "size_compressed": {"width": new_size[0], "height": new_size[1]},
        "compression_ratio": ratio,
        "optics": optical_metadata,
        "exif": exif_data,
    }

    if location:
        metadata["location"] = location

    return output, safe_json(metadata)

async def download_images_locally(image_urls: list[str]) -> list[str]:
    """
    Télécharge les images depuis MinIO (via URLs) et retourne les chemins temporaires.
    """
    local_paths = []

    for url in image_urls:
        parsed = urlparse(url)
        # Exemple URL : http://minio:9000/piano-identification-sessions/xxx.jpg
        path_parts = parsed.path.lstrip("/").split("/")
        if len(path_parts) < 2:
            raise ValueError(f"Invalid MinIO image URL: {url}")

        bucket = path_parts[0]
        object_name = "/".join(path_parts[1:])

        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_path = tmp_file.name
        tmp_file.close()

        try:
            minio_client.client.fget_object(bucket, object_name, tmp_path)
            local_paths.append(tmp_path)
        except Exception as e:
            raise RuntimeError(f"Error downloading from MinIO: {object_name} ({bucket}): {e}")

    return local_paths

