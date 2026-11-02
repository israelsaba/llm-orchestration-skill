---
name: orchestration
description: List Anthropic, xAI, Google Gemini, and OpenAI models with full pricing, pick the cheapest model able to do a task, and delegate work to it through the local LiteLLM install with API keys from the macOS Keychain. Use when the orchestrating model is expensive and a task step (code generation, tests, docs, summaries, extraction, review, evaluation) can be outsourced to a cheaper model, or when the user asks to list, compare, price, or call external models. Keeps OKF memory notes, with provenance, on what worked or failed in practice for each model and provider.
license: MIT
compatibility: macOS (Keychain) with uv. Memory provenance auto-detects opencode sessions; other agents pass --session and --orchestrator.
metadata:
  audience: developers
  category: llm-routing
---

# Orchestration: price-aware delegation

Runner: `run.py` in this skill's directory (for example `~/.config/opencode/skills/orchestration/run.py`). It declares its dependencies inline (PEP 723) and runs through `uv run --script`, so call it directly; do not run it with another Python.

You are the orchestrator. Your job is to plan, decompose, decide, and accept. When you are the more expensive model, hand every step that does not need your judgment to a cheaper worker model, have a second cheap model evaluate the result, verify with real tests, and only then accept it.

## 1. List models and prices

```bash
run.py list --live --verify
```

This prints only text chat models that answered a test call with the user's keys, sorted by price. Columns, all in USD per million tokens: `in` (input), `out` (output), `cached` (cache-read input), `blended` ((3 x input + output) / 4, a typical prompt-heavy ratio), then `ctx` (max input tokens) and capabilities (`tools`, `vision`, `reasoning`, `cache`, `web`, `json`).

Useful filters: `--max-price N` (max blended), `--min-context N`, `--require reasoning` (comma-separated capability names as shown in `info`), `--provider anthropic|xai|gemini|openai`, `--filter TEXT`, `--sort price|name|context`, `--json`, `--full-pricing` (with `--json`, every pricing field). Verification results are cached for 7 days; `--refresh` retests. `--show-excluded` explains hidden models.

Full pricing for any model, including yourself, with a cost estimate:

```bash
run.py info --model anthropic/claude-opus-5-5 --model gemini/gemini-3.1-flash-lite --input-tokens 50000 --output-tokens 8000
```

`info` returns every pricing field LiteLLM has: cache writes (5 minute, 1 hour), cache reads, long-context tiers (above 128k, 200k, 272k tokens), batch, flex, priority, audio, image, and video input, reasoning output, web search per query, free search quotas, the minimum cacheable prompt size, and the provider's pricing page URL. Prices come from LiteLLM's pricing table, refreshed on every run, not from the providers.

## 2. Select the model

Find your own model ID in your system prompt and run `run.py info --model <your-id>` to get your price. Then pick a worker:

1. Define what the step needs: input size (`--min-context`), whether it needs multi-step reasoning (`--require reasoning`), and how hard it is.
2. Pick the cheapest verified model that can do the job reliably:
   - Trivial (formatting, extraction, summaries, translation, simple evaluation): blended under about $0.50, such as nano, luna, flash-lite, haiku-class models.
   - Standard (a well-specified function or file, tests, docs, first-pass code review): blended about $0.50 to $3, such as mini, flash, and current grok models.
   - Hard (subtle algorithms, cross-file refactors, tricky debugging): blended about $3 to $6, such as sonnet-class, pro-preview, or mainline GPT models.
3. Prefer the newest generation in a family and undated aliases over dated snapshots. Avoid `-pro` and `o*-pro` models (blended $35 and up) unless the user asks.
4. Never delegate to a model whose blended price is at or above yours. If the cheapest capable worker is within about 3x of your price, delegation overhead outweighs savings; do the step yourself.
5. Pick the evaluator from a different provider or family than the worker, so they do not share blind spots, and at the same or lower tier.
6. Read the memory notes for each candidate. `run.py info` returns `memory.model_notes` and `memory.provider_notes` for every model, and the `notes` column of `list` shows which models have any. Weigh limitation notes against the step you are about to delegate: a note about broken diff output matters for a code step, not for a summary. If the notes rule a candidate out, pick another and read its notes, until you are satisfied. No notes means nothing has been observed yet, not that the model is safe or unsafe.

State the chosen worker, evaluator, and estimated cost in one line, then proceed. Ask the user first only if they named a model, or if `info` estimates the whole job above $1.

## 3. Delegate non-orchestrating work

Keep for yourself: planning and step definition, architecture and trade-off decisions, anything that needs tools (editing files, running commands, browsing), resolving conflicting results, final acceptance, and talking to the user. Delegate: writing code for a defined step, writing tests, docs and comments, boilerplate, summarizing large files or logs, data extraction and conversion, first-pass review, and evaluation.

Workers only return text. They cannot read the repository, run code, or edit files. Give them everything they need, and apply their output yourself.

