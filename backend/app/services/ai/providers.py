from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx


class ProviderUnavailable(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class AIResult:
    text: str
    usage: dict[str, int]
    model: str


class AIProvider:
    name: str

    async def complete(self, messages: list[dict[str, str]], model: str) -> AIResult:
        raise NotImplementedError

    async def health(self, model: str) -> bool:
        raise NotImplementedError

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
                    response = await client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable or attempt == 2:
                    break
                await asyncio.sleep(0.25 * (2**attempt))
        if isinstance(last_error, httpx.HTTPStatusError):
            raise ProviderRequestError(f"{self.name} returned HTTP {last_error.response.status_code}") from last_error
        raise ProviderRequestError(f"{self.name} request failed after retries") from last_error


class OpenAIProvider(AIProvider):
    name = "openai"

    def _key(self) -> str:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise ProviderUnavailable("OPENAI_API_KEY is required for OpenAI")
        return key

    async def complete(self, messages: list[dict[str, str]], model: str) -> AIResult:
        response = await self._request(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key()}"},
            json={"model": model, "messages": messages},
        )
        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"]
            usage = {
                "prompt_tokens": int(data.get("usage", {}).get("prompt_tokens", 0)),
                "completion_tokens": int(data.get("usage", {}).get("completion_tokens", 0)),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderRequestError("OpenAI returned an invalid completion response") from exc
        return AIResult(text, usage, model)

    async def health(self, model: str) -> bool:
        response = await self._request(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key()}"},
            json={"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 2},
        )
        return response.status_code == 200


class GeminiProvider(AIProvider):
    name = "gemini"

    def _key(self) -> str:
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise ProviderUnavailable("GEMINI_API_KEY is required for Gemini")
        return key

    def _url(self, model: str) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    async def complete(self, messages: list[dict[str, str]], model: str) -> AIResult:
        contents = [
            {"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]}
            for item in messages
            if item["role"] in {"user", "assistant"}
        ]
        system = next((item["content"] for item in messages if item["role"] == "system"), "")
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        response = await self._request(
            "POST",
            self._url(model),
            params={"key": self._key()},
            json=body,
        )
        data = response.json()
        try:
            text = "".join(part["text"] for part in data["candidates"][0]["content"]["parts"] if "text" in part)
            metadata = data.get("usageMetadata", {})
            usage = {
                "prompt_tokens": int(metadata.get("promptTokenCount", 0)),
                "completion_tokens": int(metadata.get("candidatesTokenCount", 0)),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderRequestError("Gemini returned an invalid completion response") from exc
        return AIResult(text, usage, model)

    async def health(self, model: str) -> bool:
        return bool((await self.complete([{"role": "user", "content": "Reply OK"}], model)).text)


class OllamaProvider(AIProvider):
    name = "ollama"

    def _base_url(self) -> str:
        return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    async def complete(self, messages: list[dict[str, str]], model: str) -> AIResult:
        response = await self._request(
            "POST",
            f"{self._base_url()}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
        )
        data = response.json()
        try:
            text = data["message"]["content"]
            usage = {
                "prompt_tokens": int(data.get("prompt_eval_count", 0)),
                "completion_tokens": int(data.get("eval_count", 0)),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRequestError("Ollama returned an invalid completion response") from exc
        return AIResult(text, usage, model)

    async def health(self, model: str) -> bool:
        response = await self._request("GET", f"{self._base_url()}/api/tags")
        models = response.json().get("models", [])
        return any(item.get("name") == model or item.get("model") == model for item in models)


PROVIDERS: dict[str, AIProvider] = {
    "openai": OpenAIProvider(),
    "gemini": GeminiProvider(),
    "ollama": OllamaProvider(),
}
