"""Unified LLM client for MOSS.

Single entry point for all LLM calls (extractor, intent_agent, agent_extractor).
Supports Ollama (default, local) and any OpenAI-compatible endpoint.

This replaces the scattered httpx.Client / ChatOpenAI initialization
across extractor.py, intent_agent.py, and agent_extractor.py.
"""

import json
import re
import sys
import time
from typing import Optional

import httpx

from moss.config import get_config


class LLMClient:
    """Unified LLM client — works with Ollama, GLM, DeepSeek, OpenAI, etc.

    All endpoints that expose /v1/chat/completions are supported.
    Default config points to local Ollama (zero external dependency).
    """

    def __init__(self, api_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 60.0):
        cfg = get_config()
        self.api_url = api_url or cfg.llm.api_url
        self.api_key = api_key or cfg.llm.api_key
        self.model = model or cfg.llm.model
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], temperature: float = 0.1,
             max_tokens: int = 4096) -> str:
        """Send chat messages and return the assistant's text response."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.time()
        print(f"[LLMClient] Calling {self.api_url} model={self.model}...", file=sys.stderr)
        response = self._client.post(self.api_url, json=payload, headers=headers)
        t1 = time.time()
        print(f"[LLMClient] Response {response.status_code} in {t1-t0:.1f}s", file=sys.stderr)

        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content

    def chat_json(self, messages: list[dict], temperature: float = 0.1,
                  max_tokens: int = 4096) -> dict:
        """Send chat messages and parse JSON from response.

        Strips markdown code fences and trailing commas before parsing.
        """
        raw = self.chat(messages, temperature, max_tokens)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Parse JSON from LLM response, handling common formatting issues."""
        cleaned = raw.strip().removeprefix("\ufeff")

        # Strip code fences
        lines = cleaned.split("\n")
        if lines and lines[0].strip().startswith(("```", "~~~")):
            lines = lines[1:]
        if lines and lines[-1].strip() in ("```", "~~~"):
            lines = lines[:-1]
        elif len(lines) >= 2 and lines[-1].strip().startswith(("```", "~~~")):
            lines = lines[:-1]
        cleaned = "\n".join(lines)

        # Fix trailing commas
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}

    def is_available(self) -> bool:
        """Quick check if the LLM endpoint is reachable."""
        try:
            # Simple health check — send a minimal request
            self._client.get(
                self.api_url.rsplit("/", 2)[0],  # base URL
                timeout=5.0,
            )
            return True
        except Exception:
            return False

    def close(self):
        self._client.close()

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass
