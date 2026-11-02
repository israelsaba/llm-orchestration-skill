#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3", "litellm>=1.106,<2", "httpx>=0.28,<1"]
# ///
# run.py is started with this interpreter, so litellm must be in this environment too.
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer

RUNNER = Path(__file__).resolve().parent / "run.py"
SKILL_FILE = Path(__file__).resolve().parent / "SKILL.md"

server = MCPServer(
    name="orchestration",
    instructions=(
        "Price-aware delegation to Anthropic, xAI, Gemini, and OpenAI models through LiteLLM, "
        "with API keys from the macOS Keychain. Workflow: list_models (prices, verified access), "
        "model_info on your own model and candidates (it includes memory notes on each model and its "
        "provider; switch candidates if the notes rule one out), pick the cheapest capable worker and a "
        "cheaper evaluator from another provider, call complete for each, then spend for the total. "
        "Record practical findings with memory_add only after ruling out your own misuse. "
        "Load the orchestration skill or call orchestration_guide for the full policy."
    ),
)

DESC_KEYS = "Show which providers (anthropic, xai, gemini, openai) have an API key in the macOS Keychain."
DESC_LIST = (
    "List text chat models available with the user's keys, with prices in USD per million tokens "
    "(input, output, cached input, blended = (3 x input + output) / 4), context size, and capabilities. "
    "verify=True keeps only models that answered a test call (cached 7 days; the first uncached run "
    "takes about 30 seconds). max_price filters on blended price. require takes comma-separated "
    "capabilities such as reasoning,function_calling,vision. The notes column counts memory notes on "
    "each model. Listing also deletes memory on models the provider no longer serves."
)
DESC_INFO = (
    "Full pricing (cache writes and reads, long-context tiers, batch, flex, priority, audio, image, "
    "web search, provider pricing URL) and capabilities for one or more provider/model IDs. With token "
    "counts, also estimates the cost of a job on each model. Use it on your own model ID to know your price. "
    "Each entry has memory.model_notes and memory.provider_notes: what earlier orchestrators observed in "
    "practice. Read them before choosing a worker or evaluator."
)
DESC_COMPLETE = (
    "Send a prompt to a provider/model (e.g. gemini/gemini-3.8-flash) and return JSON with the reply, "
    "token usage (including reasoning and cached tokens), cost_usd, and seconds. files are absolute paths "
    "appended to the prompt. output_file writes the reply to disk and omits it from the result, keeping it "
    "out of your context. label (worker, evaluator, ...) tags the call in the spend ledger. The model only "
    "returns text; it cannot read the repository, run code, or edit files. Never send secrets."
)
DESC_SPEND = (
    "Summarize recorded call costs from the spend ledger, optionally for the last N hours, "
    "grouped by model or label."
)
DESC_MEMORY_SHOW = (
    "Read orchestrator memory: field notes on what worked and what did not when delegating to a model "
    "or provider. models returns each model's notes plus its provider's notes; providers returns provider "
    "notes; no arguments returns everything. model_info already includes these notes. An empty result "
    "means nothing has been observed yet, not that the model is good or bad."
)
DESC_MEMORY_ADD = (
    "Record a note observed in practice on a model (scope=model, target provider/model) or a provider "
    "(scope=provider, target openai, ...). kind is works or limitation. Only record what you saw in "
    "this session's complete() calls, never claims from training knowledge. Before an AI limitation "
    "note, rule out "
    "your own prompt, parameters, or usage and describe how in skill_issue_check. author is human when "
    "the user reported it. Provenance (author, orchestrator model, session ID, timestamp, reason) is "
    "recorded; session and orchestrator default to the active opencode session and its model."
)
DESC_MEMORY_FORGET = (
    "Delete one memory note by ID, for example when it later proved to be your own misuse or the "
    "behavior changed. The deletion and reason go to the memory log."
)
DESC_MEMORY_PRUNE = (
    "Delete notes on models that are gone from the provider's live model list or past their LiteLLM "
    "deprecation date. list_models does this automatically for each provider it lists. dry_run reports "
    "without deleting. Providers whose listing fails are skipped, never pruned."
)
DESC_GUIDE = (
    "Return the full orchestration policy: how to list models, choose a worker and an evaluator by price "
    "and ability, delegate non-orchestrating steps, verify results, and control cost."
)


async def run(args: list[str], stdin: str | None = None, timeout: float = 900) -> str:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(RUNNER),
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return f"error: timed out after {timeout:.0f}s"
    text = out.decode(errors="replace").strip()
    notes = err.decode(errors="replace").strip()
    if proc.returncode != 0:
        return f"exit code {proc.returncode}\n{notes}\n{text}".strip()
    return f"{text}\n\n{notes}".strip() if notes else text


