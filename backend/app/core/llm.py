"""The only door to the LLM (CLAUDE.md): cache, record/replay, retries, schema-checked output.

Talks to an Azure OpenAI (Microsoft Foundry) chat-completions deployment over REST. Output is requested
as a JSON object that must validate against the caller's Pydantic schema; one repair round is allowed.
Untrusted text (release notes, code, logs) must be passed inside delimiters by the caller. No tools are
ever offered to the model.

    python -m app.core.llm --smoke      # one tiny structured request per deployment
"""
import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core import llm_store
from app.core.config import Settings, get_settings
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Tier = Literal["strong", "fast"]
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class LLMError(RuntimeError):
    """The model call failed for a reason a retry won't fix."""


class LLMNotConfigured(LLMError):
    pass


class LLMReplayMiss(LLMError):
    """LLM_MODE=replay and the request is not in the cache: tests fail loudly instead of calling out."""


class LLMContentFiltered(LLMError):
    """Azure's content filter blocked the request or the response. Treat as a per-item failure."""


class LLMOutputError(LLMError):
    """The model's answer did not validate against the schema, even after a repair round."""


def sha256_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def deployment_for(settings: Settings, tier: Tier) -> str:
    name = settings.azure_openai_deployment_strong if tier == "strong" else settings.azure_openai_deployment_fast
    if not name:
        raise LLMNotConfigured(f"AZURE_OPENAI_DEPLOYMENT_{tier.upper()} is not set")
    return name


class LLM:
    """One per pipeline run. Counts real calls and cache hits for the run statistics."""

    def __init__(self, conn: sqlite3.Connection, http: HttpClient, settings: Settings | None = None):
        self._conn = conn
        self._http = http
        self._settings = settings or get_settings()
        self.calls = 0
        self.cache_hits = 0
        self._send_temperature = True
        self._credential = None

    # -- auth ------------------------------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if self._settings.azure_openai_api_key:  # hackathon
            return {"api-key": self._settings.azure_openai_api_key}
        try:  # production: managed identity / az login
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise LLMNotConfigured(
                "No AZURE_OPENAI_API_KEY and azure-identity is not installed (pip install azure-identity)"
            ) from exc
        self._credential = self._credential or DefaultAzureCredential()
        return {"Authorization": f"Bearer {self._credential.get_token(TOKEN_SCOPE).token}"}

    # -- request ---------------------------------------------------------------------------------------

    def _url(self, deployment: str) -> str:
        s = self._settings
        if not s.azure_openai_endpoint or not s.azure_openai_api_version:
            raise LLMNotConfigured("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_VERSION must be set")
        return (
            f"{s.azure_openai_endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={s.azure_openai_api_version}"
        )

    async def _complete(self, deployment: str, messages: list[dict]) -> str:
        body: dict = {"messages": messages, "response_format": {"type": "json_object"}}
        for _ in range(2):
            if self._send_temperature:
                body["temperature"] = 0
            else:
                body.pop("temperature", None)
            resp = await self._http.request(
                "azure_openai", "POST", self._url(deployment),
                json=body, headers=self._auth_headers(), timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 400:
                is_json = resp.headers.get("content-type", "").startswith("application/json")
                error = (resp.json().get("error") or {}) if is_json else {}
                if error.get("code") == "content_filter" or "content_filter" in json.dumps(error):
                    raise LLMContentFiltered(error.get("message", "blocked by the content filter"))
                if self._send_temperature and "temperature" in json.dumps(error).lower():
                    self._send_temperature = False  # reasoning models reject it; retry without
                    continue
            if resp.status_code in (401, 403):
                raise LLMError(f"Azure OpenAI rejected the credentials (HTTP {resp.status_code})")
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                raise LLMContentFiltered("response blocked by the content filter")
            self.calls += 1
            return choice["message"]["content"] or ""
        raise LLMError("Azure OpenAI request failed")  # pragma: no cover

    # -- public ----------------------------------------------------------------------------------------

    async def chat(
        self, schema: type[T], *, prompt_version: str, messages: list[dict], tier: Tier = "strong"
    ) -> T:
        """Return a validated `schema` instance. Identical requests are answered from the cache."""
        # The key uses the tier, not the deployment name, so a cache recorded on one machine replays on
        # another (CI has no Azure settings). The deployment actually used is stored with the row.
        key = sha256_json([tier, prompt_version, messages, schema.model_json_schema()])

        hit = llm_store.cache_get(self._conn, key)
        if hit is not None:
            self.cache_hits += 1
            return schema.model_validate(hit)
        if self._settings.llm_mode == "replay":
            raise LLMReplayMiss(f"no recorded response for {prompt_version} ({key[:12]})")

        deployment = deployment_for(self._settings, tier)
        model_id = f"{tier}:{deployment}"
        started = time.perf_counter()
        instruction = (
            "Respond with a single JSON object matching this JSON Schema, and nothing else:\n"
            + json.dumps(schema.model_json_schema())
        )
        if messages and messages[0]["role"] == "system":
            system = messages[0]["content"] + "\n\n" + instruction
            wire = [{"role": "system", "content": system}, *messages[1:]]
        else:
            wire = [{"role": "system", "content": instruction}, *messages]
        content = await self._complete(deployment, wire)
        try:
            result = schema.model_validate_json(content)
        except (ValidationError, ValueError) as first_error:
            repair = [
                *wire,
                {"role": "assistant", "content": content},
                {"role": "user", "content": "That did not match the schema: "
                 f"{str(first_error)[:1500]}\nReturn the corrected JSON object only."},
            ]
            content = await self._complete(deployment, repair)
            try:
                result = schema.model_validate_json(content)
            except (ValidationError, ValueError) as second_error:
                raise LLMOutputError(
                    f"output did not match {schema.__name__}: {str(second_error)[:300]}"
                ) from second_error

        llm_store.cache_put(self._conn, key, model_id, result.model_dump(mode="json"))
        logger.info(
            "llm %s %s %d ms", tier, prompt_version, (time.perf_counter() - started) * 1000,
            extra={"model": model_id, "prompt_version": prompt_version},
        )
        return result


class _Smoke(BaseModel):
    answer: str


async def _smoke() -> int:
    settings = get_settings()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE llm_cache (key TEXT PRIMARY KEY, model TEXT NOT NULL, response JSON NOT NULL, "
                 "created_at TEXT NOT NULL DEFAULT '')")
    failed = False
    async with HttpClient() as http:
        llm = LLM(conn, http, settings)
        for tier in ("strong", "fast"):
            started = time.perf_counter()
            try:
                out = await llm.chat(
                    _Smoke, prompt_version="smoke",
                    messages=[{"role": "system", "content": "You answer in JSON."},
                              {"role": "user", "content": 'Set "answer" to the word "pong".'}],
                    tier=tier,  # type: ignore[arg-type]
                )
                print(f"{tier}: {out.answer!r} in {(time.perf_counter() - started) * 1000:.0f} ms")
            except LLMError as exc:
                failed = True
                print(f"{tier}: FAILED {exc}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    import asyncio

    parser = argparse.ArgumentParser(prog="app.core.llm")
    parser.add_argument("--smoke", action="store_true", help="send one tiny structured request per deployment")
    args = parser.parse_args(argv)
    if not args.smoke:
        parser.print_help()
        return 0
    return asyncio.run(_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
