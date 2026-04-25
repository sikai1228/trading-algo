"""Tests for the title-based subject extractor."""

from __future__ import annotations

import pytest

from trumpbot.discovery.subject_extraction import (
    ExtractionFailure,
    extract_subject,
    make_subject_key,
)


class TestExtractSubject:
    @pytest.mark.parametrize(
        ("title", "full_name", "subject_key", "last_name"),
        [
            (
                "Donald Trump and Vladimir Putin meet before May 1, 2026?",
                "Vladimir Putin",
                "vladimirputin",
                "Putin",
            ),
            (
                "Donald Trump and Xi Jinping meet before May 1, 2026?",
                "Xi Jinping",
                "xijinping",
                "Jinping",
            ),
            (
                "Donald Trump and John Thune meet before May 1, 2026?",
                "John Thune",
                "johnthune",
                "Thune",
            ),
            (
                "Donald Trump and Benjamin Netanyahu meet before May 1, 2026?",
                "Benjamin Netanyahu",
                "benjaminnetanyahu",
                "Netanyahu",
            ),
            (
                "Donald Trump and María Corina Machado meet before May 1, 2026?",
                "María Corina Machado",
                "mariacorinamachado",
                "Machado",
            ),
            (
                "Donald Trump and Kim Jong-un meet before May 1, 2026?",
                "Kim Jong-un",
                "kimjongun",
                "Jong-un",
            ),
            # Case insensitivity on the prefix
            (
                "donald trump and Vladimir Putin meet before May 1, 2026?",
                "Vladimir Putin",
                "vladimirputin",
                "Putin",
            ),
        ],
    )
    def test_extracts(self, title: str, full_name: str, subject_key: str, last_name: str) -> None:
        result = extract_subject(title)
        assert result.full_name == full_name
        assert result.subject_key == subject_key
        assert result.last_name == last_name

    @pytest.mark.parametrize(
        "title",
        [
            "",
            "Will Trump call Putin?",
            "Trump and Putin shake hands",  # missing 'meet before'
            "Donald Trump and meet before May 1, 2026?",  # empty subject
            "Trump and Vladimir Putin meet before May 1, 2026?",  # missing 'Donald'
        ],
    )
    def test_failure_modes(self, title: str) -> None:
        with pytest.raises(ExtractionFailure):
            extract_subject(title)


class TestMakeSubjectKey:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Vladimir Putin", "vladimirputin"),
            ("Xi Jinping", "xijinping"),
            ("María Corina Machado", "mariacorinamachado"),
            ("Kim Jong-un", "kimjongun"),
            ("J.D. Vance", "jdvance"),
            ("ALL CAPS", "allcaps"),
            ("é", "e"),  # diacritic stripped
            ("    spaces   around   ", "spacesaround"),
        ],
    )
    def test_normalization(self, name: str, expected: str) -> None:
        assert make_subject_key(name) == expected

    def test_returns_empty_for_no_letters(self) -> None:
        assert make_subject_key("123 !!!") == ""
