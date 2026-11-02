"""Orchestrator memory: field notes on delegated models and providers.

The store is an Open Knowledge Format (OKF v0.2) bundle:

    <root>/index.md                       generated listing, removed when empty
    <root>/log.md                         dated history of additions and deletions
    <root>/providers/<provider>.md        one concept per provider with notes
    <root>/models/<provider>/<model>.md   one concept per model with notes

Nothing is created until the first note is written. Each concept body holds
<memory-note> elements; the file is rewritten from the parsed notes on every
change, so the tagged notes are the source of truth.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

Note = dict[str, Any]

MEMORY_DIR = Path(
    os.environ.get("ORCHESTRATION_MEMORY_DIR")
    or Path.home() / ".local" / "share" / "orchestration" / "memory"
)
OPENCODE_DB = Path(
    os.environ.get("ORCHESTRATION_OPENCODE_DB")
    or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
)
ACTIVE_SESSION_WINDOW_MS = 5 * 60 * 1000

KINDS = ("works", "limitation")
AUTHORS = ("human", "ai")
NOTE_FIELDS = ("kind", "observation", "task", "evidence", "skill-issue-check")
PROVENANCE_FIELDS = ("author", "orchestrator", "session", "timestamp", "reason")


class MemoryStoreError(Exception):
    """An expected, user-correctable problem with a memory request."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- paths ----------

def model_path(model: str) -> Path:
    provider, name = model.split("/", 1)
    return MEMORY_DIR / "models" / provider / f"{quote(name, safe='._-')}.md"


def provider_path(provider: str) -> Path:
    return MEMORY_DIR / "providers" / f"{provider}.md"


def path_for(scope: str, target: str) -> Path:
    return model_path(target) if scope == "model" else provider_path(target)


def model_from_path(path: Path) -> str:
    return f"{path.parent.name}/{unquote(path.stem)}"


def bundle_link(path: Path) -> str:
    return "/" + path.relative_to(MEMORY_DIR).as_posix()


# ---------- parsing and rendering ----------

def parse_notes(path: Path) -> list[Note]:
    if not path.exists():
        return []
    text = path.read_text()
    notes = []
    start = 0
    while (i := text.find("<memory-note", start)) != -1:
        j = text.find("</memory-note>", i)
        if j == -1:
            raise MemoryStoreError(f"{path}: unterminated <memory-note>")
        j += len("</memory-note>")
        try:
            el = ET.fromstring(text[i:j])
        except ET.ParseError as exc:
            raise MemoryStoreError(f"{path}: malformed <memory-note>: {exc}") from None
        note: Note = {"id": el.get("id")}
        for field in NOTE_FIELDS:
            note[field.replace("-", "_")] = (el.findtext(field) or "").strip() or None
        note = {k: v for k, v in note.items() if v is not None}
        prov = el.find("provenance")
        if prov is None or note.get("kind") not in KINDS:
            raise MemoryStoreError(f"{path}: note {note.get('id')} lacks a valid kind or provenance")
        note["provenance"] = {f: (prov.findtext(f) or "").strip() for f in PROVENANCE_FIELDS}
        notes.append(note)
        start = j
    return notes


def render_note(note: Note) -> str:
    lines = [f'<memory-note id="{escape(note["id"])}">']
    for field in NOTE_FIELDS:
        value = note.get(field.replace("-", "_"))
        if value:
            lines.append(f"<{field}>{escape(value, quote=False)}</{field}>")
    lines.append("<provenance>")
    for field in PROVENANCE_FIELDS:
        lines.append(f"<{field}>{escape(note['provenance'][field], quote=False)}</{field}>")
    lines.append("</provenance>")
    lines.append("</memory-note>")
    return "\n".join(lines)


def actor(prov: dict[str, str]) -> str:
    if prov["author"] == "human":
        return f"human:{os.environ.get('USER', 'unknown')}"
    return f"opencode/{prov['orchestrator']}"


def render_concept(scope: str, target: str, notes: list[Note]) -> str:
    works = sum(n["kind"] == "works" for n in notes)
    limits = len(notes) - works
    last = max(notes, key=lambda n: n["provenance"]["timestamp"])["provenance"]
    provider = target.split("/", 1)[0]
    fm = [
        "---",
        f"type: Orchestration {'Model' if scope == 'model' else 'Provider'} Memory",
        f"title: {json.dumps(target)}",
        f"description: {json.dumps(f'Delegation notes on {target}: {works} works, {limits} limitations.')}",
        f"tags: [orchestration, memory, {provider}]",
        f"scope: {scope}",
        f"{scope}: {json.dumps(target)}",
        f"generated: {{ by: {json.dumps(actor(last))}, at: {last['timestamp']} }}",
        "---",
        "",
        f"# Notes on {target}",
        "",
        "Each note was observed in practice by an orchestrator or reported by a human."
        " Provenance is inside each note.",
        "",
    ]
    body = "\n\n".join(render_note(n) for n in sorted(notes, key=lambda n: n["provenance"]["timestamp"]))
    return "\n".join(fm) + "\n" + body + "\n"


