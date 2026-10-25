#!/usr/bin/env python3
"""
ui.py — web UI, memory manager, and debug panel for the mem.py memory system.

Pages:
  /         chat (memory-augmented, logged, auto-refreshing)
  /memory   memory manager + debug panel (identity, facts, journal, LLM log)

Run:  python ui.py          (optional: python ui.py 9000 for a custom port)
Then open the printed URL in a browser. Stdlib only.
"""
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mem

PORT = 8765

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- state

def snapshot():
    cfg, mem_dir = mem.load_config()
    identity = ""
    ip = mem_dir / "identity.md"
    if ip.exists():
        identity = ip.read_text(encoding="utf-8").strip()
    facts = mem.active_facts(mem.load_facts(mem_dir))
    journal = list(mem.iter_journal_newest_first(mem_dir))[:30]
    debug = mem.read_json(mem_dir / "debug_log.json", [])[-20:][::-1]
    state = mem.load_state(mem_dir)
    return {
        "identity": identity,
        "facts": facts,
        "journal": journal,
        "debug": debug,
        "state": state,
        "config": {
            "base_url": cfg["llm"]["base_url"],
            "model": cfg["llm"]["model"],
            "api_key": "(set)" if cfg["llm"].get("api_key") else "(none)",
            "recall": cfg.get("recall"),
            "memory_dir": str(mem_dir),
        },
    }


def ping_llm(cfg):
    url = cfg["llm"]["base_url"].rstrip("/") + "/models"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + cfg["llm"].get("api_key", "")}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "models": [m.get("id") for m in data.get("data", [])]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_cmd(fn, *args):
    """Run a mem command; convert its sys.exit errors into a JSON error tuple."""
    try:
        result = fn(*args)
        return result, None
    except SystemExit as e:
        msg = str(e.code) if e.code else "command failed"
        return None, msg
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self, page):
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")  # never let the browser keep a stale UI
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, *args):
        pass  # keep the console quiet

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send_page(PAGE_CHAT)
        elif path == "/memory":
            self._send_page(PAGE_MEMORY)
        elif path == "/api/state":
            self._send(200, snapshot())
        elif path == "/api/ping":
            cfg, _ = mem.load_config()
            self._send(200, ping_llm(cfg))
        else:
            self._send(404, {"error": "not found"})

    # ----------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        cfg, mem_dir = mem.load_config()

        if path == "/api/remember":
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "text is required"})
                return
            entry = mem.append_journal(mem_dir, text)
            self._send(200, {"ok": True, "entry": entry})

        elif path == "/api/chat":
            question = (body.get("message") or "").strip()
            if not question:
                self._send(400, {"error": "message is required"})
                return
            block = mem.build_memory_block(cfg, mem_dir)
            system = (
                "You are a helpful assistant with long-term memory. The MEMORY block below "
                "was persisted from past sessions; use it naturally when relevant. If memory "
                "conflicts with what the user says now, the user's current statement wins."
            )
            answer, err = run_cmd(mem.llm_chat, cfg, system, f"{block}\n\nUSER: {question}")
            if err:
                self._send(502, {"error": err})
                return
            mem.append_journal(mem_dir, f"Q: {question}")
            mem.append_journal(mem_dir, f"A: {answer}")
            resp = {"answer": answer}
            if body.get("auto_consolidate"):
                csummary, cerr = run_cmd(mem.cmd_consolidate, [], cfg, mem_dir)
                resp["consolidation"] = {
                    "ok": not cerr,
                    "detail": cerr or "done",
                    "summary": csummary or {},
                }
            self._send(200, resp)

        elif path == "/api/consolidate":
            summary, err = run_cmd(mem.cmd_consolidate, [], cfg, mem_dir)
            if err:
                self._send(502, {"error": err})
                return
            self._send(200, {"ok": True, "summary": summary or {}})

        elif path == "/api/fact":
            key = (body.get("key") or "").strip()
            value = (body.get("value") or "").strip()
            tags = [t.strip() for t in (body.get("tags") or "").split(",") if t.strip()]
            if not key or not value:
                self._send(400, {"error": "key and value are required"})
                return
            _, err = run_cmd(mem.cmd_add_fact, [key, value], cfg, mem_dir)
            if err:
                self._send(400, {"error": err})
                return
            self._send(200, {"ok": True, "key": key, "tags": tags})

        elif path == "/api/fact/supersede":
            fid = (body.get("id") or "").strip()
            if not fid:
                self._send(400, {"error": "id is required"})
                return
            _, err = run_cmd(mem.cmd_forget, [fid], cfg, mem_dir)
            if err:
                self._send(400, {"error": err})
                return
            self._send(200, {"ok": True})

        elif path == "/api/identity":
            text = body.get("identity") or ""
            (mem_dir / "identity.md").write_text(text.strip() + "\n", encoding="utf-8")
            self._send(200, {"ok": True})

        elif path == "/api/settings":
            state = mem.load_state(mem_dir)
            if "auto_consolidate" in body:
                state["auto_consolidate"] = bool(body["auto_consolidate"])
                mem.save_state(mem_dir, state)
            self._send(200, {"ok": True, "auto_consolidate": bool(state.get("auto_consolidate"))})

        else:
            self._send(404, {"error": "not found"})


