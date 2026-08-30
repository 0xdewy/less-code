"""LLM backends. `none` = static-only mode. `ollama` = local Qwen via
ollama serve. `openai` = any OpenAI-compatible endpoint (base URL + key)."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Backend:
    name: str

    def complete(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        raise NotImplementedError


class NullBackend(Backend):
    def __init__(self) -> None:
        super().__init__("none")

    def complete(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        return ""


class OllamaBackend(Backend):
    def __init__(self, model: str = "qwen2.5-coder:1.5b", host: str = "http://127.0.0.1:11434", num_ctx: int = 32768) -> None:
        super().__init__("ollama")
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx

    def complete(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": self.num_ctx},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
        return data.get("message", {}).get("content", "")


class OpenAICompatBackend(Backend):
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None) -> None:
        super().__init__("openai")
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def complete(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": 8192,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]


def ensure_ollama(model: str = "qwen2.5-coder:1.5b") -> OllamaBackend:
    """Start ollama server if needed and pull the model."""
    backend = OllamaBackend(model=model)
    try:
        backend.complete("ping", "ping", temperature=0.0)
        return backend
    except (urllib.error.URLError, OSError):
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        import time

        for _ in range(30):
            time.sleep(1)
            try:
                subprocess.run(
                    ["ollama", "pull", model], check=True, capture_output=True, timeout=1200
                )
                return backend
            except (subprocess.SubprocessError, OSError):
                continue
        raise RuntimeError("could not start ollama server")


class BudgetBackend(Backend):
    """Hard cap on total LLM calls — keeps GPU time bounded on shared machines."""

    def __init__(self, inner: Backend, max_calls: int) -> None:
        super().__init__(f"{inner.name}(budget={max_calls})")
        self.inner = inner
        self.max_calls = max_calls
        self.calls = 0

    def complete(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        if self.calls >= self.max_calls:
            raise RuntimeError(f"LLM budget exhausted ({self.max_calls} calls)")
        self.calls += 1
        return self.inner.complete(system, prompt, temperature)


def make_backend(spec: str, model: str | None = None, num_ctx: int = 32768) -> Backend:
    if spec == "none":
        return NullBackend()
    if spec == "ollama":
        try:
            backend = OllamaBackend(model=model or "qwen2.5-coder:1.5b", num_ctx=num_ctx)
            backend.complete("ping", "ping", temperature=0.0)
            return backend
        except Exception:
            return ensure_ollama(model=model or "qwen2.5-coder:1.5b")
    if spec == "openai":
        return OpenAICompatBackend(model=model or "gpt-4o-mini")
    raise ValueError(f"unknown backend {spec}")
