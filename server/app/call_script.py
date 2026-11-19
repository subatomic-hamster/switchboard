"""The scripted approval conversation used when the call is placed through AgentPhone.

AgentPhone runs in webhook mode: it turns the caller's speech into text, posts each turn to
this server, and speaks whatever text the server returns. No language model takes part in
the call. This module is the pure part: what each line says, and what a caller's turn means.
`app/voice.py` holds the state and applies the same approve / deny / freeze rules as every
other path.

The order is fixed:

1. the operator states the request (the greeting, spoken when the phone is answered);
2. the risk officer gives one objection;
3. the operator asks for the amount and the word shown on the owner's dashboard.

AgentPhone gives one voice per call, so the risk officer is announced by name in the same
voice. Nothing here recognises who is speaking: a voice is never proof of identity.
"""

from __future__ import annotations

import difflib
import itertools
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.intent import read_amount
from app.planner import say_money

# Said to anyone who reaches the webhook on a call this server did not place.
NOT_OUR_CALL = (
    "This number only places approval calls for its owner. Nothing can be approved on this "
    "call. Goodbye."
)

Intent = Literal["freeze", "deny", "repeat", "none"]

_FREEZE_WORDS = frozenset({"freeze", "frozen", "freezing"})
_FREEZE_PHRASES = ("kill switch", "stop everything", "stop all", "lock everything", "lock it down")
_DENY_WORDS = frozenset(
    {"deny", "denied", "decline", "declined", "reject", "rejected", "refuse", "cancel"}
)
# "No" on its own is a refusal. Inside a longer sentence ("no wait, it's two fifty") it is not.
_SHORT_NO = frozenset({"no", "nope", "stop"})
_SHORT_TURN_WORDS = 3
_REPEAT_WORDS = frozenset({"repeat", "again", "pardon", "what", "sorry"})

_SMALL = frozenset(
    {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}
)  # fmt: skip
_TEENS_AND_TENS = frozenset(
    {
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
        "eighty", "ninety",
    }
)  # fmt: skip
_WORD_MATCH_CUTOFF = 0.88
_MIN_FUZZY_LENGTH = 5


@dataclass(frozen=True)
class Heard:
    """What one caller turn contained."""

    intent: Intent = "none"
    amount: Decimal | None = None
    #: An amount was said in a form with two readings ("two fifty"), so it was not taken.
    amount_unclear: bool = False
    #: Every distinct word from the challenge vocabulary that was said, in order.
    words: tuple[str, ...] = ()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower().replace("-", " "))


def _colloquial_amount(tokens: list[str]) -> bool:
    """True for "two fifty" or "one twenty": a small number straight before tens, with no
    "hundred". It could mean 2.50 or 250, so the caller is asked to say it in full."""
    for first, second in itertools.pairwise(tokens):
        if first in _SMALL and second in _TEENS_AND_TENS:
            return True
    return False


def spoken_amount(text: str) -> tuple[Decimal | None, bool]:
    """The dollar amount in a caller's turn, and whether it was too unclear to take."""
    lowered = text.lower()
    has_digits = bool(re.search(r"\d", lowered))
    tokens = _tokens(lowered)
    if not has_digits and _colloquial_amount(tokens):
        return None, True
    dollars_part, cents_part = lowered, ""
    split = re.search(r"\bdollars?\b", lowered)
    if split and re.search(r"\bcents?\b", lowered[split.end() :]):
        dollars_part, cents_part = lowered[: split.start()], lowered[split.end() :]
    amount = read_amount(dollars_part)
    if amount is None:
        return None, False
    if cents_part:
        cents = read_amount(cents_part)
        if cents is None or cents >= 100 or cents != cents.to_integral_value():
            return None, True
        amount += cents / 100
    return amount, False


