#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JDW Fix Proxy
=============
An Anthropic-compatible (`/v1/messages`) reverse proxy that sits in front of a
broken upstream relay (api.justwoker.icu, model `claude-opus-4-8`) and repairs
the protocol violations documented in JDW_PROTOCOL_AUDIT.md.

What the upstream does wrong (and what this proxy fixes):

  1. Streaming drops all text + tool_use blocks (only thinking deltas survive).
     -> We ALWAYS call the upstream in non-stream mode (where the full content
        is intact) and SYNTHESIZE a correct SSE stream for the client.

  2. Client-supplied `tools` / `tool_choice` are discarded; the model only knows
     its own injected tools (read_tabular / system_todo_write).
     -> We inject the client's tools into the bottom of the system prompt with a
        strict JSON "tool_call" output contract, then PARSE the model's text and
        REWRITE it into real Anthropic `tool_use` blocks (stop_reason=tool_use).

  3. max_tokens ignored (never returns stop_reason=max_tokens).
     -> We truncate output to max_tokens and set stop_reason correctly.

  4. stop_sequences ignored (stop word leaks into output).
     -> We cut the text before the stop sequence and report it.

  5. ~10.4k phantom context tokens inflate usage.
     -> We normalize usage (subtract the measured baseline).

  6. thinking blocks leak into content even when not requested.
     -> Optionally stripped.

  Plus: local /v1/messages/count_tokens, /v1/models passthrough, proper
  Anthropic-shaped errors, and a bilingual (EN/RU) web dashboard at `/`.

