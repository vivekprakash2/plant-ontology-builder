"""Pluggable text-similarity + text-generation providers.

Stage 1 entity resolution needs a way to score how similar two text
descriptions are (e.g. "Crude Charge Pump A" vs "Crude Charge Pump 101").
Stage 4 reasoning can optionally use a configured language model to
rewrite an already-computed, fact-checked answer into more natural prose --
never to invent facts (see `agent.py`'s `_polish_with_llm` for the grounding
prompt + fallback-to-deterministic-text safety net).

By default everything runs fully offline using stdlib `difflib` so the
pipeline works with zero installs/API keys. If a local model runtime like
`mlx-lm` is installed (Apple Silicon) or an OpenAI-compatible endpoint is
configured via environment variables (including Databricks Model Serving,
e.g. a Claude Opus endpoint), the factories below return a provider that
uses it instead -- callers never need to change.

SECURITY_NOTE: Never hardcode API keys/endpoints here. Only read them from
environment variables (or a secret manager in production). No secret is
logged or included in any output artifact. The local mlx-lm path makes no
network calls at inference time (only the one-time model download).
"""
from __future__ import annotations

import difflib
import os
from abc import ABC, abstractmethod
from typing import Optional

from . import config  # noqa: F401 -- side effect: loads .env into os.environ,
# so provider factories here see credentials regardless of which module a
# caller imports first (this module doesn't otherwise need anything from
# `config`, but relying on "some other module happened to import config
# first" is fragile -- every module that reads os.environ for credentials
# should trigger the loading itself).


class SimilarityProvider(ABC):
    """Interface for scoring textual similarity between two strings."""

    @abstractmethod
    def similarity(self, text_a: str, text_b: str) -> float:
        """Return a similarity score in [0.0, 1.0]."""
        raise NotImplementedError


class OfflineSimilarityProvider(SimilarityProvider):
    """Zero-dependency, zero-network similarity using difflib.

    Good enough for blocking/scoring short equipment names & descriptions.
    Swap for an embeddings-based provider (see below) for better recall on
    paraphrased text.
    """

    def similarity(self, text_a: str, text_b: str) -> float:
        if not text_a or not text_b:
            return 0.0
        a = text_a.strip().lower()
        b = text_b.strip().lower()
        return difflib.SequenceMatcher(None, a, b).ratio()


