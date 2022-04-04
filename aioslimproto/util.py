"""Helpers and utils."""
from __future__ import annotations

import asyncio
import socket


def get_ip():
    """Get primary IP-address for this host."""
    # pylint: disable=broad-except,no-member
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        sock.connect(("10.255.255.255", 1))
        _ip = sock.getsockname()[0]
    except Exception:
        _ip = "127.0.0.1"
    finally:
        sock.close()
    return _ip


def get_hostname():
    """Get hostname for this machine."""
    # pylint:disable=no-member
    return socket.gethostname()


def run_periodic(period):
    """Run a coroutine at interval."""

    def scheduler(fcn):
        async def wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)

        return wrapper

    return scheduler
