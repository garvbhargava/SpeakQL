"""Address checks for external database registration (Backend Plan §9.3).

An owner supplies a host, port, database name and credentials. This is the one
place the product is handed a network address by a user, which makes it the
one place server-side request forgery can enter.

**The rule that matters: resolve the name first, then check the resolved
address.** Checking the string is the mistake. `internal.attacker.com` is not
in any private range as a string, and resolves to `10.0.0.5`. So does
`127.0.0.1.nip.io`. Every one of those passes a string check and reaches the
metadata service or an internal admin port.

Every address the name resolves to is checked, not just the first. A
round-robin record that returns one public and one private address would
otherwise pass here and connect there.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from enum import Enum


class HostRefusal(str, Enum):
    UNRESOLVABLE = "that hostname does not resolve"
    PRIVATE_RANGE = "that host resolves into a private range"
    LOOPBACK = "that host resolves to this machine"
    LINK_LOCAL = "that host resolves to a link-local address"
    CLOUD_METADATA = "that address is a cloud metadata endpoint"
    RESERVED = "that address is reserved"
    BAD_PORT = "that port is not allowed"
    EMPTY = "no host was supplied"


@dataclass(frozen=True)
class HostVerdict:
    allowed: bool
    refusal: HostRefusal | None = None
    detail: str = ""
    resolved: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.allowed:
            return "accepted"
        base = self.refusal.value if self.refusal else "refused"
        return f"{base}{': ' + self.detail if self.detail else ''}"


# The cloud metadata endpoints. Link-local already covers 169.254.169.254, but
# naming them separately produces a message that says what actually happened.
_METADATA = {
    "169.254.169.254",   # AWS, Azure, GCP, DigitalOcean
    "100.100.100.200",   # Alibaba
    "192.0.0.192",       # Oracle
    "fd00:ec2::254",     # AWS IPv6
}

# Postgres, and the two ports a managed instance commonly sits on. A wide
# range would let this become a port scanner.
ALLOWED_PORTS = frozenset({5432, 5433, 6432, 25060, 26257})


def check_host(host: str, port: int = 5432) -> HostVerdict:
    """Resolve, then judge every address it resolved to."""
    host = (host or "").strip().rstrip(".")
    if not host:
        return HostVerdict(False, HostRefusal.EMPTY)

    if port not in ALLOWED_PORTS:
        return HostVerdict(
            False, HostRefusal.BAD_PORT,
            f"{port}; allowed: {', '.join(str(p) for p in sorted(ALLOWED_PORTS))}",
        )

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return HostVerdict(False, HostRefusal.UNRESOLVABLE, str(exc))

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return HostVerdict(False, HostRefusal.UNRESOLVABLE, host)

    # EVERY address, not just the first.
    for raw in addresses:
        verdict = _judge(raw, addresses)
        if not verdict.allowed:
            return verdict

    return HostVerdict(True, resolved=tuple(addresses))


def _judge(raw: str, all_addresses: list[str]) -> HostVerdict:
    try:
        address = ipaddress.ip_address(raw.split("%")[0])  # strip zone id
    except ValueError:
        return HostVerdict(False, HostRefusal.UNRESOLVABLE, raw)

    resolved = tuple(all_addresses)

    if raw in _METADATA or str(address) in _METADATA:
        return HostVerdict(False, HostRefusal.CLOUD_METADATA, raw, resolved)
    # Order matters only for the message, never for the outcome: every branch
    # below refuses. The more specific classification is checked first so the
    # owner is told what actually happened -- `is_private` is true for
    # 0.0.0.0/8 too, and "that host resolves into a private range" would be a
    # confusing thing to say about 0.0.0.0.
    if address.is_loopback:
        return HostVerdict(False, HostRefusal.LOOPBACK, raw, resolved)
    if address.is_link_local:
        return HostVerdict(False, HostRefusal.LINK_LOCAL, raw, resolved)
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return HostVerdict(False, HostRefusal.RESERVED, raw, resolved)
    if address.is_private:
        return HostVerdict(False, HostRefusal.PRIVATE_RANGE, raw, resolved)

    # IPv4-mapped IPv6 (::ffff:10.0.0.1) hides a private address inside a
    # public-looking one.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        mapped = address.ipv4_mapped
        if mapped.is_private or mapped.is_loopback or mapped.is_link_local:
            return HostVerdict(False, HostRefusal.PRIVATE_RANGE,
                               f"{raw} maps to {mapped}", resolved)

    return HostVerdict(True, resolved=resolved)


def require_tls(dsn: str) -> str:
    """sslmode=require, and never downgraded.

    Appended rather than trusted from the caller, so a DSN carrying
    `sslmode=disable` cannot opt out of it.
    """
    if "sslmode=" in dsn:
        import re
        dsn = re.sub(r"sslmode=[a-z-]+", "sslmode=require", dsn)
        return dsn
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}sslmode=require"
