# Reading the engine output

Contents: 1) severity and confidence · 2) pattern weight · 3) rule families · 4) replay ·
5) what the engine does not do

## 1. Severity and confidence

Severity is the rule's base severity from `rules/rules.json`, shifted down by fixed amounts:

- **Traffic weight:** one level for a WARM pattern, two for a COLD one.
- **Table size:** one level below 1M rows, two below 10k rows.

Thresholds are in `THRESHOLDS` in `scripts/ybm/analyze.py` and are printed in `review.json`.

| Confidence | Meaning |
|---|---|
| `confirmed` | Follows from a measurement (`pg_stats`, `pg_stat_statements`, index usage) or from DDL text. |
| `confirmed (replay)` | Static prediction matched by the plan on a scratch cluster of the customer's version with their statistics injected. |
| `confirmed (measured)` | Static prediction corroborated by DocDB counters for the same pattern (rows scanned vs returned, read RPCs per call). |
| `probable` | Static prediction only. The *Evidence needed* line says what closes it. |

Two models agreeing is never a reason to upgrade `probable`.

## 2. Pattern weight

With `pg_stat_statements`, patterns are ranked by total execution time:

- **HOT:** the patterns that cover the first 80% of total time (at most 25 of them), plus
  the 10 most-called patterns.
- **WARM:** at least 1% of time or calls.
- **COLD:** everything else.

Without `pg_stat_statements`, patterns come from `queries.sql`, are marked UNRANKED, and
severity is not traffic-weighted.

## 3. Rule families

| Prefix | Source | Example |
|---|---|---|
| `CAP` | Planner capability; independent of data | Hash group not fully bound, ORDER BY not served, non-covering index, no partition pruning |
| `STA` | `pg_stats` | NULLs on a hash lead, low-cardinality or skewed hash key, monotonic range lead |
| `WRK` | `pg_stat_statements`, `pg_stat_user_*` | Unused index, too many indexes on a write-hot table |
| `SPL` | Tablet counts | Large relation still on one tablet |
| `CFG` | `pg_settings` | Cost model off, default sharding is ASC |
| `PLN` | Replayed plans only | Seq Scan on a large table that no static rule predicted |
| `LINT-YB*` | `yb-lint.py` | Clause order, missing `TABLETS` keyword, redundant prefix index |
| `SAF` | Safety pass over the engine's own DDL | A fix that would lose a uniqueness guarantee, an ON CONFLICT target, or a plan |

Rules marked `"section": "hygiene"` in `rules.json` (WRK005 to WRK010) describe how the
application uses the database. The report lists them under *Workload hygiene*, keeps them out
of the headline, and words their fixes for the application team.

*Pinned by* names the regress output file and a fixed string in it. To read the test, open
`src/postgres/src/test/regress/expected/<file>` in yugabyte-db at the customer's release tag
and search for the string.

## 4. Replay

`plans.json` holds one `EXPLAIN (FORMAT JSON)` per pattern. Plans are generic
(`plan_cache_mode = force_generic_plan`), so parameters are costed with default selectivity
rather than one specific value. Settings are copied from the customer's `pg_settings`
unless `--mode on|off` forces the cost model.

During reconciliation, each static access-path finding is checked against the replayed plan
of its patterns:

- **Matched:** the finding becomes `confirmed (replay)`.
- **Contradicted:** the finding is removed and listed under *Findings removed because the
  replayed plan contradicted them*. Read that list: it is where the static rules are
  weakest.
- **Planner chose a different path:** the finding keeps `probable` and shows the chosen path.

Replay plans are estimates. Injected statistics do not reproduce cache state, network
latency, tablet leadership or contention.

### Rule probes

Some rules claim how YugabyteDB *executes* a plan, not which plan it picks (CAP001: a partial
hash key is a full scan; CAP012: an ordered walk with a filter reads until LIMIT matches;
CAP013: a row-comparison cursor is rechecked, not sought). Injected statistics cannot test
these, because empty tables give zero execution counters, so each such rule carries a `probe`
in `rules.json`. A probe has setup SQL that generates a few thousand rows, literal queries
(`claim`, usually a `control` with the fix), `applies` checks (the planner took the path the
rule is about) and `holds` checks over the measured counters.