# ---------------------------------------------------------------- pages

CSS = """
  :root { --bg:#0f1115; --panel:#171a21; --panel2:#1d212b; --border:#2a2f3a;
          --text:#d7dce5; --dim:#8b93a3; --accent:#4f8cff; --ok:#3ecf8e; --err:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 "Segoe UI", system-ui, sans-serif; }
  header { display:flex; align-items:center; gap:12px; padding:10px 16px;
           border-bottom:1px solid var(--border); background:var(--panel); }
  header h1 { font-size:15px; margin:0; letter-spacing:.5px; }
  header .pill { font-size:12px; padding:2px 10px; border-radius:12px;
                 background:var(--panel2); border:1px solid var(--border); color:var(--dim); }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--dim); }
  header .dot.ok { background:var(--ok); } header .dot.bad { background:var(--err); }
  nav { display:flex; gap:4px; margin-left:8px; }
  .navlink { text-decoration:none; color:var(--dim); font-size:13px; padding:5px 13px;
             border-radius:6px; border:1px solid transparent; }
  .navlink:hover { color:var(--text); border-color:var(--border); }
  .navlink.active { color:#fff; background:var(--accent); border-color:var(--accent); }
  main { display:grid; gap:10px; padding:10px; height:calc(100vh - 54px); }
  main.chat { grid-template-columns:1fr; }
  main.chat .panel { max-width:880px; width:100%; margin:0 auto; }
  main.mem { grid-template-columns:1.15fr 1fr; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:10px;
           display:flex; flex-direction:column; overflow:hidden; min-width:0; }
  .panel h2 { margin:0; padding:10px 14px; font-size:12px; text-transform:uppercase;
              letter-spacing:1px; color:var(--dim); border-bottom:1px solid var(--border);
              display:flex; justify-content:space-between; align-items:center; }
  .body { flex:1; overflow-y:auto; padding:12px; }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--border);
           border-radius:6px; padding:5px 12px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button:disabled { opacity:.6; cursor:default; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.danger:hover { border-color:var(--err); color:var(--err); }
  input, textarea, select { background:var(--panel2); color:var(--text);
           border:1px solid var(--border); border-radius:6px; padding:6px 9px; font:inherit; width:100%; }
  input:focus, textarea:focus { outline:none; border-color:var(--accent); }
  /* chat */
  #msgs { display:flex; flex-direction:column; gap:8px; }
  .msg { max-width:85%; padding:8px 12px; border-radius:10px; white-space:pre-wrap;
         word-break:break-word; font-size:13.5px; }
  .msg.user { align-self:flex-end; background:#24406e; }
  .msg.bot  { align-self:flex-start; background:var(--panel2); border:1px solid var(--border); }
  .msg .who { font-size:11px; color:var(--dim); margin-bottom:3px; }
  #chatbar { display:flex; gap:8px; padding:10px; border-top:1px solid var(--border); }
  #chatbar input { flex:1; }
  #chatstatus { padding:0 12px 6px; font-size:12.5px; color:var(--dim); }
  #chatstatus:empty { display:none; }
  #chatstatus.err { color:var(--err); }
  #chatstatus a { color:var(--accent); }
  /* facts */
  .fact { display:flex; gap:8px; align-items:flex-start; padding:7px 9px; margin-bottom:6px;
          background:var(--panel2); border:1px solid var(--border); border-radius:7px; }
  .fact .k { color:var(--accent); font-weight:600; flex-shrink:0; }
  .fact .v { flex:1; font-size:13px; }
  .fact .tags { color:var(--dim); font-size:11px; margin-left:6px; }
  .fact .del { background:none; border:none; color:var(--dim); padding:0 4px; font-size:15px; }
  .fact .del:hover { color:var(--err); }
  .sect { margin:14px 0 6px; font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--dim); }
  .addfact { display:flex; flex-direction:column; gap:6px; margin-bottom:4px; }
  .addfact .row { display:flex; gap:6px; }
  .jentry { font-size:12.5px; padding:5px 8px; border-left:2px solid var(--border); margin-bottom:5px; }
  .jentry .ts { color:var(--dim); font-size:11px; }
  .status { font-size:12.5px; color:var(--dim); padding:4px 2px; }
  .status.err { color:var(--err); }
  .status a { color:var(--accent); }
  /* debug */
  .dentry { background:var(--panel2); border:1px solid var(--border); border-radius:7px;
            margin-bottom:8px; overflow:hidden; font-size:12.5px; }
  .dentry .head { display:flex; gap:10px; align-items:center; padding:7px 10px; cursor:pointer; }
  .dentry .head:hover { background:#222836; }
  .badge { font-size:11px; padding:1px 8px; border-radius:9px; }
  .badge.ok { background:#123527; color:var(--ok); }
  .badge.err { background:#3a1f1f; color:var(--err); }
  .dentry .meta { color:var(--dim); }
  .dentry .detail { display:none; padding:10px; border-top:1px solid var(--border); }
  .dentry.open .detail { display:block; }
  .dentry pre { margin:6px 0; white-space:pre-wrap; word-break:break-word;
                background:var(--bg); border-radius:6px; padding:8px; font-size:12px;
                max-height:220px; overflow-y:auto; }
  .dentry .lbl { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:3px 12px; font-size:12.5px; }
  .kv .k { color:var(--dim); }
  @media (max-width:1100px) { main.mem { grid-template-columns:1fr; height:auto; }
    main.mem .panel { max-height:75vh; } }
"""