class EmbeddingSimilarityProvider(SimilarityProvider):
    """Real embeddings-based text similarity via any OpenAI-compatible
    `/embeddings` endpoint -- OpenAI, Azure OpenAI, or Databricks Model
    Serving (Databricks Foundation Model / external-model endpoints expose
    the same `/serving-endpoints/embeddings` shape used elsewhere in this
    module for chat completions).

    Reads connection details from environment variables only:
      - DATABRICKS_TOKEN + DATABRICKS_HOST (or DATABRICKS_SERVER_HOSTNAME)
        + EMBEDDING_MODEL (or DATABRICKS_EMBEDDING_MODEL) -- the serving
        endpoint name for an embedding model (e.g. a BGE/E5 endpoint;
        NOT the same endpoint as a chat model like Claude Opus), or
      - OPENAI_API_KEY (+ optional OPENAI_BASE_URL, EMBEDDING_MODEL), or
      - AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY (+ EMBEDDING_MODEL).

    Caches one embedding vector per unique input TEXT, not per pair.
    Entity resolution's `score_pair()` calls `similarity()` once per
    cross-system profile PAIR (O(n^2) over ~30-40 profiles); without
    caching that would be one network round trip per pair. Caching by
    text means it's one network call per unique profile name (computed
    once, reused for every pair that mentions it), then pure in-memory
    cosine similarity for every subsequent comparison.

    Uses stdlib `urllib` + hand-rolled cosine similarity only -- no numpy
    dependency, matching this project's zero-install-required design.

    SECURITY_NOTE: the token/key is read from an environment variable only
    (via the same gitignored `.env` pattern as the text-generation
    provider) -- never hardcoded, never logged.
    """

    def __init__(self) -> None:
        self.api_key = (
            os.environ.get("DATABRICKS_TOKEN")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("AZURE_OPENAI_API_KEY")
        )
        host = os.environ.get("DATABRICKS_HOST") or os.environ.get(
            "DATABRICKS_SERVER_HOSTNAME"
        )
        if host:
            host = host.replace("https://", "").replace("http://", "").rstrip("/")
            self.base_url = f"https://{host}/serving-endpoints"
        else:
            self.base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
                "AZURE_OPENAI_ENDPOINT", "https://api.openai.com/v1"
            )
        self.model = (
            os.environ.get("EMBEDDING_MODEL")
            or os.environ.get("DATABRICKS_EMBEDDING_MODEL")
            or "text-embedding-3-small"
        )
        if not self.api_key:
            raise RuntimeError("No embeddings credentials found in environment.")
        self._cache: dict[str, list[float]] = {}

    def _embed(self, text: str) -> list[float]:
        if text in self._cache:
            return self._cache[text]
        import json
        import urllib.request

        payload = {"model": self.model, "input": text}
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vector = data["data"][0]["embedding"]
        self._cache[text] = vector
        return vector

    def similarity(self, text_a: str, text_b: str) -> float:
        if not text_a or not text_b:
            return 0.0
        if text_a == text_b:
            return 1.0
        vec_a = self._embed(text_a)
        vec_b = self._embed(text_b)
        dot = sum(x * y for x, y in zip(vec_a, vec_b))
        norm_a = sum(x * x for x in vec_a) ** 0.5
        norm_b = sum(y * y for y in vec_b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        cosine = dot / (norm_a * norm_b)
        # Text-embedding cosine similarities for real (non-adversarial) text
        # are effectively always >= 0; clip defensively rather than rescale,
        # since rescaling would compress the useful 0.3-0.95 range that
        # actually distinguishes "related" from "same" equipment names.
        return max(0.0, min(1.0, cosine))


def get_similarity_provider() -> SimilarityProvider:
    """Factory: prefer a configured embeddings provider, else fall back.

    Does a real one-time test call (embedding a trivial string) before
    committing to the embeddings provider -- if that fails for ANY reason
    (no embedding-model endpoint configured, wrong model name, network
    issue, etc.), falls back to the deterministic offline provider rather
    than letting the whole resolution pipeline crash. This mirrors
    `get_text_generation_provider()`'s existing fallback philosophy: a
    configured-but-broken AI provider should never take down a pipeline
    that has a safe deterministic default.
    """
    has_databricks = bool(os.environ.get("DATABRICKS_TOKEN")) and bool(
        os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    )
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_azure = bool(os.environ.get("AZURE_OPENAI_API_KEY")) and bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT")
    )
    if has_databricks or has_openai or has_azure:
        try:
            provider = EmbeddingSimilarityProvider()
            provider._embed("connectivity check")  # real network call -- fail fast here, not mid-pipeline
            return provider
        except Exception:
            pass
    return OfflineSimilarityProvider()


# --------------------------------------------------------------------------
# Text generation (Stage 4 answer polishing)
# --------------------------------------------------------------------------


class TextGenerationProvider(ABC):
    """Interface for turning (system_prompt, user_prompt) into text."""

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
        raise NotImplementedError

    def chat(
        self, messages: list[dict], tools: Optional[list[dict]] = None, max_tokens: int = 600
    ) -> dict:
        """Optional: tool-calling chat completion.

        Returns the raw assistant message dict, e.g.
        `{"role": "assistant", "content": "...", "tool_calls": [...]}` in the
        standard OpenAI tool-calling shape, so callers can append it directly
        to their running `messages` list. Providers that don't support tool
        calling raise `NotImplementedError` -- callers (see `agent.py`'s
        agentic loop) must catch this and fall back to deterministic text.
        """
        raise NotImplementedError


class NullTextGenerationProvider(TextGenerationProvider):
    """No language model available -- callers should fall back to deterministic text."""

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
        raise NotImplementedError("No text-generation provider is configured.")


# Model name kept small deliberately: a 3B 4-bit instruct model is enough
# for "rewrite this grounded draft more naturally," runs in ~2GB RAM, and
# needs no network access after the one-time download.
_MLX_MODEL = os.environ.get("LOCAL_LLM_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit")


class MLXTextGenerationProvider(TextGenerationProvider):
    """Local model via Apple's mlx-lm (Apple Silicon only, no network at
    inference time). The model is loaded once and cached on the instance."""

    def __init__(self) -> None:
        from mlx_lm import load  # local import: only required if this provider is used

        self.model = _MLX_MODEL
        self._model, self._tokenizer = load(_MLX_MODEL)

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
        from mlx_lm import generate as mlx_generate

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        prompt = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        return mlx_generate(
            self._model, self._tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False
        ).strip()


