"""T1 / AC1: a stable primary model, and a fallback model once the primary's
retries are exhausted on overload. The bot used to surface the preview model's
503 straight to the user."""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini

PRIMARY = "primary-model"
FALLBACK = "fallback-model"


class FakeModels:
    def __init__(self, fail_models=(), error="503 UNAVAILABLE: high demand"):
        self.fail_models = set(fail_models)
        self.error = error
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs["model"])
        if kwargs["model"] in self.fail_models:
            raise Exception(self.error)
        return SimpleNamespace(text=f"answer from {kwargs['model']}")


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        models = FakeModels(**kw)
        monkeypatch.setattr(gemini, "_client", SimpleNamespace(aio=SimpleNamespace(models=models)))
        monkeypatch.setattr(gemini, "GEN_MODEL", PRIMARY)
        monkeypatch.setattr(gemini, "FALLBACK_MODEL", FALLBACK)

        async def no_sleep(_):
            return None
        monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
        return models
    return install


def test_defaults_are_stable_models_not_previews():
    assert "preview" not in gemini.DEFAULT_MODEL
    assert "preview" not in gemini.DEFAULT_FALLBACK_MODEL
    assert gemini.DEFAULT_MODEL != gemini.DEFAULT_FALLBACK_MODEL


def test_overloaded_primary_falls_back_after_its_retries(fake):
    models = fake(fail_models={PRIMARY})

    resp = asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert resp.text == f"answer from {FALLBACK}"
    assert models.calls[-1] == FALLBACK
    assert models.calls.count(PRIMARY) == 4  # 1 try + 3 retries before giving up on it


def test_healthy_primary_never_touches_fallback(fake):
    models = fake()

    asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert models.calls == [PRIMARY]


def test_non_transient_error_raises_without_fallback(fake):
    models = fake(fail_models={PRIMARY}, error="400 INVALID_ARGUMENT: bad request")

    with pytest.raises(Exception, match="INVALID_ARGUMENT"):
        asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))
    assert FALLBACK not in models.calls


def test_calling_the_fallback_directly_does_not_fall_back_to_itself(fake):
    models = fake(fail_models={FALLBACK})

    with pytest.raises(Exception):
        asyncio.run(gemini._generate_with_retry(model=FALLBACK, contents="hi"))
    assert models.calls == [FALLBACK] * 4  # its own retries only, no extra round


def test_both_models_overloaded_raises_transient_error(fake):
    models = fake(fail_models={PRIMARY, FALLBACK})

    with pytest.raises(Exception) as exc:
        asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))
    assert gemini._is_transient(exc.value)
    assert models.calls[-1] == FALLBACK