During replay, the engine runs the probes of the rules that fired, in the same container (the
customer's release, or the nearest image in its line) under the same planner settings.
Verdicts: **holds** (the finding names the release and the counters), **refuted** (the
finding is withdrawn and listed with the counters), **inconclusive** (the finding says
unverified). Without replay, the finding says its mechanism is unverified on the release.
Nothing is stored: a new release is checked by running the review on it.
`yb-model.py probe --image <img>` runs the probes alone.

## 5. Safety pass

Before the report is written, `ybm/safety.py` applies each finding's DDL to a copy of the
schema, alone and then all together, and compares the result with the original:

- Every PRIMARY KEY and UNIQUE guarantee must survive, on the same or fewer columns and with
  a predicate that is no narrower. If a fix would lose one, the pass adds the UNIQUE index
  that keeps it, and marks the row `amended`.
- Every `INSERT ... ON CONFLICT (cols)` must still have a unique index on exactly those
  columns.
- Every ranked pattern is planned again. Losing an access path, a point lookup, index order
  or coverage is a regression.
- A pure `DROP INDEX` of an index that `pg_stat_user_indexes` shows was scanned is flagged.

Side effects that cannot be amended mechanically become `SAF001`, or `SAF002` when they only
appear once the fixes are combined.

## 6. Releases and deployment tools

No release number or release behaviour is written by hand in the engine or the rules. Release
facts live in two generated files. Both are local caches: they are built on the machine that
runs reviews, for the releases being reviewed, and are never committed (`.gitignore`):

- `rules/versions.json`, built by `scripts/extract-version-data.py` from the yugabyte-db
  source of every release tag (local checkout via `git show`, or GitHub for one release).
  Per release: planner setting defaults (compiled default, overridden by the tserver's
  PG-flag default), server flag defaults, whether each rule's test citation exists, and the
  planner settings each deployment tool injects, discovered by scanning `bin/yugabyted`
  (default path vs the Enhanced PG Compatibility path) and the YBA universe-creation code.
- `rules/observations.json`, written by `evals/yb-model/oracle.py`: which releases and
  planner modes the static model was checked on, and per-rule observations such as how
  often the planner chose a partial hash-key Index Scan and how many rows it really read.

The engine resolves the customer's release to the newest table entry at or below it, then:

- fills settings missing from the bundle only when every deployment profile agrees;
- records a setting as conditional, with each profile's value, when they differ;
- lists settings absent on the release and uses a rule's `fix_if_unavailable` when a fix
  `requires` one of them;
- marks findings whose rule is not pinned by a test on that release;
- says when the planner model has not been checked by the oracle on that release;
- emits `RELEASE-DATA-MISSING` with the exact `update-versions --github` command when the
  release is not in the table. The skill asks the user before running it (network).

Replay picks the exact image, or else the nearest image of the same release line. It sets
the customer's effective planner settings explicitly (the container's launcher defaults never
apply), records the drift between the two releases, and does not let replay confirm a rule
whose tested behaviour differs between them.

## 7. Self-checks

- **Fixpoint** (`yb-model.py fixpoint`, also run by `review`): apply every recommended DDL,
  review again; fixed findings must be gone, nothing new may appear, and a second round
  must change nothing.
- **Planner oracle** (`evals/yb-model/oracle.py`, maintainer tool): generates hundreds of
  seeded schema and query cases, plans them on a real YugabyteDB with injected statistics,
  and compares the planner's choices with the engine's static model. Capability
  disagreements are engine bugs or version differences.

## 8. Missing inputs: preflight and muting

`preflight.py` detects each input listed in `rules/inputs.json`; that file says, per input,
what the review loses without it. Three effects:

- **Muted rule:** not evaluated, because it would conclude from a guess. A candidate the rule
  matched anyway is recorded as *withheld* and named in the report, so the user sees what
  collecting the input would settle. Example: without pg_stat_statements, a query list is
  unranked and may be partial, so CAP050 / CAP051 (re-key or swap a primary key, the most
  invasive DDL the engine writes) are withheld and the lighter CAP020 covering fix stands.
- **Weakened rule:** runs, and every finding it makes carries a caveat in its confidence
  (`pattern unranked`, `table size unknown`, `no pg_stats`, `planner settings not captured`,
  `sharding inferred`). `sharding inferred` applies only to findings on keys the DDL does not
  annotate.
- **Stage:** a workflow step skipped or reduced, for example replay without a release or
  workload, or drop-instead-of-rebuild without pg_stat_statements.

A query list whose first line is `-- ybm: workload-complete` (the user states nothing else
touches these tables) re-enables IDX001, the coverage-based unused-index check, with the caveat
`workload declared complete`. Duplicate and prefix-redundant indexes (linter YB052 / YB051) are
structural and run with or without a workload.

Inputs are *major* (they change findings) or *minor*. A major gap makes `preflight` exit 3 and
`review` refuse to run until `--accept-missing`, which the skill passes only after the user
says yes. When a broader input is missing, the narrower one is not listed again (no
pg_stat_statements implies no DocDB columns). Muting is applied in `analyze` regardless of the
flag, so `analyze`, `review` and the fixpoint self-check all see the same rule set.

## 9. What the engine does not do

- Measure latency or tablet sizes on the live cluster.
- Judge client retry behaviour, CDC, retention, or erasure.
- Parse every SQL construct. Unresolved columns and unparsed statements are listed as open
  items; they are never silently dropped.

These go under Limitations, or into Reviewer notes with `references/checks.md` as the
reference.
