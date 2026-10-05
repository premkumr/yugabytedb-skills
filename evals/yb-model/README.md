# yb-model evals

These evals compare two revisions of the skill (for example `main` and a change under review)
across models on the same evidence bundle. We score each run against an answer key.

## Layout

```
fixtures/<name>/
  bundle/            what the skill under test sees: schema.sql + ybm_*.csv (collect.sql output)
  answer-key.json    planted or known defects, with "engine_match" and "judge" text, plus traps
  build.sh           optional: rebuilds bundle/ on a scratch container (synthetic fixtures)
  setup.sql, load.sql, workload.sql   optional: inputs to build.sh
judge.md             grading brief for the blind judge
prepare.sh           creates isolated run directories
```

Only `bundle/` is copied into a run directory. The model under test never sees the answer
key, setup SQL or workload.

## Adding a real-world fixture

1. Capture against the customer database (read-only):

   ```bash
   ysqlsh -h <host> -U <user> -d <db> -f skills/yb-model/scripts/collect.sql
   ysql_dump -h <host> -U <user> -d <db> --schema-only --include-yb-metadata > schema.sql
   ```

2. Put the files in `fixtures/<name>/bundle/`. Strip anything confidential from
   `ybm_pss.csv` query text first. Literals are already normalised to `$n`.
3. Write `answer-key.json` from what the case actually established: the ticket, the RCA,
   what the customer changed. List traps too: things a review should not claim.

## Running a comparison

```bash
evals/yb-model/prepare.sh <fixture> <run-root>   # creates <run-root>/<fixture>/<skill>-<model>/bundle
```

Then, for each `<skill>-<model>` directory, start one agent with the model under test. Use
the prompt in `judge.md` §Runner, with SKILL_DIR pointing at the worktree for that skill
version:

- `git worktree add ../yugabytedb-skills-base main` for the base revision.
- This checkout for the change.

When all runs have written `final.md`, give the judge (one strong model) the answer key and
the `final.md` files, anonymised as A, B, C and so on. The judge writes `scores.json`.

For the engine alone, no model is involved:

```bash
python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py'
```

That test asserts byte-identical output across runs and full recall on the answer key's
`engine_match`.