# JS shared by both pages (ES2019-safe, no ??. no optional catch binding)
JS_COMMON = """
const $ = id => document.getElementById(id);
const esc = s => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;"}[c]));

async function api(path, body, ctl) {
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"},
                      body: JSON.stringify(body), signal: ctl && ctl.signal}
                   : {signal: ctl && ctl.signal};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}

function kv(pairs) { return pairs.map(function(p){
  return '<span class="k">' + esc(p[0]) + '</span><span>' + esc(p[1]) + '</span>';
}).join(""); }

async function ping() {
  const cfg = (await api("/api/state")).config;
  const p = await api("/api/ping");
  if (p.ok) { $("dot").className = "dot ok";
              $("pill").textContent = "server up";
              $("model").textContent = cfg.model; }
  else { $("dot").className = "dot bad";
         $("pill").textContent = "server down"; }
}
"""

JS_CHAT = """
let chatting = false;

function note(text, isErr, html) {
  const n = $("chatstatus");
  if (html) n.innerHTML = text; else n.textContent = text;
  n.classList.toggle("err", !!isErr);
}

/* The journal is the single source of truth for the conversation. Every render
   is a full, idempotent rebuild from the journal (no incremental counter), so an
   old answer can never surface against the wrong question or appear twice. A
   signature check skips the rebuild when nothing changed (no scroll jumps). */
const CHAT_MAX = 30;
let lastChatSig = null;

function chatItems(s) {
  // /api/state returns the 30 most recent journal notes, newest->oldest.
  // Reverse to chronological (oldest->newest) for display. Q and A are always
  // adjacent per turn (the server appends them together), so collecting them in
  // order preserves the question<->answer pairing. Non-chat entries (memory
  // notes) are simply skipped, wherever they sit.
  const items = [];
  for (const e of s.journal.slice().reverse()) {
    const t = e.text;
    if (t.indexOf("Q: ") === 0) items.push({who: "user", text: t.slice(3)});
    else if (t.indexOf("A: ") === 0) items.push({who: "bot", text: t.slice(3)});
  }
  return items;   // oldest -> newest
}

function chatSig(items) {
  if (!items.length) return "0";
  const last = items[items.length - 1];
  return items.length + "|" + last.who + "|" + last.text;
}

function appendMsg(who, text) {
  const d = document.createElement("div");
  d.className = "msg " + who;
  d.innerHTML = '<div class="who">' + (who === "user" ? "you" : "assistant") + "</div>" + esc(text);
  $("msgs").appendChild(d);
  return d;
}

function renderChat(s, force) {
  const items = chatItems(s);
  const sig = chatSig(items);
  if (!force && sig === lastChatSig) return;      // unchanged -> leave the DOM alone
  lastChatSig = sig;
  const box = $("msgs");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="status">Send a message - your saved memory is included in the prompt, and the exchange is appended to the journal.</div>';
    return;
  }
  items.slice(-CHAT_MAX).forEach(it => appendMsg(it.who, it.text));
  box.scrollTop = box.scrollHeight;
}

async function syncChat(force) {
  try {
    const s = await api("/api/state");
    renderChat(s, force);
    const ac = !!(s.state && s.state.auto_consolidate);
    if ($("autoc").checked !== ac) $("autoc").checked = ac;
  } catch (e) {}
}

async function chat() {
  if (chatting) return;
  const inp = $("chatin"); const msg = inp.value.trim();
  if (!msg) return;
  chatting = true;
  inp.value = "";
  // Optimistic UI: show the user's message + a thinking bubble right away. The
  // turn is not in the journal yet (the server appends Q then A only after the
  // model answers), so render it locally, then let the full rebuild from the
  // journal replace it the moment the turn lands.
  lastChatSig = null;                          // force the rebuild once it lands
  appendMsg("user", msg);
  const wait = appendMsg("bot", "thinking...");
  $("msgs").scrollTop = $("msgs").scrollHeight;
  const t0 = Date.now();
  const iv = setInterval(() => {
    wait.lastChild.textContent =
      "thinking... " + Math.round((Date.now() - t0) / 1000) + "s (local model - be patient)";
  }, 1000);
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), 300000);
  try {
    const r = await api("/api/chat", {message: msg, auto_consolidate: $("autoc").checked}, ctl);
    // Q and A are now in the journal -> rebuild the whole view from the source
    // of truth (this also removes the thinking bubble and pairs the answer with
    // the correct question).
    await syncChat(true);
    const c = r.consolidation;
    if (c) {
      const s = c.summary || {};
      if (c.ok) {
        note('<a href="/memory">consolidation done</a> +' + (s.added||0) + ' added, ' +
             (s.updated||0) + ' updated, ' + (s.removed||0) + ' removed (' + (s.notes||0) + ' notes)', false, true);
      } else {
        note("consolidation failed: " + c.detail, true, false);
      }
    } else {
      note('<a href="/memory">saved to journal</a> enable auto-consolidate, or press Consolidate on the memory page to turn it into facts', false, true);
    }
  } catch (e) {
    // The failed turn is not in the journal: drop the thinking bubble, keep the
    // user's message, and surface the error in the status line.
    const msgs = $("msgs").querySelectorAll(".msg");
    if (msgs.length) {
      const last = msgs[msgs.length - 1];
      if (last && last.classList.contains("bot")) last.remove();
    }
    note("ERROR: " + e.message +
      (e.name === "AbortError" ? " - timed out after 5 min. The server may still finish it; it will show up in the journal after a refresh." : ""), true, false);
  } finally {
    clearInterval(iv); clearTimeout(to); chatting = false;
  }
}

$("chatin").addEventListener("keydown", e => { if (e.key === "Enter") chat(); });
$("autoc").addEventListener("change", () =>
  api("/api/settings", {auto_consolidate: $("autoc").checked}));

syncChat(true);
// Live polling: pick up late LLM responses without a manual refresh.
setInterval(() => { if (!chatting) syncChat(false); }, 5000);
"""

