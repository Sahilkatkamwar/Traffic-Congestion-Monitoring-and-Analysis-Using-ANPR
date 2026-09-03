"""Indian plate grammar: what a plate can be, and what OCR turns it into.

A plate is not an arbitrary string, and that is the whole point of this module.
Two facts do the work:

  * The format fixes the class of every character. MH15HY2237 is
    letter-letter-digit-digit-letter-letter-digit-digit-digit-digit, and no
    plate anywhere puts a letter in the last four slots.
  * OCR confusions are between glyphs that look alike, and nearly every pair
    straddles the letter/digit line -- 0 and O, 5 and S, 8 and B.

Put together, a wrong character usually announces itself: a 0 in a letter slot
is an O misread, because a plate cannot hold a digit there. The correction is
therefore position-aware, not a global find-and-replace. Replacing every 0 with
O would destroy MH15HY2007.

The layout is not known in advance -- [2 letters][1-2 digits][1-3 letters]
[4 digits] is eight to eleven characters and several shapes -- so every layout
valid for the read's length is fitted and the cheapest wins. Cost counts the
characters that had to be explained as confusions, so the winning layout is the
one that needs the fewest corrections to be true.

What this module will not do is invent. A character with no confusion partner in
the class its slot requires is left exactly as it was and the read is reported
invalid, because a truncated or garbled read is not a plate and dressing it up
as one puts a confident wrong string in front of a user. MH15HY22 -- a real read
from this footage, two digits short -- comes back uncorrected and invalid.
Recovering the vehicle behind it is matching.py's job, not this one's.
"""

import re

import yaml

from app.config import CONFIG_DIR

# Glyph pairs the OCR model actually confuses, from CLAUDE.md. Each string is
# one equivalence class: every character in it can be read as any other.
#
# Membership crosses the letter/digit line on purpose -- that crossing is what
# makes a position-aware fix possible. MH is the exception, two letters that
# look alike and carry no digit between them, so a slot needing a digit that
# reads M is not repairable. It should not be.
CONFUSION_GROUPS = ("0ODQ", "1I7", "2Z", "5S", "6G", "8B", "MH")

# character -> every character it can be confused with, itself included
CONFUSED_WITH = {}
for _group in CONFUSION_GROUPS:
    for _char in _group:
        CONFUSED_WITH.setdefault(_char, set()).update(_group)

# A slot needing a digit, reading a letter: the digit in that letter's group.
TO_DIGIT = {
    char: next(c for c in sorted(CONFUSED_WITH[char]) if c.isdigit())
    for char in CONFUSED_WITH
    if char.isalpha() and any(c.isdigit() for c in CONFUSED_WITH[char])
}

# A slot needing a letter, reading a digit. 0 could be O, D or Q; O is the
# canonical answer, and the state-code repair below reconsiders it for the two
# slots where the alternatives can actually be decided.
_CANONICAL = {"0": "O", "1": "I", "7": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
TO_LETTER = {
    char: _CANONICAL[char] for char in CONFUSED_WITH if char in _CANONICAL
}

# Letters a digit could be, best guess first, for the state-code repair.
LETTER_OPTIONS = {
    char: [TO_LETTER[char]]
    + sorted(c for c in CONFUSED_WITH[char] if c.isalpha() and c != TO_LETTER[char])
    for char in TO_LETTER
}

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")

# Cost, in characters that had to be explained, of each kind of fit problem.
_CONFUSION_COST = 1.0   # a glyph swap the model is known to make
_IMPOSSIBLE_COST = 4.0  # a character with no confusion partner in the needed class
_UNKNOWN_STATE = 1.5    # well formed, but the state code is not a real one
_ZERO_DISTRICT = 4.0    # districts are numbered from 1; there is no RTO 0
_MAX_COST = 3.5         # above this the read is not a plate and is left alone

_state_codes = None


def state_codes(reload=False):
    """{code: state name}, current and retired codes together.

    A retired code is still a legal plate on a real vehicle, so it validates.
    """
    global _state_codes
    if _state_codes is not None and not reload:
        return _state_codes

    path = CONFIG_DIR / "state_codes.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No state code table at {path}. Plate validation cannot run without it."
        )
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    codes = dict(raw.get("codes") or {})
    codes.update(raw.get("retired") or {})
    _state_codes = {str(k).upper(): str(v) for k, v in codes.items()}
    return _state_codes


def normalize(text):
    """Uppercase, and drop everything that is not A-Z or 0-9.

    Plates are written with spaces and dashes and OCR sometimes emits them.
    None of that is part of the identity of the vehicle.
    """
    if not text:
        return ""
    return _NOT_ALNUM.sub("", str(text).upper())


# ------------------------------------------------------------------- layouts


def _masks(length):
    """Every layout a read of this length could have, likeliest first.

    L means the slot must hold a letter, D a digit. The order is the tie break:
    when two layouts fit equally well the earlier one wins, so the common
    MH 12 AB 1234 shape beats the rarer single-letter series.
    """
    out = []
    # Standard: [2 letters][1-2 digits][1-3 letters][4 digits]
    for digits in (2, 1):
        for series in (2, 3, 1):
            mask = "LL" + "D" * digits + "L" * series + "DDDD"
            if len(mask) == length:
                out.append(("standard", mask))
    # Bharat series: [2 digits]BH[4 digits][1-2 letters], e.g. 22BH1234AA
    for tail in (2, 1):
        mask = "DDLLDDDD" + "L" * tail
        if len(mask) == length:
            out.append(("bh", mask))
    return out


