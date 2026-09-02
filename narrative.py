#!/usr/bin/env python3
"""
narrative.py — Grok-powered narrative intelligence.

Two jobs:
1. scan_for_candidates()  — find NEW viral, off-crypto, organic, cross-community
   narratives (same job the original bot did).
2. recheck_narrative(term) — for a narrative we ALREADY have a position in,
   ask Grok whether it's still accelerating, peaking, declining, or dead.
   This is the "smart exit" signal that price/on-chain data alone can't see:
   a narrative can be declining on X well before the token price fully
   reflects it (or after — either way it's another independent data point).

Both funnel through xAI's /v1/chat/completions with `search_parameters`
turned on so Grok actually looks at live X data rather than answering from
training memory.
"""

import os
import sys
import re
import json
import datetime as dt
from typing import List, Dict, Any, Optional

import requests

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_URL = "https://api.x.ai/v1/chat/completions"
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"  # preferred: supports live web search
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4-latest")


def _call_grok(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """Ask Grok, returning parsed JSON. Prefers the Responses API (with the
    web_search tool) so Grok can actually look at live X data; xAI deprecated
    `search_parameters` on the old /v1/chat/completions endpoint (410 Gone),
    so the legacy path no longer gets live search. Falls back to the legacy
    chat-completions surface (no search) if the Responses API fails, so the
    bot keeps running degraded rather than crashing."""
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY env var not set. Get one at https://console.x.ai")

    # --- Preferred: Responses API with web_search tool ---
    resp = _call_grok_responses(system_prompt, user_prompt)
    if resp is not None and "json" in resp:
        return resp["json"]  # already parsed

    # --- Fallback: legacy chat completions, no search_parameters ---
    return _call_grok_legacy_no_search(system_prompt, user_prompt)


def _call_grok_responses(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """POST /v1/responses with a web_search tool so Grok can browse live X.
    Returns {'json': <parsed>} on success, or None (caller falls back)."""
    payload = {
        "model": GROK_MODEL,
        "instructions": system_prompt,
        "input": user_prompt,
        "tools": [{"type": "web_search"}],
        "store": False,
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(XAI_RESPONSES_URL, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[narrative] Responses API failed ({e}), falling back to chat-completions.",
              file=sys.stderr)
        return None

    # Responses API output shape: data["output"] = [{"type":"message","content":[...]}]
    content_text = ""
    try:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    content_text += (c.get("text") or "")
    except Exception:
        pass
    if not content_text.strip():
        return None
    parsed = _parse_json_content(content_text)
    if parsed is None:
        return None
    return {"json": parsed}


def _call_grok_legacy_no_search(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """Legacy /v1/chat/completions WITHOUT search_parameters (which xAI
    removed — sending it returns 410 Gone). No live search here, so results
    come from Grok's knowledge; keeps the bot from crashing on the narrative
    scan."""
    payload = {
        "model": GROK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(XAI_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[narrative] Grok call failed: {e}", file=sys.stderr)
        return None
    return _parse_json_content(content)


def _parse_json_content(content: str) -> Optional[Dict[str, Any]]:
    """Best-effort: strip code fences and parse JSON from Grok's text reply."""
    if content.startswith("```"):
        content = content.strip("`")
        content = content.split("\n", 1)[-1] if "\n" in content else content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # allow a leading/trailing fence or stray prose outside the JSON
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        print(f"[narrative] Grok returned non-JSON: {content[:300]}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# 1. NEW CANDIDATE SCAN
# ----------------------------------------------------------------------------

SCAN_SYSTEM_PROMPT = """You are a trend-scout with live access to X (Twitter).
Your job is NOT to look at crypto/CT accounts. Look at NORMIE / mainstream
accounts: sports, celebrities, TV, gaming, comedy, news, weird viral videos,
reaction images, catchphrases, animals, politics-adjacent memes, etc.

Find things that started or accelerated sharply in the LAST 1-24 HOURS and
answer only with things you have reasonable evidence are actually trending
right now (real recent post volume), not evergreen memes.

For each candidate return:
- term: the exact short phrase/name/slang people would use as a token ticker
  or name (how it's actually being typed/said, not your paraphrase)
- description: one sentence, what it is / where it came from
- category: meme | slang | character | news_event | visual | phrase | person
- est_posts_1_24h: rough order of magnitude estimate ("hundreds", "thousands",
  "tens of thousands")
- cross_community: true/false — is it jumping between different unrelated
  communities (not just one fandom)?
- organic: true/false — do the posts still read as organic reactions, or are
  a meaningful share of replies/quotes already dropping a CA / "$TICKER" /
  pump.fun links?
- crypto_notice_level: none | early_whispers | actively_being_tokenized

Return STRICT JSON only, no prose, no markdown fences, shape:
{"candidates": [ {...}, {...} ] }

Only include candidates where cross_community is true AND organic is true
AND crypto_notice_level is "none" or "early_whispers". Max 12 candidates.
If you genuinely find nothing that qualifies, return {"candidates": []}.
"""


def scan_for_candidates() -> List[Dict[str, Any]]:
    user_prompt = (
        f"Current UTC time: {dt.datetime.utcnow().isoformat()}Z. "
        "Scan X now and give me today's candidates per the rules above."
    )
    result = _call_grok(SCAN_SYSTEM_PROMPT, user_prompt)
    if not result:
        return []
    return result.get("candidates", [])


# ----------------------------------------------------------------------------
# 2. NARRATIVE HEALTH RECHECK (for open positions)
# ----------------------------------------------------------------------------

RECHECK_SYSTEM_PROMPT = """You are a trend-scout with live access to X (Twitter).
You previously flagged a narrative/meme/phrase as newly viral. Now check its
CURRENT status by looking at recent X activity (last few hours).

Classify status as exactly one of:
- "accelerating" — post volume and reach still clearly growing
- "peaking" — still high volume but growth has flattened, saturation signs
- "declining" — post volume/engagement is clearly dropping off from its peak
- "dead" — barely any new organic activity, conversation has moved on

Also give:
- score: 0-10, overall current narrative strength (10 = still exploding)
- crypto_notice_level: none | early_whispers | actively_being_tokenized | saturated
- note: one short sentence explaining your read

Return STRICT JSON only, no prose, no markdown fences:
{"status": "...", "score": 0, "crypto_notice_level": "...", "note": "..."}
"""


def recheck_narrative(term: str, description: str = "") -> Optional[Dict[str, Any]]:
    user_prompt = (
        f"Current UTC time: {dt.datetime.utcnow().isoformat()}Z. "
        f"Narrative/term to check: \"{term}\". "
        f"Original context when first flagged: {description or '(none provided)'}. "
        "What is its status on X right now?"
    )
    return _call_grok(RECHECK_SYSTEM_PROMPT, user_prompt)