JS_MEMORY = """
function renderFacts(facts) {
  $("facts").innerHTML = facts.length ? facts.map(f => `
    <div class="fact">
      <span class="k">${esc(f.key)}</span>
      <span class="v">${esc(f.value)}
        ${(f.tags||[]).length ? `<span class="tags">[${f.tags.map(esc).join(", ")}]</span>` : ""}
      </span>
      <button class="del" title="supersede (soft delete)"
        onclick="supFact('${f.id}')">×</button>
    </div>`).join("") : '<div class="status">no facts yet</div>';
}

function renderJournal(j) {
  $("journal").innerHTML = j.length ? j.map(e =>
    `<div class="jentry"><span class="ts">${esc(e.ts)}</span><br>${esc(e.text)}</div>`
  ).join("") : '<div class="status">journal empty</div>';
}

function renderDebug(d) {
  $("dlog").innerHTML = d.length ? d.map((e,i) => `
    <div class="dentry" id="de${i}">
      <div class="head" onclick="document.getElementById('de${i}').classList.toggle('open')">
        <span class="badge ${e.status}">${e.status}</span>
        <span class="meta">${esc((e.model||"").split("/").pop())} · ${e.elapsed_s}s ·
          req ${e.request_chars != null ? e.request_chars : "-"}c · resp ${e.response_chars != null ? e.response_chars : "-"}c</span>
      </div>
      <div class="detail">
        <div class="lbl">url</div><pre>${esc(e.url)}</pre>
        ${e.error ? `<div class="lbl">error</div><pre>${esc(e.error)}</pre>` : ""}
        ${e.system_excerpt ? `<div class="lbl">system (excerpt)</div><pre>${esc(e.system_excerpt)}</pre>` : ""}
        ${e.user_excerpt ? `<div class="lbl">user (excerpt)</div><pre>${esc(e.user_excerpt)}</pre>` : ""}
        ${e.response_excerpt ? `<div class="lbl">response (excerpt)</div><pre>${esc(e.response_excerpt)}</pre>` : ""}
        ${e.usage ? `<div class="lbl">usage</div><pre>${esc(JSON.stringify(e.usage))}</pre>` : ""}
      </div>
    </div>`).join("") : '<div class="status">no LLM calls logged yet</div>';
}

function refreshAll() {
  api("/api/state").then(s => {
    const ac = !!(s.state && s.state.auto_consolidate);
    if ($("autoc").checked !== ac) $("autoc").checked = ac;
    $("ident").value = s.identity;
    renderFacts(s.facts);
    renderJournal(s.journal);
    renderDebug(s.debug);
    $("cfginfo").innerHTML = kv([
      ["base_url", s.config.base_url], ["model", s.config.model],
      ["api_key", s.config.api_key],
      ["recall", JSON.stringify(s.config.recall)],
      ["memory_dir", s.config.memory_dir],
    ]);
    const cfg = s.config;
    api("/api/ping").then(p => {
      $("srvinfo").innerHTML = kv([
        ["endpoint", cfg.base_url + "/chat/completions"],
        ["status", p.ok ? "reachable" : "UNREACHABLE"],
        p.ok ? ["models", p.models.join(", ")] : ["error", p.error],
      ]);
    }).catch(() => {});
  }).catch(() => {});
}

async function addFact() {
  const key = $("fk").value.trim(), value = $("fv").value.trim();
  if (!key || !value) return;
  try {
    await api("/api/fact", {key, value, tags: $("ft").value});
    $("fk").value = $("fv").value = $("ft").value = "";
    refreshAll();
  } catch (e) { alert(e.message); }
}
async function supFact(id) {
  if (!confirm("Supersede this fact? (kept in facts.json for the audit trail)")) return;
  await api("/api/fact/supersede", {id});
  refreshAll();
}
async function saveIdentity() {
  try {
    await api("/api/identity", {identity: $("ident").value});
    $("identstatus").textContent = "saved ✓";
    setTimeout(() => $("identstatus").textContent = "", 2000);
  } catch (e) { alert(e.message); }
}
async function consolidate() {
  const b = $("conbbtn"); b.disabled = true; b.textContent = "consolidating…";
  const st = $("constatus"); st.textContent = ""; st.classList.remove("err");
  try {
    const r = await api("/api/consolidate", {});  // {} forces POST
    const s = r.summary || {};
    st.textContent = s.up_to_date
      ? "journal already up to date — nothing new to consolidate"
      : `+${s.added||0} added · ${s.updated||0} updated · ${s.removed||0} removed (${s.notes||0} notes processed)`;
    refreshAll();
  } catch (e) {
    st.textContent = "failed: " + e.message;
    st.classList.add("err");
  } finally { b.disabled = false; b.textContent = "Consolidate"; }
}
$("autoc").addEventListener("change", () =>
  api("/api/settings", {auto_consolidate: $("autoc").checked}));

ping(); refreshAll();
// Live polling: new facts, journal entries, and LLM call log appear automatically.
setInterval(() => { refreshAll(); }, 5000);
"""

