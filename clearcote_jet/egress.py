"""Where the MCP tools may send the browser: not to this machine, the local network or a cloud metadata endpoint.

The same guard as the Clearcote MCP server (clearcote-mcp): a url given to a tool is refused when its host is
localhost, a private, loopback, link-local, reserved, multicast or unspecified address, or resolves to one. A url
without a host (file:, data:, about:) passes. CLEARCOTE_JET_ALLOW_PRIVATE_EGRESS=1 (or clearcote-mcp's
CLEARCOTE_ALLOW_PRIVATE_EGRESS=1) turns the guard off, e.g. to work on a site running locally.
"""

import asyncio
import ipaddress
import os
from urllib.parse import urlparse

OPT_IN = "CLEARCOTE_JET_ALLOW_PRIVATE_EGRESS"


class EgressRefused(ValueError):
    """The url points at a private or internal address."""


def allowed_private() -> bool:
    return "1" in (os.environ.get(OPT_IN), os.environ.get("CLEARCOTE_ALLOW_PRIVATE_EGRESS"))


def ip_blocked(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


async def check_url(url: str | None) -> None:
    """Raise EgressRefused for a url on this machine, the local network or a metadata endpoint (see the module)."""
    if not url or allowed_private():
        return
    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        return
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise EgressRefused(f"refused private/metadata host {host!r} (set {OPT_IN}=1 to allow)")
    if ip_blocked(host):
        raise EgressRefused(f"refused private/internal address {host!r} (set {OPT_IN}=1 to allow)")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except Exception:
        return  # does not resolve: the browser will say so
    for info in infos:
        if ip_blocked(info[4][0]):
            raise EgressRefused(f"refused private/internal address for {host!r} (set {OPT_IN}=1 to allow)")
