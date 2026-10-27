"""Test-only asyncio compatibility for restricted Windows runners."""

import socket
import sys
import asyncio


_installed = False
_original_socketpair = socket.socketpair
_original_socket_connect = socket.socket.connect


def install_socketpair_compat() -> None:
    """Avoid the blocked Windows socketpair accept path in test processes."""
    global _installed
    if _installed or sys.platform != "win32":
        return

    def socketpair(*args, **kwargs):
        if args or kwargs:
            return _original_socketpair(*args, **kwargs)
        left = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        right = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            left.bind(("127.0.0.1", 0))
            right.bind(("127.0.0.1", 0))
            _original_socket_connect(left, right.getsockname())
            _original_socket_connect(right, left.getsockname())
            return left, right
        except Exception:
            left.close()
            right.close()
            raise

    socket.socketpair = socketpair
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    _installed = True
