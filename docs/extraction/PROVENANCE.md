# Extraction evidence

## `commit-map.tsv`

`git-filter-repo`'s own commit map from the 2026-08-27 extraction, copied
verbatim from `.git/filter-repo/commit-map`. 624 rows, header `old new`,
**space**-separated despite the `.tsv` name — the file is byte-for-byte what
filter-repo wrote, and re-formatting it would break the "copied verbatim"
property that makes it evidence rather than a transcription.

    old                                       new
    <pre-extraction sha>                      <post-rewrite sha, or 40 zeros>

Forty zeros in the `new` column means the rewrite PRUNED that commit: it touched
no path under `autoloop/`, so nothing of it exists in this repository.

### Why it is tracked

`.git/` is not cloned. Every worker repository the loop creates is a clone, so
an agent asked to reconcile pre-extraction work could not reach this file at
all — it exists only in an operator's original checkout. A task was blocked on
exactly that on 2026-08-30 (merge-08, round 3): the agent correctly reported the
map absent and 132 of 133 tasks unresolvable rather than inventing rows.

### What it is for

The extraction rewrote every commit but migrated no execution record, so each
record still names a `candidate_sha` from the old history — an object that does
not exist here. This map is the only link between the two. Chain:

    record candidate_sha -> commit-map -> new sha
      -> git merge-base --is-ancestor <new> autoloop/mainline

Every step is re-runnable locally. Nothing in it is a written claim.

### Regenerating it

Only reproducible from a checkout that still has the extraction metadata:

    cp .git/filter-repo/commit-map docs/extraction/commit-map.tsv

If that directory is gone, re-running the original extraction against the source
repository (`git filter-repo --path autoloop/` over `language-app`) reproduces
the same mapping, because the rewrite is deterministic given the same input.
