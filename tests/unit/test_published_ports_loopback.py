"""Phase 2d: the local-only stack publishes its ports on loopback only.

The hive and agent APIs speak plain HTTP/WS. A port published as "8000:8000"
listens on every interface, so anyone on the LAN could reach both APIs and
read their bearer tokens off the wire. Every published port of every service
binds to 127.0.0.1; services still reach each other on the compose network,
which publishing does not affect.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"


def _published(compose: dict) -> list[tuple[str, object]]:
    return [
        (name, port)
        for name, service in (compose.get("services") or {}).items()
        for port in (service.get("ports") or [])
    ]


def _host_ip(port: object) -> str | None:
    if isinstance(port, dict):  # long syntax
        return port.get("host_ip")
    parts = str(port).split(":")
    return parts[0] if len(parts) == 3 else None  # "ip:host:container"


def test_the_stack_publishes_ports():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert _published(compose), "no published ports found; the check below would pass vacuously"


def test_every_published_port_binds_to_loopback():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    open_ports = [(name, port) for name, port in _published(compose) if _host_ip(port) != LOOPBACK]
    assert not open_ports, f"published beyond loopback: {open_ports}"
