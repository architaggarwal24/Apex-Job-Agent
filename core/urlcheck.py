"""
core/urlcheck.py — validation for user-supplied LLM provider base URLs.

Anyone who can save a base URL in Settings makes THIS SERVER send requests
(carrying an API key) to that address. On a cloud VM that is a classic
SSRF hole: pointing it at the instance-metadata service (169.254.169.254 on
Oracle/AWS/Azure/GCP) can leak the machine's own credentials. So a base URL
must be http(s), carry no embedded credentials, and must not resolve to a
link-local / metadata address — including when the address is written in
one of the legacy numeric forms (http://2852039166/, http://0xa9fea9fe/,
http://0251.0376.0251.0376/) that browsers and libcurl still accept.

Loopback and private-LAN addresses ARE allowed: Ollama, LM Studio and vLLM
legitimately live there.

Known limit, stated plainly: a *hostname* that DNS-resolves to a blocked
address is not caught (no DNS lookup is done here — it would block the event
loop and be racy anyway; DNS rebinding needs a network-level control). Only
literal addresses and the well-known metadata hostnames are checked.
"""

import ipaddress
import re
from urllib.parse import urlsplit

MAX_URL_LEN = 2048

_BLOCKED_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
}
_BLOCKED_V4 = [ipaddress.ip_network("169.254.0.0/16"),      # link-local incl. 169.254.169.254
               ipaddress.ip_network("100.100.100.200/32")]  # Alibaba Cloud metadata
_BLOCKED_V6 = [ipaddress.ip_network("fe80::/10"),
               ipaddress.ip_network("fd00:ec2::/32")]        # AWS IMDS over IPv6

_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$", re.I)


def _legacy_ipv4(host: str):
    """Parse the inet_aton forms a=32bit, a.b, a.b.c, a.b.c.d with 0x/0-prefixed
    parts. Done by hand (not socket.inet_aton) so behaviour is identical on
    every OS."""
    if not _NUMERIC_HOST.match(host):
        return None
    nums = []
    for part in host.split("."):
        try:
            if part.lower().startswith("0x"):
                nums.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0"):
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    limits = {1: (2**32,), 2: (256, 2**24), 3: (256, 256, 2**16), 4: (256,) * 4}[len(nums)]
    if any(n >= lim or n < 0 for n, lim in zip(nums, limits)):
        return None
    value = 0
    for n, lim in zip(nums[:-1], limits[:-1]):
        value = value * 256 + n
    tail_bits = {1: 32, 2: 24, 3: 16, 4: 8}[len(nums)]
    value = (value << tail_bits) + nums[-1]
    return ipaddress.IPv4Address(value)


def _as_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _legacy_ipv4(host)


def _is_blocked_ip(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:      # ::ffff:169.254.169.254
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return True
    nets = _BLOCKED_V4 if ip.version == 4 else _BLOCKED_V6
    return any(ip in n for n in nets)


def validate_base_url(url: str) -> str:
    """Return the URL normalised (whitespace and trailing '/' stripped) or
    raise ValueError with a message that's safe to show the user."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Enter a base URL, e.g. https://api.example.com/v1")
    if len(url) > MAX_URL_LEN or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise ValueError("That doesn't look like a valid URL")
    try:
        parts = urlsplit(url)
        _ = parts.port                                  # raises on a bad port
    except ValueError:
        raise ValueError("That doesn't look like a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("The URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("The URL has no host name")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Don't put credentials in the URL — use the API key field")

    host = parts.hostname.lower().rstrip(".")
    ip = _as_ip(host)
    if host in _BLOCKED_HOSTS or (ip is not None and _is_blocked_ip(ip)):
        raise ValueError("That address is a cloud metadata / link-local address and is not allowed")
    return url.rstrip("/")


_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


def validate_smtp_host(host: str) -> str:
    """The same protection for an SMTP server's host name (Settings > Notifications): this
    server opens a connection there and logs in with a password. A bare host name or IP
    literal only - no scheme, port, path or credentials - and never a cloud metadata /
    link-local address, in any numeric spelling. Loopback and private-LAN relays are allowed.
    Returns it lower-cased, or raises ValueError with a message safe to show."""
    host = (host or "").strip()
    if not host:
        raise ValueError("Enter the SMTP server's host name, e.g. smtp.example.com")
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host     # [::1]
    bare = bare.lower().rstrip(".")
    ip = _as_ip(bare)
    if bare in _BLOCKED_HOSTS or (ip is not None and _is_blocked_ip(ip)):
        raise ValueError("That address is a cloud metadata / link-local address and is not allowed")
    if ip is None and not _HOSTNAME.match(bare):         # also refuses a scheme, port, path, "@" or spaces
        raise ValueError("Enter only the SMTP host name (no scheme, port, path or credentials), e.g. smtp.example.com")
    return bare


def is_local_url(url: str) -> bool:
    """True for loopback / private-network / localhost targets — used to decide
    whether an API key is expected (a local model server usually needs none)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    ip = _as_ip(host)
    return ip is not None and (ip.is_loopback or ip.is_private)