Run:  python jdw_proxy.py
"""

import json
import os
import re
import time
import uuid
import asyncio
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "upstream_base_url": "https://api.justwoker.icu/v1",
    "api_key": "",
    # Optional extra keys tried automatically only if the primary key is
    # genuinely rejected (401/403) after its retries are exhausted.
    "fallback_api_keys": [],
    "listen_host": "127.0.0.1",
    "listen_port": 8181,
    "model": "claude-opus-4-8",
    "upstream_timeout_s": 300,
    "features": {
        "tool_injection": True,
        "strict_tool_names": True,
        "flatten_tool_history": True,
        "strip_thinking": True,
        "curb_thinking": True,
        "enforce_max_tokens": True,
        "enforce_stop_sequences": True,
        "usage_mode": "normalized",       # "normalized" | "raw" | "estimated"
        "usage_baseline_tokens": 10380,
        # Payload guard: refuse to send (and therefore pay for) requests whose
        # estimated input exceeds this many tokens. The upstream wrapper never
        # reports stop_reason=max_tokens, so agent clients never auto-compact
        # and conversations grow until every turn burns ~1M+ billed tokens.
        # Mirrors real Anthropic behaviour (400 "prompt is too long"), which
        # clients like Cline / Claude Code / ZCode already handle by compacting.
        "max_input_tokens": 200000,
    },
    "ui_lang": "en",
}

_config_lock = threading.RLock()


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
            # shallow+features merge
            for k, v in disk.items():
                if k == "features" and isinstance(v, dict):
                    cfg["features"].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[config] failed to read {CONFIG_PATH}: {e}")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    with _config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)


CONFIG = load_config()


# --------------------------------------------------------------------------- #
# In-memory request log (for the dashboard)
# --------------------------------------------------------------------------- #

LOG: "deque[Dict[str, Any]]" = deque(maxlen=200)
_log_lock = threading.Lock()


def log_event(entry: Dict[str, Any]) -> None:
    entry["ts"] = time.time()
    with _log_lock:
        LOG.appendleft(entry)


# --------------------------------------------------------------------------- #
# Tool-injection: build the system-prompt addendum + parse tool_call blocks
# --------------------------------------------------------------------------- #

TOOL_CALL_FENCE_RE = re.compile(
    r"```(?:tool_call|json)?\s*\n(\{.*?\})\s*\n```",
    re.DOTALL,
)

TOOL_PREAMBLE_EN = (
    "## IMPORTANT — Tool access override\n"
    "The hosting provider misconfigured native tool support on this endpoint, so "
    "the normal Anthropic `tools` mechanism is not delivered to you. A compatibility "
    "proxy restores it for you here. You DO have access to the tools listed below, "
    "and you MUST use them when the task requires it.\n\n"
    "The host also injects two phantom tools named `read_tabular` and "
    "`system_todo_write`. Those are NOT yours — never call them. Your real, usable "
    "tools are ONLY the ones listed under 'Available tools' below.\n"
    "If earlier messages in THIS conversation show you saying you only have those "
    "phantom tools, or that you cannot read files / browse the repo / use tools, that "
    "was before this fix was active — DISREGARD those statements. You can use every "
    "tool listed below right now; when a task needs one, call it instead of claiming "
    "you cannot.\n\n"
    "To call a tool, emit a fenced code block tagged `tool_call` containing a single "
    "JSON object with exactly two keys: `name` (the tool name) and `input` (the "
    "arguments object matching that tool's JSON schema). Example:\n\n"
    "```tool_call\n"
    "{\"name\": \"read_file\", \"input\": {\"path\": \"src/app.ts\"}}\n"
    "```\n\n"
    "Rules:\n"
    "- Emit the tool_call block and then STOP; do not predict the tool's result.\n"
    "- You may write a short sentence before the block explaining what you are about to do.\n"
    "- Use ONE tool_call block per turn unless the task clearly needs several.\n"
    "- The `input` must be valid JSON and conform to the tool's schema.\n"
    "- Call tools ONLY by a name that appears in the list below. Never invent a\n"
    "  tool name or call one that is not listed; if a capability is not listed,\n"
    "  solve the task without a tool.\n\n"
    "### Available tools\n"
)

TOOL_PREAMBLE_FORCED_EN = (
    "\nThe caller REQUIRES you to call the tool named `{name}` on this turn. "
    "Respond with exactly one `tool_call` block for `{name}` and nothing else.\n"
)

# The upstream wrapper always runs the real model with extended thinking
# enabled (audit finding 6), even when the client never asked for it — that
# burns output tokens and adds latency on every turn. The wrapper ignores the
# `thinking` request field, so the only lever we have is a system-level
# instruction. Skipped when the client explicitly requested thinking.
CURB_THINKING_EN = (
    "\n## Response style\n"
    "Answer directly and efficiently. Keep hidden reasoning to the minimum "
    "needed: no long deliberation before answering. On complex tasks think in "
    "compact steps rather than long exploratory passages.\n"
)


def build_tool_system_block(tools: List[Dict[str, Any]],
                            tool_choice: Optional[Dict[str, Any]]) -> str:
    lines = [TOOL_PREAMBLE_EN]
    for t in tools:
        name = t.get("name", "?")
        desc = t.get("description", "") or ""
        schema = t.get("input_schema") or t.get("inputSchema") or {}
        lines.append(f"\n#### `{name}`\n{desc}\n")
        # Compact JSON: pretty-printed schemas roughly double the token count
        # of the injected addendum, which is re-sent on EVERY request.
        try:
            schema_str = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            schema_str = str(schema)
        lines.append(f"Input JSON schema:\n```json\n{schema_str}\n```\n")

    # Make the closed set of valid names explicit so the model can't invent one.
    valid_names = [t.get("name") for t in tools if t.get("name")]
    if valid_names:
        lines.append(
            "\n### Valid tool names (the ONLY names you may call)\n"
            + ", ".join(f"`{n}`" for n in valid_names)
            + "\n"
        )

    block = "".join(lines)

    if tool_choice:
        ctype = tool_choice.get("type")
        if ctype == "tool" and tool_choice.get("name"):
            block += TOOL_PREAMBLE_FORCED_EN.format(name=tool_choice["name"])
        elif ctype == "any":
            block += ("\nThe caller requires you to call one of the available tools "
                      "on this turn. Respond with a `tool_call` block.\n")
    return block


def extract_system_text(system: Any) -> str:
    """Anthropic `system` may be a string or a list of blocks."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return str(system)


# --------------------------------------------------------------------------- #
# History flattening: turn tool_use / tool_result blocks into text the upstream
# model can actually read (since it never saw the real tool protocol).
# --------------------------------------------------------------------------- #

