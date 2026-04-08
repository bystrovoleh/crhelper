import json
import re
import subprocess
import sys
import time
from typing import Any
from config.settings import LLM_PROVIDER, ANTHROPIC_API_KEY, CLAUDE_MODEL

# On Windows, claude is installed as claude.cmd
_CLAUDE_CMD = "claude.cmd" if sys.platform == "win32" else "claude"


# ---------------------------------------------------------------------------
# Response normalizer
# ---------------------------------------------------------------------------

def normalize_llm_response(raw: str) -> str:
    """
    Clean raw LLM output and extract JSON regardless of formatting.
    Handles markdown code blocks, extra text, and encoding issues.
    """
    text = raw.strip()

    # Remove markdown code fences: ```json ... ``` or ``` ... ```
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    # Try to extract the first JSON object or array
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if match:
        return match.group(1).strip()

    return text.strip()


def parse_json_response(raw: str, retry_context: str = "") -> dict:
    """
    Parse JSON from LLM response with up to 3 retries.
    Returns parsed dict or raises ValueError after exhausting retries.
    """
    cleaned = normalize_llm_response(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Failed to parse JSON from LLM response.\n"
            f"Raw (first 500 chars): {raw[:500]}\n"
            f"Cleaned: {cleaned[:500]}\n"
            f"Error: {e}"
        )


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

class ClaudeCLIBackend:
    """Send prompts to Claude via the `claude` CLI tool."""

    def __init__(self, model: str = CLAUDE_MODEL):
        self.model = model

    def complete(self, system: str, user: str, _retry: int = 0) -> str:
        prompt = f"{system}\n\n{user}" if system else user
        result = subprocess.run(
            [_CLAUDE_CMD, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=120,
            shell=(sys.platform == "win32"),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Rate limit or transient CLI error — wait and retry once
            if _retry == 0 and ("rate" in stderr.lower() or "overloaded" in stderr.lower() or not stderr):
                wait = 30
                print(f"  [llm] CLI error, waiting {wait}s before retry... ({stderr[:80] or 'no stderr'})")
                time.sleep(wait)
                return self.complete(system, user, _retry=1)
            raise RuntimeError(
                f"claude CLI error (code {result.returncode}):\n{stderr}"
            )
        return result.stdout.strip()


class ClaudeAPIBackend:
    """Send prompts to Claude via the Anthropic API."""

    def __init__(self, model: str = CLAUDE_MODEL, api_key: str = ANTHROPIC_API_KEY):
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(self, system: str, user: str) -> str:
        message = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text


# ---------------------------------------------------------------------------
# LLM client with retry logic
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Unified LLM client. Switched via LLM_PROVIDER env var.
    Handles JSON parsing with retries and normalization.
    """

    def __init__(self):
        if LLM_PROVIDER == "claude_api":
            self._backend = ClaudeAPIBackend()
        else:
            self._backend = ClaudeCLIBackend()

    def complete(self, system: str, user: str) -> str:
        """Raw completion — returns string."""
        return self._backend.complete(system, user)

    def complete_json(self, system: str, user: str, max_retries: int = 3) -> dict:
        """
        Complete and parse JSON response.
        On failure, retries with an explicit correction prompt.
        """
        json_reminder = (
            "\n\nIMPORTANT: Your response MUST be valid JSON only. "
            "No markdown, no explanation, no code fences. "
            "Start with { and end with }."
        )

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                if attempt == 1:
                    prompt = user + json_reminder
                else:
                    prompt = (
                        f"{user}{json_reminder}\n\n"
                        f"Previous attempt failed with: {last_error}\n"
                        f"Try again. Output ONLY valid JSON."
                    )
                raw = self._backend.complete(system, prompt)
                return parse_json_response(raw)
            except (ValueError, Exception) as e:
                last_error = str(e)
                if attempt < max_retries:
                    print(f"  [llm] JSON parse failed (attempt {attempt}/{max_retries}), retrying...")

        raise ValueError(
            f"LLM returned invalid JSON after {max_retries} attempts.\n"
            f"Last error: {last_error}"
        )
