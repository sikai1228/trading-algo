"""URL canonicalization for news article deduplication."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters we strip during canonicalization. Tracking parameters
# from analytics, social shares, and ad networks: their presence/absence
# does not change the article, so they should not break dedup.
_STRIP_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "src",
    "share",
    "sharetype",
    "smtyp",
    "smid",
    "_ga",
    "yclid",
    "msclkid",
    "cmpid",
    "cid",
    "icid",
}


def canonicalize_url(url: str) -> str:
    """Return a canonical form of the URL suitable for deduplication.

    - Lowercases scheme + host.
    - Removes fragments.
    - Drops common analytics/tracking query params.
    - Strips a trailing slash from the path (unless the path is "/").
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _STRIP_PARAMS
    ]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))
