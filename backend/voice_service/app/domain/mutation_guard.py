"""Deterministic mutation-intent backstop for the guardrail gate (Req 4.2).

Pure, lexical second line of defence behind the Bedrock guardrail. The
guardrail's DENY topic is a *semantic* classifier, so its verdicts are
probabilistic: measured against the deployed topic, imperative mutations
such as "Scale the auto scaling group to 10 instances", "Create an access
key for my IAM user", and "Update the security group to allow 0.0.0.0/0"
were answered ``action=NONE`` — passes — even though each one changes AWS
state. This module blocks that class deterministically, before the
guardrail is consulted.

Design: **imperative position only.** A request is blocked when its
*leading* word — after politeness and modal framing such as "please" or
"can you" is stripped — is one of :data:`MUTATION_VERBS`. An instruction
to change AWS state opens with the change verb ("Terminate the instance",
"Create an access key"), whereas a diagnostic question does not, even when
it mentions the same verb later:

- "Why can't I start an SSM session to my instance?" — leads with "why".
- "IAM role permissions to start an SSM session" — leads with "iam".
- "Instance stopped unexpectedly overnight" — leads with "instance".
- "Which IAM roles have administrator access?" — leads with "which".

All four are read-only and must reach the agent, so position — not mere
presence of the vocabulary — is what this module keys on. Scanning the
whole sentence would refuse every one of them.

The module only ever *adds* blocks: a request it declines to block still
faces the full fail-closed guardrail gate
(:mod:`app.domain.guardrail_policy`), so this can never turn a guardrail
BLOCK into a pass. Blocking here is therefore strictly conservative with
respect to Req 4.2 and 4.4.

Pure domain module: no I/O, no SDK imports, deterministic for identical
inputs (Req 17.6).
"""

import re
from typing import Final

__all__ = [
    "MUTATION_INTENT_REASON",
    "MUTATION_VERBS",
    "has_mutation_intent",
]

MUTATION_INTENT_REASON: Final = "mutation-intent"
"""Audit reason recorded when this backstop blocks a request (Req 4.5)."""

MUTATION_VERBS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "attach",
        "create",
        "delete",
        "deploy",
        "destroy",
        "detach",
        "disable",
        "drain",
        "enable",
        "grant",
        "kill",
        "launch",
        "modify",
        "provision",
        "purge",
        "put",
        "reboot",
        "remove",
        "reset",
        "restart",
        "revoke",
        "rollback",
        "rotate",
        "scale",
        "set",
        "start",
        "stop",
        "terminate",
        "update",
        "upgrade",
        "write",
    }
)
"""Verbs by which AWS state is changed, matched as whole words.

Inflections match too (see :func:`_is_mutation_token`), so "terminating",
"deletes", and "stopping" all match their base verb.
"""

_FRAMING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "able",
        "and",
        "any",
        "as",
        "assistant",
        "be",
        "can",
        "could",
        "engineer",
        "for",
        "help",
        "hey",
        "hi",
        "i",
        "id",
        "ids",
        "just",
        "kindly",
        "like",
        "may",
        "me",
        "my",
        "need",
        "ok",
        "okay",
        "please",
        "pls",
        "quickly",
        "so",
        "the",
        "then",
        "to",
        "us",
        "want",
        "we",
        "well",
        "will",
        "would",
        "you",
    }
)
"""Politeness and modal filler skipped when locating the leading token.

"Can you please delete the queue" and "delete the queue" must classify
identically, so this set is stripped from the front of the request. It
contains no mutation verb, so stripping it can never mask an instruction.
"""

_TOKEN_PATTERN: Final = re.compile(r"[a-z]+")
"""Word matcher: ASCII letter runs, applied to the lowercased request."""

_STEM_SUFFIXES: Final[tuple[str, ...]] = ("ing", "ed", "es", "s")
"""Inflection suffixes stripped when deriving a token's candidate stems."""


def _is_mutation_token(token: str) -> bool:
    """Report whether one token names a mutation verb, inflections included.

    Matches the token verbatim, then each stem obtained by removing one
    inflection suffix, plus the two spelling repairs English inflection
    needs: restoring an elided "e" ("deletes" → "delet" → "delete") and
    collapsing a doubled consonant ("stopping" → "stopp" → "stop").

    Args:
        token: Lowercased ASCII word from the request.

    Returns:
        ``True`` when any candidate form of the token is in
        :data:`MUTATION_VERBS`.
    """
    if token in MUTATION_VERBS:
        return True
    for suffix in _STEM_SUFFIXES:
        if len(token) <= len(suffix) + 2 or not token.endswith(suffix):
            continue
        stem = token[: -len(suffix)]
        candidates = {stem, f"{stem}e"}
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.add(stem[:-1])
        if candidates & MUTATION_VERBS:
            return True
    return False


def _tokens(text: str) -> list[str]:
    """Tokenize a request into its lowercased ASCII words.

    Args:
        text: Raw engineer request text.

    Returns:
        The request's words in order; empty when the text carries no
        ASCII letters.
    """
    return _TOKEN_PATTERN.findall(text.lower())


def _leading_token(tokens: list[str]) -> str | None:
    """Return the first token that carries intent, skipping framing words.

    Args:
        tokens: Normalized request tokens from :func:`_tokens`.

    Returns:
        The first token outside :data:`_FRAMING_TOKENS`, or ``None`` when
        the request is nothing but framing.
    """
    for token in tokens:
        if token not in _FRAMING_TOKENS:
            return token
    return None


def has_mutation_intent(query: str) -> bool:
    """Report whether a request unambiguously instructs a state change.

    Blocks the imperative-mutation class the semantic guardrail topic lets
    through, while leaving every question about existing state to the
    guardrail (module docstring).

    Args:
        query: The extracted engineer request text.

    Returns:
        ``True`` when the request opens with a mutation verb — the caller
        then refuses it without consulting the guardrail; ``False`` when
        the request should be evaluated by the guardrail as usual.
    """
    leading = _leading_token(_tokens(query))
    return leading is not None and _is_mutation_token(leading)
