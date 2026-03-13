"""Shared URL and neighborhood-prefix utilities used across the pipeline."""

from urllib.parse import urlsplit, urlunsplit


def normalize_listing_url(url: str | None) -> str | None:
    """Strip query/fragment to keep a stable canonical URL."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return None


# Only these words are treated as geographic prefixes extracted into `area`.
# NOTE: "Agia/Agios/Agioi" are NOT prefixes — they stay in neighborhood.
AREA_PREFIXES = {"ano", "kato", "nea", "neo"}


def extract_area_prefix(name: str | None) -> str | None:
    """Return 'Ano'/'Kato'/'Nea'/'Neo' if the name starts with one of them."""
    if not name:
        return None
    parts = name.strip().split()
    if not parts:
        return None
    return parts[0].capitalize() if parts[0].lower() in AREA_PREFIXES else None


def strip_area_prefix(name: str | None) -> str | None:
    """If name starts with Ano/Kato/Nea/Neo, remove it and return the base name."""
    if not name:
        return None
    parts = name.strip().split()
    if not parts:
        return None
    if parts[0].lower() in AREA_PREFIXES and len(parts) >= 2:
        return " ".join(parts[1:]).strip() or None
    return name.strip() or None
