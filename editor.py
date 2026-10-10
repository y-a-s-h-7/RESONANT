"""Resonant — AI editor: Ollama client + pure guards (W1.1, W1.2, W1.5).

The guards are PURE functions so tests need no network and no Ollama:

    should_use_llm(raw, mode, min_words)   smart-mode gate (skip the LLM when useless)
    validate_rewrite(raw, out)             reject chatbot output / invention / growth
    chunk_transcript(text, max_words)      sentence-boundary splitting for long input
    clean(raw, ...)                        the pipeline: gate -> LLM -> guards -> tidy

Every failure path degrades to the raw transcript ("degrade, don't lose", §2).
All thresholds are named constants (brief W1.2).
"""
import re

import requests

from config import OLLAMA_CHAT_URL, REFUSAL_MARKERS, SYSTEM_PROMPT

# ----------------------------------------------------------------- constants
INVENTED_MAX = 0.25          # max share of output words absent from the input (>=6 words)
GROWTH_FACTOR = 1.6          # output may not exceed factor * input words + slack
GROWTH_SLACK = 6
NUM_CTX = 4096               # explicit context window (W1.1)
DONE_REASON_LENGTH = "length"

PREAMBLE_RE = re.compile(
    r"^\s*(here('s| is)?|sure|certainly|of course|corrected text|cleaned text|"
    r"the corrected|the cleaned)\b.*", re.IGNORECASE)

CORRECTION_CUES = re.compile(
    r"\b(scratch that|strike that|delete that|never mind|no wait|actually|"
    r"i mean|instead|cancel|remove|replace|change it to|make it|no,)\b", re.IGNORECASE)

# Filler cues. "um"/"uh" are unambiguous; "like" is only a filler in
# filler-shaped contexts ("um like", ", like,", "was like thinking"). As a
# verb or a comparison ("I like pizza", "runs like the wind") it is real speech
# and must never on its own trigger a rewrite.
FILLER_CUES = re.compile(r"\b(?:um|uh|er|you know)\b", re.IGNORECASE)
FILLER_LIKE_RE = re.compile(
    r"\b(?:um|uh|er|you know)\s*,?\s*like\b"              # "um like", "uh, like"
    r"|\b(?:was|were|is|are|am|be)\s+like\s+\w+ing\b"      # "was like thinking"
    r"|,\s*like\s*,",                                       # "it was, like, good"
    re.IGNORECASE,
)

# List cues only count as ENUMERATION. Bare "first"/"second"/"next" are far
# too common in ordinary speech ("wait a second", "next week", "the first
# time") to imply the speaker is dictating a list.
LIST_CUES = re.compile(
    r"\bfirst\b[\s\S]{0,120}?\bsecond\b"                  # "first ... second"
    r"|\b(?:first|second|third|fourth|fifth)\s+"
    r"(?:item|point|step|thing|bullet)s?\b"               # "first item"
    r"|\bitems?\s+(?:one|two|three|four|five|\d+)\b"        # "item one"
    r"|\bbullets?\b"
    r"|\b(?:shopping|todo|to-do|task|check|grocery|packing)\s+list\b"
    r"|\b(?:create|make|write|add|start|build)\s+(?:me\s+)?(?:a\s+|the\s+)?list\b"
    r"|\blist\s*:",                                        # "list:"
    re.IGNORECASE,
)
STUTTER_RE = re.compile(r"\b(\w{2,})( \1){1,}\b", re.IGNORECASE)

TAG_RE = re.compile(r"</?transcript>", re.IGNORECASE)


# ------------------------------------------------------------ pure helpers
def _norm_tokens(text):
    """Normalise numbers/ordinals/times so 'three pm' <-> '3 PM' compare equal."""
    t = text.lower()
    t = re.sub(r"(\d+)(am|pm)", r"\1 \2", t)                 # 3pm -> 3 pm
    words = t.split()
    num_words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
        "ten": "10", "eleven": "11", "twelve": "12",
    }
    ord_words = {"first": "1st", "second": "2nd", "third": "3rd"}
    out = []
    for w in words:
        w = ord_words.get(w, w)
        w = num_words.get(w, w)
        out.append(w)
    t = " ".join(out)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", r"\1 \2", t)         # 3:00 -> 3 00
    t = re.sub(r"[^a-z0-9 ]", " ", t)                          # punctuation gone
    return t.split()


def word_count(text):
    return len(text.split())


def strip_tags_and_quotes(text):
    """Strip stray <transcript> tags and ONE enclosing quote layer."""
    t = TAG_RE.sub("", text)
    t = t.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t


# ------------------------------------------------------- decision counters
class DecisionCounts:
    """In-memory smart-gate counters — transcript text is NEVER stored.

    A skip is a smart-mode utterance the cue gate kept away from the LLM; an
    llm_call is one the LLM actually processed. Other modes ("always"/"off")
    are not counted, so the pair describes smart mode alone. Resets per
    process: `resonant_cli.py doctor` runs in its own process and therefore
    reports the counters of that run.
    """

    def __init__(self):
        self.smart_skips = 0
        self.smart_llm_calls = 0

    def reset(self):
        self.smart_skips = 0
        self.smart_llm_calls = 0

    def snapshot(self):
        return {"smart_skips": self.smart_skips,
                "smart_llm_calls": self.smart_llm_calls}


DECISION_COUNTS = DecisionCounts()


def decision_counts():
    """Snapshot for engine state/diagnostics. No transcript content."""
    return DECISION_COUNTS.snapshot()


