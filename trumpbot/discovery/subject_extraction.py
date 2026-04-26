"""Subject extraction from Kalshi market titles.

Markets in the KXTRUMPMEET series carry titles of the form::

    Donald Trump and Vladimir Putin meet before May 1, 2026?
    Donald Trump and María Corina Machado meet before May 1, 2026?

This module parses the full name out of the title and derives a
URL-safe / database-key-safe ``subject_key`` (lowercased, ASCII-only,
non-alphabetic characters stripped).

The discovery service uses the result to populate the ``subjects``
table. Callers are expected to handle ``ExtractionFailure`` (the
parser is conservative and refuses to guess on titles that don't fit
the documented pattern — those titles get logged for human review).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Anchored loosely so we tolerate trailing punctuation, and the date
# format Kalshi uses ("May 1, 2026?") doesn't trip us up.
TITLE_PATTERN = re.compile(
    r"Donald Trump and (?P<full_name>.+?) meet before",
    re.IGNORECASE,
)


class ExtractionFailure(ValueError):
    """Raised when a title doesn't conform to the expected pattern."""


@dataclass(frozen=True)
class ExtractedSubject:
    """Extracted subject data — what to write into ``markets`` + ``subjects``."""

    full_name: str  # exactly as the title contains it
    subject_key: str  # ASCII-lower, non-alpha stripped
    last_name: str  # last whitespace-separated token of full_name


def extract_subject(title: str) -> ExtractedSubject:
    """Parse a market title into an :class:`ExtractedSubject`.

    Raises :class:`ExtractionFailure` if the title doesn't match the
    documented Kalshi format. The discovery service catches the
    failure, logs a ``subject_extraction_failed`` system event with
    the raw title, and skips that market.
    """
    if not title:
        raise ExtractionFailure("title is empty")
    match = TITLE_PATTERN.search(title)
    if match is None:
        raise ExtractionFailure(f"title does not match expected pattern: {title!r}")
    full_name = match.group("full_name").strip()
    if not full_name:
        raise ExtractionFailure(f"empty subject in title: {title!r}")
    subject_key = make_subject_key(full_name)
    if not subject_key:
        raise ExtractionFailure(
            f"could not derive subject_key from full_name {full_name!r} (title {title!r})"
        )
    last_name = full_name.split()[-1]
    return ExtractedSubject(full_name=full_name, subject_key=subject_key, last_name=last_name)


def make_subject_key(full_name: str) -> str:
    """Normalize a full name to an ASCII, lowercase, alphabetic-only key.

    Examples:
        >>> make_subject_key("Vladimir Putin")
        'vladimirputin'
        >>> make_subject_key("María Corina Machado")
        'mariacorinamachado'
        >>> make_subject_key("Kim Jong-un")
        'kimjongun'
    """
    decomposed = unicodedata.normalize("NFKD", full_name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    # Keep ASCII letters only.
    return re.sub(r"[^a-z]", "", lowered)
