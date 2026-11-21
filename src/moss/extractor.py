import json
import re
import sys
from typing import Optional

from moss.config import get_config
from moss.llm_client import LLMClient
from moss.models import ParsedResult
from moss.prompts import (
    INGEST_PROMPT_TEMPLATE,
    MEETING_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
    org_vocabulary_block,
)
from moss.skills import Skill


class Extractor:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client or LLMClient()

    def extract(self, text: str, existing_context: str = "", skill: Optional[Skill] = None) -> ParsedResult:
        # Use skill's system_prompt if available, otherwise use default
        sys_prompt = skill.system_prompt if skill and skill.system_prompt else SYSTEM_PROMPT
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": INGEST_PROMPT_TEMPLATE.format(
                text=text,
                existing_context=existing_context or "(No existing decisions or issues)",
                org_vocabulary=org_vocabulary_block(),
            )},
        ]
        try:
            raw = self._call_llm(messages)
            parsed = self._parse_json(raw)
            return ParsedResult(**parsed)
        except Exception as e:
            print(f"[Extractor] LLM extraction failed: {e}", file=sys.stderr)
            return ParsedResult()

    def meeting_extract(self, text: str, existing_decisions: list[dict]) -> dict:
        decision_summary = self._format_existing_decisions(existing_decisions)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": MEETING_PROMPT_TEMPLATE.format(
                    text=text, existing_decisions=decision_summary
                ),
            },
        ]
        try:
            raw = self._call_llm(messages)
            return self._parse_json(raw)
        except Exception as e:
            print(f"[Extractor] Meeting extraction failed: {e}", file=sys.stderr)
            return {"extracted": {}, "candidate_matches": [], "governance_gaps": []}

    def _call_llm(self, messages: list[dict]) -> str:
        return self.llm.chat(messages)

    def _parse_json(self, raw: str) -> dict:
        return LLMClient._parse_json(raw)

    def _format_existing_decisions(self, decisions: list[dict]) -> str:
        if not decisions:
            return "(No existing decisions)"
        lines = []
        for d in decisions:
            owner = d.get("owner") or "Unassigned"
            status = d.get("status", "OPEN")
            lines.append(f"- {d['id']}: {d['title']} (Owner: {owner}, Status: {status})")
        return "\n".join(lines)