def flatten_content_blocks(content: Any, role: str) -> str:
    """Convert a message's content (string or block list) into plain text,
    rendering tool_use / tool_result into the JSON contract format."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: List[str] = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        btype = b.get("type")
        if btype == "text":
            parts.append(b.get("text", ""))
        elif btype == "thinking":
            # don't feed stale thinking back
            continue
        elif btype == "tool_use":
            payload = {"name": b.get("name"), "input": b.get("input", {})}
            parts.append(
                "```tool_call\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n```"
            )
        elif btype == "tool_result":
            inner = b.get("content", "")
            if isinstance(inner, list):
                texts = []
                for ib in inner:
                    if isinstance(ib, dict):
                        if ib.get("type") == "text":
                            texts.append(ib.get("text", ""))
                        elif ib.get("type") == "image":
                            texts.append("[image]")
                    else:
                        texts.append(str(ib))
                inner = "\n".join(texts)
            is_err = b.get("is_error")
            tag = "TOOL ERROR" if is_err else "TOOL RESULT"
            # The long `toolu_...` id carries no meaning for the model (results
            # pair with calls by order); dropping it saves tokens every turn.
            parts.append(f"[{tag}]\n{inner}")
        elif btype == "image":
            # keep images as proper blocks elsewhere; here mark presence
            parts.append("[image]")
        else:
            parts.append(json.dumps(b, ensure_ascii=False))
    return "\n\n".join(p for p in parts if p != "")


def message_has_image(content: Any) -> bool:
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "image" for b in content)
    return False


def flatten_messages(messages: List[Dict[str, Any]],
                     flatten_tools: bool) -> List[Dict[str, Any]]:
    """Produce an upstream-friendly messages array.

    If a message contains images we KEEP the structured block list (upstream
    supports image blocks per the audit), but still rewrite tool blocks to text.
    Otherwise we collapse to a plain string.
    """
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        if not flatten_tools:
            out.append({"role": role, "content": content})
            continue

        if message_has_image(content) and isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    new_blocks.append(b)
                else:
                    txt = flatten_content_blocks([b], role)
                    if txt:
                        new_blocks.append({"type": "text", "text": txt})
            out.append({"role": role, "content": new_blocks})
        else:
            out.append({"role": role, "content": flatten_content_blocks(content, role)})
    return out


# --------------------------------------------------------------------------- #
# Parse the model's text output -> real content blocks (text + tool_use)
# --------------------------------------------------------------------------- #

def parse_tool_calls_from_text(text: str,
                               valid_names: Optional[set] = None,
                               strict: bool = True) -> List[Dict[str, Any]]:
    """Split raw model text into Anthropic content blocks, converting fenced
    tool_call JSON into tool_use blocks.

    If `strict` is True and `valid_names` is provided, a tool_call whose name
    is NOT in valid_names is left as plain text instead of being emitted as a
    tool_use block. This prevents the client from ever receiving an
    "unknown tool" call (the model occasionally invents or mistypes a name).
    """
    blocks: List[Dict[str, Any]] = []
    last = 0
    for m in TOOL_CALL_FENCE_RE.finditer(text):
        pre = text[last:m.start()].strip()
        raw = m.group(1)
        parsed = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        name = parsed.get("name") if isinstance(parsed, dict) else None
        # Only treat as a tool call if it has a name key AND (when strict) the
        # name is one the client actually offered.
        name_ok = bool(name)
        if strict and valid_names is not None:
            name_ok = name in valid_names
        if isinstance(parsed, dict) and name_ok:
            if pre:
                blocks.append({"type": "text", "text": pre})
            tool_input = parsed.get("input", parsed.get("arguments", {}))
            if not isinstance(tool_input, dict):
                tool_input = {}
            blocks.append({
                "type": "tool_use",
                "id": "toolu_" + uuid.uuid4().hex[:24],
                "name": name,
                "input": tool_input,
            })
            last = m.end()
        elif isinstance(parsed, dict) and name and not name_ok:
            # Unknown tool name -> keep the whole fenced block as literal text
            # so the client never sees an invalid tool_use. Log it for the admin.
            log_event({"unknown_tool": name,
                       "note": "model called an unlisted tool -> kept as text"})
            # leave `last` as-is so the fence text falls into the tail/pre of
            # the next iteration; advance only past the preamble text we consumed
        # else: not a tool call -> leave as text (handled by tail)
    tail = text[last:].strip()
    if tail:
        blocks.append({"type": "text", "text": tail})
    if not blocks:
        blocks.append({"type": "text", "text": text})
    return blocks


# --------------------------------------------------------------------------- #
# Post-processing of the upstream non-stream response
# --------------------------------------------------------------------------- #

def approx_tokens(text: str) -> int:
    # rough heuristic ~4 chars/token
    return max(1, int(len(text) / 4))


# Fixed token estimate for an image block: the API bills images at a flat
# cost regardless of base64 size, while len/4 would wildly overcount.
IMAGE_TOKEN_ESTIMATE = 1600


def estimate_payload_tokens(payload: Dict[str, Any]) -> int:
    """Rough estimate of the INPUT tokens the upstream will see for a payload."""
    total = approx_tokens(payload.get("system") or "")
    for m in payload.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            total += approx_tokens(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    total += approx_tokens(str(b))
                elif b.get("type") == "image":
                    total += IMAGE_TOKEN_ESTIMATE
                elif b.get("type") == "text":
                    total += approx_tokens(b.get("text", ""))
                else:
                    total += approx_tokens(
                        json.dumps(b, ensure_ascii=False, default=str))
        else:
            total += approx_tokens(str(c))
    return total


def apply_stop_sequences(text: str,
                         stop_sequences: Optional[List[str]]) -> Tuple[str, Optional[str]]:
    if not stop_sequences:
        return text, None
    cut_idx = None
    matched = None
    for s in stop_sequences:
        if not s:
            continue
        i = text.find(s)
        if i != -1 and (cut_idx is None or i < cut_idx):
            cut_idx = i
            matched = s
    if cut_idx is not None:
        return text[:cut_idx], matched
    return text, None


def normalize_usage(raw_usage: Dict[str, Any],
                    feats: Dict[str, Any],
                    output_text_len_tokens: int) -> Dict[str, Any]:
    mode = feats.get("usage_mode", "normalized")
    baseline = int(feats.get("usage_baseline_tokens", 0) or 0)
    raw_in = int(raw_usage.get("input_tokens", 0) or 0)
    raw_out = int(raw_usage.get("output_tokens", 0) or 0)

    if mode == "raw":
        return {"input_tokens": raw_in, "output_tokens": raw_out}
    if mode == "estimated":
        return {"input_tokens": max(0, raw_in - baseline),
                "output_tokens": raw_out or output_text_len_tokens}
    # normalized: subtract baseline from input, keep real output
    norm_in = raw_in
    # upstream sometimes reports input_tokens as the big combined number
    if raw_in >= baseline:
        norm_in = raw_in - baseline
    return {"input_tokens": max(0, norm_in),
            "output_tokens": raw_out or output_text_len_tokens}


def process_upstream_message(upstream: Dict[str, Any],
                             client_req: Dict[str, Any],
                             feats: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the upstream non-stream message into a clean Anthropic message,
    applying all fixes. Returns a dict ready to serialize / to stream."""
    content_in = upstream.get("content", []) or []

    # 1) Collect raw text, drop/stash thinking, keep upstream-native tool_use.
    text_segments: List[str] = []
    native_tool_blocks: List[Dict[str, Any]] = []
    had_thinking = False
    for b in content_in:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            text_segments.append(b.get("text", ""))
        elif bt == "thinking":
            had_thinking = True
            if not feats.get("strip_thinking", True):
                # keep thinking as its own block (verbatim) at the front
                native_tool_blocks.append(b)
        elif bt == "tool_use":
            # upstream emitted its OWN tool (e.g. system_todo_write) — keep it,
            # client asked for its own tools so this is rare; pass through.
            native_tool_blocks.append(b)

    joined_text = "\n\n".join(t for t in text_segments if t != "")

    # 2) stop_sequences
    stop_seq_matched = None
    if feats.get("enforce_stop_sequences", True):
        joined_text, stop_seq_matched = apply_stop_sequences(
            joined_text, client_req.get("stop_sequences"))

    # 3) parse tool_call fences from text -> blocks
    client_tools = client_req.get("tools") or []
    client_has_tools = bool(client_tools) and feats.get("tool_injection", True)
    # Closed set of names the client actually offered.
    valid_names = {t.get("name") for t in client_tools if t.get("name")}
    # Only enforce the closed set when the client gave us a list to check against.
    strict = feats.get("strict_tool_names", True) and bool(valid_names)
    if client_has_tools:
        parsed_blocks = parse_tool_calls_from_text(
            joined_text, valid_names=valid_names, strict=strict)
    else:
        parsed_blocks = [{"type": "text", "text": joined_text}] if joined_text else []

    # If strict, drop any upstream-native tool_use whose name isn't offered by
    # the client (prevents the client receiving an "unknown tool" block).
    if strict:
        kept = []
        for b in native_tool_blocks:
            if b.get("type") == "tool_use" and b.get("name") not in valid_names:
                log_event({"unknown_tool": b.get("name"),
                           "note": "upstream-native tool not offered by client -> dropped"})
                continue
            kept.append(b)
        native_tool_blocks = kept

    # merge: thinking (if kept) + native tool blocks come first, then parsed
    content_out: List[Dict[str, Any]] = []
    for b in native_tool_blocks:
        content_out.append(b)
    content_out.extend(parsed_blocks)
    if not content_out:
        content_out = [{"type": "text", "text": ""}]

    # 4) determine stop_reason
    has_tool_use = any(b.get("type") == "tool_use" for b in content_out)
    stop_reason = "end_turn"
    stop_sequence_val = None
    if has_tool_use:
        stop_reason = "tool_use"
    if stop_seq_matched is not None:
        stop_reason = "stop_sequence"
        stop_sequence_val = stop_seq_matched

    # 5) max_tokens enforcement (only meaningful for text)
    max_tokens = client_req.get("max_tokens")
    if (feats.get("enforce_max_tokens", True) and isinstance(max_tokens, int)
            and max_tokens > 0 and stop_reason == "end_turn" and not has_tool_use):
        # truncate the LAST text block by approx token budget
        total = 0
        truncated = False
        new_content = []
        for b in content_out:
            if b.get("type") == "text":
                words = b["text"]
                tk = approx_tokens(words)
                if total + tk > max_tokens:
                    budget_chars = max(0, (max_tokens - total) * 4)
                    b = {"type": "text", "text": words[:budget_chars]}
                    truncated = True
                    total = max_tokens
                    new_content.append(b)
                    break
                total += tk
            new_content.append(b)
        if truncated:
            content_out = new_content
            stop_reason = "max_tokens"

    # 6) usage
    out_tok_est = approx_tokens(joined_text)
    usage = normalize_usage(upstream.get("usage", {}) or {}, feats, out_tok_est)

    result = {
        "id": upstream.get("id", "msg_" + uuid.uuid4().hex[:16]),
        "type": "message",
        "role": "assistant",
        "model": client_req.get("model", upstream.get("model", CONFIG["model"])),
        "content": content_out,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence_val,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }
    result["_meta"] = {
        "had_thinking": had_thinking,
        "has_tool_use": has_tool_use,
        "client_tools": len(client_req.get("tools") or []),
    }
    return result


