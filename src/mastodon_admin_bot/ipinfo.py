"""Best-effort geolocation and AS lookup for registration IPs."""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ipip.info"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_IPV4_PREFIX_LENGTH = 24
DEFAULT_IPV6_PREFIX_LENGTH = 80
POSITIVE_CACHE_TTL_SECONDS = 86_400.0
NEGATIVE_CACHE_TTL_SECONDS = 300.0
MAX_CACHE_ENTRIES = 2048


@dataclass(frozen=True)
class IpInfo:
    """Geolocation and AS details for an IP. Empty strings mean unknown."""

    country: str = ""
    asn_org: str = ""

    @property
    def has_data(self) -> bool:
        return bool(self.country or self.asn_org)

    def location_text(self) -> str:
        """Render the country and AS organization returned by the provider."""
        return ", ".join(part for part in (self.country, self.asn_org) if part)


class IpInfoLookup(Protocol):
    """Duck-typed IP lookup interface (satisfied by :class:`IpInfoClient`)."""

    async def lookup(self, ip: str) -> IpInfo: ...


def is_lookupable_ip(ip: str) -> bool:
    ip = ip.strip()
    if not ip:
        return False
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    return True


def privacy_preserving_prefix(
    ip: str,
    ipv4_prefix_length: int = DEFAULT_IPV4_PREFIX_LENGTH,
    ipv6_prefix_length: int = DEFAULT_IPV6_PREFIX_LENGTH,
) -> str | None:
    """Return the network address for the configured IP prefix length."""
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    prefix_length = ipv4_prefix_length if address.version == 4 else ipv6_prefix_length
    return str(ipaddress.ip_network((address, prefix_length), strict=False).network_address)


class IpInfoClient:
    """Look up geolocation and AS details for registration IPs.

    Uses ipip.info's JSON endpoint by default. Lookups are best-effort:
    any failure yields an empty :class:`IpInfo` instead of an exception, and
    results are cached with a TTL to stay inside the free tier's rate limit.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        positive_ttl_seconds: float = POSITIVE_CACHE_TTL_SECONDS,
        negative_ttl_seconds: float = NEGATIVE_CACHE_TTL_SECONDS,
        ipv4_prefix_length: int = DEFAULT_IPV4_PREFIX_LENGTH,
        ipv6_prefix_length: int = DEFAULT_IPV6_PREFIX_LENGTH,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._positive_ttl_seconds = positive_ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._ipv4_prefix_length = ipv4_prefix_length
        self._ipv6_prefix_length = ipv6_prefix_length
        self._cache: dict[str, tuple[float, IpInfo]] = {}
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def lookup(self, ip: str) -> IpInfo:
        """Return lookup details for ``ip``; unknown IPs and failures give an empty IpInfo."""
        key = privacy_preserving_prefix(
            ip,
            self._ipv4_prefix_length,
            self._ipv6_prefix_length,
        )
        if key is None:
            return IpInfo()
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        info = await self._fetch(key)
        ttl = self._positive_ttl_seconds if info.has_data else self._negative_ttl_seconds
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[key] = (now + ttl, info)
        return info

    async def _fetch(self, ip: str) -> IpInfo:
        try:
            response = await self._client().get(
                f"{self._base_url}/json",
                params={"ip": ip},
            )
            if response.status_code != 200:
                logger.warning(
                    "IP lookup returned HTTP %s", response.status_code, extra={"ip": ip}
                )
                return IpInfo()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("IP lookup failed: %s", type(exc).__name__, extra={"ip": ip})
            return IpInfo()
        if not isinstance(data, dict):
            logger.info("IP lookup has no data", extra={"ip": ip})
            return IpInfo()
        return IpInfo(
            country=str(data.get("country") or ""),
            asn_org=str(data.get("asn_org") or ""),
        )