def vocabulary_words(text: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    """Words from the challenge vocabulary said in this turn.

    Matching is against the public list of possible words, never against the expected word,
    so a near miss by the speech-to-text ("harbour", "lanterns") is still read as the word
    the caller said. Whether it is the right word is decided later, by `Approvals.approve`.
    """
    known = set(vocabulary)
    found: list[str] = []
    for token in _tokens(text):
        match: str | None = None
        if token in known:
            match = token
        elif len(token) >= _MIN_FUZZY_LENGTH:
            close = difflib.get_close_matches(token, vocabulary, n=1, cutoff=_WORD_MATCH_CUTOFF)
            match = close[0] if close else None
        if match and match not in found:
            found.append(match)
    return tuple(found)


def hear(text: str, vocabulary: tuple[str, ...]) -> Heard:
    """Read one caller turn. Stopping wins over refusing, and refusing wins over approving."""
    lowered = " ".join(text.lower().split())
    tokens = _tokens(lowered)
    token_set = set(tokens)
    if token_set & _FREEZE_WORDS or any(phrase in lowered for phrase in _FREEZE_PHRASES):
        return Heard(intent="freeze")
    if token_set & _DENY_WORDS or (len(tokens) <= _SHORT_TURN_WORDS and token_set & _SHORT_NO):
        return Heard(intent="deny")
    amount, unclear = spoken_amount(lowered)
    words = vocabulary_words(lowered, vocabulary)
    if amount is None and not unclear and not words and token_set & _REPEAT_WORDS:
        return Heard(intent="repeat")
    return Heard(amount=amount, amount_unclear=unclear, words=words)


# --------------------------------------------------------------------------- the lines


def greeting(amount: Decimal, payee_label: str, reason_phrase: str, case_for: str) -> str:
    """Step 1, spoken as soon as the phone is answered: the operator states the request."""
    return (
        "This is the Switchboard operator, an automated call about your own account. "
        f"Your agent wants to send {say_money(amount)} to {payee_label}. "
        f"It needs your approval because {reason_phrase}. "
        f"{case_for} "
        "Say go on to hear the risk officer. You can say deny or freeze at any time."
    )


def restatement(amount: Decimal, payee_label: str, reason_phrase: str) -> str:
    return (
        f"The operator again. Your agent wants to send {say_money(amount)} to {payee_label}. "
        f"It needs your approval because {reason_phrase}."
    )


def objection(case_against: str) -> str:
    """Step 2: the risk officer, announced by name because the call has one voice."""
    return f"Now the risk officer. {case_against}"


def ask(requires_passkey: bool) -> str:
    """Step 3: what it takes to approve."""
    passkey = (
        " This one is above your passkey limit, so you will also confirm with your passkey "
        "on the dashboard."
        if requires_passkey
        else ""
    )
    return (
        "The operator again. To approve, say the amount, then the word shown on your "
        f"dashboard.{passkey} To refuse, say deny. To stop all spending, say freeze."
    )


def approved(amount: Decimal, payee_label: str) -> str:
    return f"Approved. {say_money(amount)} to {payee_label} is going out now. Goodbye."


AWAITING_PASSKEY = (
    "The amount and the word match. This one is above your passkey limit, so it is not sent "
    "yet. Confirm with your passkey on the dashboard to finish. Goodbye."
)
DENIED = "Denied. Nothing was sent. Goodbye."
FROZEN = "Frozen. All spending is stopped, and this request is denied. Goodbye."
TOO_MANY_ATTEMPTS = (
    "That is three wrong tries, so the request is denied. Nothing was sent. Goodbye."
)
EXPIRED = "This request has expired, so it was denied. Nothing was sent. Goodbye."
TOO_LONG = "This call has gone on too long, so the request is denied. Nothing was sent. Goodbye."
NOT_RECORDED = (
    "I could not record the approval, so nothing was sent. Try again from the dashboard. Goodbye."
)
FAILED = "Something went wrong on my side. Nothing was approved. Goodbye."
CHECKING = "Checking."
AMOUNT_UNCLEAR = "Say the amount in full, for example two hundred and fifty dollars."
TOO_MANY_WORDS = "I heard more than one word. That counts as a wrong try."
NEED_BOTH = "To approve, say the amount, then the word shown on your dashboard. Or say deny."
NEED_AMOUNT = "I have the word. Now say the amount."


def need_word(amount: Decimal) -> str:
    return f"I have {say_money(amount)}. Now say the word shown on your dashboard."


def already_resolved(status: str) -> str:
    return f"This request is already {status}. Nothing more can be done on this call. Goodbye."


def mismatch(error: str, tries_left: int) -> str:
    what = "The amount" if error == "amount_mismatch" else "The word"
    tries = "1 try" if tries_left == 1 else f"{tries_left} tries"
    return f"{what} did not match. You have {tries} left. Say the amount and the word again."