# --------------------------------------------------------------------------- #
# Build the upstream payload
# --------------------------------------------------------------------------- #

def build_upstream_payload(client_req: Dict[str, Any],
                           feats: Dict[str, Any]) -> Dict[str, Any]:
    model = client_req.get("model") or CONFIG["model"]

    # system prompt (+ tool injection)
    system_text = extract_system_text(client_req.get("system"))
    tools = client_req.get("tools") or []
    if feats.get("tool_injection", True) and tools:
        addendum = build_tool_system_block(tools, client_req.get("tool_choice"))
        system_text = (system_text + "\n\n" + addendum) if system_text else addendum

    # curb thinking (see CURB_THINKING_EN) unless the client explicitly asked
    # for thinking — in that case the model is allowed to deliberate.
    thinking_req = client_req.get("thinking")
    client_wants_thinking = (isinstance(thinking_req, dict)
                             and thinking_req.get("type") == "enabled")
    if feats.get("curb_thinking", True) and not client_wants_thinking:
        system_text = (system_text + CURB_THINKING_EN) if system_text \
            else CURB_THINKING_EN

    # messages (flatten tool blocks for upstream comprehension)
    msgs = flatten_messages(client_req.get("messages", []),
                            feats.get("flatten_tool_history", True))

    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": client_req.get("max_tokens", 4096) or 4096,
        "messages": msgs,
        "stream": False,  # ALWAYS non-stream upstream
    }
    if system_text:
        payload["system"] = system_text
    # pass temperature/top_p/top_k through (they work per audit)
    for k in ("temperature", "top_p", "top_k"):
        if k in client_req and client_req[k] is not None:
            payload[k] = client_req[k]
    # NOTE: we intentionally do NOT forward client tools/tool_choice/stop_sequences
    # to the upstream (it discards them); we handle them ourselves.
    return payload