class OpenAICompatibleTextGenerationProvider(TextGenerationProvider):
    """Any OpenAI-compatible chat completions endpoint. Covers OpenAI, Azure
    OpenAI, a locally-run server (Ollama/vLLM/LM Studio), or a Databricks
    Model Serving endpoint -- Databricks Foundation Model / external-model
    endpoints implement the same `/chat/completions` shape at
    `https://<workspace-host>/serving-endpoints/chat/completions`, so no
    separate client is needed: set `DATABRICKS_HOST` (bare hostname or full
    URL, either works) and `LLM_MODEL` (the serving endpoint name). Uses
    stdlib `urllib` only -- no extra dependency.

    SECURITY_NOTE: the token/key is read from an environment variable only
    (e.g. via a local, gitignored `.env` file loaded by server.py) -- never
    hardcode it here, never log it, never echo it in any response.
    """

    def __init__(self) -> None:
        self.api_key = (
            os.environ.get("DATABRICKS_TOKEN")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("AZURE_OPENAI_API_KEY")
        )
        host = (
            os.environ.get("DATABRICKS_HOST")
            or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        )
        if host:
            # Accept either a bare hostname or a full URL -- normalize to
            # https://<host>/serving-endpoints either way.
            host = host.replace("https://", "").replace("http://", "").rstrip("/")
            self.base_url = f"https://{host}/serving-endpoints"
        else:
            self.base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
                "AZURE_OPENAI_ENDPOINT", "https://api.openai.com/v1"
            )
        self.model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("DATABRICKS_MODEL")
            or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        )
        if not self.api_key:
            raise RuntimeError("No text-generation credentials found in environment.")

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
        message = self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
        )
        return (message.get("content") or "").strip()

    def chat(
        self, messages: list[dict], tools: Optional[list[dict]] = None, max_tokens: int = 600
    ) -> dict:
        import json
        import urllib.request

        payload: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # Databricks-hosted Anthropic (Claude) models reject `temperature` /
        # `response_format` on this endpoint shape -- only set them for
        # backends known to support them.
        if not self.model.startswith("databricks-"):
            payload["temperature"] = 0.2
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = data["choices"][0]
        message = choice["message"]
        # Surface the stop reason on the returned message so callers can tell a
        # COMPLETE answer from one the model was cut off mid-sentence
        # (`finish_reason == "length"`). Without this, truncation was entirely
        # silent: agent.py parsed the half-written Markdown and the UI rendered
        # it as a finished answer (observed live -- a Recommended Actions bullet
        # ending "...to protect cr").
        #
        # Deliberately an underscore-prefixed key that callers POP before
        # appending the message back into the conversation, so this internal
        # field is never echoed to the API on a later turn.
        if choice.get("finish_reason"):
            message["_finish_reason"] = choice["finish_reason"]
        return message


_text_provider_singleton: Optional[TextGenerationProvider] = None


def get_text_generation_provider() -> TextGenerationProvider:
    """Factory, cached as a module-level singleton.

    Preference order: OpenAI-compatible endpoint / Databricks Model Serving
    (if credentials are set) -> local mlx-lm (ONLY if explicitly opted into
    via `LOCAL_LLM_PROVIDER=mlx` -- a native Metal crash can't be caught by
    Python's try/except and would take down the whole process, so it must
    never be attempted unconditionally at startup) -> null (deterministic
    text only, the default).
    """
    global _text_provider_singleton
    if _text_provider_singleton is not None:
        return _text_provider_singleton

    has_databricks = bool(os.environ.get("DATABRICKS_TOKEN")) and bool(
        os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    )
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_azure = bool(os.environ.get("AZURE_OPENAI_API_KEY")) and bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT")
    )
    if has_databricks or has_openai or has_azure:
        try:
            _text_provider_singleton = OpenAICompatibleTextGenerationProvider()
            return _text_provider_singleton
        except RuntimeError:
            pass

    if os.environ.get("LOCAL_LLM_PROVIDER") == "mlx":
        try:
            _text_provider_singleton = MLXTextGenerationProvider()
            return _text_provider_singleton
        except ImportError:
            pass  # mlx-lm not installed / not on Apple Silicon

    _text_provider_singleton = NullTextGenerationProvider()
    return _text_provider_singleton

