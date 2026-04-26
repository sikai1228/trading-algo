"""Subject extraction: from a Kalshi market title/subtitle, identify the
person Trump might talk to/meet/mention.

The mapping (canonical subject -> alias list) is configurable: passed in
at construction time so we can iterate without code changes. The same
alias dictionary feeds the news matcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SubjectExtractor:
    """Identify a canonical subject in a market title or subtitle."""

    aliases: dict[str, list[str]]

    def __post_init__(self) -> None:
        # Sanity: at least one alias per subject.
        for subject, alias_list in self.aliases.items():
            if not alias_list:
                raise ValueError(f"subject {subject!r} has no aliases")

    def extract(self, *texts: str | None) -> str | None:
        """Return the first canonical subject whose alias appears in ``texts``."""
        haystack = " ".join((t or "").lower() for t in texts if t)
        if not haystack:
            return None
        # Iterate aliases longest-first so "kim jong un" wins over "un".
        candidates: list[tuple[int, str, str]] = []
        for subject, alias_list in self.aliases.items():
            for alias in alias_list:
                pattern = rf"\b{re.escape(alias.lower())}\b"
                if re.search(pattern, haystack):
                    candidates.append((-len(alias), subject, alias))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]


DEFAULT_SUBJECT_ALIASES: dict[str, list[str]] = {
    "putin": [
        "putin",
        "vladimir putin",
        "russian president",
        "president of russia",
        "kremlin chief",
    ],
    "xi": [
        "xi jinping",
        "xi",
        "chinese president",
        "president of china",
        "general secretary xi",
    ],
    "netanyahu": [
        "netanyahu",
        "bibi",
        "israeli prime minister",
        "prime minister of israel",
        "israeli pm",
    ],
    "zelensky": [
        "zelensky",
        "zelenskyy",
        "ukrainian president",
        "president of ukraine",
    ],
    "macron": [
        "macron",
        "emmanuel macron",
        "french president",
        "president of france",
    ],
    "merz": [
        "merz",
        "friedrich merz",
        "german chancellor",
        "chancellor of germany",
    ],
    "starmer": [
        "starmer",
        "keir starmer",
        "british prime minister",
        "prime minister of the united kingdom",
        "uk prime minister",
        "uk pm",
    ],
    "modi": [
        "modi",
        "narendra modi",
        "indian prime minister",
        "prime minister of india",
    ],
    "kim jong un": [
        "kim jong un",
        "kim jong-un",
        "north korean leader",
        "leader of north korea",
        "dprk leader",
    ],
    "mbs": [
        "mbs",
        "mohammed bin salman",
        "crown prince of saudi arabia",
        "saudi crown prince",
    ],
    "sisi": [
        "sisi",
        "abdel fattah el-sisi",
        "egyptian president",
        "president of egypt",
    ],
    "erdogan": [
        "erdogan",
        "recep tayyip erdogan",
        "turkish president",
        "president of turkey",
    ],
    "meloni": [
        "meloni",
        "giorgia meloni",
        "italian prime minister",
        "prime minister of italy",
    ],
    "orban": [
        "orban",
        "viktor orban",
        "hungarian prime minister",
        "prime minister of hungary",
    ],
    "milei": [
        "milei",
        "javier milei",
        "argentine president",
        "president of argentina",
    ],
    "lula": [
        "lula",
        "luiz inacio lula da silva",
        "brazilian president",
        "president of brazil",
    ],
    "sheinbaum": [
        "sheinbaum",
        "claudia sheinbaum",
        "mexican president",
        "president of mexico",
    ],
    "carney": [
        "carney",
        "mark carney",
        "canadian prime minister",
        "prime minister of canada",
    ],
    "ishiba": [
        "ishiba",
        "shigeru ishiba",
        "japanese prime minister",
        "prime minister of japan",
    ],
    "lai": [
        "lai ching-te",
        "lai ching te",
        "william lai",
        "taiwanese president",
        "president of taiwan",
    ],
    "prabowo": [
        "prabowo",
        "prabowo subianto",
        "indonesian president",
        "president of indonesia",
    ],
}
