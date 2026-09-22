"""LLM router: Groq for the fast loop, Gemini for synthesis and embeddings, one interface.

Both providers expose OpenAI-compatible endpoints, so a single client library serves both. The router adds what free
tiers need: retry with backoff on rate limits and 5xx, provider fallback, an on-disk response cache (reruns are free
and deterministic), and token accounting for the answer file's `tokens` field.

With no API keys `available` is False and callers keep their template output, so the whole pipeline runs offline.
Exercised against the real providers (Groq gpt-oss-120b, Gemini 3.x flash): Gemini often answers 503/429 on the free tier, which
the retry/rotation/fallback absorbs. Unit tests use injected fake clients.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRYABLE_NAMES = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "ConnectError", "ReadTimeout"}


class LLMUnavailable(RuntimeError):
    """No configured provider could serve the request."""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)   # never let a stray print/log/traceback echo the raw key
    models: dict[str, str]          # role -> model id ("loop", "synth", "embed")


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def providers_from_env() -> list[Provider]:
    """One Provider per key: a comma-separated GROQ_API_KEY / GEMINI_API_KEY gives 'groq', 'groq#2', ... so a rate-limited
    key rolls over to the next one before the router falls back to the other provider."""
    out: list[Provider] = []
    keys = lambda var: [k.strip() for k in os.getenv(var, "").split(",") if k.strip()]
    m = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    for i, k in enumerate(keys("GROQ_API_KEY")):
        out.append(Provider("groq" if i == 0 else f"groq#{i + 1}", "https://api.groq.com/openai/v1", k, {"loop": m, "synth": m}))
    m = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    for i, k in enumerate(keys("GEMINI_API_KEY")):
        out.append(Provider("gemini" if i == 0 else f"gemini#{i + 1}", "https://generativelanguage.googleapis.com/v1beta/openai/", k,
                            {"loop": m, "synth": m, "embed": os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")}))
    return out


def _is_retryable(e: Exception) -> bool:
    status = getattr(e, "status_code", None)
    return (status in RETRYABLE_STATUS) if status is not None else type(e).__name__ in RETRYABLE_NAMES


def _retry_after(e: Exception) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))          # openai/httpx errors carry the response
    except Exception:                                                # noqa: BLE001
        return None


class LLMRouter:
    # which provider to try first for each role: Groq is fast for the tool loop, Gemini writes better prose and embeds
    ORDER = {"loop": ["groq", "gemini"], "synth": ["gemini", "groq"], "embed": ["gemini"]}

    def __init__(self, providers: list[Provider] | None = None, cache_dir: Path | None = None, max_retries: int = 3,
                 client_factory: Callable[[Provider], object] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.providers = {p.name: p for p in (providers if providers is not None else providers_from_env())}
        self.cache_dir = cache_dir if cache_dir is not None else ROOT / "data" / "store" / "llm_cache"
        self.max_retries = max_retries
        self._factory = client_factory or self._openai_client
        self._sleep = sleep
        self._clients: dict[str, object] = {}
        self.calls = 0
        self.cache_hits = 0
        self.tokens = 0                      # running total, including cached results' original usage
        self.errors: list[str] = []

    @classmethod
    def from_env(cls, **kw) -> "LLMRouter":
        return cls(**kw)

    @property
    def available(self) -> bool:
        return bool(self.providers)

    # ---- plumbing -------------------------------------------------------------------------------------------
    @staticmethod
    def _openai_client(p: Provider):
        from openai import OpenAI
        return OpenAI(api_key=p.api_key, base_url=p.base_url, timeout=45, max_retries=0)   # retries are handled here

    def _client(self, name: str):
        if name not in self._clients:
            self._clients[name] = self._factory(self.providers[name])
        return self._clients[name]

    def _order(self, role: str) -> list[str]:
        base = lambda n: n.split("#")[0]
        return [n for b in self.ORDER.get(role, ["gemini", "groq"]) for n in self.providers
                if base(n) == b and role in self.providers[n].models]

    def _key(self, kind: str, payload: dict) -> str:
        return hashlib.sha256(json.dumps({"k": kind, **payload}, sort_keys=True, default=str).encode()).hexdigest()

    def _cache_get(self, key: str):
        f = self.cache_dir / f"{key}.json"
        try:
            return json.loads(f.read_text()) if f.exists() else None
        except (OSError, ValueError):
            return None

    def _cache_put(self, key: str, data: dict) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(data))
        except OSError:
            pass                                             # a cache that cannot be written is not an error

    def _call(self, role: str, fn: Callable[[object, str], object]):
        """Run fn(client, model) with retry/backoff per provider, then fall back to the next provider."""
        order = self._order(role)
        if not order:
            raise LLMUnavailable(f"no provider configured for role '{role}'")
        for name in order:
            model = self.providers[name].models[role]
            for attempt in range(self.max_retries + 1):
                try:
                    self.calls += 1
                    return name, model, fn(self._client(name), model)
                except Exception as e:                       # noqa: BLE001
                    self.errors.append(f"{name}:{type(e).__name__}:{str(e)[:80]}")
                    if not _is_retryable(e) or attempt == self.max_retries:
                        break                                # non-retryable, or out of retries: next provider
                    wait = _retry_after(e) or min(2 ** attempt, 20) + random.random() * 0.3
                    self._sleep(wait)
        raise LLMUnavailable("all providers failed: " + "; ".join(self.errors[-4:]))

    # ---- public API -----------------------------------------------------------------------------------------
    def chat(self, messages: list[dict], role: str = "synth", temperature: float = 0.0, json_mode: bool = False,
             tools: list[dict] | None = None, max_tokens: int = 900, use_cache: bool = True) -> LLMResult:
        key = self._key("chat", {"role": role, "messages": messages, "t": temperature, "json": json_mode, "tools": tools, "max": max_tokens})
        if use_cache and (hit := self._cache_get(key)):
            self.cache_hits += 1
            r = LLMResult(**{**hit, "cached": True})
            self.tokens += r.total_tokens
            return r

        def go(client, model):
            kw = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
            if json_mode:
                kw["response_format"] = {"type": "json_object"}
            if tools:
                kw["tools"] = tools
            return client.chat.completions.create(**kw)

        provider, model, resp = self._call(role, go)
        msg = resp.choices[0].message
        calls = [{"name": tc.function.name, "arguments": tc.function.arguments} for tc in (getattr(msg, "tool_calls", None) or [])]
        u = getattr(resp, "usage", None)
        r = LLMResult(text=msg.content or "", provider=provider, model=model, prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      completion_tokens=getattr(u, "completion_tokens", 0) or 0, tool_calls=calls)
        self.tokens += r.total_tokens
        if use_cache and temperature == 0.0:
            self._cache_put(key, {k: v for k, v in r.__dict__.items() if k != "cached"})
        return r

    def embed(self, texts: list[str], batch: int = 64) -> list[list[float]]:
        """Embeddings for GraphRAG (Gemini only)."""
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            key = self._key("embed", {"texts": chunk, "dim": int(os.getenv("EMBED_DIM", "768"))})
            hit = self._cache_get(key)
            if hit:
                self.cache_hits += 1
                out += hit["vectors"]
                continue
            dim = int(os.getenv("EMBED_DIM", "768"))                      # 768 keeps the vector index small; Gemini supports it
            _, _, resp = self._call("embed", lambda client, model: client.embeddings.create(model=model, input=chunk, dimensions=dim))
            vecs = [d.embedding for d in resp.data]
            self._cache_put(key, {"vectors": vecs})
            out += vecs
        return out

    def snapshot(self) -> int:
        return self.tokens
