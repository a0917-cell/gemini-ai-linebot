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
        self.configs = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs["model"])
        self.configs.append(kwargs.get("config"))
        if kwargs["model"] in self.fail_models:
            raise self.error if isinstance(self.error, BaseException) else Exception(self.error)
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


# --- T22: a daily quota does not come back within seconds ---

# The shape of the real error, 2026-09-30 (message trimmed).
DAILY = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota "
         "... limit: 20, model: gemini-3.8-flash', 'details': [{'violations': [{'quotaId': "
         "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'}]}]}}")
PER_MINUTE = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': [{'violations': [{'quotaId': "
              "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}]}}")


def test_daily_quota_goes_straight_to_the_fallback(fake):
    models = fake(fail_models={PRIMARY}, error=DAILY)

    resp = asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert resp.text == f"answer from {FALLBACK}"
    assert models.calls == [PRIMARY, FALLBACK]  # no retries against an exhausted day


def test_per_minute_limit_is_still_retried(fake):
    models = fake(fail_models={PRIMARY}, error=PER_MINUTE)

    asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert models.calls.count(PRIMARY) == 4


def test_daily_quota_on_both_models_raises_without_retrying(fake):
    models = fake(fail_models={PRIMARY, FALLBACK}, error=DAILY)

    with pytest.raises(Exception) as exc:
        asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert models.calls == [PRIMARY, FALLBACK]
    assert gemini._is_transient(exc.value)  # the user still gets "AI 忙線中", not a raw error


def test_daily_quota_is_recognised():
    assert gemini._is_daily_quota(Exception(DAILY))
    assert not gemini._is_daily_quota(Exception(PER_MINUTE))
    assert not gemini._is_daily_quota(Exception("503 UNAVAILABLE: high demand"))


# --- T28: a hung call must end, and a timeout is not retried ---
# 2026-11-06 the first call hung 5 min 40 s before its 503. The SDK raises a bare
# TimeoutError with an empty message (measured against a local server that never
# answers), so it cannot be recognised by text.

def test_timeout_values_are_pinned():
    assert gemini.ANSWER_TIMEOUT_MS == 60_000
    assert gemini.PARSE_TIMEOUT_MS == 20_000


def test_every_call_carries_the_answer_timeout_by_default(fake):
    models = fake()
    tools = [gemini.types.Tool(google_search=gemini.types.GoogleSearch())]

    asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))
    asyncio.run(gemini._generate_with_retry(
        model=PRIMARY, contents="hi", config=gemini.types.GenerateContentConfig(tools=tools)))

    assert [c.http_options.timeout for c in models.configs] == [60_000, 60_000]
    assert models.configs[1].tools == tools  # the caller's config is kept


def test_a_caller_can_ask_for_a_shorter_timeout(fake):
    models = fake()

    asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi", timeout_ms=20_000))

    assert models.configs[0].http_options.timeout == 20_000


@pytest.mark.parametrize("timeout", [
    TimeoutError(),                     # what the SDK raised, measured
    TimeoutError("Deadline exceeded"),  # text that also reads as transient must not buy a retry
])
def test_timeout_goes_straight_to_the_fallback(fake, timeout):
    models = fake(fail_models={PRIMARY}, error=timeout)

    resp = asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))

    assert resp.text == f"answer from {FALLBACK}"
    assert models.calls == [PRIMARY, FALLBACK]  # waiting again on a hung model helps nobody
    assert models.configs[1].http_options.timeout == 60_000  # the fallback is bounded too


def test_timeout_on_both_models_raises_without_retrying(fake):
    models = fake(fail_models={PRIMARY, FALLBACK}, error=TimeoutError())

    with pytest.raises(TimeoutError):
        asyncio.run(gemini._generate_with_retry(model=PRIMARY, contents="hi"))
    assert models.calls == [PRIMARY, FALLBACK]


def test_timeouts_are_recognised():
    import httpx
    assert gemini._is_timeout(TimeoutError())
    assert gemini._is_timeout(asyncio.TimeoutError())
    assert gemini._is_timeout(httpx.ReadTimeout(""))
    assert not gemini._is_timeout(Exception("503 UNAVAILABLE: high demand"))
    assert not gemini._is_timeout(ValueError(""))
