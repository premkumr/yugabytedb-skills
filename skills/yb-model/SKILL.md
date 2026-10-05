---
name: yb-model
description: "Review/validate a YugabyteDB YSQL schema for distribution, sharding, indexing, partitioning, tablet-split and write-path defects, with a DDL linter and an independent second pass. Use whenever the user mentions YugabyteDB, YSQL, DocDB, tablets, hash or range sharding, hot shards, tablet skew, redundant or unused indexes, or Voyager, or pastes DDL, a pg_dump, pg_stats, pg_stat_statements or a tablet report and asks whether it looks right, even if they never say the word review. Also use when someone types yb-model."
---

# YugabyteDB schema review

Review is the only supported mode. If the user asks for a greenfield design or a full
migration plan, say in one line that those tracks are not ready, then review whatever DDL
they have and label the result a structural review.

**Scope guard.** YugabyteDB YSQL only. Do not apply these rules to plain PostgreSQL work.

## How this skill works

The findings come from a deterministic engine, `scripts/yb-model.py`. Its rules live in
`rules/rules.json`, and each one cites the yugabyte-db regress test that pins the
behaviour. The same inputs give the same findings, severities, ranking and DDL, whichever
model runs the skill.

Your job is to collect the inputs, run the engine, write a short summary and say what was
not verified. **Do not add, drop, re-rank, re-word or re-grade findings yourself.** If you
believe a finding is wrong or something is missing, write it under *Reviewer notes* with the
finding ID, or "not in engine". Never edit the findings table by hand.

| File | Read when |
|---|---|
| `references/engine.md` | Step 4. What each output field means; how to read replay results. |
| `references/intake.md` | Step 1. What to ask for, and the exact capture commands. |
| `references/validation.md` | Step 5. Live-cluster checks the engine cannot do. |
| `references/checks.md` | Only for Reviewer notes, or when the engine cannot run. Background on mechanisms. |
| `references/critic.md` | Step 6, the optional second pass. |

## Procedure

Copy this checklist and tick it off.

```
- [ ] 1. Inputs gathered into one directory
- [ ] 1b. Preflight ran; if it asked, the user said yes or no
- [ ] 2. Engine ran (review.md and review.json exist)
- [ ] 3. Replay ran, or the reason it did not is recorded
- [ ] 4. Summary written into review.md
- [ ] 5. Limitations stated
- [ ] 6. (optional) Second pass recorded under Reviewer notes
```

### 1. Gather

Make one directory (the *bundle*) and put the DDL in `schema.sql`. Copy every file the user
gave you into it unchanged:

- The `ybm_*.csv` files from `scripts/collect.sql`.
- A `pg_stat_statements` export, saved as `ybm_pss.csv`.
- A `pg_stats` export, saved as `ybm_pg_stats.csv`.

If the user pasted queries but has no `pg_stat_statements`, write them to `queries.sql`,
one statement per `;`. Keep parameters typed the way the application sends them (for example
`$2::boolean` where a parameter appears only in `$2 IS NULL`), so replay can plan them. If, and
only if, the user or their document states that no other statement touches these tables, make
the first line `-- ybm: workload-complete`. Unused-index checks (IDX001) then run on the list,
marked as resting on that statement. Never add it on your own judgement.

### 1b. Preflight: confirm before reviewing with missing inputs

```bash
python3 <skill-dir>/scripts/yb-model.py preflight <bundle>
```

- **Exit 0:** nothing that changes the findings is missing. Go to step 2. If it printed minor
  gaps, mention them in one line.
- **Exit 3:** inputs that change the findings are missing. Show the printed text to the user
  **verbatim** (it names each missing input, what it mutes or weakens, and how to collect it)
  and ask **one yes / no question**: proceed with an incomplete review? Use the question tool
  if you have one. Then stop and wait. Never answer it yourself, never assume yes from earlier
  messages, and do not summarise or soften the list.
  - **Yes:** go to step 2 and add `--accept-missing`.
  - **No:** give the user the collect commands from the printed text (and
    `references/intake.md` §1 for the full capture), then stop. Run preflight again when
    they come back with the files.

`review` refuses to run (exit 3) without `--accept-missing` while a major input is missing.
Never pass that flag unless the user said yes in this conversation to this bundle's list.

What muting means: a rule whose inputs are missing is not evaluated, because its result would
rest on a guess (for example, no primary-key re-key from an unranked query list). The report
names the candidates it withheld. A weakened rule still runs, and each finding it makes
carries a caveat such as `sharding inferred`. The mapping lives in `rules/inputs.json`; do not
re-derive it.

### 2. Run the engine

```bash
python3 <skill-dir>/scripts/yb-model.py review <bundle>
```

Add no flags other than `--accept-missing` (step 1b). The engine runs replay by itself when Docker has a `yugabytedb/yugabyte`
image for the bundle's version (from `ybm_meta.csv`), and prints why when it cannot. Replay
does three things:

1. Starts that version in a scratch container.
2. Injects the customer's row counts and column statistics, the way the TAQO planner tests
   do.
