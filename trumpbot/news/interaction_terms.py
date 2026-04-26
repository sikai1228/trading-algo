"""Interaction terms for the Stage 1 keyword pre-filter.

Phase 4 Part 2.8 (April 2026) replaced the proximity-based,
verb-classified matcher with a simple "Trump + subject + any
interaction term" pre-filter. This module owns the term list.

Design intent: be aggressively inclusive. Stage 2 (the LLM cascade)
is the precision filter; Stage 1's only job is to ensure no obvious
positive ever falls off the conveyor.

Each term is matched **case-insensitively** with **word boundaries**
(`\\b...\\b`) so substrings inside other words don't trigger
(e.g. "ate" never matches "ratification"). Multi-word entries like
"phone call" match the literal phrase because `\\b` only constrains
the outer edges. Hyphenated and unhyphenated forms are listed
separately so both spellings catch.

If you add a term: lowercase, no surrounding whitespace; if it has
multiple grammatical forms (verb tenses, plurals), list each form
you want recall on. The matcher does not stem.
"""

from __future__ import annotations

from typing import Final

INTERACTION_TERMS: Final[tuple[str, ...]] = (
    # ----- Talking / speech -------------------------------------------
    "spoke",
    "speaks",
    "speak",
    "speaking",
    "talked",
    "talks",
    "talking",
    "talk",
    "said",
    "says",
    "told",
    "tells",
    "telling",
    "mentioned",
    "mentions",
    "mention",
    "addressed",
    "addresses",
    "address",
    "referenced",
    "references",
    "reference",
    "responded",
    "responds",
    "responding",
    "reacted",
    "reacts",
    "reacting",
    "praised",
    "praises",
    "criticized",
    "criticizes",
    "attacked",
    "attacks",
    "denounced",
    # ----- Phone / call -----------------------------------------------
    "called",
    "calls",
    "calling",
    "call",
    "phoned",
    "phones",
    "phoning",
    "phone",
    "rang",
    "dialed",
    # ----- Meet / meeting ---------------------------------------------
    "met",
    "meets",
    "meeting",
    "meet",
    "summit",
    "summits",
    "bilateral",
    "bilaterals",
    "conferred",
    "confers",
    "conferring",
    "conference",
    "huddled",
    "huddle",
    "consulted",
    "consults",
    "consulting",
    "consultation",
    "convened",
    "convenes",
    "convene",
    # ----- Talks / conversations / formal interaction -----------------
    "discussed",
    "discusses",
    "discussing",
    "discussion",
    "conversation",
    "conversations",
    "briefed",
    "briefing",
    "briefings",
    "brief",
    "readout",
    "readouts",
    "negotiated",
    "negotiates",
    "negotiating",
    "negotiation",
    # ----- Sit-down / face-to-face ------------------------------------
    "sat",
    "sit",
    "sit-down",
    "sit down",
    "sat down",
    "sat-down",
    "face-to-face",
    "face to face",
    "in-person",
    "in person",
    # ----- Video / remote ---------------------------------------------
    "videoconference",
    "video conference",
    "video call",
    "video-call",
    # ----- Meals (contract-relevant) ----------------------------------
    "dined",
    "dines",
    "dining",
    "dinner",
    "dinners",
    "lunched",
    "lunches",
    "lunching",
    "lunch",
    "breakfasted",
    "breakfast",
    "breakfasts",
    # ----- Generic interaction / contact ------------------------------
    "exchanged",
    "exchanges",
    "exchange",
    "communicated",
    "communicates",
    "communicating",
    "communication",
    "contacted",
    "contacts",
    "contacting",
    "contact",
    "messaged",
    "messages",
    "messaging",
    "message",
    "wrote",
    "writes",
    "writing",
    "letter",
    "letters",
    "sent",
    "sends",
    "sending",
    "envoy",
    "envoys",
    "intermediary",
    "intermediaries",
    "reached",
    # ----- Host / receive / welcome -----------------------------------
    "hosted",
    "hosts",
    "hosting",
    "welcomed",
    "welcomes",
    "welcoming",
    "received",
    "receives",
    "receiving",
    "reception",
    "greeted",
    "greets",
    "greeting",
    # ----- Agreement / resolution forms (often signal an interaction) -
    "agreed",
    "agrees",
)
