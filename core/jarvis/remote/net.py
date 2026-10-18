"""Сеть для пульта: адреса компьютера в домашней сети (для ссылки и QR-кода) и сам QR-код в SVG."""

import contextlib
import functools
import ipaddress
import logging
import platform
import re
import socket
import time

log = logging.getLogger(__name__)

# виртуальные и служебные адаптеры: телефон по ним компьютер не найдёт
_VIRTUAL_PARTS = (
    "vethernet",
    "virtualbox",
    "vmware",
    "hyper-v",
    "wsl",
    "loopback",
    "bluetooth",
    "tailscale",
    "zerotier",
    "hamachi",
    "radmin",
    "npcap",
    "teredo",
    "isatap",
    "wireguard",
    "openvpn",
    "tap-windows",
)
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "tun", "tap", "utun", "wg", "zt")
_LOOPBACK = re.compile(r"lo\d*")
_PHYSICAL_PARTS = (
    "wi-fi",
    "wifi",
    "wireless",
    "беспровод",
    "ethernet",
    "подключение по локальной",
    "local area",
)
_PHYSICAL_PREFIXES = ("wlan", "wlp", "eth", "enp", "eno", "ens", "en")

_cache: tuple[float, list[str]] | None = None
CACHE_S = 30.0


def _is_virtual(name: str) -> bool:
    low = name.casefold()
    return (
        any(part in low for part in _VIRTUAL_PARTS)
        or low.startswith(_VIRTUAL_PREFIXES)
        or _LOOPBACK.fullmatch(low) is not None
    )


def _rank(name: str, addr: ipaddress.IPv4Address) -> tuple[int, int, str]:
    low = name.casefold()
    physical = 0 if any(part in low for part in _PHYSICAL_PARTS) or low.startswith(_PHYSICAL_PREFIXES) else 1
    text = str(addr)
    family = 0 if text.startswith("192.168.") else 1 if text.startswith("10.") else 2
    return (physical, family, text)


def _scan() -> list[str]:
    try:
        import psutil

        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as e:
        log.warning("список сетевых адаптеров не получен: %s", e)
        addrs, stats = {}, {}
    found: list[tuple[str, ipaddress.IPv4Address]] = []
    for name, entries in addrs.items():
        stat = stats.get(name)
        if (stat is not None and not stat.isup) or _is_virtual(name):
            continue
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            try:
                addr = ipaddress.IPv4Address(entry.address)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
                continue
            found.append((name, addr))
    private = [item for item in found if item[1].is_private]
    chosen = sorted(private or found, key=lambda item: _rank(*item))
    ips: list[str] = []
    for _, addr in chosen:
        if str(addr) not in ips:
            ips.append(str(addr))
    if not ips:
        fallback = _primary_ip()
        if fallback:
            ips.append(fallback)
    return ips


def _primary_ip() -> str:
    """Адрес, через который компьютер выходит в сеть (UDP connect пакетов не отправляет)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.168.0.1", 9))
        ip = sock.getsockname()[0]
        addr = ipaddress.IPv4Address(ip)
        if not (addr.is_loopback or addr.is_unspecified or addr.is_link_local):
            return ip
    return ""


def lan_ips(*, refresh: bool = False) -> list[str]:
    """IPv4-адреса компьютера в домашней сети, лучший — первым (Wi-Fi/Ethernet, 192.168.*)."""
    global _cache
    now = time.monotonic()
    if not refresh and _cache is not None and now - _cache[0] < CACHE_S:
        return list(_cache[1])
    ips = _scan()
    _cache = (now, ips)
    return list(ips)


def pc_name() -> str:
    return platform.node() or socket.gethostname() or "Компьютер"


@functools.lru_cache(maxsize=16)
def qr_svg(text: str) -> str:
    """QR-код в SVG: чёрные модули на белом, размер задаёт CSS (viewBox без width/height)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    path: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < size and row[x]:
                x += 1
            path.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path fill="#000" d="{"".join(path)}"/></svg>'
    )
