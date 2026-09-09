"""Strict output contract for audit agents.

Agents must emit one JSON object {"findings": [...]} (a bare array is also
accepted). Every finding is validated field-by-field; an item that fails
validation becomes a RejectedItem with the exact reason — it is never guessed
into shape, and one bad item does not sink the batch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SEVERITIES = ("critical", "high", "medium", "low", "info")
CONFIDENCES = ("confirmed", "probable", "speculative")
CATEGORIES = (
    "defect",
    "security",
    "data_loss",
    "architecture",
    "doc_drift",
    "missing_test",
    "improvement",
    "human_decision",
    "style",
)

#: The keys every finding MUST carry. Unchanged since the contract was written:
#: an item missing one of these is rejected with "missing keys", and adding to
#: this set would refuse every report an agent has ever produced.
_REQUIRED_FINDING_KEYS = {
    "id",
    "category",
    "severity",
    "confidence",
    "affected_files",
    "symbols",
    "evidence",
    "impact",
    "proposed_action",
    "dependencies",
    "acceptance_criteria",
    "validation_commands",
    "safe_to_parallelize",
}

#: The keys a finding MAY carry (ctx-06). Optional, and that is a compatibility
#: decision rather than a softening: an agent that has never heard of them emits
#: a valid finding, and one that has can say the four things a fresh session
#: cannot otherwise reconstruct — what the code does TODAY and where that was
#: read, what is being assumed, what is still open, and which context records
#: were consulted.
#:
#: `current_behaviour` and `current_behaviour_citation` are a PAIR by design.
#: Stating what the code does now and declining to say where you read it is the
#: exact shape `taskgen` refuses (`Finding.claims`), so an agent that cannot cite
#: it writes it under `assumptions` instead — where it renders as an assumption
#: and is never asserted as fact.
#:
#: `context_ids` are REFERENCES and make no claim: they are rendered so a reader
#: can go and check the records, and nothing here or in `taskgen` believes one.
#: Same rule `Task.context_ids` already states — a citation of a record is not a
#: reading of the tree.
_OPTIONAL_FINDING_KEYS = {
    "current_behaviour",
    "current_behaviour_citation",
    "assumptions",
    "open_questions",
    "context_ids",
}

#: Every key an item may carry — the union. `unknown keys` is judged against
#: this; `missing keys` against `_REQUIRED_FINDING_KEYS` alone.
_FINDING_KEYS = _REQUIRED_FINDING_KEYS | _OPTIONAL_FINDING_KEYS

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# Structural bounds on the free-text fields. These are NOT truncation limits:
# nothing is ever shortened here. A finding that exceeds one is held as
# `OversizedFinding` and sent back to its agent to be re-expressed within the
# schema it was already given — see `ParseOutcome.oversized` and the executor's
# single bounded reshape round.
#
# The numbers come from measuring a real audit report (2026-07-30): 28 finding
# blocks, median 1,464 characters, and ONE block at 21,022 — 32% of all finding
# bytes in a single item. That outlier is not a verbose finding, it is prose
# written into a field specified as "quote code/doc lines". These bounds are set
# well above the median so they catch that shape and nothing else; a normal
# finding never comes near them.
MAX_EVIDENCE_CHARS = 700
MAX_IMPACT_CHARS = 400
MAX_PROPOSED_ACTION_CHARS = 300
MAX_ACCEPTANCE_ITEM_CHARS = 200
#: Whole-finding budget across the free-text fields, so a report cannot be
#: inflated by many individually-legal fields.
MAX_FINDING_CHARS = 2200

FINDINGS_SCHEMA_TEXT = """\
Return EXACTLY one JSON object (fenced in ```json ... ``` or bare):
{"findings": [
  {
    "id": "short unique id within your report, e.g. sec-01",
    "category": "defect | security | data_loss | architecture | doc_drift | missing_test | improvement | human_decision | style",
    "severity": "critical | high | medium | low | info",
    "confidence": "confirmed (you verified it in the code) | probable | speculative",
    "affected_files": ["repo-relative paths"],
    "symbols": ["function/class or file:line-range, e.g. progression_service.apply_progression:178-210"],
    "evidence": "file:line references to what you saw, plus AT MOST one sentence — cite, do not transcribe (max 700 chars)",
    "impact": "one sentence: what breaks and for whom (max 400 chars)",
    "proposed_action": "one concrete, scoped change (max 300 chars)",
    "dependencies": ["ids of findings that must be fixed first, usually []"],
    "acceptance_criteria": ["observable outcomes that prove the fix"],
    "validation_commands": ["exact commands that verify, e.g. 'ruff check .'"],
    "safe_to_parallelize": true,
    "current_behaviour": "OPTIONAL: what the code does TODAY, in one sentence",
    "current_behaviour_citation": "OPTIONAL but REQUIRED if you wrote current_behaviour: the path:line you read it at",
    "assumptions": ["OPTIONAL: what you are taking for granted but did not verify"],
    "open_questions": ["OPTIONAL: what you could not settle"],
    "context_ids": ["OPTIONAL: context records you consulted; references, not evidence"]
  }
]}
Rules: every field above `safe_to_parallelize` is required; the five after it
are optional. State `current_behaviour` ONLY with the `path:line` you read it
at — an uncited claim about what the code does today is REFUSED by name and the
finding is not promoted. If you cannot cite it, write it under `assumptions`
instead, where it is carried as an assumption rather than as a fact. Nothing you
remember from a conversation is evidence; only what you read in this tree is.
Do not report style opinions as defects — use
category "style" and they will not become tasks. Use confidence "speculative"
honestly; speculation is recorded but never promoted. An empty {"findings": []}
is a valid answer for a clean domain.