def _fit(text, mask):
    """Force text into mask. Returns (cost, fixed text).

    Every character is judged only against the class its own slot requires. One
    already in the right class is free; one that is a known confusion of the
    right class is corrected and charged; one that is neither is left untouched
    and charged enough to disqualify the layout outright.
    """
    cost = 0.0
    out = []
    for char, want in zip(text, mask):
        if want == "D":
            if char.isdigit():
                out.append(char)
            elif char in TO_DIGIT:
                out.append(TO_DIGIT[char])
                cost += _CONFUSION_COST
            else:
                out.append(char)
                cost += _IMPOSSIBLE_COST
        else:
            if char.isalpha():
                out.append(char)
            elif char in TO_LETTER:
                out.append(TO_LETTER[char])
                cost += _CONFUSION_COST
            else:
                out.append(char)
                cost += _IMPOSSIBLE_COST
    return cost, "".join(out)


def _repair_state(fixed, original):
    """Second look at the two state-code letters. Returns (text, extra cost).

    The letter slots were filled with the canonical guess -- 0 became O -- which
    is right on average and wrong for Delhi, where D is meant. The state code is
    the one place in a plate where those alternatives can be decided, so the
    whitelist is consulted: if exactly one substitution within the confusion
    groups produces a real code, that is the answer.

    Ambiguity is not resolved by guessing. Two plausible codes means the prefix
    stays as it was read and the plate is reported unknown-state, which is a
    question for matching.py rather than an answer invented here.
    """
    codes = state_codes()
    prefix = fixed[:2]
    if prefix in codes:
        return fixed, 0.0

    # Alternatives come from what was actually read, not from the canonical
    # guess: the read is the evidence.
    seen = original[:2]
    options = []
    for index in (0, 1):
        source = seen[index]
        if source.isdigit():
            alternatives = LETTER_OPTIONS.get(source, [])
        else:
            alternatives = sorted(
                c for c in CONFUSED_WITH.get(source, ()) if c.isalpha()
            )
        for alternative in alternatives:
            candidate = prefix[:index] + alternative + prefix[index + 1 :]
            if candidate != prefix and candidate in codes:
                options.append(candidate)

    unique = sorted(set(options))
    if len(unique) == 1:
        return unique[0] + fixed[2:], _CONFUSION_COST
    return fixed, _UNKNOWN_STATE


def correct(text):
    """Apply the grammar to one OCR read.

    Always returns a dict -- never None and never a bare string, because the
    caller needs to know whether the answer is trustworthy as much as it needs
    the answer:

        text     the corrected plate, or the normalised read when nothing could
                 be corrected. Never None for a non-empty input.
        raw      the normalised read, before correction
        valid    True only for a real format carrying a real state code
        format   'standard', 'bh', or None
        state    the two-letter code, or None
        changed  whether correction altered anything
        cost     characters that had to be explained as confusions
    """
    raw = normalize(text)
    result = {
        "text": raw or None,
        "raw": raw or None,
        "valid": False,
        "format": None,
        "state": None,
        "changed": False,
        "cost": None,
    }
    if not raw:
        return result

    best = None
    for rank, (kind, mask) in enumerate(_masks(len(raw))):
        cost, fixed = _fit(raw, mask)
        if kind == "bh":
            # The mask only says "two letters here". The Bharat series says
            # which two, and a plate not shouting BH is not one.
            if fixed[2:4] != "BH":
                continue
        else:
            fixed, extra = _repair_state(fixed, raw)
            cost += extra
            # Districts are numbered from 01, so a district of 0 means the
            # layout is wrong rather than the plate. Without this, KA0SMN7788
            # parses as KA-0-SMN-7788 at no cost at all and beats the correct
            # KA-05-MN-7788, which costs one S-for-5.
            district = fixed[2 : 2 + mask.count("D") - 4]
            if not district.strip("0"):
                cost += _ZERO_DISTRICT
        # Earlier layouts are likelier. The nudge is small enough to decide
        # genuine ties and nothing else.
        cost += rank * 0.01
        if best is None or cost < best[0]:
            best = (cost, kind, fixed)

    if best is None or best[0] > _MAX_COST:
        # No layout fits without inventing a character. The read stands as it
        # is, marked invalid: an incomplete plate is a real and common outcome,
        # and it is still a sighting worth writing.
        return result

    cost, kind, fixed = best
    state = fixed[:2] if kind == "standard" else None
    result.update(
        {
            "text": fixed,
            "valid": kind == "bh" or state in state_codes(),
            "format": kind,
            "state": state,
            "changed": fixed != raw,
            "cost": round(cost, 2),
        }
    )
    return result


def apply(text):
    """The corrected string alone, for callers that only want the plate."""
    return correct(text)["text"]


def is_valid(text):
    """Is this string already a well-formed plate with a real state code?"""
    result = correct(text)
    return result["valid"] and not result["changed"]


def state_name(text):
    """Full state name for a plate or a bare code, or None."""
    return state_codes().get(normalize(text)[:2])