HEADER = (
    "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>AI-MEM — __TITLE__</title>\n"
    "<style>" + CSS + "</style></head>\n<body>\n<header>\n"
    "  <h1>AI-MEM</h1>\n"
    "  <span class=\"pill\" title=\"UI version — if this is not the latest after a hard refresh, your browser cached an old page\">ui v9</span>\n"
    "  <span class=\"dot\" id=\"dot\"></span><span class=\"pill\" id=\"pill\">checking server…</span>\n"
    "  <span class=\"pill\" id=\"model\"></span>\n"
    "  <span style=\"flex:1\"></span>\n"
    "  <nav>\n"
    "    <a class=\"navlink __CHATCLS__\" href=\"/\">Chat</a>\n"
    "    <a class=\"navlink __MEMCLS__\" href=\"/memory\">Memory &amp; Debug</a>\n"
    "  </nav>\n"
    "  <button onclick=\"location.reload()\">↻ refresh</button>\n"
    "</header>\n"
)


def make_header(title, chat_cls, mem_cls):
    return (HEADER.replace("__TITLE__", title)
                  .replace("__CHATCLS__", chat_cls)
                  .replace("__MEMCLS__", mem_cls))


PAGE_CHAT = (
    make_header("chat", "active", "")
    + """<main class="chat">
  <section class="panel">
    <h2>Chat <span class="pill">memory-augmented · logged</span>
      <label style="font-size:12px;color:var(--dim);display:flex;gap:5px;align-items:center;cursor:pointer;margin-left:auto">
        <input type="checkbox" id="autoc" style="width:auto"> auto-consolidate
      </label></h2>
    <div class="body" id="msgs"></div>
    <div id="chatstatus"></div>
    <div id="chatbar">
      <input id="chatin" placeholder="Ask something…" autocomplete="off">
      <button class="primary" onclick="chat()">Send</button>
    </div>
  </section>
</main>
<script>""" + JS_COMMON + JS_CHAT + """</script>
</body></html>
"""
)

