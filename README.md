# JDW Fix Proxy

An **Anthropic-compatible** (`/v1/messages`) reverse proxy that repairs a broken
upstream relay (`api.justwoker.icu`, model `claude-opus-4-8`) so that real
Anthropic clients — **Cline**, DeepSeek Harness, Claude Code, ZCode, etc. — work
against it.

The upstream is not a transparent relay: it is an agent wrapper that injects its
own ~10.4k-token system prompt and its own tools (`read_tabular`,
`system_todo_write`), runs its own agent loop, and ships a broken SSE converter.
See `JDW_PROTOCOL_AUDIT.md` for the full breakdown.

## What it fixes

| Upstream bug | Fix |
|---|---|
| Streaming drops all text & tool_use (only thinking survives) | Always calls upstream **non-stream**, then **synthesizes a correct SSE stream** for the client |
| Client `tools` / `tool_choice` are discarded | Injects client tools into the **bottom of the system prompt** with a JSON `tool_call` contract, parses the model's output and **rewrites it into real `tool_use` blocks** (`stop_reason=tool_use`) |
| `max_tokens` ignored | Truncates and reports `stop_reason=max_tokens` |
| `stop_sequences` ignored | Cuts text before the stop word, reports `stop_sequence` |
| ~10.4k phantom context tokens | Normalizes `usage` (subtracts baseline) |
| `thinking` leaks into content | Optionally stripped |
| Model always runs extended thinking (even when not requested) | `curb_thinking` adds a system instruction to reason minimally — saves output tokens and latency |
| Tool addendum schemas pretty-printed | Schemas are injected compact (single-line JSON) to cut input tokens |
| Conversations grow unbounded (upstream never reports `max_tokens`, so clients never compact) | `max_input_tokens` guard returns a real `400 "prompt is too long"` before an oversized request is sent and billed |
| `count_tokens` → 404 | Implemented locally (estimate) |

Tool/tool_result history from the client is flattened into text so the model can
read prior turns.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python jdw_proxy.py
```

```
JDW Fix Proxy listening on http://127.0.0.1:8181
  Anthropic endpoint : http://127.0.0.1:8181/v1
  Dashboard          : http://127.0.0.1:8181/
```

## Point your client here

- **Base URL:** `http://localhost:8181` (Cline) or `http://localhost:8181/v1`
- **API key:** your JDW key (also stored in `config.json`)
- **Model:** `claude-opus-4-8`

## Dashboard

Open `http://localhost:8181/` for a bilingual (EN/RU) control panel:

- Server status, client endpoint, upstream URL, masked API key.
- Toggles for every fix: tool injection, history flattening, strip thinking,
  enforce max_tokens / stop_sequences, usage mode + baseline.
- A live request log (model, mode, tools, stop_reason, usage, latency).

All settings persist to `config.json`.

## Configuration (`config.json`)

```json
{
  "upstream_base_url": "https://api.justwoker.icu/v1",
  "api_key": "sk-...",
  "listen_host": "127.0.0.1",
  "listen_port": 8181,
  "model": "claude-opus-4-8",
  "features": {
    "tool_injection": true,
    "flatten_tool_history": true,
    "strip_thinking": true,
    "curb_thinking": true,
    "enforce_max_tokens": true,
    "enforce_stop_sequences": true,
    "usage_mode": "normalized",
    "usage_baseline_tokens": 10380
  }
}
```

## Cutting token burn

The upstream wrapper runs the real model with extended thinking always on and
re-sends its own ~10.4k system prompt on every call, so spend adds up fast.
What this proxy can (and cannot) do about it:

- **`curb_thinking`** (default on): injects a short system instruction telling
  the model to reason minimally and answer directly. Skipped automatically when
  the client explicitly sends `thinking: {"type": "enabled"}`. This is the only
  reliable lever — the wrapper ignores the `thinking` request field.
- **`strip_thinking`** (default on): thinking blocks are still *generated*
  upstream (that costs output tokens) but never reach the client.
- **Compact tool addendum**: client tool schemas are injected as single-line
  JSON instead of pretty-printed — saves a large share of the injected block on
  every request, which matters most with agent clients offering many tools.
- **Compact history**: past `tool_use`/`tool_result` blocks are flattened
  without the long `toolu_...` ids and with compact JSON.
- **Input guard (`max_input_tokens`, default 200000)**: the upstream never
  reports `stop_reason=max_tokens`, so agent clients never auto-compact and
  conversations can grow until a single turn is billed at 1M+ input tokens.
  When the estimated payload exceeds the limit the proxy returns the same
  `400 invalid_request_error "prompt is too long: N tokens > M maximum"` real
  Anthropic returns, which Cline / Claude Code / ZCode recognize and handle by
  compacting the conversation. Set `0` to disable.
- **Cheaper retries**: hard upstream errors (500/502/504) are retried at most
  once instead of three times — the wrapper runs its agent loop *before*
  responding, so a failed request has already burned and billed its tokens.
- What it **cannot** fix: the wrapper's own ~10.4k injected prompt, its internal
  agent loop (it may issue more than one upstream call per request), and tokens
  the relay bills for requests that fail inside the wrapper. The dashboard's
  **Sent (est)** column shows how many tokens the proxy actually put on the
  wire per request — if that number is small while the relay's own panel claims
  millions, the inflation is happening inside the relay (evidence for a
  refund/abuse complaint).

## How the tool bridge works

Because the upstream throws away the native `tools` field and the model only
knows its injected tools, the proxy appends a block to the **end** of the system
prompt that (1) explains the provider misconfigured native tools, (2) lists the
client's tools with their JSON schemas, and (3) defines the output contract:

~~~
```tool_call
{"name": "read_file", "input": {"path": "src/app.ts"}}
```
~~~

The proxy scans the model's reply for these fenced blocks and converts each into
a real Anthropic `tool_use` content block, setting `stop_reason` to `tool_use`.
The client then returns results as normal `tool_result` blocks, which the proxy
flattens back into readable text on the next turn.

## Notes

- OpenAI `/v1/chat/completions` on the upstream is WAF-blocked (403); this proxy
  only speaks the Anthropic Messages API.
- Token counts are heuristic estimates (upstream has no real counter).
