"""T6a: the store the bot queries must be the store the KB was uploaded to.

The free project holds two empty stores with the same display name, and
get_or_create_store picked "the first one listed" after its GCS lookup failed
on Render. GEMINI_STORE_NAME pins the choice."""
from types import SimpleNamespace

import app.gemini_service as gemini

PINNED = "fileSearchStores/pinned-store"


def _client_that_must_not_be_used():
    def forbidden(*args, **kwargs):
        raise AssertionError("store discovery ran although GEMINI_STORE_NAME was set")
    return SimpleNamespace(file_search_stores=SimpleNamespace(list=forbidden, create=forbidden))


def test_pinned_store_is_used_without_listing_or_creating(monkeypatch):
    monkeypatch.setenv("GEMINI_STORE_NAME", PINNED)
    monkeypatch.setattr(gemini, "_client", _client_that_must_not_be_used())
    monkeypatch.setattr(gemini, "_load_store_name_from_gcs", lambda: "fileSearchStores/from-gcs")

    assert gemini.get_or_create_store() == PINNED


def test_without_pin_discovery_still_reuses_an_existing_store(monkeypatch):
    monkeypatch.delenv("GEMINI_STORE_NAME", raising=False)
    stores = [SimpleNamespace(display_name="linebot-multimodal-rag", name="fileSearchStores/found")]
    monkeypatch.setattr(gemini, "_client", SimpleNamespace(file_search_stores=SimpleNamespace(
        list=lambda: stores, create=lambda **kw: (_ for _ in ()).throw(AssertionError("created")))))
    monkeypatch.setattr(gemini, "_load_store_name_from_gcs", lambda: "")

    assert gemini.get_or_create_store() == "fileSearchStores/found"
