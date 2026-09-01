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

**What a segment is.** The lines after a `codex` ROLE MARKER, up to the first
line that is transcript FURNITURE rather than message: another role marker, a
`hook:` line, the `tokens used` counter, or a separator rule. Those four shapes
are what the CLI prints around a message, and none of them can be produced by a
JSON directive — a JSON line begins with `{`, `"`, `[` or `}`, and the contract
already forbids a literal newline inside a string value, so a one-object reply
cannot contain a bare `hook:` or a rule of dashes at the start of a line.

**The trailing duplicate is excluded structurally, not by counting.** It sits
after `tokens used`, which closes the segment, so it is never inside one. This
is deliberately NOT "take the last object" or "de-duplicate identical objects":
both are the position rule the contract refuses, and the second one is worse —
it reads two objects that happen to agree as agreement, which stops being true
the moment they differ.

**The ECHO is the hard half, and "the segment after the `codex` marker" is not
on its own enough to exclude it.** The prompt comes back under the `user`
marker, and the prompt is the review packet: the response contract, the task
text, the candidate's diff, the agent's report. Text the loop SENT read back as
the reviewer's answer would be an approval the reviewer never gave — and this is
not hypothetical prose. The task that produced this module quotes the captured
transcript INSIDE its own description, so the packet for that very review
contains

        hook: UserPromptSubmit Completed
        codex
        {"version": 3, "decision": "push", ...}

A rule that only asked "is the previous line a hook line" would open a message
there. Three bounds answer it instead, and each is independently sufficient for
a different shape:

1. **A role marker starts at column 0** (`_role_marker`). The CLI prints its own
   markers flush left; a transcript quoted inside a prompt, a report or a diff
   is indented, bulleted or prefixed. That alone excludes the shape above.
2. **A role marker only opens a TURN at a turn boundary** (`_turn_markers`) —
   after furniture, or at the start. In the middle of a message a `codex` line
   is message text, which is what a packet's prose or a pasted log is.
3. **The echoed prompt is skipped outright when it is found verbatim**
   (`_anchor`). The loop knows what it sent, so it can start reading after it.

**The anchor is a bound, not a guarantee, and nothing depends on it.** Like
`quota.strip_echoed_prompt`, it matches the prompt EXACTLY: a codex build that
re-wraps or truncates its echo defeats it, and when it does, bounds 1 and 2
still stand and the outcome is a refusal rather than a wrong verdict. It also
never widens what is read — the region it hands on is a SUFFIX of stdout — and
it never fires unless a role marker survives it, so it cannot delete the reply.
Its state (`CodexReply.echo_anchor`) is recorded rather than inferred, for the
reason `quota.failure_digest` records `prompt_guard`: a bound that can quietly
not apply must say when it did not.

**When the prompt IS FOUND, every marker inside it is a REFUSAL, never a
fallback.** There is no reviewer message then — only ours coming back — and
reading it would be the echo failure in full: the packet states this round's own
`request_id` and `head_sha`, so the downstream stamp gates (`orchestrator` at
the head-SHA check) would pass an echoed example of THIS round. That shape
returns no verdict.

**What is NOT closed, stated rather than implied.** The refusal above needs the
prompt to be found. A REFLOWED echo (`quota.py` records that these happen) whose
packet quotes a flush-left transcript, in a round where the reviewer's own
message is empty, leaves exactly one segment and it is ours. Three conditions at
once, and the surface is strictly smaller than before this module (which passed
the whole transcript, echo included, to the parser), but it is not zero. It is
left open deliberately: the only discriminator available is whether the
segment's text is contained in the prompt, and that cannot tell an echo from a
reviewer who copied the example directive the prompt showed it — refusing there
would break a legitimate reply. `echo_anchor` says `unmatched` on exactly the
rounds where this applies, so the bound announces when it did not apply.

