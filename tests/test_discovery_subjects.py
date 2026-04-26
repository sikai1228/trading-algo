"""Tests for the SubjectExtractor."""

from __future__ import annotations

import pytest

from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor


class TestSubjectExtractor:
    def test_extracts_putin_from_title(self, extractor: SubjectExtractor) -> None:
        assert extractor.extract("Will Trump call Putin this month?") == "putin"

    def test_extracts_xi_from_alias(self, extractor: SubjectExtractor) -> None:
        assert extractor.extract("Trump and the Chinese President", None) == "xi"

    def test_returns_none_when_no_subject(self, extractor: SubjectExtractor) -> None:
        assert extractor.extract("Trump golfs in Florida") is None

    def test_returns_none_for_empty_inputs(self, extractor: SubjectExtractor) -> None:
        assert extractor.extract(None, "", None) is None

    def test_longer_alias_wins_over_short(self, extractor: SubjectExtractor) -> None:
        # "kim jong un" should win over any single-word alias also present.
        assert extractor.extract("Trump met with Kim Jong Un today") == "kim jong un"

    def test_subject_with_no_aliases_rejected(self) -> None:
        with pytest.raises(ValueError):
            SubjectExtractor(aliases={"x": []})

    def test_default_aliases_complete_for_required_subjects(self) -> None:
        for subject in (
            "putin",
            "xi",
            "netanyahu",
            "zelensky",
            "macron",
            "merz",
            "starmer",
            "modi",
            "kim jong un",
            "mbs",
            "sisi",
            "erdogan",
            "meloni",
            "orban",
            "milei",
            "lula",
            "sheinbaum",
            "carney",
            "ishiba",
            "lai",
            "prabowo",
        ):
            assert subject in DEFAULT_SUBJECT_ALIASES
            assert DEFAULT_SUBJECT_ALIASES[subject]