@server.tool(description=DESC_KEYS)
async def keys() -> str:
    return await run(["keys"])


@server.tool(description=DESC_LIST)
async def list_models(
    provider: Literal["anthropic", "xai", "gemini", "openai"] | None = None,
    verify: bool = True,
    refresh: bool = False,
    max_price: float | None = None,
    min_context: int | None = None,
    require: str | None = None,
    name_filter: str | None = None,
    sort: Literal["price", "name", "context"] = "price",
    as_json: bool = False,
    full_pricing: bool = False,
    show_excluded: bool = False,
) -> str:
    args = ["list", "--live", "--sort", sort]
    if verify:
        args.append("--verify")
    if refresh:
        args.append("--refresh")
    if provider:
        args += ["--provider", provider]
    if max_price is not None:
        args += ["--max-price", str(max_price)]
    if min_context:
        args += ["--min-context", str(min_context)]
    if require:
        args += ["--require", require]
    if name_filter:
        args += ["--filter", name_filter]
    if as_json or full_pricing:
        args.append("--json")
    if full_pricing:
        args.append("--full-pricing")
    if show_excluded:
        args.append("--show-excluded")
    return await run(args)


@server.tool(description=DESC_INFO)
async def model_info(
    models: list[str],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
) -> str:
    args = ["info"]
    for m in models:
        args += ["--model", m]
    if input_tokens is not None:
        args += ["--input-tokens", str(input_tokens)]
    if output_tokens is not None:
        args += ["--output-tokens", str(output_tokens)]
    if cached_tokens is not None:
        args += ["--cached-tokens", str(cached_tokens)]
    return await run(args)


@server.tool(description=DESC_COMPLETE)
async def complete(
    model: str,
    prompt: str,
    system: str | None = None,
    files: list[str] | None = None,
    output_file: str | None = None,
    label: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None,
) -> str:
    args = ["complete", "--model", model, "--json"]
    if system:
        args += ["--system", system]
    for f in files or []:
        args += ["--file", f]
    if output_file:
        args += ["--output", output_file]
    if label:
        args += ["--label", label]
    if max_tokens:
        args += ["--max-tokens", str(max_tokens)]
    if temperature is not None:
        args += ["--temperature", str(temperature)]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    return await run(args, stdin=prompt)


@server.tool(description=DESC_SPEND)
async def spend(hours: float | None = None, by: Literal["model", "label"] = "label") -> str:
    args = ["spend", "--by", by]
    if hours:
        args += ["--hours", str(hours)]
    return await run(args)


@server.tool(description=DESC_MEMORY_SHOW)
async def memory_show(models: list[str] | None = None, providers: list[str] | None = None) -> str:
    args = ["memory", "show"]
    for m in models or []:
        args += ["--model", m]
    for p in providers or []:
        args += ["--provider", p]
    return await run(args)


@server.tool(description=DESC_MEMORY_ADD)
async def memory_add(
    scope: Literal["model", "provider"],
    target: str,
    kind: Literal["works", "limitation"],
    observation: str,
    task: str,
    evidence: str,
    reason: str,
    author: Literal["human", "ai"],
    skill_issue_check: str | None = None,
    orchestrator: str | None = None,
    session: str | None = None,
) -> str:
    args = [
        "memory", "add", "--scope", scope, "--target", target, "--kind", kind,
        "--observation", observation, "--task", task, "--evidence", evidence,
        "--reason", reason, "--author", author,
    ]
    if skill_issue_check:
        args += ["--skill-issue-check", skill_issue_check]
    if orchestrator:
        args += ["--orchestrator", orchestrator]
    if session:
        args += ["--session", session]
    return await run(args)


@server.tool(description=DESC_MEMORY_FORGET)
async def memory_forget(
    note_id: str, reason: str, author: Literal["human", "ai"], session: str | None = None
) -> str:
    args = ["memory", "forget", "--id", note_id, "--reason", reason, "--author", author]
    if session:
        args += ["--session", session]
    return await run(args)


@server.tool(description=DESC_MEMORY_PRUNE)
async def memory_prune(dry_run: bool = False) -> str:
    args = ["memory", "prune"]
    if dry_run:
        args.append("--dry-run")
    return await run(args)


@server.tool(description=DESC_GUIDE)
async def orchestration_guide() -> str:
    return SKILL_FILE.read_text()


if __name__ == "__main__":
    server.run("stdio")
