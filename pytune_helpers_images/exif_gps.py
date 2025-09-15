# exif.py

from __future__ import annotations
from io import BytesIO
from typing import Any, Dict, Optional, Tuple
import re
import requests
import exifread

from pytune_configuration.sync_config_singleton import config, SimpleConfig


if config is None:
    config = SimpleConfig()


# ========== EXIF READ ==========

def extract_exif_with_exifread(image_bytes: bytes) -> Dict[str, str]:
    """
    Extrait les tags EXIF sous forme { "TagName": "string-value", ... }.
    Rend uniquement des strings (sérialisables).
    """
    tags: Dict[str, str] = {}
    try:
        stream = BytesIO(image_bytes)
        tags_raw = exifread.process_file(stream, details=False)
        for tag, value in tags_raw.items():
            try:
                tags[tag] = str(value)
            except Exception:
                tags[tag] = repr(value)
    except Exception as e:
        tags["error"] = str(e)
    return tags


# ========== GPS PARSING (ROBUSTE, SANS eval) ==========

def _normalize_key(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()

def _find_key(tags: Dict[str, Any], *candidates: str) -> Optional[str]:
    """
    Retourne la *vraie* clé présente dans tags en testant plusieurs variantes (insensible à la casse et aux espaces).
    """
    if not tags:
        return None
    norm = { _normalize_key(k): k for k in tags.keys() }
    for cand in candidates:
        key_norm = _normalize_key(cand)
        if key_norm in norm:
            return norm[key_norm]
    return None

def _parse_rational(token: str) -> float:
    token = token.strip()
    if "/" in token:
        a, b = token.split("/", 1)
        return float(a) / float(b)
    return float(token)

def _parse_dms(value: Any) -> Tuple[float, float, float]:
    """
    Accepte : "[48, 51, 179/100]", "48, 51, 179/100", ["48","51","179/100"], (48, 51, 179/100)
    Retourne (deg, min, sec) floats.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        s = str(value).strip()
        s = s.strip("[]()")
        parts = [p for p in re.split(r"[,\s]+", s) if p]

    if len(parts) < 3:
        raise ValueError(f"DMS format invalid: {value!r}")

    deg = _parse_rational(parts[0])
    minutes = _parse_rational(parts[1])
    seconds = _parse_rational(parts[2])
    return deg, minutes, seconds

def _dms_to_decimal(dms_val: Any, ref: str) -> float:
    d, m, s = _parse_dms(dms_val)
    dec = d + m/60.0 + s/3600.0
    if str(ref).upper().strip() in ("S", "W"):
        dec = -dec
    return dec

def extract_gps_from_exifread(data: Dict[str, Any]) -> Optional[dict]:
    """
    Accepte soit:
      - un container: {"exif": {...}, "optics": {...}, "location": {...}}
      - un dict EXIF plat
    Retourne {"latitude": float, "longitude": float, "method": "..."} ou None.
    """
    if not data:
        return None

    # 0) Cas déjà normalisé: location décimale
    loc = data.get("location") if isinstance(data, dict) else None
    if isinstance(loc, dict):
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is not None and lon is not None:
            try:
                return {
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "method": loc.get("method") or "LOCATION"
                }
            except Exception:
                pass

    # 1) Récupérer le sous-dict EXIF s’il existe, sinon considérer tout le dict comme EXIF plat
    tags = data.get("exif") if isinstance(data, dict) and isinstance(data.get("exif"), dict) else data
    if not isinstance(tags, dict):
        return None

    # 2) Chercher les clés avec ou sans espace, insensible à la casse
    def _normalize_key(s: str) -> str:
        import re
        return re.sub(r"\s+", "", s).lower()

    def _find_key(d: Dict[str, Any], *cands: str) -> Optional[str]:
        norm = { _normalize_key(k): k for k in d.keys() }
        for c in cands:
            k = _normalize_key(c)
            if k in norm:
                return norm[k]
        return None

    lat_key     = _find_key(tags, "GPS GPSLatitude", "GPSLatitude")
    lat_ref_key = _find_key(tags, "GPS GPSLatitudeRef", "GPSLatitudeRef")
    lon_key     = _find_key(tags, "GPS GPSLongitude", "GPSLongitude")
    lon_ref_key = _find_key(tags, "GPS GPSLongitudeRef", "GPSLongitudeRef")

    if not (lat_key and lat_ref_key and lon_key and lon_ref_key):
        return None

    # 3) Parsing DMS robuste (sans eval)
    import re
    def _parse_rational(tok: str) -> float:
        tok = tok.strip()
        if "/" in tok:
            a, b = tok.split("/", 1)
            return float(a) / float(b)
        return float(tok)

    def _parse_dms(value: Any):
        if isinstance(value, (list, tuple)):
            parts = [str(x) for x in value]
        else:
            s = str(value).strip().strip("[]()")
            parts = [p for p in re.split(r"[,\s]+", s) if p]
        if len(parts) < 3:
            raise ValueError(f"DMS invalid: {value!r}")
        d = _parse_rational(parts[0]); m = _parse_rational(parts[1]); s = _parse_rational(parts[2])
        return d, m, s

    def _dms_to_decimal(v: Any, ref: str) -> float:
        d, m, s = _parse_dms(v)
        dec = d + m/60.0 + s/3600.0
        if str(ref).upper().strip() in ("S", "W"):
            dec = -dec
        return dec

    try:
        lat = _dms_to_decimal(tags.get(lat_key), tags.get(lat_ref_key))
        lon = _dms_to_decimal(tags.get(lon_key), tags.get(lon_ref_key))
        return {"latitude": lat, "longitude": lon, "method": "EXIFREAD"}
    except Exception:
        return None


# ========== REVERSE GEOCODING ==========

def reverse_geocode_from_latlon(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Reverse geocoding minimal via Nominatim (ou autre service défini par config.GEOCODE_REVERSE_URL).
    """
    try:
        url = config.GEOCODE_REVERSE_URL  # ex: "https://nominatim.openstreetmap.org/reverse"
        params = {"lat": lat, "lon": lon, "format": "jsonv2"}
        headers = {"User-Agent": "PyTune PianoBeautify/1.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            addr = data.get("address", {}) if isinstance(data, dict) else {}
            city = addr.get("city") or addr.get("town") or addr.get("village")
            country = addr.get("country")
            return city, country
    except Exception as e:
        print(f"[reverse_geocode_from_latlon] Error: {e}")
    return None, None

def reverse_geocode_from_gps(gps_or_lat: Any, lon: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Wrapper rétro-compatible :
      - reverse_geocode_from_gps({'lat':..,'lon':..})  ou  ({'latitude':..,'longitude':..})
      - reverse_geocode_from_gps(lat, lon)
    """
    if isinstance(gps_or_lat, dict):
        lat = gps_or_lat.get("lat") or gps_or_lat.get("latitude")
        lon_val = gps_or_lat.get("lon") or gps_or_lat.get("longitude")
        if lat is None or lon_val is None:
            return None, None
        return reverse_geocode_from_latlon(float(lat), float(lon_val))

    if lon is None:
        return None, None
    return reverse_geocode_from_latlon(float(gps_or_lat), float(lon))


# ========== UTILS SUPPLÉMENTAIRES ==========

def exif_gps_to_decimal(exif: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Essaie d'extraire (lat, lon) depuis un dict EXIF déjà "structuré".
    Ex: {'lat':..., 'lon':...} ou clés EXIF brutes 'GPSLatitude' etc.
    """
    try:
        if "lat" in exif and "lon" in exif:
            return float(exif["lat"]), float(exif["lon"])

        # Cas EXIF structurés (pas strings) — rarement utile si on passe par exifread
        need = ("GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef")
        if all(k in exif for k in need):
            lat = _dms_to_decimal(exif["GPSLatitude"], exif["GPSLatitudeRef"])
            lon = _dms_to_decimal(exif["GPSLongitude"], exif["GPSLongitudeRef"])
            return lat, lon
    except Exception:
        pass
    return None


# ========== PIPELINE HAUT-NIVEAU ==========

def get_city_country_from_image(minio_url: str,
                                default_city: Optional[str] = None,
                                default_country: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Télécharge l'image depuis MinIO, lit les EXIF, extrait GPS si présent,
    fait un reverse geocoding, sinon renvoie les valeurs par défaut.
    """
    from pytune_data.minio_utils import download_from_minio_url
    try:
        image_bytes = download_from_minio_url(minio_url)
        exif_tags = extract_exif_with_exifread(image_bytes)
        gps = extract_gps_from_exifread(exif_tags)
        if gps:
            city, country = reverse_geocode_from_latlon(gps["latitude"], gps["longitude"])
            if city or country:
                return city, country
    except Exception as e:
        print(f"[get_city_country_from_image] Error for {minio_url}: {e}")

    return default_city, default_country
