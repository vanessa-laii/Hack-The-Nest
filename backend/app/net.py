# Outbound HTTP setup shared by every backend module that calls the internet.
# Inputs: settings (Nominatim user agent); the OS certificate store.
# Outputs: `http_client()` → configured httpx.AsyncClient; truststore injected on import.
#
# How it works:
#   1. On import, switch TLS checks to the operating system's certificate store.
#   2. http_client() builds an AsyncClient with shared timeouts, limits and User-Agent.
from __future__ import annotations

import httpx
import truststore

# Antivirus on the demo laptop re-signs HTTPS certificates, so Python's bundled
# ones reject every connection (ADR-001). truststore keeps verification ON and
# trusts what Windows trusts. Never use verify=False instead.
truststore.inject_into_ssl()

USER_AGENT = "revisit-hackthenest/1.0"


def http_client(
    timeout: float = 60,
    max_connections: int = 24,
    headers: dict | None = None,
) -> httpx.AsyncClient:
    """An AsyncClient with shared defaults; use as `async with http_client() as c:`.
    Some APIs (Nominatim especially) block clients without a real User-Agent."""
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
    )
    all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    return httpx.AsyncClient(timeout=timeout, limits=limits, headers=all_headers)