def write_concept(scope: str, target: str, notes: list[Note]) -> None:
    path = path_for(scope, target)
    if not notes:
        path.unlink(missing_ok=True)
        if path.parent != MEMORY_DIR and not any(path.parent.iterdir()):
            path.parent.rmdir()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render_concept(scope, target, notes))
    tmp.replace(path)


def concept_files() -> list[tuple[str, str, Path]]:
    out = []
    if (MEMORY_DIR / "providers").is_dir():
        out += [("provider", p.stem, p) for p in sorted((MEMORY_DIR / "providers").glob("*.md"))]
    if (MEMORY_DIR / "models").is_dir():
        out += [("model", model_from_path(p), p) for p in sorted((MEMORY_DIR / "models").glob("*/*.md"))]
    return out


def write_index() -> None:
    files = concept_files()
    index = MEMORY_DIR / "index.md"
    if not files:
        index.unlink(missing_ok=True)
        return
    sections = {"provider": ["# Providers", ""], "model": ["# Models", ""]}
    for scope, target, path in files:
        notes = parse_notes(path)
        works = sum(n["kind"] == "works" for n in notes)
        sections[scope].append(
            f"* [{target}]({bundle_link(path)}) - {works} works, {len(notes) - works} limitations"
        )
    body = ["---", "okf_version: 0.2", "---", ""]
    for lines in sections.values():
        if len(lines) > 2:
            body += lines + [""]
    index.write_text("\n".join(body))


def append_log(entries: list[str]) -> None:
    log = MEMORY_DIR / "log.md"
    today = f"## {datetime.now(timezone.utc).date().isoformat()}"
    lines = log.read_text().splitlines() if log.exists() else ["# Orchestration Memory Update Log", ""]
    new = [f"* {e}" for e in entries]
    if today in lines:
        at = lines.index(today) + 1
        lines[at:at] = new
    else:
        at = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
        lines[at:at] = [today, *new, ""]
    log.write_text("\n".join(lines).rstrip() + "\n")