Be concise per finding. `evidence` cites locations — `path:line` — it does not
reproduce the code; the reviewer can open the file. If one finding needs more
than a few hundred characters of prose, it is almost always several findings
that belong apart, so split it. An over-long finding is not discarded: it is
sent back to you once to re-express, which costs a whole extra agent run."""


def scope_subject(qualified_id: str) -> str:
    """THE subject every source's SCOPE claim about one finding shares (ctx-06).

    One spelling, in one place, because a conflict is only ever DETECTED when
    two sources name the same subject: the finding's own scope-bearing claim and
    the accepted task that already declares a scope for it have to agree on this
    string or a real disagreement is silently invisible. `audit/taskgen.py` is
    the other caller.

    Every OTHER question a finding speaks to gets a `#suffix` off this — the
    proposed action, the current behaviour, each assumption — so that a source
    disagreeing about scope produces ONE record naming both sides, rather than
    one per sentence the finding happens to contain.
    """
    return str(qualified_id)


@dataclass(frozen=True)
class Finding:
    id: str
    category: str
    severity: str
    confidence: str
    affected_files: tuple[str, ...]
    symbols: tuple[str, ...]
    evidence: str
    impact: str
    proposed_action: str
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    validation_commands: tuple[str, ...]
    safe_to_parallelize: bool
    domain: str = ""
    #: What the code does TODAY, and where that was read (ctx-06). A pair: see
    #: `_OPTIONAL_FINDING_KEYS`, and `claims` below for what an unpaired one
    #: costs.
    current_behaviour: str = ""
    current_behaviour_citation: str = ""
    #: What this finding is TAKING FOR GRANTED rather than asserting, and what
    #: it could not settle. Both are rendered into the task; neither is evidence.
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    #: Context records consulted. REFERENCES, never evidence — see
    #: `_OPTIONAL_FINDING_KEYS`.
    context_ids: tuple[str, ...] = ()

    @property
    def qualified_id(self) -> str:
        return f"{self.domain}:{self.id}" if self.domain else self.id

    def claims(self) -> tuple:
        """Every REPOSITORY CLAIM this finding makes, as `inbox.Claim`s (ctx-06).

        What `audit/taskgen.generate_tasks` validates before it will propose a
        task, and what it compares against the other sources. Extends the
        existing evidence primitives rather than adding a second system: each
        claim's support is an `inbox.Evidence`, whose `source` must name a real
        reader.

        THE READER IS THE REPORT, and the location is what makes citing it
        honest. A finding is agent-authored, so "the audit report" is a reader of
        a real file whose CONTENT is a model's assertion — `inbox.
        LOCATION_REQUIRED_READERS` therefore admits it only when the cited text
        names a `path` or `path:line` a reviewer can open, and `taskgen` renders
        it attributed ("as the audit cited") rather than asserted. That is the
        difference between quoting a claim and making one.

        `affected_files` is deliberately NOT accepted as the location that
        satisfies the evidence claim, though it IS carried as the claim's
        `paths`. The parser already requires that field to be non-empty, so
        accepting it would make the citation check pass for every finding ever
        written — a guard that cannot fail, which is the fail-open this exists to
        close. `evidence` and `symbols` are where a location has to appear.
        """
        from ..inbox import (
            CLAIM_BEHAVIOUR,
            CLAIM_INTENT,
            SOURCE_REPOSITORY,
            Claim,
            Evidence,
        )

        report = "the audit report"
        subject = scope_subject(self.qualified_id)
        out = [
            # WHAT WAS SEEN. The `evidence` field, cited to the report line it
            # was written on, and carrying the files it puts in scope — which is
            # what makes a disagreement with an accepted task's `approved_paths`
            # measurable rather than unknown.
            Claim(
                text=self.evidence,
                source=SOURCE_REPOSITORY,
                subject=subject,
                kind=CLAIM_BEHAVIOUR,
                citation=Evidence(
                    text=" ".join((self.evidence, *self.symbols)).strip(),
                    source=report,
                ),
                paths=self.affected_files,
            ),
            # WHAT IS WANTED. Not a statement about the tree, so it is not held
            # to the citation rule — unless it names a file, and then
            # `Claim.about_repository` flips and the report citation answers for
            # it. Both directions are covered without a special case.
            Claim(
                text=self.proposed_action,
                source=SOURCE_REPOSITORY,
                subject=f"{subject}#action",
                kind=CLAIM_INTENT,
                citation=Evidence(text=self.proposed_action, source=report),
                paths=self.affected_files,
                repository_specific=False,
            ),
        ]
        if self.current_behaviour.strip():
            # THE PAIR. With a citation this is the verified current behaviour a
            # fresh session needs; without one it is an uncited repository claim,
            # and `unsupported_claims` refuses it BY NAME. Nothing here supplies
            # a citation on the finding's behalf, and nothing promotes one of the
            # finding's `assumptions` to cover it: an assumption that was written
            # about something else is not a statement that THIS is unverified.
            citation = None
            if self.current_behaviour_citation.strip():
                citation = Evidence(
                    text=self.current_behaviour_citation, source=report
                )
            out.append(
                Claim(
                    text=self.current_behaviour,
                    source=SOURCE_REPOSITORY,
                    subject=f"{subject}#current-behaviour",
                    kind=CLAIM_BEHAVIOUR,
                    citation=citation,
                    paths=self.affected_files,
                )
            )
        # STATED ASSUMPTIONS. Always supported, by definition — saying out loud
        # that something is not known is the other half of the rule, and the
        # half that keeps an honest finding usable.
        out += [
            Claim(
                text=text,
                source=SOURCE_REPOSITORY,
                subject=f"{subject}#assumption-{i}",
                kind=CLAIM_BEHAVIOUR,
                assumption=text,
                paths=self.affected_files,
            )
            for i, text in enumerate(self.assumptions)
            if str(text).strip()
        ]
        return tuple(out)


@dataclass(frozen=True)
class RejectedItem:
    reason: str
    raw: str
    domain: str = ""


@dataclass(frozen=True)
class OversizedFinding:
    """A VALID finding whose free-text fields exceed the structural bounds.

    Deliberately neither accepted nor rejected: it is held, with its original
    item dict intact, so the agent can re-express it. Nothing about it is
    shortened, dropped, or silently let through — the three outcomes this class
    exists to prevent.
    """

    item: dict
    reasons: tuple[str, ...]
    finding_id: str
    domain: str = ""


def oversize_reasons(finding: "Finding") -> tuple[str, ...]:
    """Which structural bounds this finding exceeds (empty when it is fine)."""
    reasons = []
    if len(finding.evidence) > MAX_EVIDENCE_CHARS:
        reasons.append(
            f"evidence is {len(finding.evidence)} chars (max {MAX_EVIDENCE_CHARS}) — "
            "cite file:line references, do not transcribe the code"
        )
    if len(finding.impact) > MAX_IMPACT_CHARS:
        reasons.append(
            f"impact is {len(finding.impact)} chars (max {MAX_IMPACT_CHARS}) — "
            "one sentence on what breaks and for whom"
        )
    if len(finding.proposed_action) > MAX_PROPOSED_ACTION_CHARS:
        reasons.append(
            f"proposed_action is {len(finding.proposed_action)} chars "
            f"(max {MAX_PROPOSED_ACTION_CHARS}) — one concrete scoped change"
        )
    for criterion in finding.acceptance_criteria:
        if len(criterion) > MAX_ACCEPTANCE_ITEM_CHARS:
            reasons.append(
                f"an acceptance criterion is {len(criterion)} chars "
                f"(max {MAX_ACCEPTANCE_ITEM_CHARS}) — state one observable outcome"
            )
            break
    # The ctx-06 fields are inside the whole-finding budget, not outside it.
    # Adding free text to a finding without adding it to the sum would reopen
    # exactly the inflation path this measured bound was set for — one 21,022
    # character block, 32% of all finding bytes — behind four new field names.
    total = (
        len(finding.evidence)
        + len(finding.impact)
        + len(finding.proposed_action)
        + sum(len(c) for c in finding.acceptance_criteria)
        + len(finding.current_behaviour)
        + len(finding.current_behaviour_citation)
        + sum(len(a) for a in finding.assumptions)
        + sum(len(q) for q in finding.open_questions)
    )
    if total > MAX_FINDING_CHARS:
        reasons.append(
            f"the finding's free text totals {total} chars (max {MAX_FINDING_CHARS}) — "
            "split genuinely separate problems into separate findings"
        )
    return tuple(reasons)


@dataclass
class ParseOutcome:
    findings: list[Finding] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)
    #: Valid findings held back for ONE reshape round because their free text
    #: exceeds the structural bounds. Never dropped and never truncated: the
    #: executor either gets a compact re-expression or parks with the finding
    #: intact.
    oversized: list[OversizedFinding] = field(default_factory=list)
    #: False when the agent's output as a WHOLE was unusable (empty, not JSON,
    #: wrong top-level shape). That is a coverage GAP for the domain, not a
    #: per-item rejection: the agent may have found real problems we never got
    #: to see. Callers must report it as a failure rather than as "0 findings",
    #: or an entire domain vanishes behind a clean-looking summary.
    usable: bool = True


def _str_list(value: object, name: str, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{name}' must be a list of strings")
    items = tuple(v.strip() for v in value if v.strip())
    if not items and not allow_empty:
        raise ValueError(f"'{name}' must not be empty")
    return items


def _optional_str(item: dict, key: str) -> str:
    """One optional free-text field, type-checked when present (ctx-06).

    Absent is fine and means the agent did not answer that question — NOT that
    the answer is empty, and nothing downstream reads it as one: `Finding.claims`
    emits no claim for a `current_behaviour` nobody wrote, where an uncited one
    that IS written is refused by name.
    """
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    return value.strip()


def _validate_finding(item: dict, domain: str) -> Finding:
    unknown = set(item) - _FINDING_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
    # Against the REQUIRED set only: the ctx-06 fields are optional, so a report
    # written by an agent that has never heard of them is still a valid report.
    missing = _REQUIRED_FINDING_KEYS - set(item)
    if missing:
        raise ValueError(f"missing keys: {sorted(missing)}")
    for key in ("id", "evidence", "impact", "proposed_action"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ValueError(f"'{key}' must be a non-empty string")
    if item["category"] not in CATEGORIES:
        raise ValueError(f"category '{item['category']}' not in {CATEGORIES}")
    if item["severity"] not in SEVERITIES:
        raise ValueError(f"severity '{item['severity']}' not in {SEVERITIES}")
    if item["confidence"] not in CONFIDENCES:
        raise ValueError(f"confidence '{item['confidence']}' not in {CONFIDENCES}")
    if not isinstance(item["safe_to_parallelize"], bool):
        raise ValueError("'safe_to_parallelize' must be a boolean")
    return Finding(
        id=item["id"].strip(),
        category=item["category"],
        severity=item["severity"],
        confidence=item["confidence"],
        affected_files=_str_list(item["affected_files"], "affected_files", allow_empty=False),
        symbols=_str_list(item["symbols"], "symbols", allow_empty=True),
        evidence=item["evidence"].strip(),
        impact=item["impact"].strip(),
        proposed_action=item["proposed_action"].strip(),
        dependencies=_str_list(item["dependencies"], "dependencies", allow_empty=True),
        acceptance_criteria=_str_list(
            item["acceptance_criteria"], "acceptance_criteria", allow_empty=True
        ),
        validation_commands=_str_list(
            item["validation_commands"], "validation_commands", allow_empty=True
        ),
        safe_to_parallelize=item["safe_to_parallelize"],
        domain=domain,
        current_behaviour=_optional_str(item, "current_behaviour"),
        current_behaviour_citation=_optional_str(item, "current_behaviour_citation"),
        assumptions=_str_list(item.get("assumptions", []), "assumptions", allow_empty=True),
        open_questions=_str_list(
            item.get("open_questions", []), "open_questions", allow_empty=True
        ),
        context_ids=_str_list(item.get("context_ids", []), "context_ids", allow_empty=True),
    )


def parse_findings(text: str, domain: str) -> ParseOutcome:
    outcome = ParseOutcome()
    if not isinstance(text, str) or not text.strip():
        outcome.rejected.append(RejectedItem("empty agent output", raw="", domain=domain))
        outcome.usable = False
        return outcome
    blocks = _JSON_BLOCK.findall(text)
    candidate = blocks[-1] if blocks else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        outcome.rejected.append(
            RejectedItem(f"agent output is not valid JSON: {exc}", raw=text[:2000], domain=domain)
        )
        outcome.usable = False
        return outcome
    if isinstance(data, dict):
        if set(data) != {"findings"}:
            outcome.rejected.append(
                RejectedItem(
                    f"top-level object must have exactly the 'findings' key, got {sorted(data)}",
                    raw=candidate[:2000],
                    domain=domain,
                )
            )
            outcome.usable = False
            return outcome
        items = data["findings"]
    else:
        items = data
    if not isinstance(items, list):
        outcome.rejected.append(
            RejectedItem("'findings' must be a list", raw=candidate[:2000], domain=domain)
        )
        outcome.usable = False
        return outcome
    seen_ids: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            outcome.rejected.append(
                RejectedItem(f"findings[{i}] is not an object", raw=str(item)[:500], domain=domain)
            )
            continue
        try:
            finding = _validate_finding(item, domain)
        except ValueError as exc:
            outcome.rejected.append(
                RejectedItem(
                    f"findings[{i}]: {exc}", raw=json.dumps(item)[:500], domain=domain
                )
            )
            continue
        if finding.id in seen_ids:
            outcome.rejected.append(
                RejectedItem(
                    f"findings[{i}]: duplicate id '{finding.id}' within one report",
                    raw=finding.id,
                    domain=domain,
                )
            )
            continue
        seen_ids.add(finding.id)
        reasons = oversize_reasons(finding)
        if reasons:
            outcome.oversized.append(
                OversizedFinding(
                    item=item, reasons=reasons, finding_id=finding.id, domain=domain
                )
            )
            continue
        outcome.findings.append(finding)
    return outcome
