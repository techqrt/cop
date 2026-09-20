"""Deterministic text chunking for Sarvam's translation input limit
(docs/phase3-sarvam-translation.md §Chunking strategy). A pure, independently
testable function with no Sarvam SDK dependency - this module never imports
`sarvamai` or makes a network call.

`MAX_TRANSLATION_INPUT_CHARS` is the one place this limit is defined
(sarvam-translate:v1's documented 2,000-character input ceiling) - nothing else in
the codebase hardcodes it.
"""

import re

MAX_TRANSLATION_INPUT_CHARS = 2000

# Sentence boundary: a Latin sentence-ending mark (. ! ?) or a Devanagari danda
# (। U+0964, ॥ U+0965 - common in Hindi/Marathi/etc. crash-scene speech) followed by
# whitespace. Deliberately a simple, deterministic regex rather than a full NLP
# sentence tokenizer, which would be a disproportionate dependency for this step.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?।॥])(\s+)')

# Paragraph boundary: one or more newlines, kept as their own unit (via the
# capturing group) so re-joining every piece with '' always reconstructs the
# original text exactly - no separator is ever dropped.
_PARAGRAPH_SPLIT_RE = re.compile(r'(\n+)')


def chunk_text(text: str, max_chars: int = MAX_TRANSLATION_INPUT_CHARS) -> list[str]:
    """Splits `text` into chunks of at most `max_chars` characters each, preferring
    paragraph and then sentence boundaries, falling back to a whitespace (or, only
    for a single unbroken run longer than `max_chars`, a hard) cut. Invariant that
    every test in csc_apps/processing/tests.py verifies:
    ``''.join(chunk_text(text)) == text`` for any input - no character is ever lost,
    duplicated, or reordered, and chunks are returned in source order.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units = _split_into_units(text)

    chunks: list[str] = []
    current = ''
    for unit in units:
        if len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ''
            chunks.extend(_split_long_unit(unit, max_chars))
            continue
        if current and len(current) + len(unit) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current += unit
    if current:
        chunks.append(current)
    return chunks


def _split_into_units(text: str) -> list[str]:
    """Paragraphs (and their separating newlines), each further split into
    sentences - the ordered list of pieces that, concatenated, reconstruct `text`
    exactly."""
    units: list[str] = []
    for part in _PARAGRAPH_SPLIT_RE.split(text):
        if not part:
            continue
        if _PARAGRAPH_SPLIT_RE.fullmatch(part):
            units.append(part)  # a run of newlines - an atomic unit on its own
        else:
            units.extend(_split_into_sentences(part))
    return units


def _split_into_sentences(paragraph: str) -> list[str]:
    """Splits one paragraph into sentences, each carrying its own trailing
    whitespace so joining them with '' reconstructs the paragraph exactly."""
    pieces = _SENTENCE_SPLIT_RE.split(paragraph)
    sentences: list[str] = []
    i = 0
    while i < len(pieces):
        piece = pieces[i]
        if i + 1 < len(pieces):
            piece += pieces[i + 1]
            i += 2
        else:
            i += 1
        if piece:
            sentences.append(piece)
    return sentences


def _split_long_unit(unit: str, max_chars: int) -> list[str]:
    """Splits a single sentence/paragraph too long to fit in one chunk. Prefers the
    last whitespace boundary within the limit; falls back to a hard character cut
    only when no whitespace exists in that window (e.g. one pathologically long
    "word") - every character is still preserved, just not at a word boundary."""
    chunks: list[str] = []
    remaining = unit
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind(' ')
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
