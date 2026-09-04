"""Tiny synchronous client for the omen-fxd control socket."""

from __future__ import annotations

import json
import socket

from .config import SOCKET_PATH


class ClientError(RuntimeError):
    pass


def send(request: dict, path: str = SOCKET_PATH, timeout: float = 3.0) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except FileNotFoundError:
        raise ClientError(f"{path} not found -- is omen-fxd running?") from None
    except ConnectionRefusedError:
        raise ClientError(f"nothing listening on {path} -- is omen-fxd running?") from None
    except PermissionError:
        raise ClientError(f"no permission on {path} -- are you in the 'input' group?") from None
    except OSError as exc:
        raise ClientError(f"{path}: {exc}") from None

    raw = b"".join(chunks).decode("utf-8", "replace").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ClientError(f"malformed reply: {raw[:200]}") from None


def send_quiet(request: dict, path: str = SOCKET_PATH, timeout: float = 0.5) -> bool:
    """Fire-and-check-nothing; used where a failure must never matter."""
    try:
        send(request, path, timeout)
        return True
    except Exception:
        return False