# --------------------------------------------------------------------------- #
# SSE synthesis
# --------------------------------------------------------------------------- #

def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def synthesize_sse(message: Dict[str, Any], emit_start: bool = True):
    """Yield a spec-correct Anthropic SSE stream from a finished message dict.

    When `emit_start` is False the leading `message_start` + `ping` events are
    skipped (used by stream_with_keepalive, which already opened the stream).
    """
    usage = message.get("usage", {})
    msg_id = message.get("id")
    model = message.get("model")

    if emit_start:
        # message_start (with real input usage)
        start_msg = {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": 0,
                },
            },
        }
        yield sse("message_start", start_msg)
        yield sse("ping", {"type": "ping"})

    content = message.get("content", [])
    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            text = block.get("text", "")
            # chunk text so clients render progressively
            for chunk in chunk_text(text, 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": chunk},
                })
                await asyncio.sleep(0)
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

        elif btype == "tool_use":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            })
            partial = json.dumps(block.get("input", {}), ensure_ascii=False)
            for chunk in chunk_text(partial, 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": chunk},
                })
                await asyncio.sleep(0)
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

        elif btype == "thinking":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            for chunk in chunk_text(block.get("thinking", ""), 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": chunk},
                })
                await asyncio.sleep(0)
            if block.get("signature"):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "signature_delta",
                              "signature": block["signature"]},
                })
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

    # message_delta with final stop_reason + output usage
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason"),
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield sse("message_stop", {"type": "message_stop"})


