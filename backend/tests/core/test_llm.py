import asyncio
import json

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from app.core import db as core_db
from app.core.config import Settings
from app.core.llm import (
    LLM,
    LLMContentFiltered,
    LLMError,
    LLMNotConfigured,
    LLMOutputError,
    LLMReplayMiss,
)
from app.updates.http import HttpClient
from tests.updates.conftest import make_db

ENDPOINT = "https://oai.example.com"
URL = f"{ENDPOINT}/openai/deployments/gpt-strong/chat/completions"


class Answer(BaseModel):
    answer: str = Field(max_length=10)


def settings(**kw) -> Settings:
    base = dict(
        azure_openai_endpoint=ENDPOINT, azure_openai_api_version="2024-10-21",
        azure_openai_deployment_strong="gpt-strong", azure_openai_deployment_fast="gpt-fast",
        azure_openai_api_key="k3y", llm_mode="live",
    )
    return Settings(_env_file=None, **{**base, **kw})


def completion(content: str, finish="stop") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})


@pytest.fixture
def conn(tmp_path):
    c = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(c)
    return c


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


async def ask(conn, cfg=None, messages=None, **kw):
    async with HttpClient(wait_scale=0) as http:
        llm = LLM(conn, http, cfg or settings())
        out = await llm.chat(Answer, prompt_version="t_v1", messages=messages or MESSAGES, **kw)
        return out, llm


def run(coro):
    return asyncio.run(coro)


@respx.mock
def test_structured_answer_request_shape_and_cache(conn):
    route = respx.post(URL).mock(return_value=completion('{"answer": "pong"}'))
    out, llm = run(ask(conn))
    assert out.answer == "pong" and llm.calls == 1 and llm.cache_hits == 0
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]
    assert body["messages"][0]["content"].startswith("sys")
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert request.headers["api-key"] == "k3y" and "api-version=2024-10-21" in str(request.url)

    out2, llm2 = run(ask(conn))  # identical request: answered from the cache
    assert out2.answer == "pong" and llm2.calls == 0 and llm2.cache_hits == 1
    assert route.call_count == 1


@respx.mock
def test_different_prompt_version_or_messages_miss_the_cache(conn):
    route = respx.post(URL).mock(return_value=completion('{"answer": "a"}'))
    run(ask(conn))
    run(ask(conn, messages=[*MESSAGES[:1], {"role": "user", "content": "other"}]))
    assert route.call_count == 2


@respx.mock
def test_fast_tier_uses_fast_deployment(conn):
    route = respx.post(f"{ENDPOINT}/openai/deployments/gpt-fast/chat/completions").mock(
        return_value=completion('{"answer": "f"}')
    )
    out, _ = run(ask(conn, tier="fast"))
    assert out.answer == "f" and route.called


@respx.mock
def test_schema_violation_gets_one_repair_round(conn):
    route = respx.post(URL).mock(
        side_effect=[completion('{"answer": "this is far too long"}'), completion('{"answer": "ok"}')]
    )
    out, llm = run(ask(conn))
    assert out.answer == "ok" and llm.calls == 2
    repair = json.loads(route.calls[1].request.content)["messages"]
    assert repair[-2]["role"] == "assistant" and "did not match the schema" in repair[-1]["content"]


@respx.mock
def test_second_schema_violation_raises_and_is_not_cached(conn):
    respx.post(URL).mock(return_value=completion("not json"))
    with pytest.raises(LLMOutputError):
        run(ask(conn))
    assert conn.execute("SELECT count(*) FROM llm_cache").fetchone()[0] == 0


@respx.mock
def test_content_filter_is_a_distinct_error(conn):
    respx.post(URL).mock(return_value=httpx.Response(
        400, json={"error": {"code": "content_filter", "message": "blocked"}}))
    with pytest.raises(LLMContentFiltered):
        run(ask(conn))


@respx.mock
def test_finish_reason_content_filter(conn):
    respx.post(URL).mock(return_value=completion("", finish="content_filter"))
    with pytest.raises(LLMContentFiltered):
        run(ask(conn))


@respx.mock
def test_temperature_rejected_by_reasoning_models_is_dropped(conn):
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(400, json={"error": {"code": "unsupported_value",
                                            "message": "'temperature' does not support 0"}}),
        completion('{"answer": "ok"}'),
    ])
    out, _ = run(ask(conn))
    assert out.answer == "ok"
    assert "temperature" in json.loads(route.calls[0].request.content)
    assert "temperature" not in json.loads(route.calls[1].request.content)


@respx.mock
def test_rate_limit_is_retried(conn):
    route = respx.post(URL).mock(side_effect=[httpx.Response(429), completion('{"answer": "ok"}')])
    assert run(ask(conn))[0].answer == "ok" and route.call_count == 2


@respx.mock
def test_bad_credentials(conn):
    respx.post(URL).mock(return_value=httpx.Response(401))
    with pytest.raises(LLMError, match="rejected the credentials"):
        run(ask(conn))


def test_replay_mode_never_calls_out(conn):
    cfg = settings(llm_mode="replay", azure_openai_endpoint=None, azure_openai_deployment_strong=None)
    with pytest.raises(LLMReplayMiss):
        run(ask(conn, cfg))


@respx.mock
def test_record_then_replay_on_a_machine_without_azure_settings(conn):
    respx.post(URL).mock(return_value=completion('{"answer": "rec"}'))
    run(ask(conn, settings(llm_mode="record")))
    bare = settings(llm_mode="replay", azure_openai_endpoint=None, azure_openai_deployment_strong=None,
                    azure_openai_api_key=None)
    out, llm = run(ask(conn, bare))
    assert out.answer == "rec" and llm.calls == 0 and llm.cache_hits == 1


def test_not_configured(conn):
    with pytest.raises(LLMNotConfigured):
        run(ask(conn, settings(azure_openai_deployment_strong=None)))
    with pytest.raises(LLMNotConfigured):
        run(ask(conn, settings(azure_openai_endpoint=None)))


@respx.mock
def test_messages_without_system_prompt_get_one(conn):
    route = respx.post(URL).mock(return_value=completion('{"answer": "ok"}'))
    run(ask(conn, messages=[{"role": "user", "content": "hi"}]))
    sent = json.loads(route.calls[0].request.content)["messages"]
    assert sent[0]["role"] == "system" and sent[1] == {"role": "user", "content": "hi"}
