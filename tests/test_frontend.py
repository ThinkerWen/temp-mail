import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.api as api
from app.config import Settings
from app.providers.registry import Registry
from tests.fakes import FakeProvider


@pytest.fixture
def frontend_client(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend" / "dist"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text('<!doctype html><title>Temp Mail</title><div id="root"></div>')
    (assets / "index-example.js").write_text("document.title = 'Temp Mail';")
    monkeypatch.setattr(api, "FRONTEND_DIST", frontend)
    settings = Settings(
        db_path=str(tmp_path / "service.sqlite"),
        api_token="frontend-test-token-with-enough-entropy",
        encryption_key=Fernet.generate_key().decode(),
    )
    registry = Registry([FakeProvider("fake", str(tmp_path / "upstream.sqlite"))])
    with TestClient(api.create_app(settings, registry)) as client:
        yield client


def test_frontend_is_public_and_api_authentication_is_preserved(frontend_client):
    page = frontend_client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    assert '<div id="root"></div>' in page.text
    settings = frontend_client.app.state.service.settings
    assert settings.api_token not in page.text
    assert settings.encryption_key not in page.text
    assert frontend_client.get("/v1/mailboxes").status_code == 401
    authenticated = frontend_client.get("/v1/mailboxes", headers={"Authorization": f"Bearer {settings.api_token}"})
    assert authenticated.status_code == 200
    assert authenticated.json()["items"] == []
    assert frontend_client.get("/health/ready").status_code == 200
    assert frontend_client.get("/docs").status_code == 200


def test_frontend_assets_are_served_independently_of_working_directory(frontend_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = frontend_client.get("/assets/index-example.js")
    assert script.status_code == 200
    assert script.text == "document.title = 'Temp Mail';"
    assert "javascript" in script.headers["content-type"]


@pytest.mark.parametrize("path", ["/v1/unknown", "/health/unknown", "/unknown", "/assets/missing.js", "/config.yaml"])
def test_unknown_routes_do_not_return_frontend(frontend_client, path):
    response = frontend_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Not Found"}


def test_frontend_assets_cannot_escape_static_directory(frontend_client, tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("private-test-content")
    (api.FRONTEND_DIST / "assets" / "linked.txt").symlink_to(secret)
    for path in ("/assets/linked.txt", "/assets/%2e%2e/index.html", "/assets/%2e%2e/%2e%2e/%2e%2e/private.txt"):
        response = frontend_client.get(path)
        assert response.status_code == 404
        assert "private-test-content" not in response.text


def test_api_starts_without_built_frontend(frontend_client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "FRONTEND_DIST", tmp_path / "not-built")
    service = frontend_client.app.state.service
    with TestClient(api.create_app(service.settings, service.registry)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/assets/index-example.js").status_code == 404
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/capabilities").status_code == 401
