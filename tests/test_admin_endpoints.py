"""T16: /store/info listed every user's uploaded filenames to anyone on the
internet, and /health exposed the store's resource name. /store/info now needs
X-Admin-Token matching ADMIN_TOKEN, and fails closed when ADMIN_TOKEN is unset."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.gemini_service as gemini
import app.main as main

client = TestClient(main.app)
TOKEN = "correct-admin-token"


@pytest.fixture
def store(monkeypatch):
    """A fake store so the authorized path never reaches Gemini."""
    docs = [SimpleNamespace(name="d1", display_name="客戶A_送審單.pdf", state="ACTIVE")]
    fake = SimpleNamespace(file_search_stores=SimpleNamespace(
        get=lambda name: SimpleNamespace(display_name="store", embedding_model="m"),
        documents=SimpleNamespace(list=lambda parent: docs),
    ))
    monkeypatch.setattr(gemini, "get_or_create_store", lambda: "fileSearchStores/secret-name")
    monkeypatch.setattr(gemini, "get_client", lambda: fake)


def test_health_reveals_nothing_but_status():
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_store_info_without_token_is_forbidden(monkeypatch, store):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)

    resp = client.get("/store/info")

    assert resp.status_code == 403
    assert "送審單" not in resp.text


def test_store_info_with_wrong_token_is_forbidden(monkeypatch, store):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)

    resp = client.get("/store/info", headers={"X-Admin-Token": "guess"})

    assert resp.status_code == 403


@pytest.mark.parametrize("sent", ["", "anything"])
def test_store_info_is_closed_when_no_admin_token_is_configured(monkeypatch, store, sent):
    # An unset ADMIN_TOKEN must not turn into "empty header == empty token".
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    resp = client.get("/store/info", headers={"X-Admin-Token": sent})

    assert resp.status_code == 403


def test_store_info_with_correct_token_returns_documents(monkeypatch, store):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)

    resp = client.get("/store/info", headers={"X-Admin-Token": TOKEN})

    assert resp.status_code == 200
    assert resp.json()["document_count"] == 1


def test_store_info_error_does_not_echo_the_exception(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)

    def boom():
        raise RuntimeError("403 PERMISSION_DENIED project gen-lang-client-123 key AIzaFAKE")
    monkeypatch.setattr(gemini, "get_or_create_store", boom)

    resp = client.get("/store/info", headers={"X-Admin-Token": TOKEN})

    assert resp.status_code == 500
    assert "PERMISSION_DENIED" not in resp.text and "gen-lang-client" not in resp.text