PAGE_MEMORY = (
    make_header("memory & debug", "", "active")
    + """<main class="mem">
  <section class="panel">
    <h2>Memory
      <span><label style="font-size:12px;color:var(--dim);display:flex;gap:5px;align-items:center;cursor:pointer;margin-right:8px">
        <input type="checkbox" id="autoc" style="width:auto"> auto</label>
        <button class="primary" id="conbbtn" onclick="consolidate()">Consolidate</button></span>
    </h2>
    <div class="body">
      <div class="sect">Identity (hot memory)</div>
      <textarea id="ident" rows="6" spellcheck="false"></textarea>
      <div style="margin-top:6px"><button onclick="saveIdentity()">Save identity</button>
        <span class="status" id="identstatus"></span>
        <span class="status" id="constatus"></span></div>

      <div class="sect">Facts</div>
      <div id="facts"></div>

      <div class="sect">Add / update fact</div>
      <div class="addfact">
        <div class="row">
          <input id="fk" placeholder="key (e.g. favorite_editor)">
        </div>
        <div class="row">
          <input id="fv" placeholder="value">
        </div>
        <div class="row">
          <input id="ft" placeholder="tags, comma separated (optional)">
          <button onclick="addFact()">Add</button>
        </div>
      </div>

      <div class="sect">Journal (newest first)</div>
      <div id="journal"></div>
    </div>
  </section>

  <section class="panel">
    <h2>Debug <span class="pill">last 20 LLM calls</span></h2>
    <div class="body">
      <div class="sect">LLM server</div>
      <div class="kv" id="srvinfo"></div>

      <div class="sect">Config</div>
      <div class="kv" id="cfginfo"></div>

      <div class="sect">LLM call log</div>
      <div id="dlog"></div>
    </div>
  </section>
</main>
<script>""" + JS_COMMON + JS_MEMORY + """</script>
</body></html>
"""
)


# ---------------------------------------------------------------- main

def main():
    global PORT
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"AI-MEM UI running at  http://localhost:{PORT}  (Ctrl+C to stop)")
    print(f"  chat:              http://localhost:{PORT}/")
    print(f"  memory & debug:    http://localhost:{PORT}/memory")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
