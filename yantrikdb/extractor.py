"""v0.5 Wave B cheap-tier fact extractor.

Pulls high-precision factoid candidates out of conversation turns
using regex + light heuristic NER. Zero new dependencies — runs in
<1ms per turn so it can sit on the hot path of `sync_turn` without
adding latency.

Design rules (from `docs/v0.5-design.md`):

1. **High precision, low recall** — false positives pollute the
   substrate; missing one fact is recoverable on the next turn.
2. **Only extract from USER text** OR an assistant assertion the user
   explicitly confirmed. Never extract from bare LLM output (HANDOFF
   §10.1).
3. **Candidates land tagged**: ``source="extracted"``,
   ``certainty<=0.4``, with ``metadata.extractor`` naming the pattern
   that fired so the effectiveness ledger can tune or disable noisy
   ones.
4. **Each pattern targets a single named shape** — never a catch-all
   "any noun phrase" rule. The shapes here were picked to maximize
   precision on conversational English.

The extractor returns a list of ``ExtractionCandidate`` records. The
caller (provider ``sync_turn``) decides what to do with them; this
module never writes to the substrate directly.

Patterns shipped in v1:

================  ====================================  ===========
pattern key       trigger                                example
================  ====================================  ===========
``preference``    "I prefer X" / "I like X better"      "I prefer tabs over spaces"
``identity``      "my name is X" / "call me X"          "my name is Pranab"
``location``      "I (live|work) in X"                  "I work in Walmart"
``possession``    "my (NOUN) is (VALUE)"                "my favorite editor is Neovim"
``confirm``       "yes/right/exactly" + ref-assertion    (handled by caller, see below)
``url``           bare URL or markdown link              "https://example.com"
``email``         bare email                             "alice@example.com"
================  ====================================  ===========

Out of scope (deferred to v0.5.1 ``embedding`` and ``llm`` tiers):

* Open-domain entity extraction (no spaCy / no LLM dep in cheap tier).
* Implicit-fact inference ("Bob walked to school" → "Bob can walk").
* Coreference resolution across turns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractionCandidate:
    """One factoid extracted from a turn.

    ``text`` is the substrate-shaped statement the caller should
    ``remember()`` (rewritten in third-person where applicable so the
    canonical form is searchable across turns). ``pattern`` names the
    extractor that fired so the effectiveness ledger can attribute
    promote/forget outcomes.
    """

    text: str
    pattern: str
    span: tuple[int, int]  # char range in source text
    domain: str = "general"
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pattern set — keep tight; precision over coverage.
# ---------------------------------------------------------------------------

# "I prefer X" / "I like X (better|more)" / "I'd rather X"
# Captures the object of preference. Stops at sentence boundaries.
_PREFERENCE_RE = re.compile(
    r"\bI\s+(?:prefer|like)\s+(?P<obj>[^.!?\n,;]{2,80}?)(?:\s+(?:better|more|over\s+[^.!?\n,;]{2,80}))?[.!?\n,;]",
    re.IGNORECASE,
)

# "my (favorite|preferred) X is Y" / "my X is Y"
# Filters to attribute-like X (favorite, preferred, name, email, etc.) so
# we don't capture "my dog is good" style sentences.
_POSSESSION_RE = re.compile(
    r"\bmy\s+(?P<attr>(?:favorite\s+|preferred\s+)?(?:name|email|phone|editor|os|shell|language|framework|tool|laptop|car|address|company|employer|role|title|stack|database|browser|distro))\s+(?:is|=)\s+(?P<val>[^.!?\n,;]{1,100})[.!?\n,;]",
    re.IGNORECASE,
)

# "my name is X" / "call me X" / "I am X" → identity
_IDENTITY_RE = re.compile(
    r"\b(?:my\s+name\s+is|call\s+me|I\s+am|I'm)\s+(?P<name>[A-Z][a-zA-Z'-]{1,30}(?:\s+[A-Z][a-zA-Z'-]{1,30})?)\b",
)

# "I work at X" / "I live in X" / "I'm from X" — geographic / employer
_LOCATION_RE = re.compile(
    r"\bI\s+(?P<verb>work\s+at|live\s+in|am\s+from|'m\s+from|am\s+at|'m\s+at)\s+(?P<place>[A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,3})\b",
)

# bare URL
_URL_RE = re.compile(
    r"\bhttps?://[^\s)>]+", re.IGNORECASE,
)

# bare email
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",
)

# Phrases the user uses to confirm a prior assistant assertion — used by
# the caller to decide whether to extract from the PRIOR assistant turn.
# We match the WHOLE user message (stripped of punctuation) being a
# short confirmation, not just containing one of these — "yes the
# database is Postgres" should EXTRACT the second clause as user text
# directly (the user said it), not promote the assistant's prior turn.
_CONFIRM_RE = re.compile(
    r"^(?:yes|yep|yeah|right|correct|exactly|that's right|true|confirmed|yup)[.!\s]*$",
    re.IGNORECASE,
)


def is_user_confirmation(text: str) -> bool:
    """Return True iff the text is a bare confirmation phrase.

    Used by the provider to detect "user confirmed the prior assistant
    assertion" — at which point the caller can extract from the PRIOR
    assistant turn under the §10.1 carve-out. A confirmation that ALSO
    introduces new content ("yes, the database is Postgres") does NOT
    qualify; the new content is user text and gets extracted normally.
    """
    return bool(_CONFIRM_RE.match((text or "").strip()))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_candidates(text: str, *, speaker: str = "user") -> list[ExtractionCandidate]:
    """Run all enabled patterns over ``text``, return candidates.

    ``speaker`` flags whose voice the text is in. Caller decides
    whether to call this on assistant text (only when user-confirmed).
    """
    if not text or not isinstance(text, str):
        return []
    out: list[ExtractionCandidate] = []
    text = text.strip()
    if not text:
        return []

    # Trailing-period normalization — many patterns end with [.!?\n,;]
    # so add a sentinel to catch terminal-position matches.
    sentinel = text if text.endswith((".", "!", "?", "\n", ",", ";")) else text + "."

    for m in _PREFERENCE_RE.finditer(sentinel):
        obj = m.group("obj").strip()
        if not _is_clean_value(obj):
            continue
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)} prefers {obj}",
            pattern="preference",
            span=m.span(),
            domain="preference",
            metadata={"original": m.group(0).strip(), "speaker": speaker},
        ))

    for m in _POSSESSION_RE.finditer(sentinel):
        attr = m.group("attr").strip().lower()
        val = m.group("val").strip().rstrip(".,;:!?")
        if not _is_clean_value(val):
            continue
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)}'s {attr} is {val}",
            pattern="possession",
            span=m.span(),
            domain=_domain_for_attr(attr),
            metadata={"attribute": attr, "value": val, "speaker": speaker},
        ))

    for m in _IDENTITY_RE.finditer(sentinel):
        name = m.group("name").strip()
        if name.lower() in _STOPWORD_NAMES:
            continue
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)}'s name is {name}",
            pattern="identity",
            span=m.span(),
            domain="people",
            metadata={"name": name, "speaker": speaker},
        ))

    for m in _LOCATION_RE.finditer(sentinel):
        verb = m.group("verb").strip().lower()
        place = m.group("place").strip()
        relation = "works at" if "work" in verb else (
            "is from" if "from" in verb else "lives in" if "live" in verb else "is at"
        )
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)} {relation} {place}",
            pattern="location",
            span=m.span(),
            domain="people",
            metadata={"verb": verb, "place": place, "speaker": speaker},
        ))

    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)>")
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)} referenced URL {url}",
            pattern="url",
            span=m.span(),
            domain="reference",
            metadata={"url": url, "speaker": speaker},
        ))

    for m in _EMAIL_RE.finditer(text):
        email = m.group(0)
        out.append(ExtractionCandidate(
            text=f"{_subject(speaker)} referenced email {email}",
            pattern="email",
            span=m.span(),
            domain="reference",
            metadata={"email": email, "speaker": speaker},
        ))

    # De-duplicate by canonical text — two patterns can fire on the same
    # span (e.g. possession + identity on "my name is X") and produce the
    # same canonical form. Keep the first (pattern names define priority
    # in current declaration order).
    seen: set[str] = set()
    unique: list[ExtractionCandidate] = []
    for c in out:
        key = c.text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subject(speaker: str) -> str:
    """Rewrite first-person to third-person canonical subject.

    "I prefer X" → "user prefers X" is more retrievable across turns than
    leaving the "I" pronoun in. Use ``user`` as the substrate-canonical
    subject; identity-pattern extraction can later resolve it to a real
    name once captured. For confirmed assistant assertions the subject
    is the agent itself — keep "agent" so the source is visible at
    recall time.
    """
    return "agent" if speaker == "assistant" else "user"


def _domain_for_attr(attr: str) -> str:
    """Map possession-pattern attribute to a recall domain."""
    # Strip the "favorite "/"preferred " prefix before lookup — the regex
    # captures them but they don't change the domain.
    core = attr
    for prefix in ("favorite ", "preferred "):
        if core.startswith(prefix):
            core = core[len(prefix):]
            break
    if core in {"name", "email", "phone", "address"}:
        return "people"
    if core in {"company", "employer", "role", "title"}:
        return "work"
    if core in {"editor", "os", "shell", "language", "framework", "tool",
                "stack", "database", "browser", "distro"}:
        return "preference"
    if core in {"laptop", "car"}:
        return "preference"
    return "general"


def _is_clean_value(val: str) -> bool:
    """Filter out values that are too short, too long, or pure filler."""
    val = val.strip().strip("\"'")
    if len(val) < 2 or len(val) > 120:
        return False
    if val.lower() in _STOPWORD_VALUES:
        return False
    return any(c.isalnum() for c in val)


# Words that pretend to be names but aren't.
_STOPWORD_NAMES = frozenset({
    "Sorry", "Actually", "Just", "Wondering", "Curious", "Fine", "Okay",
    "Good", "Bad", "Better", "Worse", "Trying", "Working", "Looking",
    "Thinking", "Going", "Coming", "Done", "Sure",
})

# Values that would be noise as extracted facts.
_STOPWORD_VALUES = frozenset({
    "it", "that", "this", "them", "those", "these", "stuff", "things",
    "something", "anything", "nothing", "everything",
    "yes", "no", "maybe", "okay", "ok", "sure", "fine",
    "true", "false", "right", "wrong",
})