async def stream_with_keepalive(payload: Dict[str, Any],
                                client_req: Dict[str, Any],
                                feats: Dict[str, Any],
                                t0: float,
                                keepalive_s: float = 2.0,
                                sent_tokens: int = 0):
    """Streaming path that eliminates the "dead air" while the (non-stream)
    upstream is still composing its full reply.

    It opens the SSE stream to the client IMMEDIATELY (so the client sees bytes
    and never times out / feels stalled), emits a `ping` every `keepalive_s`
    seconds while call_upstream runs concurrently in the background, and only
    then streams the real content via synthesize_sse.
    """
    # Open the stream right away with a spec-correct message_start FIRST, so
    # the client sees bytes immediately and the ordering stays valid.
    provisional_id = "msg_" + uuid.uuid4().hex[:16]
    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": provisional_id,
            "type": "message",
            "role": "assistant",
            "model": payload.get("model") or CONFIG["model"],
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield sse("ping", {"type": "ping"})

    # Run the upstream call concurrently with the keepalive pings.
    task = asyncio.ensure_future(call_upstream(payload))
    try:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=keepalive_s)
            except asyncio.TimeoutError:
                # still waiting -> send a keepalive ping and loop
                yield sse("ping", {"type": "ping"})
            except Exception:
                # the task raised; break out and handle below via task.result()
                break

        status, upstream = task.result()
    except Exception as e:
        log_event({"model": payload.get("model"), "stream": True,
                   "error": f"upstream connection: {e}"})
        err = {"type": "error",
               "error": {"type": "api_error",
                         "message": f"Upstream connection failed: {e}"}}
        yield sse("error", err)
        return

    if status >= 400:
        msg = ""
        if isinstance(upstream, dict):
            msg = (upstream.get("error", {}) or {}).get("message") \
                  or upstream.get("message") \
                  or upstream.get("_raw") \
                  or json.dumps(upstream, ensure_ascii=False)[:500]
        else:
            msg = str(upstream)[:500]
        log_event({"model": payload.get("model"), "stream": True,
                   "status": status, "error": (msg or "")[:200], "attempts": 3})
        etype = "invalid_request_error" if status == 400 else "api_error"
        yield sse("error", {"type": "error",
                            "error": {"type": etype,
                                      "message": f"Upstream {status} (after retries): {msg}"}})
        return

    message = process_upstream_message(upstream, client_req, feats)
    meta = message.pop("_meta", {})
    dt = round(time.time() - t0, 2)
    log_event({
        "model": message.get("model"), "stream": True, "status": status,
        "latency_s": dt, "client_tools": meta.get("client_tools", 0),
        "tool_use": meta.get("has_tool_use", False),
        "had_thinking": meta.get("had_thinking", False),
        "stop_reason": message.get("stop_reason"),
        "usage": message.get("usage"),
        "sent_tokens": sent_tokens,
    })

    # Now stream the real content. message_start was already emitted above,
    # so skip it here to keep the SSE event ordering spec-correct.
    async for evt in synthesize_sse(message, emit_start=False):
        yield evt


def chunk_text(text: str, size: int):
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(title="JDW Fix Proxy")


def anthropic_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


async def _try_one_key(client: httpx.AsyncClient, url: str, api_key: str,
                       payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Optional[Exception]]:
    """Run the full retry loop for a SINGLE api key.

    Returns (status, data, last_exc). status == 0 means every attempt hit a
    network-level exception (last_exc carries the reason).
    """
    # Send BOTH auth styles: some upstreams expect Anthropic-native `x-api-key`,
    # others expect `Authorization: Bearer`. Sending both is safe and fixes
    # sporadic "API key is invalid" when the relay checks the other header.
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
    }
    # Some relays return 401 "invalid key" sporadically under load/rate-limiting,
    # so 401 is treated as retryable (the configured key is assumed correct).
    retryable = {401, 408, 409, 425, 429, 500, 502, 503, 504, 529}
    # Hard server errors: the wrapper runs its agent loop BEFORE responding, so
    # by the time it returns one of these the tokens are already burned and
    # billed. Retrying them more than once multiplies the cost for the same
    # likely-deterministic failure. One retry is enough for a transient blip.
    hard_server_errors = {500, 502, 504}
    backoff = [0.5, 1.5, 3.0]  # seconds waited BEFORE attempts 2, 3, ...
    max_attempts = 3
    status = 0
    data: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            delay = backoff[min(attempt - 2, len(backoff) - 1)]
            log_event({"retry": attempt, "after_status": status,
                       "sleep_s": delay, "note": "retry upstream"})
            await asyncio.sleep(delay)
        try:
            r = await client.post(url, headers=headers, json=payload)
        except Exception as e:  # network error -> retry
            last_exc = e
            status = 0
            data = {"error": {"type": "api_error",
                              "message": f"upstream connection: {e}"}}
            continue
        last_exc = None
        status = r.status_code
        raw_text = r.text or ""
        bad_body = False
        if raw_text.strip() == "":
            # Upstream dropped the connection / returned an empty body.
            # This is a transient relay failure (seen as {"_raw": ""}),
            # NOT a real key error -> retry it.
            bad_body = True
            data = {"_raw": "",
                    "error": {"type": "api_error",
                              "message": "upstream returned an empty response body"}}
        else:
            try:
                data = r.json()
            except Exception:
                # Unparseable body: preserve RAW so the reason is visible,
                # and treat as transient -> retry.
                bad_body = True
                data = {"_raw": raw_text,
                        "error": {"type": "api_error",
                                  "message": raw_text[:500]}}
            else:
                # Parsed OK but a 2xx with no usable content is also a
                # transient relay glitch -> retry.
                if status < 400 and isinstance(data, dict) \
                        and not data.get("content") and not data.get("error"):
                    bad_body = True

        if bad_body:
            log_event({"attempt": attempt, "status": status,
                       "note": "empty/invalid upstream body -> retry"})
            if attempt < max_attempts:
                continue
            # last attempt: surface as a retryable-style server error
            if status < 400:
                status = 502
            return status, data, last_exc

        if status < 400 or status not in retryable:
            return status, data, last_exc
        if status in hard_server_errors and attempt >= 2:
            return status, data, last_exc
        # retryable status -> loop again
    return status, data, last_exc


