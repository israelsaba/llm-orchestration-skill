---
type: knowledge_bundle_index
title: LLM Orchestration Skill
description: An Agent Skill that lets an expensive coding agent price models, delegate well-specified steps to cheaper Anthropic, xAI, Gemini, or OpenAI models through LiteLLM, and verify the results.
tags:
  [
    agent-skills,
    llm-orchestration,
    model-routing,
    llm-pricing,
    litellm,
    mcp,
    cost-optimization,
  ]
timestamp: 2026-11-02T00:00:00-03:00
---

# LLM Orchestration Skill

This repository publishes the `orchestration` Agent Skill. It teaches a coding agent running on an expensive model to act as an orchestrator: list the models your API keys can reach with current prices, pick the cheapest one able to do a step, hand that step to it, have a second cheap model from another provider grade the output, and accept the result only after real tests pass.

It works with OpenCode, Claude Code, Codex, Hermes, and other agents that load `SKILL.md` files and can run shell commands. An optional MCP server exposes the same functions as tools.

Requirements: macOS (API keys are read from the Keychain), [uv](https://docs.astral.sh/uv/), and at least one API key for Anthropic, xAI, Google AI Studio, or OpenAI. uv installs LiteLLM and the MCP SDK on first run from the inline script metadata.

## Install The Skill

Review `SKILL.md`, `run.py`, `memory_store.py`, and `mcp_server.py` first. The skill tells your agent to send prompts and files to third-party model providers, so read it as you would any code that runs with your agent's permissions.

Clone a reviewed release and copy the whole directory, keeping all four files together:

```bash
git clone --depth 1 --branch v1.0.0 https://github.com/israelsaba/llm-orchestration-skill.git
mkdir -p ~/.config/opencode/skills/orchestration
cp llm-orchestration-skill/SKILL.md llm-orchestration-skill/*.py ~/.config/opencode/skills/orchestration/
```

Other agents use the same files in a different directory:

| Agent | User skill directory |
| --- | --- |
| OpenCode | `~/.config/opencode/skills/orchestration/` |
| Claude Code | `~/.claude/skills/orchestration/` |
| Codex | `~/.agents/skills/orchestration/` |
| Hermes | `~/.hermes/skills/orchestration/` |

Start a new agent session after installing. A ZIP download from the release page works too: copy `SKILL.md` and the three `.py` files into the directory above.

## Add API Keys

Keys live in the macOS Keychain under the account `$USER`. Copy a key to the clipboard and run this in your own terminal so it never lands in shell history:

```bash
KEY="$(pbpaste)"; security add-generic-password -U -a "$USER" -s openai_api_key -w "$KEY"; unset KEY; printf '' | pbcopy
```

Default service names are `anthropic-api-key`, `xai-api-key`, `gemini-api-key`, and `openai_api_key`. Check what the runner can see with:

```bash
~/.config/opencode/skills/orchestration/run.py keys
```

To use other service names, put a `keychain.json` next to `run.py` (see `SKILL.md`).

## Use It

Ask the agent to use the `orchestration` skill on a task, or ask it to compare model prices. Typical runner calls:

```bash
run.py list --live --verify --max-price 3
run.py info --model gemini/gemini-3.1-flash-lite --input-tokens 50000 --output-tokens 8000
run.py complete --model openai/gpt-5-mini --label worker --file src/parser.py --output /tmp/step1.diff "..."
run.py spend --hours 2 --by label
```

`list --live --verify` shows only text chat models that answered a test call with your keys. Prices come from LiteLLM's pricing table, refreshed on each run. Every `complete` call is logged with its cost in `~/.cache/orchestration/ledger.jsonl`.

## Optional MCP Server

`mcp_server.py` wraps the runner as MCP tools (`orchestration_list_models`, `orchestration_complete`, and so on). For OpenCode, add this to `opencode.json`, replacing the path with your install location:

```json
{
  "mcp": {
    "orchestration": {
      "type": "local",
      "command": ["uv", "run", "--script", "/Users/you/.config/opencode/skills/orchestration/mcp_server.py"],
      "enabled": true,
      "timeout": 900000
    }
  }
}
```

For Claude Code:

```bash
claude mcp add orchestration -- uv run --script ~/.claude/skills/orchestration/mcp_server.py
```

Restart the agent after changing MCP configuration. The long timeout matters because the first verified listing can take about 30 seconds and a worker call can take minutes.

## Memory Notes

The skill keeps notes on what each model and provider did well or badly in real delegated calls, stored as an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundle in `~/.local/share/orchestration/memory`. Each note records who wrote it, which orchestrator model and session, when, and why. The skill requires the agent to rule out its own prompt or parameter mistakes before recording a limitation. Notes on models a provider stops serving are deleted automatically.

In OpenCode, session and orchestrator are read from the local OpenCode database. Other agents pass `--session` and `--orchestrator` to `memory add`.

## Safe Permissions

- Network: the runner calls provider APIs (Anthropic, xAI, Google, OpenAI) and fetches LiteLLM's pricing table from GitHub. uv downloads the pinned dependency ranges from PyPI on first run.
- Keychain: read access to the four service names above. Keys go straight to LiteLLM and are redacted from error messages.
- Filesystem: reads only the files the agent passes with `--file`; writes the ledger and verify cache under `~/.cache/orchestration/`, memory under `~/.local/share/orchestration/memory`, and any `--output` path.
- Data: everything passed to `complete` leaves your machine. Do not let the agent send `.env` files, credentials, or customer data without your approval.

## Limitations

- macOS only, because key lookup uses the `security` command. Linux and Windows are not supported.
- Model prices and capability flags are as accurate as LiteLLM's table. Check the provider's pricing page (`run.py info` links it) before trusting an estimate for a large job.
- An evaluator model's PASS is an opinion. The skill tells the agent to run real tests before accepting output, and that step matters most.
- Tested with OpenCode on macOS. Claude Code, Codex, and Hermes installs follow their documented skill directories but were not run end to end.

## FAQ

### Does this save money?

It can when the orchestrating model costs much more than a capable worker. The skill tells the agent to skip delegation when the cheapest suitable worker is within about 3x of its own price, because prompt overhead eats the savings. `run.py spend` reports what delegation actually cost.

### Which providers are supported?

Anthropic, xAI, Google AI Studio (Gemini), and OpenAI, through LiteLLM. Adding a provider means extending `DEFAULT_SERVICES` and `live_models` in `run.py`.

### Can workers edit my files?

No. Workers return text only. The orchestrating agent applies their diffs or file contents itself and runs your tests.

## Contributing

Contributions are welcome through pull requests. Keep `SKILL.md` as the entry point, keep the four files usable when copied as one directory, describe the commands you ran to check the change, and never include keys, ledger entries, or memory notes from your own machine. Discuss larger changes, such as new providers or Linux key storage, in an issue first.

Use issues for reproducible bugs, unclear guidance, installation problems, compatibility reports for other agents, and focused proposals. Include the version or commit, agent and version, macOS version, uv version, expected behavior, actual behavior, and a minimal example with keys and private data removed. Report security problems as described in [SECURITY.md](SECURITY.md), not in public issues.

## Releases

Stable releases use `vMAJOR.MINOR.PATCH` tags and GitHub release notes. Install a tagged release when reproducibility matters.

## Sources

| Source | Claim supported |
| --- | --- |
| [`SKILL.md`](SKILL.md) | Delegation policy, model selection rules, memory rules, command reference |
| [Agent Skills specification](https://agentskills.io/specification) | `SKILL.md` frontmatter and directory format |
| [OpenCode skills](https://opencode.ai/docs/skills/) and [MCP servers](https://opencode.ai/docs/mcp-servers/) | OpenCode skill and MCP paths |
| [Claude Code skills](https://code.claude.com/docs/en/skills) | Claude Code skill directory |
| [Codex skills](https://developers.openai.com/codex/skills) | Codex user skill directory `~/.agents/skills` (observed 2026-11-02) |
| [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) | Hermes skill directory |
| [uv scripts guide](https://docs.astral.sh/uv/guides/scripts/) | Inline script metadata and `uv run --script` shebang |
| [LiteLLM](https://github.com/BerriAI/litellm) | Provider calls, pricing table, cost calculation |
| [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) | Memory bundle layout |

## License

MIT. See [LICENSE](LICENSE).

[![M8ven Score](https://m8ven.ai/badge/mcp/israelsaba-llm-orchestration-skill-0tf030?v=6a5dfdfa32f8608f1bd9fc1025af8191)](https://m8ven.ai/mcp/israelsaba-llm-orchestration-skill-0tf030?s=readme)

