"""Test doubles that behave like broken servers."""

import socket
from typing import Final

BACKLOG: Final = 64


class SilentServer:
    """Accepts TCP connections but never answers: what a frozen, overloaded or
    cut-off Redis looks like from outside. The operating system completes each
    connection for us (up to the backlog), and we never read a byte."""

    def __init__(self) -> None:
        self._socket = socket.create_server(("127.0.0.1", 0), backlog=BACKLOG)

    @property
    def url(self) -> str:
        host, port = self._socket.getsockname()
        return f"redis://{host}:{port}/0"

    def connections_made(self) -> int:
        """How many connections clients opened since the last call."""
        self._socket.setblocking(False)
        count = 0
        for _ in range(BACKLOG):
            try:
                connection, _ = self._socket.accept()
            except BlockingIOError:
                break
            connection.close()
            count += 1
        return count

    def close(self) -> None:
        self._socket.close()
