"""The admin REST+WS server: auth, core reads/writes, hot-reload wiring."""

from __future__ import annotations

import base64
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingTypeStubs]
from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow_server import create_app


class _PrefStorage:
    def __init__(self) -> None:
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def _service(tmp_path: Any) -> MailFlowService:
    config = MailFlowConfig()
    config.server.username = "admin"
    config.server.password = "secret"
    return MailFlowService(
        config=config,
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, _PrefStorage()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


@pytest.fixture
def clients(tmp_path: Any) -> tuple[Any, Any]:
    service = _service(tmp_path)
    service.config_path = tmp_path / "config.toml"
    app = create_app(service)
    token = base64.b64encode(b"admin:secret").decode()
    authed = TestClient(app, headers={"Authorization": f"Basic {token}"})
    anonymous = TestClient(app)
    return authed, anonymous


def test_missing_or_wrong_credentials_rejected(clients: tuple[Any, Any]) -> None:
    _authed, anonymous = clients
    assert anonymous.get("/snapshot").status_code == 401
    import base64 as b64

    bad_token = b64.b64encode(b"admin:wrong").decode()
    bad = anonymous.get("/snapshot", headers={"Authorization": f"Basic {bad_token}"})
    assert bad.status_code == 401


def test_snapshot_and_commands(clients: tuple[Any, Any]) -> None:
    authed, _anonymous = clients
    body = authed.get("/snapshot").json()
    assert body["version"]
    response = authed.post("/commands", json={"line": "help"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_settings_put_reports_validation_errors(clients: tuple[Any, Any], tmp_path: Any) -> None:
    authed, _anonymous = clients
    ok = authed.put("/settings/general.timezone", json={"value": "Europe/Berlin"})
    assert ok.status_code == 200
    assert ok.json()["key"] == "general.timezone"
    bad = authed.put("/settings/general.workers", json={"value": "not-a-number"})
    assert bad.status_code == 400


def test_llm_test_unknown_id(clients: tuple[Any, Any]) -> None:
    authed, _anonymous = clients
    response = authed.post("/llms/test", json={"llm_id": "ghost"})
    assert response.status_code == 404


def test_require_credentials_refuses_empty() -> None:
    from mailflow.config import MailFlowConfig as Config
    from mailflow_server.auth import require_credentials

    config = Config()
    with pytest.raises(RuntimeError, match="refusing"):
        require_credentials(config.server)
