# utils/image_bytes.py
from __future__ import annotations
from io import BytesIO
from typing import Any, Optional, Tuple
import base64
import requests

def _download_url_bytes(url: str, timeout: int = 20) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def coerce_to_image_bytes(raw: Any) -> bytes:
    """
    Rend toujours des bytes si possible.
    Gère:
      - bytes / bytearray / file-like
      - OpenAI Images API: {"data":[{"b64_json": "..."}]}
      - ChatCompletion avec content -> image_url
      - Dict maison: {"image_bytes": ...} ou {"image_b64": "..."} ou {"url": "..."}
    """
    # 1) déjà bytes
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)

    # 2) file-like
    if hasattr(raw, "read"):
        return raw.read()

    # 3) dict
    if isinstance(raw, dict):
        # a) OpenAI Images API style
        data = raw.get("data")
        if isinstance(data, list) and data:
            item = data[0]
            b64 = item.get("b64_json") or item.get("b64") or item.get("image_b64")
            if b64:
                return base64.b64decode(b64)

            url = item.get("url")
            if url:
                return _download_url_bytes(url)

        # b) ChatCompletion-like (choices[0].message.content[*].image_url.url)
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block and block.get("type") == "image_url":
                        img = (block.get("image_url") or {})
                        url = img.get("url")
                        if url:
                            return _download_url_bytes(url)

        # c) conventions maison
        if raw.get("image_bytes"):
            val = raw["image_bytes"]
            return bytes(val) if isinstance(val, (bytes, bytearray)) else base64.b64decode(val)

        if raw.get("image_b64"):
            return base64.b64decode(raw["image_b64"])

        if raw.get("url"):
            return _download_url_bytes(raw["url"])

    raise TypeError("coerce_to_image_bytes: unsupported response shape")


def _sniff_mime_ext(b: bytes) -> Tuple[str, str]:
    # JPEG
    if len(b) > 3 and b[:3] == b"\xFF\xD8\xFF":
        return "image/jpeg", "jpg"
    # PNG
    if len(b) > 8 and b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    # WEBP (RIFF....WEBP)
    if len(b) > 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp", "webp"
    # HEIC/HEIF (very loose check)
    if b[4:8] == b"ftyp" and any(x in b[8:16] for x in (b"heic", b"heix", b"hevc", b"mif1", b"heif")):
        # MinIO can store it; browsers may not preview. Prefer converting upstream if needed.
        return "image/heic", "heic"
    # default fallback
    return "image/jpeg", "jpg"
