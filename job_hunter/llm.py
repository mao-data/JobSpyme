"""Two ways to ask Claude for structured JSON:

- ClaudeCode: runs the `claude` CLI (Claude Code) in print mode. Uses your Claude
  Pro/Max subscription, no API key. Counts against the subscription's usage limits.
- ClaudeAPI: the Anthropic API with ANTHROPIC_API_KEY. Billed per token.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

# Opus 5.5 list price, USD per million tokens, for the API cost estimate.
PRICE_IN, PRICE_OUT = 4.00, 20.00


class LLMError(RuntimeError):
    pass


class ClaudeCode:
    def __init__(self, system: str, model: str | None = None, timeout: int = 600):
        self.exe = shutil.which("claude")
        if not self.exe:
            raise SystemExit(
                "Claude Code CLI not found. Install it with `npm install -g @anthropic-ai/claude-code`, "
                "run `claude` once to log in with your Claude account, then try again."
            )
        self.system = system
        self.model = model
        self.timeout = timeout
        # Run from an empty folder so no project CLAUDE.md or settings leak into the prompt.
        self.workdir = tempfile.mkdtemp(prefix="job_hunter_")
        self.calls = 0

    def ask(self, prompt: str, schema: dict, effort: str = "low") -> dict:
        cmd = [
            self.exe, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--tools", "",
            "--no-session-persistence",
            "--append-system-prompt", self.system,
        ]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=self.timeout, cwd=self.workdir, encoding="utf-8")
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude CLI timed out after {self.timeout}s") from e
        self.calls += 1
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"claude CLI gave no JSON (exit {proc.returncode}): {proc.stderr[-500:] or proc.stdout[-500:]}") from e
        if out.get("is_error") or out.get("subtype") != "success":
            raise LLMError(f"claude CLI error: {out.get('result') or out.get('subtype')}")
        data = out.get("structured_output")
        if data is None:  # older CLI without structured output: parse the text
            data = json.loads(out.get("result", ""))
        return data

    def usage_note(self) -> str:
        return f"Claude Code: {self.calls} calls on your subscription"


class ClaudeAPI:
    def __init__(self, system: str, model: str = "claude-opus-5-5"):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=4)
        self.system = system
        self.model = model
        self.tokens_in = self.tokens_out = 0

    def ask(self, prompt: str, schema: dict, effort: str = "low") -> dict:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                cache_control={"type": "ephemeral"},
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self.anthropic.APIError as e:
            raise LLMError(str(e)) from e
        u = response.usage
        self.tokens_in += (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        self.tokens_out += u.output_tokens or 0
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response cut off at max_tokens")
        return json.loads(next(b.text for b in response.content if b.type == "text"))

    def usage_note(self) -> str:
        cost = self.tokens_in / 1e6 * PRICE_IN + self.tokens_out / 1e6 * PRICE_OUT
        return f"Claude API: {self.tokens_in:,} in / {self.tokens_out:,} out tokens, about ${cost:.2f}"


def make_llm(claude_cfg: dict, system: str):
    if claude_cfg.get("backend", "claude_code") == "api":
        return ClaudeAPI(system, claude_cfg.get("model", "claude-opus-5-5"))
    return ClaudeCode(system, claude_cfg.get("cli_model"))