3. Runs `EXPLAIN` on every pattern.
4. Runs the **probe** of every fired rule that has one: a few thousand generated rows and
   `EXPLAIN (ANALYZE, DIST)`, checking that the rule's mechanism holds on that release. A
   refuted rule's finding is withdrawn; the report says which. Probes never use customer data
   and store nothing.

Never `docker pull` without asking: the image is a download of about 1 GB. If the user
agrees to the pull, run the same command again afterwards.

The engine writes `review.md`, `review.json` and, with replay, `plans.json` into the
bundle. Exit codes:

- **0:** no findings at high or above.
- **1:** findings at high or above. This is not a failure.
- **2:** the engine failed. Show the error.
- **3:** inputs are missing and were not accepted. Go back to step 1b.

If the engine cannot run on this surface, say so and review by hand with
`references/checks.md`. Label the result **manual review, engine not run**.

### What the report contains

`review.md` always has these sections, in this order. Do not reorder or drop them:

1. **Headline**: the worst schema finding, its cost and the first action (engine-written).
2. **What this review is based on**: inputs, release, where each planner setting came from.
3. **Assumptions and unknowns**: missing inputs, settings that depend on how the cluster was
   deployed, features that do not exist on this release, and the **Muted checks** table
   (which rules were muted or weakened by which missing input, and the candidates withheld).
4. **Access patterns**: P1..Pn with weight, calls, time share, access path.
5. **Findings**: schema findings, ranked, each with fact, mechanism, fix and the test that
   pins it.
6. **Workload hygiene (confirm with the application team)**: how the application uses the
   database (lookups that find nothing, UPDATEs that match nothing or rewrite every column,
   full-table counts, fan-out, planning time). Not schema defects; hand these to the app
   owners.
7. **What is already sound.**
8. **Recommended DDL**, after the safety pass.
9. **Safety check of the recommendations.**
10. **Validation and limitations**, including replay and the self-check.
11. **Action items**, schema first, then the application team.

### Versions

Behaviour and defaults differ between YugabyteDB releases and between deployment tools on
the same release (yugabyted and YBA new universes turn the cost model on; a manual install or
an upgraded universe keeps `legacy_mode`). The engine reads these facts from a local cache,
`rules/versions.json`, built from the yugabyte-db source for the releases being reviewed
(it is not shipped with the skill). Your job is only to report what the engine says:

- Settings come from the bundle's pg_settings. Without them, the release default is used
  only when every deployment tool agrees; otherwise the finding is conditional and the open
  items say so. Ask for `ybm_settings.csv` rather than guessing.
- A fix that needs a feature the release lacks (for example merge scan streams) is replaced
  with one that works on that release.
- A finding whose behaviour no regress test pins on the customer's release is marked "not
  pinned by tests on <release>".
- Replay on an image that is not the customer's exact release lists the differences between
  the two releases, and does not confirm rules whose tested behaviour differs.
- If the open items contain `RELEASE-DATA-MISSING`, the cache has no entry for the customer's
  release (on a fresh install it is empty). If the user has a yugabyte-db checkout, run the
  `--repo` command quoted in that item with its path; it reads local files only. Otherwise
  **ask the user** whether you may fetch the release's source facts from GitHub, and run the
  `--github` command only if they agree. Then run the review again. If they decline, keep
  the review and say which release facts are unknown or come from the nearest release.

### 3. Replay

If replay failed or was skipped, give the reason in one sentence under Validation. Without
replay, access-path findings stay `probable`. Do not upgrade them by reasoning.

### 4. Summarise

The engine writes the headline (worst finding, its cost, first action). Keep it as it is.
Replace the `<!-- NOTES ... -->` line with at most two sentences of context that only the
user gave you, or delete the line. Do not write DDL, numbers or estimates of your own, and do not
add causes or effects (contention, latency, retries) that no finding states: every
statement, figure and claim in the review comes from the engine. Add a `## Reviewer notes` section
only when step 6 produced something or you disagree with a finding.

The report ends with a **Safety check** table: the engine applied every recommended DDL
to a copy of the schema and checked uniqueness, ON CONFLICT targets and the plans of every
ranked pattern. Rows marked `amended` already include the fix (for example an added UNIQUE
index). Never recommend DDL that bypasses a `warn` row; point the user at the SAF finding.

### 5. Limitations

The engine already lists the open items and what replay did. Add only facts from the user
that change the picture, for example "statistics are from staging".

### 6. Second pass (optional)

For a schema with more than a handful of tables, and when subagents are available, run the
critic in `references/critic.md` on the DDL and the pattern list only. For each item it
raises that no engine finding covers, add it under Reviewer notes as "second pass, not
verified by the engine". Do not merge these into the findings table.

### 7. Deliver

Give the path to `review.md`. In chat, give only:

- The headline, copied unchanged.
- The top three findings by ID, each with its fix, copied from the report.
- The review level line from the report. If it says INCOMPLETE, also list the missing
  inputs and the withheld candidates from the Muted checks table.

## Style

Write as a senior YugabyteDB architect talking to a principal engineer. Lead with the worst
problem. No praise, filler or canned conclusion. No em or en dashes. Avoid *delve, leverage,
robust, crucial, seamlessly, holistic, furthermore, moreover*. Refer to findings by ID
(`F3`) rather than restating them.
