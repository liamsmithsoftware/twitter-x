from __future__ import annotations

import http.client
import socket

import run
from run import _try_bind_socket, bind_server_socket


def _occupy_ephemeral_port() -> tuple[socket.socket, int]:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(socket.SOMAXCONN)
    return blocker, int(blocker.getsockname()[1])


def test_is_our_app_treats_non_http_response_as_another_service(monkeypatch):
    def return_bad_status(*args, **kwargs):
        raise http.client.BadStatusLine("\x05")

    monkeypatch.setattr(run.urllib.request, "urlopen", return_bad_status)

    assert run.is_our_app("127.0.0.1", 8000) is False


def test_bind_server_socket_uses_another_port_when_preferred_is_occupied():
    blocker, occupied_port = _occupy_ephemeral_port()
    server_socket = None
    try:
        server_socket, actual_port = bind_server_socket("127.0.0.1", occupied_port)

        assert actual_port != occupied_port
        assert server_socket.getsockname()[1] == actual_port
        # The selected port remains reserved until Uvicorn takes the socket.
        assert _try_bind_socket("127.0.0.1", actual_port) is None
    finally:
        if server_socket is not None:
            server_socket.close()
        blocker.close()


def test_bind_server_socket_uses_os_port_when_nearby_range_is_full(monkeypatch):
    requested_port = 42000
    busy_ports = {requested_port, requested_port + 1, requested_port + 2}
    attempted_ports: list[int] = []
    original_try_bind_socket = run._try_bind_socket

    def block_nearby_ports(host: str, port: int) -> socket.socket | None:
        attempted_ports.append(port)
        if port in busy_ports:
            return None
        return original_try_bind_socket(host, port)

    monkeypatch.setattr(run, "_try_bind_socket", block_nearby_ports)
    server_socket, actual_port = bind_server_socket("127.0.0.1", requested_port, span=2)
    try:
        assert attempted_ports[-1] == 0
        assert actual_port > 0
        assert server_socket.getsockname()[1] == actual_port
    finally:
        server_socket.close()


def test_bind_server_socket_accepts_an_os_assigned_port():
    server_socket, actual_port = bind_server_socket("127.0.0.1", 0)
    try:
        assert actual_port > 0
        assert server_socket.getsockname()[1] == actual_port
    finally:
        server_socket.close()
