"""Which part of a `codex exec` stdout is the reviewer's verdict.

**The measured defect (2026-08-17).** `codex/conversation.py` took
`result.stdout.strip()` and handed the WHOLE of it to `contract.parse_response`.
`codex exec` stdout is not a reply, it is a rendered transcript: a separator
rule, a `user` role marker with the prompt under it, four hook lines, a `codex`
role marker with the answer under it, two more hook lines, a token count — and
then, on this machine, THE ANSWER A SECOND TIME as the run's closing summary.
Fed to the real parser, that is

    ContractError: invalid_json: the reply is not exactly one JSON value

so the `codex_cli` provider could not return a verdict at all.

**The contract is not the thing that was wrong, and nothing here weakens it.**
It rejects prose-plus-a-bare-object, and it rejects a second object, on purpose:
"with a directive that can authorize a commit or push, guess which one they
meant is not an acceptable rule" (`contract._extract_envelope`). Two identical
objects today are two DIFFERENT objects the first day a hook prints something
unexpected. So the fix is upstream of the parser: isolate the one segment that
is the reviewer's message, and hand the parser exactly that. Every rule the
contract applies still applies, to a smaller and honestly-delimited input.

**What a segment is.** The lines after a line that is exactly `codex`, up to the
first line that is transcript FURNITURE rather than message: another role
marker, a `hook:` line, the `tokens used` counter, or a separator rule. Those
four shapes are what the CLI prints around a message, and none of them can be
produced by a JSON directive — a JSON line begins with `{`, `"`, `[` or `}`, and
the contract already forbids a literal newline inside a string value, so a
one-object reply cannot contain a bare `hook:` or a rule of dashes at the start
of a line.

**The trailing duplicate is excluded structurally, not by counting.** It sits
after `tokens used`, which closes the segment, so it is never inside one. This
is deliberately NOT "take the last object" or "de-duplicate identical objects":
both are the position rule the contract refuses, and the second one is worse —
it reads two objects that happen to agree as agreement, which stops being true
the moment they differ.

**The ECHO is excluded structurally too, and that is the point that matters.**
The prompt comes back under the `user` marker, and the prompt CONTAINS the
response contract and, in the shape that was reproduced, an example of the
directive being asked for. Text the loop SENT read back as the reviewer's answer
would be an approval the reviewer never gave. `quota.py` carries the same rule
for failure classification and says why in more detail; this module is that rule
applied to the reply.

**Undecorated stdout is passed through unchanged.** When no `codex` role marker
is present at all, this is a build (or a fake) whose stdout is simply the reply,
which is what every caller got before this module existed. That is a
pass-through, not a guess: the whole text still goes to the contract, and the
contract still refuses it if it is not exactly one directive.

The one thing the pass-through will not do is carry FURNITURE ONLY. Hook and
token lines with no role marker anywhere are decoration a build did not label,
and passing them on would get them refused by the contract — which is the
EXPENSIVE refusal, since a parse failure spends `policy.max_parse_retries` and
parks the loop `parse_budget_exhausted`, loop_fatal. That shape is reported as
no verdict instead, which is bounded at one resend and then a park.

**Nothing here defaults a decision, and nothing here parses.** `isolate_reply`
returns text or it returns nothing with a reason. Deciding whether that text is
a valid directive stays exactly where it was — with `contract.parse_response`,
one layer up — because a reviewer that answers in prose should draw the
contract's corrective re-prompt, not a silent re-invocation of the CLI. What
this module reports as a failure is the narrower thing: stdout from which no
segment could be isolated AT ALL.

**Ambiguity fails closed.** Two `codex` segments in one stdout is two messages,
and choosing between them by position is the rule the contract refuses. The
caller is told nothing was isolated, which makes the invocation REJECTED —
bounded at one resend and then a park (`orchestrator._step_submission_rejected`)
— rather than handing the parser a concatenation, which would spend the
parse-retry budget (`policy.max_parse_retries` is 2) and park the loop
`parse_budget_exhausted`, which is loop_fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The role marker `codex exec` prints on its own line before the assistant's
#: message. A whole stripped line, never a prefix: `codex exec is running` is
#: prose, and matching it as a marker would open a segment inside a diagnostic.
CODEX_ROLE_MARKER = "codex"

#: Every role marker observed in this CLI's transcript. `user` matters as much
#: as `codex` does: it opens the segment that holds the ECHOED PROMPT, and a
#: segment boundary is what keeps that text from ever being read as an answer.
ROLE_MARKERS = frozenset({"codex", "user"})

#: A hook line — `hook: SessionStart`, `hook: Stop Completed`. Prefix-anchored on
#: the stripped line, which no JSON line can be: a directive's lines start with
#: `{`, `"`, `[` or `}`.
_HOOK_LINE = re.compile(r"^hook:", re.IGNORECASE)

#: The token counter that closes a run. Prefix-anchored, not an exact line: the
#: captured output puts the count on the NEXT line, and a build that writes
#: `tokens used: 6,080` on one line must close the message just the same. No
#: JSON line can begin with these words, so the looser rule costs nothing.
_TOKENS_LINE = re.compile(r"^tokens used\b", re.IGNORECASE)

#: The rule the CLI draws between turns.
_SEPARATOR_LINE = re.compile(r"^-{3,}$")

#: A bare count — the number `tokens used` puts on its own line (`6,080`).
#: Read as furniture by `_is_message_line` ONLY, never by `_is_furniture`, so it
#: can decide "is there a message in this stdout at all" without ever being able
#: to END one. A digits-only line cannot begin a JSON directive, but a rule that
#: TRUNCATED a message at one would be a guess this defect gives no evidence
#: for, and the two questions do not have to share an answer.
_COUNT_LINE = re.compile(r"^[0-9][0-9,._]*$")

#: `source` values on `CodexReply`, written into the transcript record and read
#: back by eye — strings rather than an enum for the reason `quota.py`'s four
#: classifications are.
FROM_SEGMENT = "codex_segment"
FROM_WHOLE_STDOUT = "whole_stdout"
FROM_NOTHING = "none"


@dataclass(frozen=True)
class CodexReply:
    """What one invocation's stdout yielded, and how it was decided.

    `text` empty means NO VERDICT WAS ISOLATED, and `note` says which way that
    happened. It never means "the reply was not valid" — this module does not
    judge that; `contract.parse_response` does.

    The counts are not decoration. A record saying `segments: 2` is the
    difference between "codex printed nothing" and "codex printed two messages
    and choosing between them is not this transport's call".
    """

    text: str
    source: str
    segments: int
    stdout_chars: int
    reply_chars: int = 0
    note: str = ""


def _is_furniture(line: str) -> bool:
    """Is this line transcript FURNITURE — something the CLI printed around a
    message rather than part of one?

    Blank lines are not: a message may contain them, and a blank line ending a
    segment would truncate a pretty-printed directive at its first empty line.
    Trailing blanks are removed by the strip at the end instead.
    """
    stripped = line.strip()
    if not stripped:
        return False
    return (
        stripped in ROLE_MARKERS
        or _HOOK_LINE.match(stripped) is not None
        or _TOKENS_LINE.match(stripped) is not None
        or _SEPARATOR_LINE.match(stripped) is not None
    )


def _is_message_line(line: str) -> bool:
    """Could this line be part of a message at all?

    Used only by the undecorated pass-through, to tell "stdout IS the reply"
    from "stdout is decoration this build did not label with a role marker".
    Without the distinction the second passes through to the contract, which
    refuses it — and a parse refusal is the expensive one: it spends
    `policy.max_parse_retries` and parks the loop `parse_budget_exhausted`.

    STRICTER than `_is_furniture`, by exactly the bare token count: `tokens
    used` and `6,080` are one piece of decoration printed on two lines, and a
    guard that read the second as a message would never fire on the shape it
    exists for. It is stricter HERE and nowhere else — see `_COUNT_LINE`.
    """
    stripped = line.strip()
    if not stripped or _is_furniture(line):
        return False
    return _COUNT_LINE.match(stripped) is None


def role_marker_count(stdout: str) -> int:
    """How many `codex` role markers `stdout` carries.

    Counted separately from the segments themselves so "this output is not
    decorated" and "this output is decorated and the segment was empty" stay
    two different answers. Collapsing them is how a fallback becomes a guess:
    the first is stdout that IS the reply, the second is a reply that is missing.
    """
    if not isinstance(stdout, str):
        return 0
    return sum(1 for line in stdout.splitlines() if line.strip() == CODEX_ROLE_MARKER)


def codex_segments(stdout: str) -> tuple[str, ...]:
    """Every non-empty `codex`-role segment in `stdout`, in order.

    Order is recorded because it is the natural reading of the transcript, and
    it is never USED to choose between two of them — see `isolate_reply`, which
    refuses rather than picking.
    """
    if not isinstance(stdout, str) or not stdout:
        return ()
    segments: list[str] = []
    current: list[str] | None = None
    for line in stdout.splitlines():
        if line.strip() == CODEX_ROLE_MARKER:
            # A second marker closes the first segment and opens another. Both
            # survive to `isolate_reply`, which is where two of them is refused.
            if current is not None:
                segments.append("\n".join(current))
            current = []
            continue
        if current is None:
            continue
        if _is_furniture(line):
            segments.append("\n".join(current))
            current = None
            continue
        current.append(line)
    if current is not None:
        segments.append("\n".join(current))
    return tuple(text for text in (segment.strip() for segment in segments) if text)


def isolate_reply(stdout: str) -> CodexReply:
    """The reviewer's message in `stdout`, or nothing with a reason why.

    Four outcomes, and only the first two carry text:

    * exactly one `codex` segment — that segment, decoration removed;
    * no `codex` marker at all — the whole stripped stdout, unchanged, which is
      what this transport did before this module existed;
    * a marker whose segment is empty, nothing on stdout at all, or stdout that
      is furniture and nothing else — no verdict;
    * two or more segments — no verdict, because choosing by position is the
      rule the contract refuses and this is not the layer to break it in.
    """
    raw = stdout if isinstance(stdout, str) else ""
    chars = len(raw)
    markers = role_marker_count(raw)
    segments = codex_segments(raw)

    if markers == 0:
        whole = raw.strip()
        if not whole:
            # Kept verbatim from before this module: a clean exit with nothing
            # on stdout is a broken invocation, and the wording is what an
            # operator greps for.
            return CodexReply(
                "", FROM_NOTHING, 0, chars, note="exited 0 with no reply on stdout"
            )
        if not any(_is_message_line(line) for line in raw.splitlines()):
            # Hooks and a token count with no message and no role marker: a
            # build that decorates without labelling. Passing this on would hand
            # the contract text it must refuse, and a parse failure is the
            # expensive refusal — `policy.max_parse_retries` is 2 and exhausting
            # it parks the loop `parse_budget_exhausted`, which is loop_fatal.
            # A rejected invocation is bounded at one resend and then a park.
            return CodexReply(
                "",
                FROM_NOTHING,
                0,
                chars,
                note=(
                    "stdout carries only transcript furniture — hook or token "
                    "lines and no message at all, under no role marker"
                ),
            )
        return CodexReply(whole, FROM_WHOLE_STDOUT, 0, chars, reply_chars=len(whole))

    if not segments:
        return CodexReply(
            "",
            FROM_NOTHING,
            0,
            chars,
            note=(
                f"stdout carries {markers} codex role marker(s) and no message "
                "under any of them — nothing to hand the contract"
            ),
        )

    if len(segments) > 1:
        return CodexReply(
            "",
            FROM_NOTHING,
            len(segments),
            chars,
            note=(
                f"stdout carries {len(segments)} codex messages; refusing to "
                "choose a verdict by position — send one reply per invocation"
            ),
        )

    return CodexReply(
        segments[0], FROM_SEGMENT, 1, chars, reply_chars=len(segments[0])
    )


__all__ = [
    "CODEX_ROLE_MARKER",
    "FROM_NOTHING",
    "FROM_SEGMENT",
    "FROM_WHOLE_STDOUT",
    "ROLE_MARKERS",
    "CodexReply",
    "codex_segments",
    "isolate_reply",
    "role_marker_count",
]
