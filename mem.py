#!/usr/bin/env python3
"""
mem.py — file-based persistent memory layer for local LLMs (LM Studio / Ollama).

Memory layout (all plain files, human-readable, git-friendly):
  memory/identity.md    hot memory: short summary of who/what, always loaded
  memory/facts.json     durable structured facts (key/value + tags, audit trail)
  memory/journal/       append-only raw notes, one JSON object per line
  memory/state.json     internal bookkeeping (last consolidation, etc.)

Commands:
  mem.py recall [--top N]            print a prompt-ready memory block (offline)
  mem.py remember "text"             append a raw note to the journal (offline)
  mem.py add-fact KEY "value" [--tags a,b]   add/update a fact manually (offline)
  mem.py list-facts                  show all facts
  mem.py forget <fact_id>            supersede (soft-delete) a fact (offline)
  mem.py consolidate                 LLM pass: journal -> facts + identity.md
  mem.py chat "question"             LLM chat with memory prepended; Q&A logged

No third-party dependencies. Python 3.8+.
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mem_dir = ROOT / cfg.get("memory_dir", "memory")
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "journal").mkdir(exist_ok=True)
    return cfg, mem_dir


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_facts(mem_dir):
    return read_json(mem_dir / "facts.json", [])


def save_facts(mem_dir, facts):
    write_json(mem_dir / "facts.json", facts)


def active_facts(facts):
    return [f for f in facts if not f.get("superseded")]


def append_journal(mem_dir, text):
    entry = {"ts": now_iso(), "text": text.strip()}
    path = mem_dir / "journal" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def iter_journal_newest_first(mem_dir):
    files = sorted((mem_dir / "journal").glob("*.jsonl"), reverse=True)
    for f in files:
        # Lines are appended oldest->newest within a file, so reverse them so
        # the iterator truly yields newest-first (newest file comes first too).
        for line in reversed(f.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_state(mem_dir):
    return read_json(mem_dir / "state.json", {})


def save_state(mem_dir, state):
    write_json(mem_dir / "state.json", state)


# ---------------------------------------------------------------- LLM client

DEBUG_LOG_LIMIT = 50


def _debug_log(cfg, entry):
    """Append a debug entry about an LLM call (shared by CLI and web UI)."""
    try:
        mem_dir = ROOT / cfg.get("memory_dir", "memory")
        path = mem_dir / "debug_log.json"
        entries = read_json(path, [])
        entries.append({"ts": now_iso(), **entry})
        write_json(path, entries[-DEBUG_LOG_LIMIT:])
    except Exception:
        pass  # debug logging must never break a real call


def llm_chat(cfg, system, user, temperature=0.2):
    llm = cfg["llm"]
    url = llm["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": llm["model"],
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + llm.get("api_key", ""),
        },
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
        _debug_log(cfg, {
            "status": "ok",
            "url": url,
            "model": llm["model"],
            "elapsed_s": round(time.time() - started, 2),
            "request_chars": len(system) + len(user),
            "response_chars": len(content),
            "usage": data.get("usage"),
            "system_excerpt": system[:400],
            "user_excerpt": user[:400],
            "response_excerpt": content[:1500],
        })
        return content
    except urllib.error.URLError as e:
        _debug_log(cfg, {
            "status": "error",
            "url": url,
            "error": str(e),
            "elapsed_s": round(time.time() - started, 2),
        })
        sys.exit(
            f"ERROR: could not reach LLM at {url}: {e}\n"
            "Hint: start the LM Studio server (Server tab) or Ollama, "
            "then update base_url/model in config.json."
        )


def extract_json_array(text):
    """Pull the first JSON array out of an LLM response, tolerating fences/prose."""
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array found in LLM response")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- memory block

def build_memory_block(cfg, mem_dir, top_journal=None):
    top_journal = top_journal or cfg.get("recall", {}).get("journal_entries", 10)
    identity = (mem_dir / "identity.md").read_text(encoding="utf-8").strip() \
        if (mem_dir / "identity.md").exists() else ""
    facts = active_facts(load_facts(mem_dir))
    max_facts = cfg.get("recall", {}).get("max_facts", 100)

    lines = [f"=== MEMORY (as of {datetime.now().strftime('%Y-%m-%d')}) ==="]
    if identity:
        lines += ["[IDENTITY]", identity, ""]
    if facts:
        lines.append("[FACTS]")
        for f in facts[:max_facts]:
            tags = f.get("tags") or []
            tag_str = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"- {f['key']}: {f['value']}{tag_str}")
        lines.append("")
    journal = list(iter_journal_newest_first(mem_dir))[:top_journal]
    if journal:
        lines.append(f"[RECENT JOURNAL (last {len(journal)} notes, newest first)]")
        for e in journal:
            lines.append(f"- {e['ts']}: {e['text']}")
        lines.append("")
    lines.append("=== END MEMORY ===")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_recall(args, cfg, mem_dir):
    top = None
    i = 0
    while i < len(args):
        if args[i] == "--top" and i + 1 < len(args):
            top = int(args[i + 1])
            i += 2
        else:
            sys.exit("unknown argument for recall (expected: [--top N]): " + args[i])
    print(build_memory_block(cfg, mem_dir, top))


def cmd_remember(args, cfg, mem_dir):
    if not args:
        sys.exit('usage: mem.py remember "text"')
    entry = append_journal(mem_dir, " ".join(args))
    print(f"remembered at {entry['ts']} (use 'mem.py consolidate' to distill into facts)")


def cmd_add_fact(args, cfg, mem_dir):
    if len(args) < 2:
        sys.exit('usage: mem.py add-fact KEY "value" [--tags a,b]')
    key = args[0].strip().lower().replace(" ", "_")
    value = " ".join(args[1:]).strip()
    tags = []
    facts = load_facts(mem_dir)

    # parse --tags out of value if present
    m = re.search(r"--tags\s+(.+)$", value)
    if m:
        value = value[: m.start()].strip()
        tags = [t.strip() for t in m.group(1).split(",") if t.strip()]

    existing = [f for f in active_facts(facts) if f["key"] == key]
    if existing:
        old = existing[0]
        old["superseded"] = True
        old["superseded_at"] = now_iso()
        existing.append(  # re-add fresh
            {
                "id": uuid.uuid4().hex[:8],
                "key": key, "value": value, "tags": tags,
                "created_at": now_iso(), "updated_at": now_iso(),
                "superseded": False, "superseded_by": None,
                "note": "replaced " + old["id"],
            }
        )
        print(f"updated fact {key}")
    else:
        facts.append({
            "id": uuid.uuid4().hex[:8],
            "key": key, "value": value, "tags": tags,
            "created_at": now_iso(), "updated_at": now_iso(),
            "superseded": False, "superseded_by": None,
        })
        print(f"added fact {key}")
    save_facts(mem_dir, facts)


def cmd_list_facts(args, cfg, mem_dir):
    facts = active_facts(load_facts(mem_dir))
    if not facts:
        print("(no facts yet)")
        return
    for f in facts:
        tags = f.get("tags") or []
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"{f['id']}  {f['key']}: {f['value']}{tag_str}")


def cmd_forget(args, cfg, mem_dir):
    if not args:
        sys.exit("usage: mem.py forget <fact_id>")
    fid = args[0]
    facts = load_facts(mem_dir)
    target = next((f for f in facts if f["id"] == fid), None)
    if not target:
        sys.exit(f"no fact with id {fid}")
    target["superseded"] = True
    target["superseded_at"] = now_iso()
    save_facts(mem_dir, facts)
    print(f"superseded fact {target['key']} (kept in file for audit trail)")


def cmd_consolidate(args, cfg, mem_dir):
    state = load_state(mem_dir)
    last_ts = state.get("last_consolidated", "1970-01-01T00:00:00+00:00")

    pending = [e for e in iter_journal_newest_first(mem_dir) if e["ts"] > last_ts]
    if not pending:
        print("nothing new to consolidate (journal is up to date)")
        return {"added": 0, "updated": 0, "removed": 0, "notes": 0, "up_to_date": True}
    max_batch = cfg.get("consolidation", {}).get("max_batch", 100)
    # pending is newest-first (iter_journal_newest_first). Reverse to oldest->newest
    # so we catch up chronologically, the prompt stays "oldest first", and batch[-1]
    # is the NEWEST processed entry -- the correct progress marker for the next run.
    batch = pending[::-1][:max_batch]

    current = active_facts(load_facts(mem_dir))
    identity = (mem_dir / "identity.md").read_text(encoding="utf-8").strip() \
        if (mem_dir / "identity.md").exists() else ""

    system = (
        "You are a memory-consolidation engine. Given new conversation notes, decide what "
        "to remember long-term. Worth remembering: identity details, preferences, "
        "relationships, projects, goals, and concrete plans or events (keep their dates). "
        "STRICT RULES:\n"
        "1. Use ONLY the user's own statements as evidence — journal lines that start "
        "with 'Q:'. Lines starting with 'A:' are the assistant's replies and may be "
        "wrong, guessed, or confabulated; NEVER extract facts from them.\n"
        "2. Only extract facts EXPLICITLY STATED by the user — never infer, guess, or "
        "invent anything the user did not say (e.g. about food, hobbies, or family).\n"
        "3. Ignore pure small talk and pleasantries, but keep concrete facts, "
        "preferences, and plans.\n"
        "If there is nothing durable to remember, return an empty array (plus the "
        "identity action only if it genuinely needs updating). "
        "Respond with ONLY a JSON array of actions. "
        "Each action object has:\n"
        '  {"action":"add","key":"snake_case_key","value":"concise fact","tags":["optional"]}\n'
        '  {"action":"update","key":"existing_key","value":"new value"}   (when a fact changed)\n'
        '  {"action":"remove","key":"existing_key"}                       (when a fact became false)\n'
        "Also include at most one: {\"action\":\"identity\",\"value\":\"...\"} which rewrites the "
        "IDENTITY summary: 3-8 bullet lines capturing who the user is, their goals, "
        "preferences, and ongoing projects. Keep it current and merge with the old identity."
    )
    user = (
        f"CURRENT IDENTITY:\n{identity or '(none yet)'}\n\n"
        f"CURRENT FACTS:\n{json.dumps(current, indent=2, ensure_ascii=False)}\n\n"
        f"NEW NOTES (oldest first):\n"
        + "\n".join(f"- {e['ts']}: {e['text']}" for e in batch)
        + "\n\nReturn the JSON array."
    )

    print(f"consolidating {len(batch)} journal entries...")
    raw = llm_chat(cfg, system, user)
    try:
        actions = extract_json_array(raw)
    except (json.JSONDecodeError, ValueError) as e:
        sys.exit(f"ERROR: could not parse consolidation JSON: {e}\nRaw response:\n{raw}")

    facts = load_facts(mem_dir)
    added = updated = removed = 0
    for a in actions:
        act = (a.get("action") or "").lower()
        if act == "identity":
            (mem_dir / "identity.md").write_text(
                (a.get("value") or "").strip() + "\n", encoding="utf-8"
            )
            continue
        key = (a.get("key") or "").strip().lower().replace(" ", "_")
        if not key:
            continue
        match = next((f for f in active_facts(facts) if f["key"] == key), None)
        if act == "add":
            if match:  # key exists -> upsert so changed values are not silently dropped
                match["value"] = a.get("value", match["value"])
                match["tags"] = a.get("tags") or match.get("tags") or []
                match["updated_at"] = now_iso()
                updated += 1
            else:
                facts.append({
                    "id": uuid.uuid4().hex[:8],
                    "key": key,
                    "value": a.get("value", ""),
                    "tags": a.get("tags") or [],
                    "created_at": now_iso(), "updated_at": now_iso(),
                    "superseded": False, "superseded_by": None,
                })
                added += 1
        elif act == "update" and match:
            match["value"] = a.get("value", match["value"])
            match["updated_at"] = now_iso()
            updated += 1
        elif act == "remove" and match:
            match["superseded"] = True
            match["superseded_at"] = now_iso()
            removed += 1

    save_facts(mem_dir, facts)
    state["last_consolidated"] = batch[-1]["ts"]
    save_state(mem_dir, state)
    summary = {"added": added, "updated": updated, "removed": removed,
               "notes": len(batch)}
    print(f"done: +{added} added, {updated} updated, {removed} removed. "
          f"({len(batch)} notes processed)")
    return summary


def cmd_chat(args, cfg, mem_dir):
    if not args:
        sys.exit('usage: mem.py chat "question"')
    question = " ".join(args)
    block = build_memory_block(cfg, mem_dir)
    system = (
        "You are a helpful assistant with long-term memory. The MEMORY block below was "
        "persisted from past sessions; use it naturally when relevant, and remember "
        "facts about the user from it. If memory conflicts with what the user says now, "
        "the user's current statement wins."
    )
    answer = llm_chat(cfg, system, f"{block}\n\nUSER: {question}")
    print("\n" + answer)
    append_journal(mem_dir, f"Q: {question}")
    append_journal(mem_dir, f"A: {answer}")


def cmd_clear_journal(args, cfg, mem_dir):
    """Delete every journal note (chat turns and any raw remembered notes).

    Facts (facts.json) and identity.md are intentionally left untouched.
    """
    jdir = mem_dir / "journal"
    removed = 0
    if jdir.exists():
        for f in sorted(jdir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    removed += 1
            f.unlink()
    print(f"cleared {removed} journal entries")
    return {"cleared": removed}


# ---------------------------------------------------------------- main

COMMANDS = {
    "recall": cmd_recall,
    "remember": cmd_remember,
    "add-fact": cmd_add_fact,
    "list-facts": cmd_list_facts,
    "forget": cmd_forget,
    "consolidate": cmd_consolidate,
    "chat": cmd_chat,
    "clear-journal": cmd_clear_journal,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
    cfg, mem_dir = load_config()
    COMMANDS[cmd](sys.argv[2:], cfg, mem_dir)


if __name__ == "__main__":
    main()
