"""URL allow/deny policy to reduce SSRF and local-file reach."""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from config.settings import settings
from src.exceptions import ErrorCode, NFESecurityError


class UrlPolicyError(NFESecurityError):
    """Raised when a URL violates the configured navigation policy."""

    default_code = ErrorCode.URL_DENIED
    default_user_message = "This URL was blocked by NFE security policy."

    def __init__(self, message: str = "", **kwargs: object) -> None:
        kwargs.setdefault("code", ErrorCode.URL_DENIED)  # type: ignore[arg-type]
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


_BLOCKED_SCHEMES = frozenset({"file", "javascript", "data", "blob", "about", "ftp"})
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


def _parse_allowlist(raw: str) -> List[str]:
    return [h.strip().lower() for h in (raw or "").split(",") if h.strip()]


def _host_matches_allowlist(host: str, allowlist: Sequence[str]) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    for entry in allowlist:
        e = entry.lower().rstrip(".")
        if not e:
            continue
        if h == e or h.endswith("." + e):
            return True
    return False


def _is_private_or_local_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_ips(host: str) -> List[ipaddress._BaseAddress]:
    ips: List[ipaddress._BaseAddress] = []
    try:
        # Literal IP
        ips.append(ipaddress.ip_address(host))
        return ips
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return ips
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return ips


def is_url_allowed(
    url: str,
    *,
    allowlist: Optional[Iterable[str]] = None,
    deny_private: Optional[bool] = None,
    allow_localhost: Optional[bool] = None,
    resolve_dns: bool = False,
) -> bool:
    """Return True when ``url`` may be navigated by the browser agent."""
    try:
        assert_url_allowed(
            url,
            allowlist=allowlist,
            deny_private=deny_private,
            allow_localhost=allow_localhost,
            resolve_dns=resolve_dns,
        )
        return True
    except UrlPolicyError:
        return False


def assert_url_allowed(
    url: str,
    *,
    allowlist: Optional[Iterable[str]] = None,
    deny_private: Optional[bool] = None,
    allow_localhost: Optional[bool] = None,
    resolve_dns: bool = False,
) -> str:
    """Validate ``url`` against scheme/host/private-IP policy.

    Returns:
        The stripped URL when allowed.

    Raises:
        UrlPolicyError: When the URL is missing, uses a blocked scheme, or
            targets a denied host/address.
    """
    text = (url or "").strip()
    if not text:
        raise UrlPolicyError("URL is empty")

    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES or not scheme:
        raise UrlPolicyError(f"Blocked or missing URL scheme: {scheme or '(none)'}")
    if scheme not in ("http", "https"):
        raise UrlPolicyError(f"Only http/https URLs are allowed (got {scheme})")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlPolicyError("URL has no hostname")

    if host in _METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise UrlPolicyError(f"Blocked metadata host: {host}")

    if allowlist is None:
        allowlist = _parse_allowlist(settings.NFE_URL_ALLOWLIST)
    allowlist = list(allowlist)
    if allowlist and not _host_matches_allowlist(host, allowlist):
        raise UrlPolicyError(f"Host not in NFE_URL_ALLOWLIST: {host}")

    if deny_private is None:
        deny_private = settings.NFE_URL_DENY_PRIVATE
    if allow_localhost is None:
        allow_localhost = settings.NFE_ALLOW_LOCALHOST

    is_local_name = host in ("localhost", "localhost.") or host.endswith(".localhost")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if deny_private:
        if is_local_name and not allow_localhost:
            raise UrlPolicyError("Localhost navigation denied (set NFE_ALLOW_LOCALHOST=true)")
        if literal_ip is not None:
            if _is_private_or_local_ip(literal_ip) and not (
                allow_localhost and literal_ip.is_loopback
            ):
                raise UrlPolicyError(f"Private/local IP denied: {host}")
            # Link-local / metadata-ish
            if str(literal_ip) == "169.254.169.254":
                raise UrlPolicyError("Blocked cloud metadata IP")
        elif resolve_dns:
            for ip in _resolve_ips(host):
                if str(ip) == "169.254.169.254":
                    raise UrlPolicyError("Blocked cloud metadata IP (resolved)")
                if _is_private_or_local_ip(ip) and not (
                    allow_localhost and ip.is_loopback
                ):
                    raise UrlPolicyError(
                        f"Host resolves to private/local IP ({ip}): {host}"
                    )

    return text
