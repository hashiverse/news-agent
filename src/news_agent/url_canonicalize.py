"""Pure URL canonicalisation, used as the cross-identity post-dedupe key.

Two articles whose URLs canonicalise to the same string are treated as the
same article. The transformations are deliberately conservative — text-only,
no network — so a URL maps to the same canonical form whether we're online,
offline, or running a unit test.

Operations:
- Lowercase the scheme and host.
- Drop tracking query parameters (utm_*, fbclid, gclid, mc_cid, mc_eid, _ga).
- Sort remaining query parameters by key (different orderings of the same
  params are otherwise treated as different URLs by string compare).
- Drop the URL fragment (``#section`` etc.).
- Drop a trailing slash on the path, except when the entire path is ``/``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^utm_"),
    re.compile(r"^fbclid$"),
    re.compile(r"^gclid$"),
    re.compile(r"^mc_cid$"),
    re.compile(r"^mc_eid$"),
    re.compile(r"^_ga$"),
)


def _is_tracking_param(name: str) -> bool:
    return any(p.match(name) for p in _TRACKING_PARAM_PATTERNS)


def canonicalize(url: str) -> str:
    """Return the canonical form of ``url``.

    Pure, no network. If the input is malformed or doesn't have a host,
    returns the original string unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.netloc:
        return url

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    query_pairs.sort()
    new_query = urlencode(query_pairs)

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    return urlunsplit((scheme, netloc, path, new_query, ""))
