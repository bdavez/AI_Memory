# AI Memory — file-based persistent memory for local LLMs

A persistent memory layer that works with any OpenAI-compatible local server
(currently wired to the Bionic server on `http://localhost:1235/v1`,
model `qwen/qwen3.8-27b`). Everything is stored as **plain, human-readable
files** — no database.

## How it works

```
config.json           endpoint + model + recall settings
mem.py                the CLI (stdlib only, no pip installs)
memory/
  identity.md         hot memory: 3–8 line summary, always loaded into context
  facts.json          durable facts with tags + audit trail (updates supersede, never delete)
  journal/            raw notes, one JSON object per line (YYYY-MM-DD.jsonl)
  state.json          bookkeeping (last consolidation timestamp)
```

**Pipeline:** raw notes → `remember` appends to the journal → `consolidate`
asks the LLM to distill durable facts (add / update / remove) and rewrite the
identity summary → `recall` builds a compact memory block to prepend to any
prompt.

## Web UI (chat + memory manager + debug panel)

```powershell
python ui.py            # -> http://localhost:8765   (python ui.py 9000 for a custom port)
```

Three panels:
- **Chat** — memory-augmented chat; every exchange is logged to the journal.
- **Memory** — edit the identity summary, add/update/supersede facts, browse the
  journal, and trigger **Consolidate** (LLM distillation) with one click.
- **Debug** — LLM server reachability + model list, active config, and the last
  20 LLM calls (latency, sizes, token usage, and request/response excerpts).

Debug entries are stored in `memory/debug_log.json` (capped at 50), so the CLI
and the UI share the same log.

## Commands

```powershell
# Works offline (no LLM needed)
python mem.py remember "I prefer dark mode in editors"
python mem.py add-fact favorite_editor "VS Code" --tags tools,preference
python mem.py list-facts
python mem.py forget <fact_id>
python mem.py recall                    # prompt-ready memory block
python mem.py recall --top 20           # include 20 journal entries

# Needs the LLM server running
python mem.py consolidate               # journal -> facts + identity.md
python mem.py chat "What do you know about me?"   # memory-augmented chat (logged)
```

## Typical workflow

1. **During a session** — dump anything worth keeping: `mem.py remember "..."`
   (or let `chat` auto-log the Q&A).
2. **End of session** — `mem.py consolidate` distills new notes into facts.
3. **Start of any new session/prompt** — paste `mem.py recall` output into the
   system prompt, or pipe it: `python mem.py recall | clip` (Windows).

## Pointing it at a different server

Edit `config.json`:

| Backend | `base_url` | `model` example |
|---|---|---|
| Bionic local server | `http://localhost:1235/v1` | `qwen/qwen3.8-27b` |
| LM Studio server | `http://localhost:1234/v1` | whatever is loaded |
| Ollama | `http://localhost:11434/v1` | `llama3.2` |

Available models on the current server: `qwen/qwen3.8-27b`,
`qwen3-coder-next-reap-48b-a3b`, `prism-ml/bonsai-27b`,
`text-embedding-nomic-embed-text-v1.5` (embedding).

## Notes

- Facts are **soft-superseded** (`"superseded": true`), so `facts.json` is a
  full audit trail — you can always see how something changed.
- `recall` is deliberately compact: identity + active facts + a tail of raw
  notes. Tune sizes in `config.json` → `recall`.
- Everything is portable: copy the folder + `mem.py` + `config.json` anywhere.