### The loop for each step

1. Plan. Break the task into steps small enough for one worker call, each with explicit acceptance criteria.
2. Write the worker prompt. Make it self-contained: goal, constraints (language, versions, style, what must not change), the relevant files, the exact output format (a unified diff, or complete file contents in one fenced block per file with the path in the header), and the acceptance criteria. Ask for the output only, without explanation.
3. Call the worker. Pass files with `--file` so you do not load them into your own context, and save output with `--output` so it does not pass through your context either:

   ```bash
   run.py complete --model gemini/gemini-3.8-flash --label worker \
     --system "You are a senior Python engineer. Output only a unified diff." \
     --file src/parser.py --file tests/test_parser.py \
     --output /tmp/llm/step1.diff --reasoning-effort low \
     "Add support for quoted fields to parse_line(). Acceptance criteria: ..."
   ```

4. Call the evaluator with the same spec, the original files, and the worker output. Ask for a strict verdict in JSON:

   ```bash
   run.py complete --model openai/gpt-5-mini --label evaluator \
     --file src/parser.py --file /tmp/llm/step1.diff --json \
     'Review the diff against the spec below. Reply only with JSON: {"verdict": "PASS"|"FAIL", "issues": ["..."]}. Spec: ...'
   ```

5. On FAIL, call the worker again once with the evaluator's issues appended. On a second FAIL, move up one tier. If that also fails, do the step yourself.
6. On PASS, apply the output (`git apply /tmp/llm/step1.diff`, or the edit tool), then run the real checks: tests, type checker, linter, build. A real test result outranks the evaluator's opinion.
7. For UI work, run a browser test with whatever browser automation the host offers, preferably driven by a cheap model. Give it the URL, the steps, and the expected visible result.
8. Review the final diff yourself before reporting. Read diffs and verdicts, not full worker transcripts.
9. If a worker or evaluator failed in a way that survives the skill-issue check in section 4, or a setting made it notably reliable, record a memory note.

### Cost discipline

- Reasoning tokens bill as output and can dwarf the answer (a nano evaluator can spend 700+ reasoning tokens on a one-line verdict). Use `--reasoning-effort low` or `minimal` for simple steps, and `--max-tokens` to cap runaway output.
- Use `--json` on a call to see `usage` (including `reasoning_tokens` and `cached_prompt_tokens`), `cost_usd`, and `seconds`.
- Every call is logged to `~/.cache/orchestration/ledger.jsonl`. At the end of a delegated job, run `run.py spend --hours 2 --by label` and report the total, split by worker and evaluator.
- Long repeated prefixes (system prompt plus files) get cheaper with providers that support prompt caching. Keep the shared part first and identical across calls.

## 4. Memory: notes on models and providers

Orchestrators share a memory of what worked and what did not when delegating. It is an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundle at `~/.local/share/orchestration/memory` (override with `ORCHESTRATION_MEMORY_DIR`): `models/<provider>/<model>.md`, `providers/<provider>.md`, a generated `index.md`, and a dated `log.md`. Nothing exists until the first note is written. Do not create placeholder notes or files, and do not write a note just because a step finished.

Each note is a `<memory-note>` element in the concept body:

```xml
<memory-note id="mn-20261102T153012Z-a1b2">
<kind>limitation</kind>
<observation>Wrapped the unified diff in prose and a second code block on 2 of 2 calls.</observation>
<task>Unified diff for one Python function, reasoning_effort=low, max_tokens=4000.</task>
<evidence>Ledger calls at 15:28 and 15:29; same prompt passed on gemini/gemini-3.8-flash.</evidence>
<skill-issue-check>System prompt and user prompt both said diff only; output was not truncated; a second model followed the identical prompt.</skill-issue-check>
<provenance>
<author>ai</author>
<orchestrator>anthropic/claude-opus-5-5</orchestrator>
<session>ses_e62fc4958ffexcef3dI7Cd0z3A</session>
<timestamp>2026-11-02T15:30:12Z</timestamp>
<reason>Code steps on this model need a diff-extraction pass or a different worker.</reason>
</provenance>
</memory-note>
```

`memory add` writes provenance itself. Session and orchestrator default to the single opencode session active in the last 5 minutes and its model; if several are active, the error lists the candidates and you pass `--session`. Outside opencode, pass both `--session` (any stable ID for the current conversation) and `--orchestrator` (your model ID). You can edit notes by hand, keeping the tags well formed.

### When to write a note