async def call_upstream(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    url = CONFIG["upstream_base_url"].rstrip("/") + "/messages"
    # Build the ordered, de-duplicated key list: primary first, then fallbacks.
    keys: List[str] = []
    primary = CONFIG.get("api_key", "") or ""
    if primary:
        keys.append(primary)
    for k in (CONFIG.get("fallback_api_keys") or []):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    if not keys:
        keys = [""]  # preserve old behaviour (send empty key, let upstream 401)

    timeout = httpx.Timeout(float(CONFIG.get("upstream_timeout_s", 300)))
    # A genuine key rejection -> try the next key in the list.
    auth_reject = {401, 403}
    status = 0
    data: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for idx, key in enumerate(keys):
            if idx > 0:
                log_event({"fallback_key": idx, "after_status": status,
                           "note": "primary key rejected -> trying fallback key"})
            status, data, last_exc = await _try_one_key(client, url, key, payload)
            # Success or a real (non-auth) error -> stop, this is the answer.
            if status < 400 or status not in auth_reject:
                if last_exc is not None and status == 0:
                    # all attempts for this key were network failures;
                    # try the next key too (maybe a different endpoint path works)
                    if idx < len(keys) - 1:
                        continue
                    raise last_exc
                if status < 400:
                    # Record which key actually worked (masked, never full).
                    log_event({"key_used": "primary" if idx == 0 else f"fallback#{idx}",
                               "key_masked": masked_key(key), "status": status,
                               "note": "upstream OK with this key"})
                return status, data
            # auth rejection -> record the dead key, then loop to next one
            log_event({"key_rejected": "primary" if idx == 0 else f"fallback#{idx}",
                       "key_masked": masked_key(key), "status": status,
                       "note": "key rejected by upstream (401/403)"})
    if last_exc is not None and status == 0:
        raise last_exc
    return status, data


@app.post("/v1/messages")
async def v1_messages(request: Request):
    feats = CONFIG["features"]
    try:
        client_req = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")

    if not isinstance(client_req, dict) or "messages" not in client_req:
        return anthropic_error(400, "invalid_request_error", "Missing required field: messages.")

    wants_stream = bool(client_req.get("stream"))
    payload = build_upstream_payload(client_req, feats)

    # Input guard: estimate what the upstream will be billed for and refuse
    # oversized payloads BEFORE burning (potentially dollars of) tokens.
    est_in = estimate_payload_tokens(payload)
    limit = int(feats.get("max_input_tokens") or 0)
    if limit > 0 and est_in > limit:
        log_event({"model": payload.get("model"), "stream": wants_stream,
                   "rejected": "prompt_too_long", "sent_tokens": est_in,
                   "limit": limit})
        # Mirror the real Anthropic "prompt is too long" 400 so agent clients
        # recognize it and auto-compact the conversation.
        return anthropic_error(
            400, "invalid_request_error",
            f"prompt is too long: {est_in} tokens > {limit} maximum "
            f"(jdw-proxy input guard — compact or summarize the conversation)")

    t0 = time.time()

    # STREAMING PATH: open the SSE stream immediately and run the upstream call
    # concurrently with keepalive pings, so the client never sees dead air.
    if wants_stream:
        return StreamingResponse(
            stream_with_keepalive(payload, client_req, feats, t0,
                                  sent_tokens=est_in),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # NON-STREAMING PATH: wait for the full upstream reply, then return JSON.
    try:
        status, upstream = await call_upstream(payload)
    except Exception as e:
        log_event({"model": payload.get("model"), "stream": wants_stream,
                   "error": f"upstream connection: {e}"})
        return anthropic_error(502, "api_error", f"Upstream connection failed: {e}")

    if status >= 400:
        # Wrap upstream errors in Anthropic shape, surfacing the RAW reason so
        # the real upstream cause is visible (not a vague "invalid key").
        msg = ""
        if isinstance(upstream, dict):
            msg = (upstream.get("error", {}) or {}).get("message") \
                  or upstream.get("message") \
                  or upstream.get("_raw") \
                  or json.dumps(upstream, ensure_ascii=False)[:500]
        else:
            msg = str(upstream)[:500]
        log_event({"model": payload.get("model"), "stream": wants_stream,
                   "status": status, "error": (msg or "")[:200],
                   "attempts": 3, "sent_tokens": est_in})
        etype = "invalid_request_error" if status == 400 else "api_error"
        return anthropic_error(status, etype,
                               f"Upstream {status} (after retries): {msg}")

    message = process_upstream_message(upstream, client_req, feats)
    meta = message.pop("_meta", {})
    dt = round(time.time() - t0, 2)

    log_event({
        "model": message.get("model"),
        "stream": wants_stream,
        "status": status,
        "latency_s": dt,
        "client_tools": meta.get("client_tools", 0),
        "tool_use": meta.get("has_tool_use", False),
        "had_thinking": meta.get("had_thinking", False),
        "stop_reason": message.get("stop_reason"),
        "usage": message.get("usage"),
        "sent_tokens": est_in,
    })

    # (The streaming path returned earlier via stream_with_keepalive.)
    return JSONResponse(content=message)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        req = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Body is not valid JSON.")
    total = 0
    total += approx_tokens(extract_system_text(req.get("system")))
    for m in req.get("messages", []):
        total += approx_tokens(flatten_content_blocks(m.get("content"), m.get("role", "user")))
    for t in req.get("tools") or []:
        total += approx_tokens(json.dumps(t, ensure_ascii=False))
    return JSONResponse(content={"input_tokens": total})


@app.get("/v1/models")
async def models():
    url = CONFIG["upstream_base_url"].rstrip("/") + "/models"
    headers = {"x-api-key": CONFIG["api_key"], "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers)
            return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        # fallback to configured model
        return JSONResponse(content={
            "data": [{"type": "model", "id": CONFIG["model"],
                      "display_name": CONFIG["model"]}]
        })


# --------------------------------------------------------------------------- #
# Admin / dashboard API
# --------------------------------------------------------------------------- #

def masked_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 10:
        return "*" * len(k)
    return k[:7] + "..." + k[-4:]


@app.get("/admin/config")
async def get_admin_config():
    c = json.loads(json.dumps(CONFIG))
    c["api_key_masked"] = masked_key(c.get("api_key", ""))
    c.pop("api_key", None)
    # expose fallback keys masked, so they can be shown but not leaked in full
    c["fallback_api_keys_masked"] = [masked_key(k) for k in (c.get("fallback_api_keys") or [])]
    c.pop("fallback_api_keys", None)
    return JSONResponse(content=c)


@app.post("/admin/config")
async def set_admin_config(request: Request):
    global CONFIG
    body = await request.json()
    with _config_lock:
        feats = body.get("features", {})
        if isinstance(feats, dict):
            CONFIG["features"].update(feats)
        for k in ("upstream_base_url", "model", "ui_lang", "upstream_timeout_s"):
            if k in body and body[k] is not None:
                CONFIG[k] = body[k]
        # only overwrite api key if a non-masked value is provided
        newkey = body.get("api_key")
        if newkey and "..." not in newkey:
            CONFIG["api_key"] = newkey
        # fallback keys: replace the whole list only if a clean (non-masked)
        # list is sent. Masked entries contain "..." -> means "keep current".
        fb = body.get("fallback_api_keys")
        if isinstance(fb, list) and not any("..." in str(k) for k in fb):
            CONFIG["fallback_api_keys"] = [str(k).strip() for k in fb if str(k).strip()]
        save_config(CONFIG)
    return await get_admin_config()


@app.get("/admin/logs")
async def get_logs():
    with _log_lock:
        return JSONResponse(content={"logs": list(LOG)})


@app.post("/admin/logs/clear")
async def clear_logs():
    with _log_lock:
        LOG.clear()
    return JSONResponse(content={"ok": True})


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# Dashboard HTML is defined in a separate constant appended below.
from dashboard_html import DASHBOARD_HTML  # noqa: E402


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8181))
    print(f"JDW Fix Proxy listening on http://{host}:{port}")
    print(f"  Anthropic endpoint : http://{host}:{port}/v1")
    print(f"  Dashboard          : http://{host}:{port}/")
    print(f"  Upstream           : {CONFIG['upstream_base_url']}")
    uvicorn.run(app, host=host, port=port, log_level="info")
