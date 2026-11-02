#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["litellm>=1.106,<2", "httpx>=0.28,<1"]
# ///
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import memory_store as memory

os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "False")

SKILL_DIR = Path(__file__).resolve().parent
OVERRIDES_FILE = SKILL_DIR / "keychain.json"

DEFAULT_SERVICES: dict[str, str] = {
    "anthropic": "anthropic-api-key",
    "xai": "xai-api-key",
    "gemini": "gemini-api-key",
    "openai": "openai_api_key",
}
FALLBACK_SERVICES: dict[str, list[str]] = {
    "gemini": ["browser-use-google-api-key"],
}
PROVIDERS = list(DEFAULT_SERVICES)


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def load_overrides() -> dict:
    if OVERRIDES_FILE.exists():
        try:
            return json.loads(OVERRIDES_FILE.read_text())
        except json.JSONDecodeError as exc:
            eprint(f"warning: ignoring invalid {OVERRIDES_FILE}: {exc}")
    return {}


def keychain_lookup(service: str, account: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def get_key(provider: str) -> tuple[str | None, str | None]:
    ov = load_overrides()
    account = ov.get("account") or os.environ.get("USER") or getpass.getuser()
    services = []
    if provider in ov.get("services", {}):
        services.append(ov["services"][provider])
    services.append(DEFAULT_SERVICES[provider])
    services.extend(FALLBACK_SERVICES.get(provider, []))
    for svc in dict.fromkeys(services):
        key = keychain_lookup(svc, account)
        if key:
            return key, svc
    return None, None


def normalize(provider: str, model: str) -> str:
    model = model.strip()
    if model.startswith("models/"):
        model = model[len("models/"):]
    if model.startswith(provider + "/"):
        return model
    return f"{provider}/{model}"


PRICING_EXTRA_KEYS = (
    "prompt_cache_min_tokens",
    "search_free_requests",
    "web_search_billing_unit",
    "source",
)


def per_m(v: object) -> float | None:
    return round(v * 1_000_000, 6) if isinstance(v, (int, float)) else None


def raw_entry(full: str) -> dict:
    import litellm

    bare = full.split("/", 1)[1] if "/" in full else full
    return dict(litellm.model_cost.get(full) or litellm.model_cost.get(bare) or {})


def deprecation_date(full: str) -> str | None:
    return raw_entry(full).get("deprecation_date")


def normalize_pricing(entry: dict) -> dict:
    pricing: dict = {}
    for k, v in sorted(entry.items()):
        if "cost" not in k or v is None or v == {}:
            continue
        if isinstance(v, (int, float)) and "_per_token" in k:
            pricing[k.replace("_per_token", "_per_mtok")] = per_m(v)
        elif isinstance(v, (int, float)) and "_token_cost" in k:
            pricing[k.replace("_token_cost", "_cost_per_mtok")] = per_m(v)
        else:
            pricing[k] = v
    for k in PRICING_EXTRA_KEYS:
        if k in entry:
            pricing["pricing_source" if k == "source" else k] = entry[k]
    return pricing


def model_info(full: str) -> dict:
    import litellm

    try:
        info = dict(litellm.get_model_info(full))
    except Exception:
        return {}
    entry = raw_entry(full)
    merged = {**{k: v for k, v in info.items() if v is not None}, **entry}
    inp = per_m(merged.get("input_cost_per_token"))
    out = per_m(merged.get("output_cost_per_token"))
    cached = per_m(merged.get("cache_read_input_token_cost"))
    blended = round((3 * inp + out) / 4, 6) if inp is not None and out is not None else None
    capabilities = sorted(
        k[len("supports_"):] for k, v in merged.items() if k.startswith("supports_") and v is True
    )
    return {
        "mode": merged.get("mode"),
        "max_input_tokens": merged.get("max_input_tokens"),
        "max_output_tokens": merged.get("max_output_tokens"),
        "input_usd_per_mtok": inp,
        "output_usd_per_mtok": out,
        "cached_input_usd_per_mtok": cached,
        "blended_usd_per_mtok": blended,
        "input_modalities": merged.get("supported_modalities"),
        "output_modalities": merged.get("supported_output_modalities"),
        "capabilities": capabilities,
        "endpoints": merged.get("supported_endpoints"),
        "rpm": merged.get("rpm"),
        "tpm": merged.get("tpm"),
        "pricing": normalize_pricing(merged),
    }


def estimate_cost(info: dict, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
    inp = info.get("input_usd_per_mtok")
    out = info.get("output_usd_per_mtok")
    if inp is None or out is None:
        return None
    cached_rate = info.get("cached_input_usd_per_mtok")
    if cached_rate is None:
        cached_rate = inp
    fresh = max(input_tokens - cached_tokens, 0)
    return round((fresh * inp + cached_tokens * cached_rate + output_tokens * out) / 1_000_000, 6)


def catalog_models(provider: str) -> list[str]:
    import litellm

    return sorted({normalize(provider, m) for m in litellm.models_by_provider.get(provider, [])})


class LiveListError(Exception):
    pass


def _get_json(url: str, headers: dict, key: str, params: dict | None = None) -> dict:
    import httpx

    try:
        r = httpx.get(url, headers=headers, params=params, timeout=30)
    except httpx.HTTPError as exc:
        raise LiveListError(f"network error: {type(exc).__name__}") from None
    if r.status_code != 200:
        detail = r.text.replace(key, "***")
        try:
            err = r.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else str(err)
        except Exception:
            pass
        import re
        detail = re.sub(r"(sk-|xai-|AIza)[A-Za-z0-9_\-*]{6,}", r"\1***", detail)
        raise LiveListError(f"HTTP {r.status_code}: {detail[:200]}")
    return r.json()


def live_models(provider: str, key: str) -> list[str]:
    ids: list[str] = []
    if provider in ("openai", "xai"):
        base = "https://api.openai.com/v1" if provider == "openai" else "https://api.x.ai/v1"
        data = _get_json(f"{base}/models", {"Authorization": f"Bearer {key}"}, key)
        ids = [m["id"] for m in data.get("data", [])]
    elif provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        params: dict = {"limit": 1000}
        while True:
            data = _get_json("https://api.anthropic.com/v1/models", headers, key, params)
            ids += [m["id"] for m in data.get("data", [])]
            if not data.get("has_more"):
                break
            params["after_id"] = data.get("last_id")
    elif provider == "gemini":
        headers = {"x-goog-api-key": key}
        params = {"pageSize": 1000}
        while True:
            data = _get_json(
                "https://generativelanguage.googleapis.com/v1beta/models", headers, key, params
            )
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    ids.append(m["name"])
            tok = data.get("nextPageToken")
            if not tok:
                break
            params["pageToken"] = tok
    return sorted({normalize(provider, i) for i in ids})


NON_TEXT_PATTERNS = re.compile(
    r"(audio|tts|transcribe|realtime|lyria|imagen|image|veo|embedding|"
    r"moderation|computer-use|deep-research|-instruct|babbage|davinci|"
    r"live-|native-audio)",
    re.IGNORECASE,
)

CACHE_FILE = Path.home() / ".cache" / "orchestration" / "verify.json"
VERIFY_TTL_SECONDS = 7 * 24 * 3600


def text_chat_reason(model: str, info: dict) -> str | None:
    if not info:
        return "not in LiteLLM catalog"
    if info.get("mode") not in ("chat", "responses"):
        return f"mode={info.get('mode')}"
    if NON_TEXT_PATTERNS.search(model.split("/", 1)[1]):
        return "non-text model family"
    endpoints = info.get("endpoints")
    if endpoints and not any(e in endpoints for e in ("/v1/chat/completions", "/v1/responses")):
        return "no chat endpoint"
    return None


def load_verify_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_verify_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def clean_error(exc: Exception, key: str, limit: int = 300) -> str:
    text = str(exc).replace(key, "***")
    text = re.sub(r"(sk-|xai-|AIza)[A-Za-z0-9_\-*]{6,}", r"\1***", text)
    match = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', text)
    if match:
        text = match.group(1)
    else:
        text = re.sub(r"^(litellm\.)?\w+Error:\s*", "", text.strip().splitlines()[0])
    return f"{type(exc).__name__}: {text[:limit]}"


def probe(model: str, key: str) -> tuple[bool, str]:
    import litellm

    litellm.drop_params = True
    litellm.suppress_debug_info = True
    try:
        litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with: ok"}],
            max_completion_tokens=64,
            api_key=key,
            timeout=60,
            num_retries=0,
        )
        return True, "ok"
    except Exception as exc:
        return False, clean_error(exc, key, 160)


def verify_models(rows: list[dict], keys: dict[str, str], refresh: bool, failed: list[str]) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    cache = load_verify_cache()
    now = int(time.time())
    todo = [
        r for r in rows
        if refresh
        or r["model"] not in cache
        or now - cache[r["model"]].get("at", 0) > VERIFY_TTL_SECONDS
    ]
    if todo:
        eprint(f"verifying {len(todo)} models with a short test prompt...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(lambda r: (r["model"], probe(r["model"], keys[r["provider"]])), todo)
            for model, (ok, detail) in results:
                cache[model] = {"ok": ok, "detail": detail, "at": now}
        save_verify_cache(cache)
    kept = []
    for r in rows:
        entry = cache.get(r["model"], {})
        if entry.get("ok"):
            kept.append(r)
        else:
            failed.append(f"{r['model']} (failed test call: {entry.get('detail', 'unknown')})")
    return kept


def cmd_keys(_: argparse.Namespace) -> int:
    for p in PROVIDERS:
        key, svc = get_key(p)
        status = f"found (service: {svc})" if key else f"MISSING (expected service: {DEFAULT_SERVICES[p]})"
        print(f"{p:10s} {status}")
    return 0


LEDGER_FILE = Path.home() / ".cache" / "orchestration" / "ledger.jsonl"

SORT_KEYS = {
    "name": lambda r: (r["provider"], r["model"]),
    "price": lambda r: (r.get("blended_usd_per_mtok") is None, r.get("blended_usd_per_mtok") or 0, r["model"]),
    "context": lambda r: (-(r.get("max_input_tokens") or 0), r["model"]),
}


def short_caps(r: dict) -> str:
    caps = set(r.get("capabilities") or [])
    flags = [
        ("tools", "function_calling"),
        ("vision", "vision"),
        ("reasoning", "reasoning"),
        ("cache", "prompt_caching"),
        ("web", "web_search"),
        ("json", "response_schema"),
    ]
    return ",".join(label for label, cap in flags if cap in caps)


def fmt_price(v: object) -> str:
    return "?" if v is None else f"{v:g}"


def cmd_list(a: argparse.Namespace) -> int:
    providers = [a.provider] if a.provider else PROVIDERS
    rows: list[dict] = []
    notes: list[str] = []
    excluded: list[str] = []
    keys: dict[str, str] = {}
    required = {c.strip() for c in (a.require or "").split(",") if c.strip()}
    for p in providers:
        key, _ = get_key(p)
        if not key:
            notes.append(f"{p}: no Keychain key, skipped")
            continue
        keys[p] = key
        source = "catalog"
        if a.live:
            try:
                models = live_models(p, key)
                source = "live"
            except LiveListError as exc:
                notes.append(f"{p}: live listing failed, provider skipped ({exc})")
                continue
            if not models:
                notes.append(f"{p}: live listing returned no models")
                continue
            gone = memory.deprecated_models(p, set(models), deprecation_date)
            memory.delete_models(gone)
            notes.extend(f"memory: deleted notes on {m} ({why})" for m, why in gone)
        else:
            models = catalog_models(p)
        for m in models:
            if a.filter and a.filter.lower() not in m.lower():
                continue
            info = model_info(m)
            if not a.all:
                reason = text_chat_reason(m, info)
                if reason:
                    excluded.append(f"{m} ({reason})")
                    continue
            if required and not required <= set(info.get("capabilities") or []):
                continue
            if a.max_price is not None:
                blended = info.get("blended_usd_per_mtok")
                if blended is None or blended > a.max_price:
                    continue
            if a.min_context and (info.get("max_input_tokens") or 0) < a.min_context:
                continue
            rows.append({"model": m, "provider": p, "source": source, **info})

    failed: list[str] = []
    if a.verify and rows:
        rows = verify_models(rows, keys, a.refresh, failed)

    rows.sort(key=SORT_KEYS[a.sort])

    for r in rows:
        r["memory_notes"] = len(memory.parse_notes(memory.model_path(r["model"])))
    for p in keys:
        n = len(memory.parse_notes(memory.provider_path(p)))
        if n:
            notes.append(f"memory: {n} provider notes on {p} (model_info shows them)")

    if a.show_excluded:
        notes.extend(f"excluded: {e}" for e in excluded + failed)
    else:
        if excluded:
            notes.append(f"{len(excluded)} non-text-chat models hidden (--show-excluded lists them)")
        if failed:
            notes.append(f"{len(failed)} models failed the test call and are hidden (--show-excluded lists them)")

    if not a.full_pricing:
        for r in rows:
            r.pop("pricing", None)
            r.pop("endpoints", None)

    if a.json:
        print(json.dumps({"models": rows, "notes": notes}, indent=2, default=str))
        return 0 if rows else 1

    print("USD per million tokens. blended = (3 x input + output) / 4. cached = cache-read input.")
    print("notes = memory notes on the model; read them with info before choosing it.")
    print(f"{'model':48s} {'in':>8s} {'out':>8s} {'cached':>8s} {'blended':>8s} {'ctx':>9s} {'notes':>5s}  capabilities")
    for r in rows:
        print(
            f"{r['model']:48s} {fmt_price(r.get('input_usd_per_mtok')):>8s} "
            f"{fmt_price(r.get('output_usd_per_mtok')):>8s} "
            f"{fmt_price(r.get('cached_input_usd_per_mtok')):>8s} "
            f"{fmt_price(r.get('blended_usd_per_mtok')):>8s} "
            f"{str(r.get('max_input_tokens') or '?'):>9s} {r['memory_notes'] or '':>5}  {short_caps(r)}"
        )
    print(f"\n{len(rows)} models")
    for n in notes:
        eprint(f"note: {n}")
    return 0 if rows else 1


def cmd_info(a: argparse.Namespace) -> int:
    out = []
    for m in a.model:
        if "/" not in m:
            eprint(f"error: {m}: use provider/model")
            return 2
        info = model_info(m)
        notes = memory.notes_for([m])[m]
        if not info:
            out.append({"model": m, "error": "not in LiteLLM catalog", "memory": notes})
            continue
        row = {"model": m, **info, "memory": notes}
        if a.input_tokens is not None or a.output_tokens is not None:
            row["estimate"] = {
                "input_tokens": a.input_tokens or 0,
                "output_tokens": a.output_tokens or 0,
                "cached_tokens": a.cached_tokens or 0,
                "cost_usd": estimate_cost(info, a.input_tokens or 0, a.output_tokens or 0, a.cached_tokens or 0),
            }
        out.append(row)
    print(json.dumps(out, indent=2, default=str))
    return 0


def build_prompt(a: argparse.Namespace) -> str:
    parts = []
    text = " ".join(a.prompt).strip() if a.prompt else ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if text:
        parts.append(text)
    for f in a.file or []:
        path = Path(f).expanduser()
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            raise SystemExit(f"error: cannot read {f}: {exc}") from None
        parts.append(f"### File: {f}\n```\n{content}\n```")
    return "\n\n".join(parts).strip()


def append_ledger(record: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def cmd_complete(a: argparse.Namespace) -> int:
    import litellm

    if "/" not in a.model:
        eprint("error: --model must be provider/model, e.g. anthropic/claude-sonnet-4-6")
        return 2
    provider = a.model.split("/", 1)[0]
    if provider not in PROVIDERS:
        eprint(f"error: provider must be one of {', '.join(PROVIDERS)}")
        return 2
    prompt = build_prompt(a)
    if not prompt:
        eprint("error: no prompt given (args, stdin, or --file)")
        return 2
    key, _ = get_key(provider)
    if not key:
        eprint(f"error: no Keychain key for {provider} (service {DEFAULT_SERVICES[provider]})")
        return 3

    messages = []
    if a.system:
        messages.append({"role": "system", "content": a.system})
    messages.append({"role": "user", "content": prompt})
    kwargs: dict = {"model": a.model, "messages": messages, "api_key": key}
    if a.max_tokens:
        kwargs["max_completion_tokens"] = a.max_tokens
    if a.temperature is not None:
        kwargs["temperature"] = a.temperature
    if a.reasoning_effort:
        kwargs["reasoning_effort"] = a.reasoning_effort
    litellm.drop_params = True
    litellm.suppress_debug_info = True

    started = time.time()
    try:
        resp = litellm.completion(**kwargs)
    except Exception as exc:
        eprint(f"error: {clean_error(exc, key, 800)}")
        return 4
    elapsed = round(time.time() - started, 2)

    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "completion_tokens_details", None) if usage else None
    prompt_details = getattr(usage, "prompt_tokens_details", None) if usage else None
    usage_out = {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "cached_prompt_tokens": getattr(prompt_details, "cached_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    } if usage else None
    cost = None
    try:
        cost = litellm.completion_cost(completion_response=resp)
    except Exception:
        pass

    append_ledger({
        "at": int(started),
        "model": a.model,
        "label": a.label,
        "usage": usage_out,
        "cost_usd": cost,
        "seconds": elapsed,
        "output_file": a.output,
    })

    if a.output:
        out_path = Path(a.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)

    if a.json:
        result = {
            "model": a.model,
            "label": a.label,
            "usage": usage_out,
            "cost_usd": cost,
            "seconds": elapsed,
        }
        if a.output:
            result["output_file"] = a.output
            result["output_chars"] = len(text)
        else:
            result["text"] = text
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    elif a.output:
        cost_txt = f"${cost:.6f}" if isinstance(cost, (int, float)) else "unknown cost"
        print(f"wrote {len(text)} chars to {a.output} ({cost_txt}, {elapsed}s)")
    else:
        print(text)
    return 0


def cmd_spend(a: argparse.Namespace) -> int:
    if not LEDGER_FILE.exists():
        print("no calls recorded")
        return 0
    since = time.time() - a.hours * 3600 if a.hours else 0
    totals: dict[str, dict] = {}
    grand = 0.0
    calls = 0
    for line in LEDGER_FILE.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("at", 0) < since:
            continue
        group = rec.get(a.by) or "-"
        t = totals.setdefault(group, {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0})
        usage = rec.get("usage") or {}
        t["calls"] += 1
        t["cost_usd"] += rec.get("cost_usd") or 0
        t["prompt_tokens"] += usage.get("prompt_tokens") or 0
        t["completion_tokens"] += usage.get("completion_tokens") or 0
        grand += rec.get("cost_usd") or 0
        calls += 1
    for t in totals.values():
        t["cost_usd"] = round(t["cost_usd"], 6)
    print(json.dumps({"calls": calls, "total_cost_usd": round(grand, 6), f"by_{a.by}": totals}, indent=2))
    return 0


def cmd_memory_show(a: argparse.Namespace) -> int:
    print(json.dumps(memory.show(a.model or [], a.provider or []), indent=2, ensure_ascii=False))
    return 0


def cmd_memory_add(a: argparse.Namespace) -> int:
    result = memory.add(
        scope=a.scope,
        target=a.target,
        kind=a.kind,
        observation=a.observation,
        task=a.task,
        evidence=a.evidence,
        reason=a.reason,
        author=a.author,
        skill_issue_check=a.skill_issue_check,
        orchestrator=a.orchestrator,
        session=a.session,
        providers=PROVIDERS,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_memory_forget(a: argparse.Namespace) -> int:
    print(json.dumps(memory.forget(a.id, a.reason, a.author, a.session), indent=2))
    return 0


def cmd_memory_prune(a: argparse.Namespace) -> int:
    deleted: list[dict] = []
    skipped: list[str] = []
    for p in PROVIDERS:
        if not memory.stored_models(p):
            continue
        key, _ = get_key(p)
        if not key:
            skipped.append(f"{p}: no Keychain key, cannot confirm which models still exist")
            continue
        try:
            live = set(live_models(p, key))
        except LiveListError as exc:
            skipped.append(f"{p}: live listing failed ({exc})")
            continue
        if not live:
            skipped.append(f"{p}: live listing returned no models")
            continue
        gone = memory.deprecated_models(p, live, deprecation_date)
        if not a.dry_run:
            memory.delete_models(gone)
        deleted += [{"model": m, "reason": why} for m, why in gone]
    print(json.dumps({"dry_run": a.dry_run, "deleted": deleted, "skipped": skipped}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="List and call Anthropic, xAI, Gemini, and OpenAI models via LiteLLM with Keychain keys.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keys", help="show Keychain key status").set_defaults(func=cmd_keys)

    lp = sub.add_parser("list", help="list available models with prices")
    lp.add_argument("--provider", choices=PROVIDERS)
    lp.add_argument("--live", action="store_true", help="query provider /models endpoints")
    lp.add_argument("--verify", action="store_true", help="test-call each model (cached 7 days)")
    lp.add_argument("--refresh", action="store_true", help="with --verify, ignore the cache")
    lp.add_argument("--all", action="store_true", help="disable the text-chat filter")
    lp.add_argument("--show-excluded", action="store_true", help="print hidden models and why")
    lp.add_argument("--filter", help="substring filter on model name")
    lp.add_argument("--require", help="comma-separated capabilities, e.g. function_calling,vision")
    lp.add_argument("--max-price", type=float, help="max blended USD per million tokens")
    lp.add_argument("--min-context", type=int, help="minimum input context in tokens")
    lp.add_argument("--sort", choices=list(SORT_KEYS), default="price")
    lp.add_argument("--full-pricing", action="store_true", help="include every pricing field (with --json)")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=cmd_list)

    ip = sub.add_parser("info", help="full pricing and capabilities for models, with cost estimate")
    ip.add_argument("--model", action="append", required=True)
    ip.add_argument("--input-tokens", type=int)
    ip.add_argument("--output-tokens", type=int)
    ip.add_argument("--cached-tokens", type=int)
    ip.set_defaults(func=cmd_info)

    cp = sub.add_parser("complete", help="call a model")
    cp.add_argument("--model", required=True)
    cp.add_argument("--system")
    cp.add_argument("--file", action="append", help="append a file to the prompt; repeatable")
    cp.add_argument("--output", help="write the response to this file instead of stdout")
    cp.add_argument("--label", help="tag recorded in the spend ledger, e.g. worker, evaluator")
    cp.add_argument("--max-tokens", type=int)
    cp.add_argument("--temperature", type=float)
    cp.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high"])
    cp.add_argument("--json", action="store_true")
    cp.add_argument("prompt", nargs="*")
    cp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("spend", help="summarize recorded call costs")
    sp.add_argument("--hours", type=float, help="only the last N hours")
    sp.add_argument("--by", choices=["model", "label"], default="model")
    sp.set_defaults(func=cmd_spend)

    mp = sub.add_parser("memory", help="field notes on delegated models and providers")
    msub = mp.add_subparsers(dest="memory_cmd", required=True)

    ms = msub.add_parser("show", help="notes for models (plus their providers) or providers; all if none given")
    ms.add_argument("--model", action="append", help="provider/model; repeatable")
    ms.add_argument("--provider", action="append", choices=PROVIDERS)
    ms.set_defaults(func=cmd_memory_show)

    ma = msub.add_parser("add", help="record a note observed in practice")
    ma.add_argument("--scope", choices=["model", "provider"], required=True)
    ma.add_argument("--target", required=True, help="provider/model for scope=model, provider for scope=provider")
    ma.add_argument("--kind", choices=list(memory.KINDS), required=True)
    ma.add_argument("--observation", required=True, help="what happened, concretely")
    ma.add_argument("--task", required=True, help="the kind of task and settings used")
    ma.add_argument("--evidence", required=True, help="call ids, costs, outputs, or test results that show it")
    ma.add_argument("--skill-issue-check", help="required for AI limitation notes: how you ruled out your own misuse")
    ma.add_argument("--reason", required=True, help="why this note is worth keeping")
    ma.add_argument("--author", choices=list(memory.AUTHORS), required=True)
    ma.add_argument("--orchestrator", help="orchestrator model ID; defaults to the session's model")
    ma.add_argument("--session", help="opencode session ID; defaults to the single active session")
    ma.set_defaults(func=cmd_memory_add)

    mf = msub.add_parser("forget", help="delete one note that proved wrong or obsolete")
    mf.add_argument("--id", required=True)
    mf.add_argument("--reason", required=True)
    mf.add_argument("--author", choices=list(memory.AUTHORS), required=True)
    mf.add_argument("--session")
    mf.set_defaults(func=cmd_memory_forget)

    mr = msub.add_parser("prune", help="delete notes on models that are gone or past their deprecation date")
    mr.add_argument("--dry-run", action="store_true")
    mr.set_defaults(func=cmd_memory_prune)

    a = ap.parse_args()
    try:
        return a.func(a)
    except memory.MemoryStoreError as exc:
        eprint(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