Only write what you observed in practice in this session, from your own `complete` calls to that model (or, for a provider note, to that provider's models). Never write what you believe from training data, documentation you remember, or other models' reputations. The runner does not check this; it is on you.

Before recording a limitation, rule out your own skill issue. Check at least:

- The prompt was self-contained, unambiguous, and stated the output format; you did not rely on context the worker cannot see.
- Parameters were not the cause: `max_tokens` did not truncate the answer, `reasoning_effort` was suitable, the input fit the context window, and no file was missing.
- The failure reproduced on a retry with a clarified prompt, or another model of a similar tier succeeded with the identical prompt.
- The error came from the model or provider (wrong output, refusals, malformed format, provider errors), not from the runner, your keys, or the network.

Describe what you did in `skill_issue_check`. If you cannot rule out your own misuse, do not write the note.

Write a `works` note when a model did something notably well or a setting made it reliable (for example, "follows JSON-only verdict format at reasoning_effort=minimal"). Do not write one for ordinary success.

Use provider scope only for behavior that belongs to the provider rather than one model: API errors, rate limits, parameter handling, or a pattern you saw on two or more of its models. Use `--author human` when the user tells you about a behavior.

If a note later turns out to be your own misuse or the behavior changed, delete it with `memory forget --id ... --reason ...`.

### Deprecated models

`list --live` deletes notes on models that the provider's live listing no longer returns or whose LiteLLM `deprecation_date` has passed, and reports each deletion. `memory prune` does the same on demand (`--dry-run` to preview). A provider whose listing fails is skipped, never pruned. Every addition and deletion is recorded in `log.md`.

## MCP tools

The same functions are exposed by the optional `orchestration` MCP server (`mcp_server.py`, registered in the agent's MCP configuration), which wraps `run.py`. Prefer the MCP tools when they are available; use `run.py` from bash otherwise.

| MCP tool | Equivalent command |
| --- | --- |
| `orchestration_keys` | `run.py keys` |
| `orchestration_list_models` | `run.py list --live --verify` (`verify` defaults to true) |
| `orchestration_model_info` | `run.py info` |
| `orchestration_complete` | `run.py complete --json` (`files`, `output_file`, `label`, `reasoning_effort`) |
| `orchestration_spend` | `run.py spend` |
| `orchestration_memory_show` | `run.py memory show` |
| `orchestration_memory_add` | `run.py memory add` |
| `orchestration_memory_forget` | `run.py memory forget` |
| `orchestration_memory_prune` | `run.py memory prune` |
| `orchestration_orchestration_guide` | returns this file |

## Command reference

- `run.py keys`: which providers have a Keychain key.
- `run.py list [--live] [--verify] [--refresh] [--max-price N] [--min-context N] [--require CAPS] [--provider P] [--filter TEXT] [--sort price|name|context] [--all] [--show-excluded] [--json] [--full-pricing]`
- `run.py info --model M [--model M ...] [--input-tokens N] [--output-tokens N] [--cached-tokens N]`
- `run.py complete --model M [--system S] [--file PATH ...] [--output PATH] [--label L] [--max-tokens N] [--temperature T] [--reasoning-effort minimal|low|medium|high] [--json] [PROMPT]` (the prompt can also come from stdin)
- `run.py spend [--hours N] [--by model|label]`
- `run.py memory show [--model M ...] [--provider P ...]`
- `run.py memory add --scope model|provider --target T --kind works|limitation --observation TEXT --task TEXT --evidence TEXT --reason TEXT --author human|ai [--skill-issue-check TEXT] [--orchestrator M] [--session ID]`
- `run.py memory forget --id ID --reason TEXT --author human|ai [--session ID]`
- `run.py memory prune [--dry-run]`

Exit codes: `0` ok, `1` no models matched, `2` bad arguments, `3` missing Keychain key, `4` provider error (auth, quota, unknown model). Notes and errors go to stderr. If live listing returns HTTP 401 for a provider, the key is invalid, revoked, or truncated. Tell the user, and do not retry with other keys.

## Providers and keys

| Provider | Prefix | Keychain service (default) |
| --- | --- | --- |
| Anthropic | `anthropic/` | `anthropic-api-key` |
| xAI | `xai/` | `xai-api-key` |
| Google AI Studio | `gemini/` | `gemini-api-key`, falls back to `browser-use-google-api-key` |
| OpenAI | `openai/` | `openai_api_key` |

The Keychain account is `$USER`. To use other service names, create `keychain.json` next to `run.py`:

```json
{ "account": "your-macos-user", "services": { "xai": "my-grok-key" } }
```

To add or replace a key, the user copies it to the clipboard and runs this in their own terminal, so the key never lands in shell history:

```bash
KEY="$(pbpaste)"; security add-generic-password -U -a "$USER" -s openai_api_key -w "$KEY"; unset KEY; printf '' | pbcopy
```

Do not suggest the interactive prompt (`-w` with no value): it silently cuts input at 128 characters, which breaks OpenAI `sk-proj-` keys (about 164 characters). If a provider returns 401 right after a key update, check the stored key's length (never its value) to spot this.

## Data safety

Everything passed to `complete` leaves the machine and goes to a third-party provider. Never send `.env` files, credentials, private keys, tokens, or customer data unless the user approved it. Never read, print, or echo key values (`security ... -w`), and never put keys in prompts, files, logs, or responses. The runner passes keys straight to LiteLLM and redacts them from error messages.