def should_use_llm(raw, mode="smart", min_words=40):
    """Smart gate: only spend LLM time when the utterance can benefit.

    True if correction/list/filler cues, stutter repeats, or length >= min_words.
    'always' -> True, 'off' -> False.
    """
    if mode == "off":
        return False
    if mode == "always":
        return True
    if not raw or not raw.strip():
        return False
    return bool(CORRECTION_CUES.search(raw) or LIST_CUES.search(raw)
                or FILLER_CUES.search(raw) or FILLER_LIKE_RE.search(raw)
                or STUTTER_RE.search(raw)
                or word_count(raw) >= min_words)


def validate_rewrite(raw, out):
    """Return (ok, reason). Rejects chatbot behaviour without rejecting the
    SPEAKER's own words (the W1.2 false-positive fix: markers count only if
    the speaker did not say them)."""
    if out is None:
        return False, "empty"
    out = out.strip()
    if not out:
        return False, "empty"
    low_in, low_out = raw.lower(), out.lower()

    # refusal markers — but only if the speaker didn't say them
    for m in REFUSAL_MARKERS:
        if m in low_out and m not in low_in:
            return False, "refusal"

    # chatbot preamble
    if PREAMBLE_RE.match(low_out) and not PREAMBLE_RE.match(low_in):
        return False, "preamble"

    r_tokens = _norm_tokens(raw)
    o_tokens = _norm_tokens(out)
    if len(o_tokens) >= 6:
        rset = set(r_tokens)
        invented = sum(w not in rset for w in o_tokens) / len(o_tokens)
        if invented > INVENTED_MAX:
            return False, "invented"
        if len(o_tokens) > GROWTH_FACTOR * len(r_tokens) + GROWTH_SLACK:
            return False, "grew"
    return True, "ok"


def chunk_transcript(text, max_words=150):
    """Split long text at sentence boundaries into chunks of <= max_words."""
    if word_count(text) <= max_words:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, cur, cur_n = [], [], 0
    for s in sentences:
        n = word_count(s)
        if cur and cur_n + n > max_words:
            chunks.append(" ".join(cur))
            cur, cur_n = [], 0
        if n > max_words:                       # one monster sentence: hard-split
            words = s.split()
            for i in range(0, len(words), max_words):
                part = " ".join(words[i:i + max_words])
                if cur:
                    chunks.append(" ".join(cur))
                    cur, cur_n = [], 0
                chunks.append(part)
            continue
        cur.append(s)
        cur_n += n
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def tidy(raw):
    """Deterministic fallback/smart-skip cleanup: whitespace + first-letter cap."""
    t = re.sub(r"\s+", " ", raw).strip()
    return t[:1].upper() + t[1:] if t else t


# ------------------------------------------------------------- LLM client
def _ollama_chat(model, raw, timeout_s, keep_alive, ollama_url=OLLAMA_CHAT_URL,
                 session=None):
    """Low-level call; returns (text, done_reason). Raises on transport errors.

    `session` is an optional requests.Session (W4.7: connection reuse)."""
    client = session or requests
    resp = client.post(ollama_url, json={
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"<transcript>{raw}</transcript>"},
        ],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": _num_predict_for(raw),
                    "num_ctx": NUM_CTX},
    }, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    msg = (data.get("message") or {}).get("content", "")
    return msg.strip(), data.get("done_reason", "")


def _num_predict_for(raw):
    """Scale output budget with input (W1.1): short input can never need 200+
    tokens; long input must never be silently cut at a fixed cap."""
    return max(256, int(2.5 * word_count(raw)) + 64)


def _clean_one(chunk, model, timeout_s, keep_alive, llm_fn, ollama_url, session=None):
    """LLM + guards for ONE chunk. Returns (text, status); text is always usable."""
    try:
        if llm_fn is _ollama_chat:
            out, done_reason = llm_fn(model, chunk, timeout_s, keep_alive,
                                      ollama_url, session)
        else:
            out, done_reason = llm_fn(model, chunk, timeout_s, keep_alive, ollama_url)
    except Exception:
        return tidy(chunk), "raw:llm-unavailable"
    if done_reason == DONE_REASON_LENGTH:
        return tidy(chunk), "raw:truncated"
    out = strip_tags_and_quotes(out)
    ok, reason = validate_rewrite(chunk, out)
    if not ok:
        return tidy(chunk), f"raw:guard-{reason}"
    return out, "cleaned"


def clean(raw, model="llama3.2", mode="smart", min_words=40,
          timeout_s=10, keep_alive="30m", llm_fn=_ollama_chat,
          ollama_url=OLLAMA_CHAT_URL, session=None):
    """Full pipeline. Returns (final_text, status) where status is one of:
    'cleaned' | 'raw:skipped-llm' | 'raw:llm-unavailable' | 'raw:truncated'
    | 'raw:guard-<reason>' | 'raw:mixed' | 'dropped:empty'.

    Never raises, never returns unusable text (§2 degrade-don't-lose).
    """
    raw = (raw or "").strip()
    if not raw:
        return "", "dropped:empty"

    use_llm = should_use_llm(raw, mode, min_words)
    if mode == "smart":
        if use_llm:
            DECISION_COUNTS.smart_llm_calls += 1
        else:
            DECISION_COUNTS.smart_skips += 1
    if not use_llm:
        return tidy(raw), "raw:skipped-llm"

    chunks = chunk_transcript(raw)
    if len(chunks) == 1:
        return _clean_one(chunks[0], model, timeout_s, keep_alive, llm_fn,
                          ollama_url, session)

    # W1.5: chunks are cleaned independently; a failing chunk keeps its raw
    # text. Corrections spanning chunk boundaries may be missed (documented).
    parts, status = [], "cleaned"
    for chunk in chunks:
        text, st = _clean_one(chunk, model, timeout_s, keep_alive, llm_fn,
                              ollama_url, session)
        parts.append(text)
        if st != "cleaned" and status == "cleaned":
            status = st
        elif st != "cleaned" and status != st:
            status = "raw:mixed"
    return " ".join(parts), status