**Undecorated stdout is passed through unchanged.** When no role marker is
present at all, this is a build (or a fake) whose stdout is simply the reply,
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
#: message. A whole line at COLUMN 0, never a prefix and never indented:
#: `codex exec is running` is prose, and `    codex` is a transcript somebody
#: quoted — see `_role_marker`.
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

#: What the prompt-echo anchor did on this invocation. Recorded, never inferred.
#:
#: * `matched`   — the prompt was found verbatim and reading starts after it.
#: * `unmatched` — the prompt was not echoed verbatim (a build that re-wraps or
#:   truncates its echo), so reading covers the whole stdout and only the line
#:   rules exclude the echo.
#: * `inert`     — no prompt was supplied, so there was never an echo to find.
#: * `swallowed` — the prompt was found and EVERY `codex` marker in stdout falls
#:   inside it. Not a fallback: no verdict, and the invocation fails.
ECHO_ANCHOR_MATCHED = "matched"
ECHO_ANCHOR_UNMATCHED = "unmatched"
ECHO_ANCHOR_INERT = "inert"
ECHO_ANCHOR_SWALLOWED = "swallowed"


@dataclass(frozen=True)
class CodexReply:
    """What one invocation's stdout yielded, and how it was decided.

    `text` empty means NO VERDICT WAS ISOLATED, and `note` says which way that
    happened. It never means "the reply was not valid" — this module does not
    judge that; `contract.parse_response` does.

    The counts are not decoration. A record saying `segments: 2` is the
    difference between "codex printed nothing" and "codex printed two messages
    and choosing between them is not this transport's call". `echo_anchor` is
    the same kind of fact one level up: it says whether the echoed prompt was
    excluded by position or only by the line rules.
    """

    text: str
    source: str
    segments: int
    stdout_chars: int
    reply_chars: int = 0
    note: str = ""
    echo_anchor: str = ECHO_ANCHOR_INERT


def _role_marker(line: str) -> str | None:
    """The role this line announces, or None if it announces nothing.

    Two conditions, and the second is a bound on the ECHO:

    * the whole line, trailing whitespace aside, is a known role name — so
      `codex exec is running` is prose, not a marker;
    * it starts at COLUMN 0. The CLI prints its own markers flush left. A
      transcript quoted inside a review packet is indented (this module's own
      task description indents one by four spaces), a diff prefixes it, a
      bulleted report bullets it. None of those is this run's transcript, and
      reading one as a marker is how the loop's own text becomes a verdict.

    The cost is stated: a codex build that indents or timestamps its markers
    yields no verdict here at all. That is fail-closed — a refused invocation
    with a record naming it, never a decision nobody made — and
    `docs/AUTOLOOP.md` says what to change if such a build appears.
    """
    if not line or line[0].isspace():
        return None
    name = line.rstrip()
    return name if name in ROLE_MARKERS else None


