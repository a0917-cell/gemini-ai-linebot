"""Test-wide guard: no test may build a real Gemini, LINE or GCS client.

.env holds live credentials, and the app builds its clients lazily
(gemini.get_client, AsyncApiClient(...), gcs.Client()), so any test that walks a
real code path would call the production APIs. Every constructor is replaced
with one that raises; tests that need a client inject a fake instead
(e.g. ``gemini._client = FakeClient()``). A Sheets client joins this list when
the reminder store lands (plan T9).
"""
import pytest

import app.gemini_service as gemini
import app.line_handler as line

BLOCKED = "real client blocked in tests"


class _Blocked:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(f"{BLOCKED}: {type(self).__name__}")


def _blocked(name: str) -> type:
    return type(name, (_Blocked,), {})


@pytest.fixture(autouse=True)
def _no_real_clients(monkeypatch):
    monkeypatch.setattr(gemini.genai, "Client", _blocked("genai.Client"))
    monkeypatch.setattr(line.gcs, "Client", _blocked("storage.Client"))
    monkeypatch.setattr(line, "AsyncApiClient", _blocked("AsyncApiClient"))
    # Module-level caches would otherwise carry a client or store name across tests.
    monkeypatch.setattr(gemini, "_client", None)
    monkeypatch.setattr(gemini, "_store_name", "")
    monkeypatch.setattr(gemini, "_kb_status_cache", None)
