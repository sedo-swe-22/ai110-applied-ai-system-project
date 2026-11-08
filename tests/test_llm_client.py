"""Tests for GeminiClient error handling, using fake API errors (no network, no quota)."""

from types import SimpleNamespace

import pytest
from google.genai import errors

from llm_client import DEFAULT_MODEL, GeminiClient, LLMError, is_daily_quota, quota_violation


def api_error(code, quota_id=None, limit=None, retry_delay=None):
    details = []
    if quota_id:
        details.append({
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaMetric": "generate_content_free_tier_requests", "quotaId": quota_id, "quotaValue": limit}],
        })
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    body = {"error": {"code": code, "message": "fake error", "status": "RESOURCE_EXHAUSTED", "details": details}}
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, body)


def make_client(outcomes):
    """A GeminiClient whose SDK call returns/raises the given outcomes in order."""
    sleeps = []
    client = GeminiClient(api_key="test-key", sleep=sleeps.append)

    def generate_content(**kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return client, sleeps


def ok_response(text="Hello"):
    part = SimpleNamespace(function_call=None, text=text, thought=None)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def test_default_model_is_flash_lite(monkeypatch):
    monkeypatch.delenv("PAWPAL_MODEL", raising=False)
    assert DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert GeminiClient(api_key="test-key").model == DEFAULT_MODEL


def test_quota_parsing():
    daily = api_error(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")
    assert quota_violation(daily)["limit"] == "20"
    assert is_daily_quota(quota_violation(daily))
    per_minute = api_error(429, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "15")
    assert not is_daily_quota(quota_violation(per_minute))
    assert quota_violation(api_error(500)) is None


def test_daily_quota_fails_fast_without_retrying():
    client, sleeps = make_client([api_error(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20", "3600s")])
    with pytest.raises(LLMError) as info:
        client.generate("system", [{"role": "user", "text": "hi"}], [])
    assert "daily Gemini quota" in str(info.value) and "20 requests/day" in str(info.value)
    assert sleeps == []  # no pointless waiting


def test_per_minute_limit_waits_and_retries():
    client, sleeps = make_client([
        api_error(429, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "15", "7s"),
        ok_response("Back again"),
    ])
    result = client.generate("system", [{"role": "user", "text": "hi"}], [])
    assert result.text == "Back again"
    assert sleeps == [7.0]  # used the server's suggested delay


def test_server_errors_back_off_then_give_up():
    client, sleeps = make_client([api_error(503)] * 4)
    with pytest.raises(LLMError) as info:
        client.generate("system", [{"role": "user", "text": "hi"}], [])
    assert "503" in str(info.value)
    assert sleeps == [2.0, 4.0, 8.0]


def test_missing_key_is_a_clear_setup_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        GeminiClient()
