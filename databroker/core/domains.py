"""databroker.core.domains -- one canonical_domain used everywhere."""
from __future__ import annotations
import re

try:
    import tldextract
    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=())  # offline
except Exception:  # pragma: no cover
    _EXTRACT = None


def canonical_domain(url_or_host: str) -> str:
    """Reduce any URL/host to a registrable eTLD+1, lowercased. '' if unparseable."""
    if not url_or_host:
        return ""
    s = url_or_host.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0].split(":")[0].strip()
    if not s or "." not in s:
        return ""
    if _EXTRACT is not None:
        e = _EXTRACT(s)
        return f"{e.domain}.{e.suffix}" if e.domain and e.suffix else ""
    if s.startswith("www."):
        s = s[4:]
    parts = s.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""
