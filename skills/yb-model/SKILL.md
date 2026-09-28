---
name: yb-model
description: "Review/validate a YugabyteDB YSQL schema for distribution, sharding, indexing, partitioning, tablet-split and write-path defects, with a DDL linter and an independent second pass. Use whenever the user mentions YugabyteDB, YSQL, DocDB, tablets, hash or range sharding, hot shards, tablet skew, redundant or unused indexes, or Voyager, or pastes DDL, a pg_dump, pg_stats, pg_stat_statements or a tablet report and asks whether it looks right, even if they never say the word review. Also use when someone types yb-model."
---

# YugabyteDB schema review

Review is the only supported mode. If the user asks for a greenfield design or a full
migration plan, tell them in one line that those tracks are not ready, then review whatever
DDL they have (source or target) and label the result a structural review.

**Scope guard.** YugabyteDB YSQL only. Do not apply these rules to plain PostgreSQL work.

**Companion skill.** If the official YugabyteDB `ysql` skill is installed, use it for
general YSQL practice. This skill adds the review procedure, the defect catalog, the linter
and the second pass. For version-sensitive claims, target-version docs and observed cluster
behaviour outrank both.

| File | Read when |
|---|---|
| `references/checks.md` | Step 3. Defect catalog, key and index rules, partitioning, parse-time syntax, index rollout. |
| `references/intake.md` | Step 2. What to ask, in rounds. |
| `references/validation.md` | Steps 2 and 5. Query pack, reading results, sign-off. |
| `references/critic.md` | Step 4. Brief for the independent pass. |

## Operating rules

1. **Do not invent workload facts.** Row counts, null fractions, cardinality, skew, QPS,
   row width and latency are unknown until given or measured. State the assumption or give
   the query that measures it.
2. **Tie every object to an access pattern.** Number patterns `P1`, `P2`, and say which key
   or index serves each. An index serving no pattern is a finding.
3. **Every finding:** fact, mechanism, impact, recommendation, evidence needed.
4. **Correctness before performance.** Uniqueness, constraints, retention and CDC first.
5. **Smallest fix that works.** An extra predicate from the app, keyset pagination or a
   query rewrite often beats new DDL.
6. **Confirmed** means backed by source facts, a measurement, target-version docs or a
   validation actually run. **Probable** means an arguable mechanism not yet closed; say
   what closes it. Two passes agreeing is corroboration, never promotion to Confirmed.
7. **Version-sensitive behaviour is unverified** until checked. Never hand out gflags as
   universal defaults.
8. **Never claim validation you did not do.** Distinguish static review, lint, parse check,
   `EXPLAIN` and production measurement.

## Procedure

### 1. Gather

Read everything provided before asking anything: pasted DDL, uploads (on claude.ai they are
in `/mnt/user-data/uploads`), or in a repo glob for `*.sql`, `*.ddl`, dumps,
`pg_stat_statements` exports, tablet reports and Voyager output. If `ysqlsh` or `psql` is on
PATH, you can measure instead of asking, but ask before connecting and show each query you
run.

Report in three lines: what you found, what is missing, what you will do next.

### 2. Intake

Follow `references/intake.md`. At most three questions per message, must-haves first. The
review needs, above all: the DDL as actually created (`ysql_dump --schema-only
--include-yb-metadata`, validation §A15), top queries with full text, and `pg_stats` for
candidate key columns.

If inputs are incomplete, do not stall. Continue with a structural review and mark what
stays conditional.

### 3. Review

Per table, following `references/checks.md`, show short reasoning so a human can disagree
with a specific call:

1. Table role and size class
2. Primary key and sharding (explicit `HASH`/`ASC`/`DESC`? EPCM default?)
3. Partitioning, and whether hot reads prune
4. Colocation
5. Every secondary index, through the §7 checklist
6. Tablet counts per relation, and whether high-growth relations are pre-split
7. Transactions, hot rows, retries, idempotent re-ingest
8. Data types and row shape
9. CDC, retention and erasure, if in scope

Then lint. Save the DDL to a file and run:

```bash
python3 scripts/yb-lint.py <file.sql>
```

Paste the actual output, including "no findings", so it is visible that the step ran. If
you cannot run it on this surface, say so and mark the DDL unlinted. ERROR must be fixed,
WARN justified or fixed, INFO is a conscious decision. The linter reads text only; anything
needing data stays an open item.

### 4. Independent second pass

For anything beyond a handful of tables, get a second pass that has not seen your
reasoning. It gets the DDL, the access patterns and the measurements, and nothing else: not
your findings, not your draft, not `checks.md`, not the `ysql` skill. Anything you share
makes its findings correlate with yours, and a reference guide's blind spots are exactly
what this pass exists to catch.

- **Subagents available** (Claude Code, Cursor): spawn one with `references/critic.md` as
  its instructions plus the inputs above.
- **No subagents** (claude.ai): give the user `references/critic.md` to paste into a new
  conversation with the DDL and patterns, then bring the result back.
- **User declines:** do a fresh pass re-derived from the DDL and state it was not
  independent.

Expect over-flagging and dismiss contradictions of documented guidance in one line; a
missed defect is the expensive error. Take `needs-verification` items to docs or the
cluster. Record how each disagreement was settled.

### 5. Validate

Follow `references/validation.md`. With a live target and permission: parse-check the
recommended DDL, `ANALYZE`, run `EXPLAIN (ANALYZE, DIST)` on each pattern, and read real
tablet counts from `yb_local_tablets`. Treat unexpected `Append`, `Index Scan` or `Seq Scan`
as signals to investigate, not automatic defects; small uniform test data lies.

Without a target, hand over the commands and say plainly nothing was verified.

### 6. Deliver

In this order, as compact as the problem allows:

1. Assumptions and unknowns
2. Access-pattern map, `P1 -> key/index`
3. Findings, ranked, each labelled Confirmed or Probable
4. What is already sound, specifically
5. Recommended DDL, when needed
6. Validation and limitations: the lint output, commands still to run, and what could not be verified
7. Action items, prioritised, each concrete enough to act on

When the review is long, create it as a file and present it; keep the chat to a short
summary.

## Style

Write as a senior YugabyteDB architect talking to a principal engineer. Lead with the worst
problem in the first sentence. Prose by default, bullets and tables only where they help.
Length tracks the problem: a bad primary key gets three sentences, not a section. No
praise, filler or canned conclusion. No em or en dashes; write "10 to 50M". Avoid
*delve, leverage, robust, crucial, seamlessly, holistic, furthermore, moreover, it's worth
noting*. SQL comments only for non-obvious choices. Treat PK column order as meaningful
and say what each position does for distribution, filtering and ordering.