@contextmanager
def locked():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with (MEMORY_DIR / ".lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


# ---------- provenance sources ----------

def _session_row(row: tuple[Any, ...]) -> dict[str, Any]:
    sid, title, directory, created, updated, model = row
    orchestrator = None
    if model:
        m = json.loads(model)
        orchestrator = f"{m['providerID']}/{m['id']}"
    return {
        "id": sid, "title": title, "directory": directory,
        "created_ms": created, "updated_ms": updated, "model": orchestrator,
    }


def resolve_session(explicit: str | None) -> dict[str, Any]:
    """Find the orchestrating opencode session.

    Without an explicit ID, exactly one session must have been active in the
    last few minutes; otherwise the caller has to pick one from the candidates.
    Agents other than opencode have no database, so an explicit ID is taken as is.
    """
    if explicit and not OPENCODE_DB.exists():
        return {"id": explicit, "title": None, "directory": None, "created_ms": None, "updated_ms": None, "model": None}
    if not OPENCODE_DB.exists():
        raise MemoryStoreError(f"opencode database not found at {OPENCODE_DB}; pass session explicitly")
    db = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    try:
        cols = "id, title, directory, time_created, time_updated, model"
        if explicit:
            row = db.execute(f"SELECT {cols} FROM session WHERE id = ?", (explicit,)).fetchone()
            if not row:
                raise MemoryStoreError(f"session {explicit} not found in {OPENCODE_DB}")
            return _session_row(row)
        since = int(time.time() * 1000) - ACTIVE_SESSION_WINDOW_MS
        rows = db.execute(
            f"SELECT {cols} FROM session WHERE time_updated >= ? ORDER BY time_updated DESC", (since,)
        ).fetchall()
    finally:
        db.close()
    if len(rows) == 1:
        return _session_row(rows[0])
    if not rows:
        raise MemoryStoreError("no opencode session was active in the last 5 minutes; pass session explicitly")
    listing = "; ".join(f"{r[0]} ({r[1]!r} in {r[2]})" for r in rows)
    raise MemoryStoreError(f"several active opencode sessions; pass session explicitly. Candidates: {listing}")


# ---------- public operations ----------

def notes_for(models: list[str]) -> dict[str, Any]:
    """Notes for each model plus its provider. Reads only; creates nothing."""
    out = {}
    for m in models:
        provider = m.split("/", 1)[0]
        out[m] = {"model_notes": parse_notes(model_path(m)), "provider_notes": parse_notes(provider_path(provider))}
    return out


def show(models: list[str], providers: list[str]) -> dict[str, Any]:
    if not models and not providers:
        return {
            "memory_dir": str(MEMORY_DIR),
            "concepts": [
                {"scope": s, "target": t, "notes": parse_notes(p)} for s, t, p in concept_files()
            ],
        }
    out: dict[str, Any] = {"memory_dir": str(MEMORY_DIR)}
    if models:
        out["models"] = notes_for(models)
    if providers:
        out["providers"] = {p: parse_notes(provider_path(p)) for p in providers}
    return out


def add(
    *,
    scope: str,
    target: str,
    kind: str,
    observation: str,
    task: str,
    evidence: str,
    reason: str,
    author: str,
    skill_issue_check: str | None,
    orchestrator: str | None,
    session: str | None,
    providers: list[str],
) -> dict[str, Any]:
    if scope not in ("model", "provider"):
        raise MemoryStoreError("scope must be model or provider")
    if kind not in KINDS:
        raise MemoryStoreError(f"kind must be one of {', '.join(KINDS)}")
    if author not in AUTHORS:
        raise MemoryStoreError(f"author must be one of {', '.join(AUTHORS)}")
    provider = target.split("/", 1)[0]
    if provider not in providers or (scope == "model") != ("/" in target):
        raise MemoryStoreError(
            "target must be provider/model for scope=model, or a bare provider for scope=provider; "
            f"providers: {', '.join(providers)}"
        )
    for name, value in (("observation", observation), ("task", task), ("evidence", evidence), ("reason", reason)):
        if not value or not value.strip():
            raise MemoryStoreError(f"{name} is required")
    if author == "ai" and kind == "limitation" and not (skill_issue_check and skill_issue_check.strip()):
        raise MemoryStoreError(
            "skill_issue_check is required for AI limitation notes: state what you did to rule out "
            "your own prompt, parameters, or usage as the cause"
        )

    sess = resolve_session(session)
    orchestrator = orchestrator or sess["model"]
    if not orchestrator:
        raise MemoryStoreError(f"session {sess['id']} has no recorded model; pass orchestrator explicitly")

    note = {
        "id": f"mn-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}",
        "kind": kind,
        "observation": observation.strip(),
        "task": task.strip(),
        "evidence": evidence.strip(),
        "provenance": {
            "author": author,
            "orchestrator": orchestrator,
            "session": sess["id"],
            "timestamp": utc_now(),
            "reason": reason.strip(),
        },
    }
    if skill_issue_check and skill_issue_check.strip():
        note["skill_issue_check"] = skill_issue_check.strip()

    with locked():
        path = path_for(scope, target)
        notes = parse_notes(path) + [note]
        write_concept(scope, target, notes)
        write_index()
        append_log([
            f"**Creation**: {kind} note `{note['id']}` on [{target}]({bundle_link(path)})"
            f" by {author} ({orchestrator}, {sess['id']}): {reason.strip()}"
        ])
    return {"added": note, "file": str(path)}


def forget(note_id: str, reason: str, author: str, session: str | None) -> dict[str, Any]:
    if not reason or not reason.strip():
        raise MemoryStoreError("reason is required")
    if author not in AUTHORS:
        raise MemoryStoreError(f"author must be one of {', '.join(AUTHORS)}")
    sess = resolve_session(session)
    with locked():
        for scope, target, path in concept_files():
            notes = parse_notes(path)
            kept = [n for n in notes if n["id"] != note_id]
            if len(kept) == len(notes):
                continue
            write_concept(scope, target, kept)
            write_index()
            append_log([
                f"**Deletion**: note `{note_id}` removed from {target} by {author}"
                f" ({sess['model'] or 'unknown orchestrator'}, {sess['id']}): {reason.strip()}"
            ])
            return {"forgot": note_id, "target": target}
    raise MemoryStoreError(f"note {note_id} not found")


def stored_models(provider: str) -> list[str]:
    d = MEMORY_DIR / "models" / provider
    return [model_from_path(p) for p in sorted(d.glob("*.md"))] if d.is_dir() else []


def deprecated_models(
    provider: str, live: set[str], deprecation_date: Callable[[str], str | None]
) -> list[tuple[str, str]]:
    """Models with memory that are gone from the provider listing or past their deprecation date."""
    gone = []
    today = date.today()
    for m in stored_models(provider):
        if m not in live:
            gone.append((m, "no longer in the provider's live model list"))
            continue
        d = deprecation_date(m)
        if d:
            try:
                if date.fromisoformat(d) <= today:
                    gone.append((m, f"deprecated on {d} per the LiteLLM catalog"))
            except ValueError:
                pass
    return gone


def delete_models(gone: list[tuple[str, str]]) -> None:
    if not gone:
        return
    with locked():
        entries = []
        for m, why in gone:
            n = len(parse_notes(model_path(m)))
            write_concept("model", m, [])
            entries.append(f"**Deprecation**: deleted {n} notes on {m}, {why}")
        write_index()
        append_log(entries)
