"""HTTP Basic auth for the MailFlow server.

Credentials come from the ``[server]`` config section; ``password_env``
wins over a literal ``password`` so deployments can keep secrets out of
TOML. A server section without any credential refuses to start: an open
admin endpoint is never the right default.
"""

# pyright: basic

from __future__ import annotations

import base64
import hmac
import os
from typing import Any

from fastapi import HTTPException, Request


def resolve_password(server_config: Any) -> str:
    env_name = getattr(server_config, "password_env", None)
    if env_name:
        value = os.environ.get(str(env_name), "")
        if value:
            return value
    return str(getattr(server_config, "password", "") or "")


def require_credentials(server_config: Any) -> tuple[str, str]:
    username = str(getattr(server_config, "username", "") or "")
    password = resolve_password(server_config)
    if not username or not password:
        raise RuntimeError(
            "[server] must define username and (password or password_env) before "
            "'mailflow serve' can run — refusing to expose an unauthenticated admin API"
        )
    return username, password


def check_basic_auth(request: Request, username: str, password: str) -> None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        got_user, _, got_pass = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="unauthorized") from None
    user_ok = hmac.compare_digest(got_user, username)
    pass_ok = hmac.compare_digest(got_pass, password)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="unauthorized")


__all__ = ["check_basic_auth", "require_credentials", "resolve_password"]