def _is_furniture(line: str) -> bool:
    """Is this line transcript FURNITURE — something the CLI printed around a
    message rather than part of one?

    Blank lines are not: a message may contain them, and a blank line ending a
    segment would truncate a pretty-printed directive at its first empty line.
    Trailing blanks are removed by the strip at the end instead.

    A role-marker-shaped line is furniture whether or not it opens a turn (see
    `_turn_markers`): it ENDS an open message either way, which is the
    conservative direction — a message that runs on past its real end reaches
    the contract as two objects and is refused there.
    """
    if _role_marker(line) is not None:
        return True
    stripped = line.strip()
    if not stripped:
        return False
    return (
        _HOOK_LINE.match(stripped) is not None
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


def _turn_markers(lines) -> dict[int, str]:
    """`{line index: role}` for the markers that OPEN a turn.

    A role marker opens a turn only at a turn BOUNDARY: at the start of the
    text, or after furniture. In the middle of a message — which is what the
    echoed prompt is, all of it — a line that reads `codex` is message text,
    because that is what a packet quoting a log, a report naming the provider or
    a pasted transcript actually is.

    An unqualified marker-shaped line still leaves a boundary behind it. Without
    that, `codex / {verdict} / user / codex / {second verdict}` would drop the
    second reviewer message silently and isolate one segment out of two — which
    is the position rule the contract refuses, arrived at by an accounting slip
    rather than a decision. Two messages must stay two.
    """
    markers: dict[int, str] = {}
    at_boundary = True
    for index, line in enumerate(lines):
        if not line.strip():
            # A blank line neither opens nor closes a turn. `hook: Stop`,
            # blank, `codex` is one boundary with a gap in it.
            continue
        role = _role_marker(line)
        if role is not None:
            if at_boundary:
                markers[index] = role
            at_boundary = True
            continue
        at_boundary = _is_furniture(line)
    return markers


def role_marker_count(stdout: str) -> int:
    """How many `codex` markers in `stdout` open a turn.

    Counted separately from the segments themselves so "this output is not
    decorated" and "this output is decorated and the segment was empty" stay
    two different answers. Collapsing them is how a fallback becomes a guess:
    the first is stdout that IS the reply, the second is a reply that is missing.
    """
    if not isinstance(stdout, str) or not stdout:
        return 0
    return sum(
        1
        for role in _turn_markers(stdout.splitlines()).values()
        if role == CODEX_ROLE_MARKER
    )


def codex_segments(stdout: str) -> tuple[str, ...]:
    """Every non-empty `codex`-role segment in `stdout`, in order.

    Order is recorded because it is the natural reading of the transcript, and
    it is never USED to choose between two of them — see `isolate_reply`, which
    refuses rather than picking.
    """
    if not isinstance(stdout, str) or not stdout:
        return ()
    lines = stdout.splitlines()
    markers = _turn_markers(lines)
    segments: list[str] = []
    current: list[str] | None = None
    for index, line in enumerate(lines):
        role = markers.get(index)
        if role is not None:
            # Any turn closes the one before it; only a `codex` turn opens a
            # segment. A `user` turn opening nothing is what keeps the echoed
            # prompt out of the answer without depending on where it sits.
            if current is not None:
                segments.append("\n".join(current))
                current = None
            if role == CODEX_ROLE_MARKER:
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


def _echo_candidates(prompt: str) -> tuple[str, ...]:
    """The exact strings worth searching for as an echo, longest first. Same
    shape as `quota._echo_candidates`, and the same caveat: EXACT matches only."""
    seen: set[str] = set()
    out: list[str] = []
    for candidate in (prompt, prompt.strip()):
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return tuple(sorted(out, key=len, reverse=True))


def _anchor(stdout: str, prompt) -> tuple[str, str]:
    """`(the text to read the reply out of, what the anchor did)`.

    The loop knows what it sent, so the surest way to keep the echoed prompt out
    of the answer is not to read it at all. When the prompt is found verbatim,
    reading starts after its FIRST occurrence — the initial echo, which precedes
    every reviewer turn.

    The cut applies to the UNDECORATED pass-through as well, which is the case
    it is least obviously needed for and most quietly needed in: with no role
    marker anywhere, the whole text goes to the contract, and the contract reads
    a lone fenced ```json block wherever it sits — so an echoed EXAMPLE would be
    the directive. Cutting is safe because the match is the whole prompt, never
    a fragment: a reply that quotes the entire prompt and then answers is cut in
    the right place, and a "reply" that is only the echo cuts to nothing, which
    is reported as no reply rather than parsed.

    One outcome is not a narrowing but a refusal. If the prompt is found and
    every `codex` marker in stdout falls inside it, there is no reviewer message
    at all (`swallowed`). Falling back to the whole text there would hand the
    contract the loop's own example directive, carrying this round's real
    `request_id` and `head_sha` — which the downstream stamp gates would accept.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return stdout, ECHO_ANCHOR_INERT
    for candidate in _echo_candidates(prompt):
        at = stdout.find(candidate)
        if at < 0:
            continue
        region = stdout[at + len(candidate) :]
        if not role_marker_count(region) and role_marker_count(stdout):
            return region, ECHO_ANCHOR_SWALLOWED
        return region, ECHO_ANCHOR_MATCHED
    return stdout, ECHO_ANCHOR_UNMATCHED


def isolate_reply(stdout: str, prompt: str = "") -> CodexReply:
    """The reviewer's message in `stdout`, or nothing with a reason why.

    `prompt` is the text this invocation SENT. It is optional because the line
    rules stand on their own and every outcome below is reachable without it —
    it narrows where they are applied, it never relaxes one. `codex exec` echoes
    the prompt back, so passing it is how a caller says which half of the
    transcript is its own; `CodexConversation.submit` does.

    Five outcomes, and only the first two carry text:

    * exactly one `codex` segment — that segment, decoration removed;
    * no `codex` marker at all — the stripped text, unchanged, which is what
      this transport did before this module existed. "The text" is the whole of
      stdout, or what follows the echo when `_anchor` found it: with no marker
      to delimit anything, the echo is the only part that can be excluded, and
      the contract reads a fenced block wherever it sits;
    * every `codex` marker inside the echoed prompt — no verdict;
    * a marker whose segment is empty, nothing on stdout at all, or stdout that
      is furniture and nothing else — no verdict;
    * two or more segments — no verdict, because choosing by position is the
      rule the contract refuses and this is not the layer to break it in.
    """
    raw = stdout if isinstance(stdout, str) else ""
    chars = len(raw)
    region, anchor = _anchor(raw, prompt)

    if anchor == ECHO_ANCHOR_SWALLOWED:
        return CodexReply(
            "",
            FROM_NOTHING,
            0,
            chars,
            note=(
                "every codex role marker on stdout falls inside the echoed "
                "prompt — this run printed our own text back and no reviewer "
                "message"
            ),
            echo_anchor=anchor,
        )

    markers = role_marker_count(region)
    segments = codex_segments(region)

    if markers == 0:
        whole = region.strip()
        if not whole:
            # Kept verbatim from before this module: a clean exit with nothing
            # on stdout is a broken invocation, and the wording is what an
            # operator greps for.
            return CodexReply(
                "",
                FROM_NOTHING,
                0,
                chars,
                note="exited 0 with no reply on stdout",
                echo_anchor=anchor,
            )
        if not any(_is_message_line(line) for line in region.splitlines()):
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
                echo_anchor=anchor,
            )
        return CodexReply(
            whole,
            FROM_WHOLE_STDOUT,
            0,
            chars,
            reply_chars=len(whole),
            echo_anchor=anchor,
        )

    if not segments:
        return CodexReply(
            "",
            FROM_NOTHING,
            0,
            chars,
            note=(
                f"stdout carries {markers} codex role marker(s) and no message "
                f"under any of them — nothing to hand the contract "
                f"(prompt echo: {anchor})"
            ),
            echo_anchor=anchor,
        )

    if len(segments) > 1:
        return CodexReply(
            "",
            FROM_NOTHING,
            len(segments),
            chars,
            note=(
                f"stdout carries {len(segments)} codex messages; refusing to "
                f"choose a verdict by position — send one reply per invocation "
                f"(prompt echo: {anchor})"
            ),
            echo_anchor=anchor,
        )

    return CodexReply(
        segments[0],
        FROM_SEGMENT,
        1,
        chars,
        reply_chars=len(segments[0]),
        echo_anchor=anchor,
    )


__all__ = [
    "CODEX_ROLE_MARKER",
    "ECHO_ANCHOR_INERT",
    "ECHO_ANCHOR_MATCHED",
    "ECHO_ANCHOR_SWALLOWED",
    "ECHO_ANCHOR_UNMATCHED",
    "FROM_NOTHING",
    "FROM_SEGMENT",
    "FROM_WHOLE_STDOUT",
    "ROLE_MARKERS",
    "CodexReply",
    "codex_segments",
    "isolate_reply",
    "role_marker_count",
]
