"""Tests for the yb-model engine. Standard library only.

    python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py' -v
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ybm import analyze, inputs, schema, sqlshape  # noqa: E402

FIXTURE = os.path.join(HERE, "..", "..", "..", "evals", "yb-model", "fixtures", "ecommerce",
                       "bundle")


def ops_of(sql, sch, table):
    sh = sqlshape.analyze(sql, sch)
    return sh, analyze._pred_index(sh.preds_for(table))


class SchemaModes(unittest.TestCase):
    def test_default_hash_then_asc(self):
        s = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a, b));")
        self.assertEqual([k.mode for k in s.tables["t"].pk.keys], ["HASH", "ASC"])

    def test_colocated_database_defaults_to_asc(self):
        s = schema.parse("CREATE TABLE t (a int PRIMARY KEY);", db_colocated=True)
        self.assertEqual(s.tables["t"].pk.keys[0].mode, "ASC")

    def test_colocation_false_keeps_hash(self):
        s = schema.parse("CREATE TABLE t (a int PRIMARY KEY) WITH (colocation = false);",
                         db_colocated=True)
        self.assertEqual(s.tables["t"].pk.keys[0].mode, "HASH")

    def test_hash_default_off(self):
        s = schema.parse("CREATE INDEX i ON t (a);", hash_default=False)
        self.assertEqual(s.indexes["i"].keys[0].mode, "ASC")

    def test_composite_hash_group(self):
        s = schema.parse("CREATE TABLE t (a int, b int, c int, PRIMARY KEY ((a, b) HASH, c DESC));")
        self.assertEqual(s.tables["t"].pk.signature(), "(a, b) HASH, c DESC")

    def test_ysql_dump_index_and_partition_attach(self):
        ddl = ("CREATE TABLE p (id bigint NOT NULL, ts timestamptz NOT NULL) PARTITION BY RANGE (ts)\n"
               "SPLIT INTO 1 TABLETS;\n\\if :use_roles\n    ALTER TABLE p OWNER TO x;\n\\endif\n"
               "CREATE TABLE p1 (id bigint NOT NULL, ts timestamptz NOT NULL, "
               "CONSTRAINT p1_pkey PRIMARY KEY((id) HASH));\n\\if :use_roles\n\\endif\n"
               "ALTER TABLE ONLY public.p ATTACH PARTITION public.p1 FOR VALUES FROM ('a') TO ('b');\n"
               "CREATE INDEX NONCONCURRENTLY pi ON public.p1 USING lsm (ts ASC) INCLUDE (id) "
               "SPLIT INTO 3 TABLETS;")
        s = schema.parse(ddl)
        self.assertEqual(s.tables["p"].partitions, ["p1"])
        self.assertEqual(s.indexes["pi"].include, ["id"])
        self.assertIn("p1_pkey", [i.name for i in s.indexes_on("p")])


class AccessPaths(unittest.TestCase):
    sch = schema.parse("""
        CREATE TABLE t (h1 int, h2 int, r int, v int, n int, PRIMARY KEY ((h1, h2) HASH, r ASC));
        CREATE INDEX t_v ON t (v HASH, r DESC) INCLUDE (n) WHERE v IS NOT NULL;
    """)

    def best(self, sql):
        sh, ops = ops_of(sql, self.sch, "t")
        paths = []
        for idx in self.sch.indexes_on("t"):
            r = analyze.eval_path(idx, ops, sh, "t", cbo=False)
            if r["usable"]:
                r["order_ok"] = analyze.order_ok(idx, ops, sh, "t")
                r["covering"], r["missing"] = analyze.covering(idx, sh, "t", self.sch.tables["t"].cols)
            paths.append(r)
        return analyze.choose(paths), {p["index"]: p for p in paths}

    def test_partial_hash_group_is_unusable(self):
        best, paths = self.best("SELECT * FROM t WHERE h1 = $1")
        self.assertIsNone(best)
        self.assertEqual(paths["t_pkey"]["status"], "hash_unbound")

    def test_range_on_hash_is_unusable(self):
        best, _ = self.best("SELECT * FROM t WHERE h1 = $1 AND h2 > $2")
        self.assertIsNone(best)

    def test_full_hash_orders_by_range(self):
        best, _ = self.best("SELECT * FROM t WHERE h1 = $1 AND h2 = $2 ORDER BY r DESC LIMIT 5")
        self.assertEqual(best["index"], "t_pkey")
        self.assertTrue(best["order_ok"])

    def test_partial_index_needs_implication(self):
        best, _ = self.best("SELECT n FROM t WHERE v = $1 ORDER BY r DESC")
        self.assertEqual(best["index"], "t_v")
        self.assertTrue(best["covering"])
        self.assertTrue(best["order_ok"])

    def test_mixed_direction_breaks_order(self):
        sch = schema.parse("CREATE TABLE u (a int, b int, c int, PRIMARY KEY (a HASH, b ASC, c ASC));")
        sh, ops = ops_of("SELECT * FROM u WHERE a = $1 ORDER BY b ASC, c DESC", sch, "u")
        self.assertFalse(analyze.order_ok(sch.tables["u"].pk, ops, sh, "u"))

    def test_in_on_hash_breaks_order(self):
        sch = schema.parse("CREATE TABLE u (a int, b int, PRIMARY KEY (a HASH, b DESC));")
        sh, ops = ops_of("SELECT * FROM u WHERE a IN ($1, $2) ORDER BY b DESC LIMIT 3", sch, "u")
        self.assertFalse(analyze.order_ok(sch.tables["u"].pk, ops, sh, "u"))

    def test_join_binds_inner_key(self):
        sch = schema.parse("CREATE TABLE a (id int PRIMARY KEY); CREATE TABLE b (a_id int, k int, "
                           "PRIMARY KEY (a_id HASH, k ASC));")
        sh = sqlshape.analyze("SELECT * FROM a JOIN b ON b.a_id = a.id WHERE a.id = $1", sch)
        self.assertEqual(sh.joins, [("b", "a_id", "a", "id")])


class QueryShapes(unittest.TestCase):
    def test_expression_predicate(self):
        sh = sqlshape.analyze("SELECT id FROM c WHERE lower(email) = $1")
        self.assertEqual([(p.expr, p.op) for p in sh.preds], [("lower(email)", "eq")])

    def test_between_and_like_prefix(self):
        sh = sqlshape.analyze("SELECT 1 FROM t WHERE a BETWEEN $1 AND $2 AND b LIKE 'x%' AND c LIKE '%x'")
        self.assertEqual(sorted((p.col, p.op) for p in sh.preds),
                         [("a", "range"), ("b", "prefix"), ("c", "like")])

    def test_cte_is_not_a_table(self):
        sh = sqlshape.analyze("WITH x AS (SELECT id FROM o WHERE c = $1) SELECT * FROM x")
        self.assertEqual(sh.tables, [])
        self.assertEqual(sh.subshapes[0].tables, ["o"])

    def test_aggregate_flag(self):
        self.assertTrue(sqlshape.analyze("SELECT count(*) FROM t WHERE a = 1").aggregate)


class Safety(unittest.TestCase):
    def setUp(self):
        from ybm import safety
        self.safety = safety

    def test_rekey_to_superset_loses_uniqueness(self):
        s = schema.parse("CREATE TABLE t (id int, p int, o int, PRIMARY KEY (id HASH));")
        after = self.safety.apply(s, self.safety.changes_of(
            "-- CREATE TABLE t_new (... PRIMARY KEY ((p) HASH, o ASC, id ASC)) SPLIT INTO 3 TABLETS;"))
        self.assertEqual([n for *_, n in self.safety.lost_uniqueness(s, after)], ["t_pkey"])
        after2 = self.safety.apply(s, self.safety.changes_of(
            "-- CREATE TABLE t_new (... PRIMARY KEY ((p) HASH, o ASC, id ASC));\n"
            "CREATE UNIQUE INDEX CONCURRENTLY t_id ON t ((id) HASH);"))
        self.assertEqual(self.safety.lost_uniqueness(s, after2), [])

    def test_non_unique_replacement_of_unique_index(self):
        s = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b HASH) WHERE b IS NOT NULL;")
        after = self.safety.apply(s, self.safety.changes_of(
            "CREATE INDEX CONCURRENTLY u_nn ON t ((b) HASH) WHERE b IS NOT NULL;\n"
            "DROP INDEX CONCURRENTLY u;"))
        self.assertEqual([n for *_, n in self.safety.lost_uniqueness(s, after)], ["u"])

    def test_on_conflict_target(self):
        s = schema.parse("CREATE TABLE t (a int, b int, c int, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b, c);")
        pats = [{"id": "P1", "shape": sqlshape.analyze(
            "INSERT INTO t (a, b, c) VALUES ($1, $2, $3) ON CONFLICT (b, c) DO NOTHING", s)}]
        after = self.safety.apply(s, self.safety.changes_of("DROP INDEX CONCURRENTLY u;"))
        self.assertEqual(self.safety.lost_conflict_targets(s, after, pats), [("P1", "t", ["b", "c"])])

    def test_bucketing_loses_order(self):
        s = schema.parse("CREATE TABLE t (id int, ts timestamptz, PRIMARY KEY (id HASH));"
                         "CREATE INDEX t_ts ON t (ts DESC);")
        pats = [{"id": "P1", "weight": "HOT", "shape": sqlshape.analyze(
            "SELECT id FROM t WHERE ts > $1 ORDER BY ts DESC LIMIT 10", s)}]
        before = self.safety.access_map(s, pats, analyze, False)
        after = self.safety.apply(s, self.safety.changes_of(
            "CREATE INDEX CONCURRENTLY t_ts_bkt ON t ((yb_hash_code(ts) % 16) ASC, ts DESC);\n"
            "DROP INDEX CONCURRENTLY t_ts;"))
        regs = self.safety.regressions(before, self.safety.access_map(after, pats, analyze, False),
                                       {"P1": "HOT"})
        # The bucketed index is still reachable by skip scan over its buckets, but rows no
        # longer come back in ts order.
        self.assertTrue(regs and "index order" in regs[0]["why"], regs)


class Merge(unittest.TestCase):
    def test_two_rebuilds_of_one_index_merge(self):
        from ybm import safety
        s = schema.parse("CREATE TABLE o (id int, c int, d int, t int, PRIMARY KEY (id HASH));"
                         "CREATE INDEX oc ON o (c HASH, d ASC);")

        class F:
            def __init__(self, rule, sev, ddl):
                self.rule, self.severity, self.ddl, self.fix, self.obj = rule, sev, ddl, "", rule

        a = F("STA001", "high", "CREATE INDEX CONCURRENTLY oc_nn ON o ((c) HASH, d ASC) "
              "WHERE c IS NOT NULL;\nDROP INDEX CONCURRENTLY oc;")
        b = F("CAP020", "medium", "CREATE INDEX CONCURRENTLY oc_cov ON o ((c) HASH, d ASC) "
              "INCLUDE (t);\nDROP INDEX CONCURRENTLY oc;")

        class C:
            items = {("STA001", "x"): a, ("CAP020", "y"): b}
        rows = safety.merge_replacements(s, C)
        self.assertEqual(rows[0]["status"], "merged")
        self.assertIsNone(b.ddl)
        self.assertIn("INCLUDE (t)", a.ddl)
        self.assertIn("WHERE (c is not null)", a.ddl)
        self.assertEqual(a.ddl.count("DROP INDEX"), 1)


def _findings(bundle):
    res = analyze.run(bundle)
    return [(f["rule"], f["object"], f["severity"]) for f in res["findings"]
            if not f["rule"].startswith("LINT-")]


@unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
class Metamorphic(unittest.TestCase):
    """Changes that must not change the review."""

    def bundle(self):
        return inputs.load(FIXTURE)

    def test_statement_order_does_not_matter(self):
        from ybm.sqltok import tokenize, split_statements
        b = self.bundle()
        base = _findings(b)
        stmts = []
        for st in split_statements(tokenize(b.ddl)):
            stmts.append(b.ddl[st[0].pos:st[-1].pos + len(st[-1].text)])
        # Tables before indexes still, but each group reversed.
        tables = [x for x in stmts if re.match(r"(?is)\s*CREATE\s+(TABLE|SEQUENCE)", x)]
        rest = [x for x in stmts if x not in tables]
        b.ddl = ";\n".join(list(reversed(tables)) + list(reversed(rest))) + ";\n"
        self.assertEqual(sorted(base), sorted(_findings(b)))

    def test_unrelated_table_does_not_change_findings(self):
        b = self.bundle()
        base = _findings(b)
        b.ddl += "\nCREATE TABLE zz_unrelated (k bigint PRIMARY KEY, v text);\n" \
                 "CREATE INDEX zz_v ON zz_unrelated (v);\n"
        res = analyze.run(b)
        got = [(f["rule"], f["object"], f["severity"]) for f in res["findings"]
               if not f["rule"].startswith("LINT-") and "zz_" not in f["object"] and
               "zz_" not in f["fact"]]
        self.assertEqual(sorted(base), sorted(got))

    def test_scaling_rows_keeps_capability_findings(self):
        b = self.bundle()
        base = {(r, o) for r, o, _ in _findings(b) if r.startswith("CAP")}
        b.reltuples = {k: v * 10 for k, v in b.reltuples.items()}
        got = {(r, o) for r, o, _ in _findings(b) if r.startswith("CAP")}
        self.assertEqual(base, got)

    def test_fixpoint(self):
        from ybm import fixpoint
        out = fixpoint.check(self.bundle())
        self.assertEqual(out["problems"], [])
        self.assertGreater(out["fixed_with_ddl"], 0)


class Versions(unittest.TestCase):
    def test_release_resolution_and_drift(self):
        from ybm import versions
        tag, note = versions.resolve("2025.2.5.2")
        self.assertEqual(tag, "2025.2.5.2")
        self.assertIsNone(note)
        # Merge scan streams arrived within 2025.2.
        self.assertFalse(versions.available("2025.2.0.0", "yb_max_merge_scan_streams"))
        self.assertTrue(versions.available("2025.2.5.2", "yb_max_merge_scan_streams"))
        # The cost model default depends on the deployment tool from 2025.2.
        v, src, per = versions.setting("2025.2.5.2", "yb_enable_cbo")
        self.assertIsNone(v)
        self.assertEqual(sorted(set(per.values())), ["legacy_mode", "on"])
        d = versions.drift("2025.1.0.0", "2026.1.1.2")
        self.assertIn("yb_max_merge_scan_streams", d["settings"])

    def test_profiles_are_discovered_not_hand_written(self):
        from ybm import versions
        p = versions.profiles("2024.2.0.0")
        # On 2024.2 yugabyted sets planner settings only under Enhanced PG Compatibility.
        self.assertIn("yugabyted --enhance_pg_compatibility", p)
        self.assertNotIn("yugabyted", p)
        self.assertEqual(p["yugabyted --enhance_pg_compatibility"]
                         ["yb_use_hash_splitting_by_default"], "off")
        # So the default sharding is conditional on how the cluster was deployed.
        v, _, per = versions.setting("2024.2.0.0", "yb_use_hash_splitting_by_default")
        self.assertIsNone(v)
        self.assertEqual(sorted(set(per.values())), ["off", "on"])

    def test_no_release_numbers_in_rules(self):
        here = os.path.dirname(os.path.abspath(__file__))
        txt = open(os.path.join(here, "..", "rules", "rules.json")).read()
        self.assertNotRegex(txt, r"\b20\d\d\.\d+\.\d+")
        self.assertNotIn('"since"', txt)


@unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
class Fixture(unittest.TestCase):
    def run_cli(self, py=sys.executable):
        r = subprocess.run([py, os.path.join(HERE, "yb-model.py"), "analyze", FIXTURE],
                           capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stderr)  # 1 = high findings, not a failure
        return r.stdout

    def test_deterministic(self):
        a, b = self.run_cli(), self.run_cli()
        self.assertEqual(hashlib.sha256(a.encode()).hexdigest(),
                         hashlib.sha256(b.encode()).hexdigest())

    def test_answer_key_recall(self):
        res = json.loads(self.run_cli())
        key = json.load(open(os.path.join(FIXTURE, "..", "answer-key.json")))
        got = {(f["rule"], f["object"]) for f in res["findings"]}
        missing = []
        for item in key["defects"]:
            if not any(r == m["rule"] and m.get("object_contains", "") in o
                       for m in item["engine_match"] for r, o in got):
                missing.append(item["id"])
        self.assertEqual(missing, [])


class Preflight(unittest.TestCase):
    """Missing inputs are named, gate the review, and mute the rules that would guess."""

    DDL = ("CREATE TABLE items (id bigint NOT NULL, group_id bigint NOT NULL, name text, "
           "PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX items_group ON items ((group_id) HASH);\n")
    Q = "SELECT * FROM items WHERE group_id = $1"

    def make(self, files):
        import tempfile
        d = tempfile.mkdtemp()
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        return d

    def test_definitions_are_consistent(self):
        from ybm import preflight
        rules = analyze.load_rules()
        for d in preflight.defs():
            self.assertIn(d["id"], preflight.DETECT, d["id"])
            for r in d.get("mutes", []) + (d.get("weakens") or {}).get("rules", []):
                self.assertIn(r, rules, "%s names unknown rule %s" % (d["id"], r))
        self.assertEqual(set(preflight.DETECT), {d["id"] for d in preflight.defs()})

    def test_unranked_list_withholds_rekey(self):
        q = self.make({"schema.sql": self.DDL, "queries.sql": self.Q + ";\n"})
        res = analyze.run(inputs.load(q))
        rules = {f["rule"] for f in res["findings"]}
        self.assertNotIn("CAP050", rules)
        self.assertEqual(res["preflight"]["muted"]["CAP050"]["withheld"], ["items"])
        self.assertTrue(all("pattern unranked" in f["caveats"] for f in res["findings"]
                            if f["rule"] == "CAP020"))
        pss = "queryid,calls,total_exec_time,mean_exec_time,rows,query\n1,1000000,5000,0.005,1000000,\"%s\"\n" % self.Q
        m = self.make({"schema.sql": self.DDL, "ybm_pss.csv": pss})
        self.assertIn("CAP050", {f["rule"] for f in analyze.run(inputs.load(m))["findings"]})

    def test_review_is_gated(self):
        q = self.make({"schema.sql": self.DDL})
        cli = [sys.executable, os.path.join(HERE, "yb-model.py")]
        r = subprocess.run(cli + ["preflight", q], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("Proceed with the review anyway? (yes / no)", r.stdout)
        r = subprocess.run(cli + ["review", q, "--no-replay"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(os.path.exists(os.path.join(q, "review.md")))
        r = subprocess.run(cli + ["review", q, "--no-replay", "--accept-missing"],
                           capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        with open(os.path.join(q, "review.md")) as fh:
            md = fh.read()
        self.assertIn("### Muted checks (inputs missing)", md)
        self.assertIn("INCOMPLETE", md)

    @unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
    def test_complete_bundle_needs_no_confirmation(self):
        from ybm import preflight
        pf = preflight.assess(inputs.load(FIXTURE))
        self.assertEqual(pf["missing"], [])
        self.assertFalse(pf["needs_confirmation"])


class KeysetAndConflicts(unittest.TestCase):
    DDL = ("CREATE TABLE ev (c uuid NOT NULL, id uuid NOT NULL, ts timestamptz NOT NULL, "
           "src text, ext bigint NOT NULL, v text, PRIMARY KEY ((c) HASH, id ASC));\n"
           "CREATE INDEX ev_tl ON ev ((c) HASH, ts DESC, id DESC);\n"
           "CREATE UNIQUE INDEX ev_ext ON ev ((ext) HASH);\n"
           "CREATE INDEX ev_src ON ev ((c) HASH, src ASC);\n")
    PAGE = ("SELECT * FROM ev WHERE c = $1 AND (ts, id) < ($2, $3) "
            "ORDER BY ts DESC, id DESC LIMIT 50")

    def run_q(self, queries):
        d = Preflight.make(self, {"schema.sql": self.DDL, "queries.sql": queries})
        return analyze.run(inputs.load(d))

    def test_row_comparison_is_not_a_seek_bound(self):
        sh = sqlshape.analyze(self.PAGE)
        ops = {(p.col, p.op) for p in sh.preds}
        self.assertIn(("ts", "rowcmp"), ops)
        self.assertNotIn(("ts", "range"), ops)

    def test_keyset_page_uses_ordered_index_and_flags_row_cursor(self):
        res = self.run_q(self.PAGE + ";\n")
        acc = res["patterns"][0]["access"][0]
        self.assertEqual(acc["path"], "ev_tl")
        rules = {f["rule"] for f in res["findings"]}
        self.assertNotIn("CAP010", rules)
        self.assertIn("CAP013", rules)
        fixed = self.PAGE.replace("AND (ts, id)", "AND ts <= $2 AND (ts, id)")
        res = self.run_q(fixed + ";\n")
        self.assertNotIn("CAP013", {f["rule"] for f in res["findings"]})

    def test_on_conflict_without_matching_unique_index(self):
        res = self.run_q("INSERT INTO ev (c, id, ts, src, ext) VALUES ($1,$2,$3,$4,$5) "
                         "ON CONFLICT (src, ext) DO NOTHING;\n"
                         "INSERT INTO ev (c, id, ts, src, ext) VALUES ($1,$2,$3,$4,$5) "
                         "ON CONFLICT (ext) DO NOTHING;\n")
        cap = [f for f in res["findings"] if f["rule"] == "CAP060"]
        self.assertEqual(len(cap), 1)
        self.assertIn("ev_ext", cap[0]["fact"])

    def test_declared_complete_list_reports_unused_index(self):
        q = self.PAGE + ";\n"
        plain = self.run_q(q)
        self.assertNotIn("IDX001", {f["rule"] for f in plain["findings"]})
        decl = self.run_q("-- ybm: workload-complete\n" + q)
        idx = [f for f in decl["findings"] if f["rule"] == "IDX001"]
        self.assertEqual(len(idx), 1)
        self.assertIn("ev_src", idx[0]["fact"])
        self.assertIn("workload declared complete", idx[0]["caveats"])


class Probes(unittest.TestCase):
    """Probe definitions are valid, and verdicts fold into findings as specified."""

    def test_definitions_parse(self):
        from ybm import probes
        rules = analyze.load_rules()
        self.assertTrue(probes.rules_with_probes())
        for rid, p in probes.rules_with_probes().items():
            self.assertIn(rid, rules)
            self.assertTrue(p["setup"] and p["cases"] and p["holds"], rid)
            for expr in p.get("applies", []) + p["holds"]:
                probes.parse_check(expr, list(p["cases"]))
            for q in p["cases"].values():
                self.assertNotRegex(q, r"\$\d", "probe queries use literals: " + rid)

    def test_checks_reject_code(self):
        from ybm import probes
        for bad in ("__import__('os')", "claim.__class__", "open('x')", "claim.nope > 1"):
            with self.assertRaises((ValueError, SyntaxError)):
                probes.parse_check(bad, ["claim"])

    def test_verdicts(self):
        from ybm import probes
        p = {"cases": {"claim": "", "control": ""}, "applies": ["claim.index == 'i'"],
             "holds": ["claim.scanned >= 10 * claim.returned"]}
        m = lambda **k: dict(dict.fromkeys(probes.METRICS, 0), **k)  # noqa: E731
        self.assertEqual(probes.judge(p, {"claim": m(index="i", scanned=500, returned=50),
                                          "control": m()})[0], "holds")
        self.assertEqual(probes.judge(p, {"claim": m(index="i", scanned=50, returned=50),
                                          "control": m()})[0], "refuted")
        self.assertEqual(probes.judge(p, {"claim": m(index="j", scanned=500, returned=50),
                                          "control": m()})[0], "inconclusive")

    def test_refuted_probe_withdraws_finding(self):
        from ybm import probes
        col = analyze.Collector()
        col.add(analyze.Finding("CAP013", "medium", "probable", "ix on t", "fact", ["P1"]))
        recon = []
        plans = {"version_match": {"image_release": "9.9.9.9", "exact": True},
                 "probes": {"CAP013": {"verdict": "refuted", "detail": "claim.scanned >= 1",
                                       "cases": {}}}}
        probes.apply(col, plans, recon)
        self.assertEqual(col.items, {})
        self.assertIn("9.9.9.9", recon[0]["outcome"])


class IndexRuleEdges(unittest.TestCase):
    """Partial queue indexes, two-valued columns, and drops that settle other findings."""

    def run_b(self, ddl, stats=None, rows=None):
        files = {"schema.sql": ddl}
        if stats:
            files["ybm_pg_stats.csv"] = ("schemaname,tablename,attname,inherited,null_frac,"
                                         "avg_width,n_distinct,most_common_vals,"
                                         "most_common_freqs,histogram_bounds,correlation\n" +
                                         "".join("public,%s\n" % r for r in stats))
        if rows:
            files["ybm_reltuples.csv"] = "relname,relkind,reltuples\n" + "".join(
                "%s,r,%s\n" % kv for kv in rows.items())
        d = Preflight.make(self, files)
        return analyze.run(inputs.load(d))

    def test_partial_queue_index_on_a_flag_is_not_flagged(self):
        res = self.run_b("CREATE TABLE q (id bigint NOT NULL, done boolean NOT NULL, "
                         "PRIMARY KEY ((id) HASH));\n"
                         "CREATE INDEX q_todo ON q (done ASC) WHERE (done = false);\n",
                         stats=["q,done,f,0,1,1,{f},{1},,1"], rows={"q": 1000000})
        rules = {(f["rule"], f["index"]) for f in res["findings"]}
        self.assertNotIn(("STA008", "q_todo"), rules)
        self.assertNotIn(("STA004", "q_todo"), rules)

    def test_absolute_skew_severity_scales_with_rows_on_one_hash_code(self):
        ddl = ("CREATE TABLE s (id bigint NOT NULL, g bigint NOT NULL, "
               "PRIMARY KEY ((id) HASH));\nCREATE INDEX s_g ON s ((g) HASH);\n")
        want = {1e7: "low", 5e8: "medium", 5e9: "high"}  # top value holds 2% of rows
        for rows, sev in want.items():
            res = self.run_b(ddl, stats=["s,g,f,0,8,5000,{7},{0.02},,0"], rows={"s": int(rows)})
            got = [f["severity"] for f in res["findings"] if f["rule"] == "STA003"]
            self.assertEqual(got, [sev], rows)

    def test_drop_settles_other_findings_on_the_index(self):
        res = self.run_b("CREATE TABLE f (id bigint NOT NULL, flag boolean, "
                         "PRIMARY KEY ((id) HASH));\n"
                         "CREATE INDEX f_flag ON f ((flag) HASH);\n",
                         stats=["f,flag,f,0.6,1,2,{t},{0.3},,0"], rows={"f": 10000000})
        on = [f for f in res["findings"] if f["index"] == "f_flag"]
        drop = [f for f in on if f["rule"] == "STA008"]
        self.assertEqual(len(drop), 1)
        self.assertIn("idx_scan", drop[0]["ddl"])
        others = [f for f in on if f["rule"] != "STA008"]
        self.assertTrue(others)
        self.assertTrue(all(f["ddl"] is None for f in others))
        self.assertNotIn("SAF002", {f["rule"] for f in res["findings"]})


if __name__ == "__main__":
    unittest.main()
