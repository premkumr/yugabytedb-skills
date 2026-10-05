"""Final safety pass over the engine's own recommendations.

Every recommended DDL is applied to a copy of the schema, first one finding at a time and then
all together, and the result is compared with the schema as it is:

1. Uniqueness: each PRIMARY KEY / UNIQUE guarantee must survive, on the same or fewer columns
   and with a predicate no narrower. A change that would lose one is amended with the UNIQUE
   index that keeps it, and the amendment is checked again.
2. ON CONFLICT targets: every upsert's conflict columns must still match a unique index.
3. Plans: every ranked pattern is re-planned statically; losing an access path, a point
   lookup, index order or coverage is a regression.
4. Measured use: dropping an index that pg_stat_user_indexes shows scanned is a regression.

The pass never deletes a recommendation. It amends DDL where the fix is mechanical and
otherwise raises a SAF finding next to the recommendation it concerns.
"""

import copy
import re

from . import schema as schema_mod
from .sqltok import tokenize, split_statements, rebase, match_paren
from . import sqlshape

REKEY = re.compile(r"--\s*CREATE TABLE (\w+)_new \(\.\.\. PRIMARY KEY \((.+)\)\)")


def changes_of(ddl):
    """Structured changes from an engine DDL string."""
    out = []
    if not ddl:
        return out
    for line in ddl.splitlines():
        m = REKEY.search(line)
        if m:
            out.append(("rekey", m.group(1), m.group(2)))
    body = "\n".join(l for l in ddl.splitlines() if not l.lstrip().startswith("--"))
    body = re.sub(r"--[^\n]*", "", body)
    for st in split_statements(tokenize(body)):
        st = rebase(st)
        if not st:
            continue
        words = [t.up for t in st[:4] if t.kind == "word"]
        if words[:1] == ["DROP"] and "INDEX" in words:
            name = [t.ident for t in st if t.ident and t.up not in
                    ("DROP", "INDEX", "CONCURRENTLY", "IF", "EXISTS")][-1]
            out.append(("drop", name.split(".")[-1]))
        elif words[:1] == ["CREATE"] and "INDEX" in words:
            out.append(("create", st))
    return out


def apply(sch, changes):
    after = copy.deepcopy(sch)
    for ch in changes:
        if ch[0] == "drop":
            after.indexes.pop(ch[1], None)
        elif ch[0] == "create":
            schema_mod._parse_create_index(ch[1], after, 0)
        elif ch[0] == "rekey":
            t = after.tables.get(ch[1])
            if t is not None:
                keys = schema_mod._parse_keys(rebase(tokenize(ch[2])))
                t.pk = schema_mod.Index(t.pk.name if t.pk else ch[1] + "_pkey", ch[1], keys,
                                        unique=True, is_pk=True)
    after.resolve_modes()
    return after


def _norm(where):
    return re.sub(r"[()\s]+", " ", (where or "").lower()).strip()


def _keyset(idx):
    return frozenset(k.col or ("expr:" + k.expr) for k in idx.keys)


def constraints(sch):
    out = []
    for t in sch.tables.values():
        if t.pk:
            out.append((t.name, _keyset(t.pk), None, t.pk.name))
    for i in sch.indexes.values():
        if i.unique:
            out.append((i.table, _keyset(i), i.where, i.name))
    return out


def lost_uniqueness(before, after):
    have = constraints(after)
    lost = []
    for table, keys, where, name in constraints(before):
        ok = any(t == table and k <= keys and (w is None or _norm(w) == _norm(where))
                 for t, k, w, _ in have)
        if not ok:
            lost.append((table, keys, where, name))
    return lost


def lost_conflict_targets(before, after, patterns):
    lost = []
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            if sh.kind != "insert" or not sh.conflict_cols or not sh.tables:
                continue
            table, target = sh.tables[0], frozenset(sh.conflict_cols)

            def has(s):
                return any(t == table and k == target and w is None
                           for t, k, w, _ in constraints(s))
            if has(before) and not has(after):
                lost.append((p["id"], table, sorted(target)))
    return lost


def access_map(sch, patterns, an, cbo):
    """pattern id -> table -> access summary, as the static planner sees it."""
    out = {}
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            if sh.kind == "insert":
                continue
            for table in sh.tables:
                if table not in sch.tables:
                    continue
                preds = sh.preds_for(table)
                if not preds and not sh.order:
                    continue
                ops = an._pred_index(preds)
                paths = []
                for idx in sch.indexes_on(table):
                    r = an.eval_path(idx, ops, sh, table, cbo)
                    if r["usable"]:
                        r["order_ok"] = an.order_ok(idx, ops, sh, table)
                        r["covering"], r["missing"] = an.covering(idx, sh, table,
                                                                  sch.tables[table].cols)
                    paths.append(r)
                best = an.choose(paths, limit=bool(sh.limit))
                walk = an.ordered_walk(sch, table, ops, sh) if best is None else None
                out.setdefault(p["id"], {})[table] = {
                    "path": best["index"] if best else (walk.name if walk else None),
                    "bound": best["bound"] if best else 0,
                    "point": bool(best and best["full_unique"]),
                    "order_ok": best.get("order_ok") if best else (True if walk else None),
                    "covering": bool(best and best.get("covering")),
                }
    return out


def regressions(before_map, after_map, weights):
    out = []
    for pid, tables in sorted(before_map.items(), key=lambda kv: int(kv[0][1:])):
        for table, b in sorted(tables.items()):
            a = after_map.get(pid, {}).get(table)
            if a is None:
                continue
            why = []
            if b["path"] and not a["path"]:
                why.append("loses its access path (%s) and falls back to a scan" % b["path"])
            elif b["path"] and a["path"]:
                if b["point"] and not a["point"]:
                    why.append("is no longer a point lookup")
                if a["bound"] < b["bound"]:
                    why.append("binds fewer key columns (%d -> %d)" % (b["bound"], a["bound"]))
                if b["order_ok"] is True and a["order_ok"] is False:
                    why.append("loses index order and needs a sort")
                if b["covering"] and not a["covering"]:
                    why.append("loses coverage (%s -> %s) and needs a base-table fetch"
                               % (b["path"], a["path"]))
            if why:
                out.append({"pattern": pid, "weight": weights.get(pid), "table": table,
                            "why": "; ".join(why)})
    return out


def _index_of(sch, st):
    tmp = copy.deepcopy(sch)
    before = set(tmp.indexes)
    schema_mod._parse_create_index(st, tmp, 0)
    new = [tmp.indexes[n] for n in tmp.indexes if n not in before]
    return new[0] if new else None


def _ddl_for(idx, replaced):
    keys = idx.signature()
    return ("CREATE %sINDEX CONCURRENTLY %s ON %s (%s)%s SPLIT INTO <n> TABLETS%s;\n"
            "DROP INDEX CONCURRENTLY %s;  -- after %s is valid" % (
                "UNIQUE " if idx.unique else "", idx.name, idx.table, keys,
                (" INCLUDE (%s)" % ", ".join(idx.include)) if idx.include else "",
                (" WHERE %s" % idx.where) if idx.where else "", replaced, idx.name))


def merge_replacements(sch, col):
    """Several findings may each rebuild the same index (for example a NULL guard and a
    covering INCLUDE). Applied one after the other they drop the original twice and the later
    rebuild undoes the earlier one. Merge them into one replacement on the highest-ranked
    finding: union of INCLUDE columns, conjunction of predicates, UNIQUE if any is."""
    by_target = {}
    for key in sorted(col.items):
        f = col.items[key]
        if getattr(f, "disputed", None):
            continue  # not recommended: replay disputed it
        chs = changes_of(f.ddl)
        drops = [c[1] for c in chs if c[0] == "drop"]
        creates = [c[1] for c in chs if c[0] == "create"]
        if len(drops) == 1 and len(creates) == 1:
            by_target.setdefault(drops[0], []).append((f, creates[0]))
    merged = []
    for target, items in sorted(by_target.items()):
        if len(items) < 2 or target not in sch.indexes:
            continue
        orig = sch.indexes[target]
        idxs = [_index_of(sch, st) for _, st in items]
        if any(i is None for i in idxs):
            continue
        keys = max(idxs, key=lambda i: len(i.keys)).keys
        include = sorted({c for i in idxs for c in i.include} - {k.col for k in keys})
        wheres = []
        for i in idxs:
            for w in ([i.where] if i.where else []):
                if w not in wheres:
                    wheres.append(w)
        out = schema_mod.Index("%s_v2" % target, orig.table, keys,
                               unique=orig.unique or any(i.unique for i in idxs),
                               include=include,
                               where=" AND ".join("(%s)" % w for w in wheres) if wheres else None)
        rank = sorted(items, key=lambda it: (["critical", "high", "medium", "low",
                                              "info"].index(it[0].severity), it[0].rule))
        lead = rank[0][0]
        lead.ddl = _ddl_for(out, target)
        others = [f for f, _ in rank[1:]]
        for f in others:
            f.ddl = None
            f.fix = (f.fix or "") + (" Merged with the %s fix for %s into one replacement, so "
                                     "the index is rebuilt once." % (lead.rule, target))
        lead.fix = (lead.fix or "") + (" This replacement also carries the %s change(s), merged "
                                       "by the safety check." % ", ".join(f.rule for f in others))
        merged.append({"finding_rule": lead.rule, "object": lead.obj, "changes": 2,
                       "status": "merged", "checks": [
                           "%d fixes rebuilt %s; merged into one %sindex%s%s" % (
                               len(items), target, "UNIQUE " if out.unique else "",
                               (" INCLUDE (%s)" % ", ".join(include)) if include else "",
                               (" WHERE %s" % out.where) if out.where else "")]})
    return merged


def reintroduced(sch_after, created_names, bundle, an):
    """Defects a new index brings back: a NULL-heavy hash lead without a guard, or a
    boolean key."""
    out = []
    for n in created_names:
        idx = sch_after.indexes.get(n)
        if not idx or not idx.keys or not idx.keys[0].col:
            continue
        t = sch_after.tables.get(idx.table)
        lead = idx.keys[0]
        st = an.col_stats(bundle, sch_after, idx.table, lead.col)
        if lead.mode == "HASH" and st and (st["null_frac"] or 0) >= an.THRESHOLDS["null_frac_flag"] \
                and not (idx.where and re.search(r"\b%s\s+is\s+not\s+null" % re.escape(lead.col),
                                                 idx.where, re.I)):
            out.append("%s hashes %s.%s again without WHERE %s IS NOT NULL (null_frac %.2f)" % (
                n, idx.table, lead.col, lead.col, st["null_frac"]))
        if t and t.cols.get(lead.col, {}).get("type") in ("boolean", "bool") and len(idx.keys) == 1:
            out.append("%s indexes a boolean" % n)
    return out


def ddl_conflicts(all_changes):
    drops, creates, out = {}, {}, []
    for ch in all_changes:
        if ch[0] == "drop":
            drops[ch[1]] = drops.get(ch[1], 0) + 1
    for n, k in sorted(drops.items()):
        if k > 1:
            out.append("DROP INDEX %s appears %d times; the second fails" % (n, k))
    return out


def check(sch, patterns, col, an, ps, bundle, Finding):
    """Run the pass; amends findings in place, adds SAF findings, returns a report."""
    weights = {p["id"]: p["weight"] for p in patterns}
    merged_rows = merge_replacements(sch, col)
    base_map = access_map(sch, patterns, an, ps["cbo"])
    report = []
    all_changes = []
    for key in sorted(col.items, key=lambda k: (k[0], k[1])):
        f = col.items[key]
        if getattr(f, "disputed", None):
            continue  # not recommended: replay disputed it
        chs = changes_of(f.ddl)
        if not chs:
            continue
        row = {"finding_rule": f.rule, "object": f.obj, "changes": len(chs), "checks": [],
               "status": "ok"}
        after = apply(sch, chs)
        lost = lost_uniqueness(sch, after)
        if lost:
            adds = []
            for table, keys, where, name in lost:
                cols = sorted(keys)
                iname = "%s_%s_uniq" % (table, "_".join(re.sub(r"\W+", "_", c) for c in cols))
                adds.append("CREATE UNIQUE INDEX CONCURRENTLY %s ON %s ((%s) HASH)%s;  -- added "
                            "by the safety check: keeps the uniqueness %s enforces" % (
                                iname, table, ", ".join(c.replace("expr:", "") for c in cols),
                                (" WHERE %s" % where) if where else "", name))
            f.ddl = f.ddl + "\n" + "\n".join(adds)
            chs = changes_of(f.ddl)
            after = apply(sch, chs)
            still = lost_uniqueness(sch, after)
            row["checks"].append("uniqueness: %s; %s" % (
                ", ".join("%s(%s)" % (n, ", ".join(sorted(k))) for _, k, _, n in lost),
                "amended" if not still else "NOT fixed by amendment"))
            row["status"] = "amended" if not still else "warn"
            f.fix = (f.fix or "") + (" The safety check added a UNIQUE index to keep the "
                                     "guarantee of %s." % ", ".join(n for *_, n in lost))
        conf = lost_conflict_targets(sch, after, patterns)
        if conf:
            row["checks"].append("ON CONFLICT target lost for %s" % ", ".join(
                "%s (%s)" % (pid, ", ".join(c)) for pid, _, c in conf))
            row["status"] = "warn"
        regs = regressions(base_map, access_map(after, patterns, an, ps["cbo"]), weights)
        if regs:
            row["checks"].append("plans: " + "; ".join(
                "%s on %s %s" % (r["pattern"], r["table"], r["why"]) for r in regs))
            row["status"] = "warn"
        replaces = any(c[0] in ("create", "rekey") for c in chs)
        for ch in chs:
            # A replacement is judged by the plan check; a pure drop also by measured use.
            if ch[0] == "drop" and not replaces:
                u = bundle.index_usage.get(ch[1])
                if u is not None and u["idx_scan"] > 0:
                    row["checks"].append("drops %s, which was scanned %d times" % (
                        ch[1], u["idx_scan"]))
                    row["status"] = "warn"
        if row["status"] == "warn":
            worst = min((["HOT", "UNRANKED", "WARM", "COLD"].index(r["weight"] or "COLD")
                         for r in regs), default=1)
            sev = ["high", "high", "medium", "low"][worst]
            tiny = regs and all((bundle.reltuples.get(r["table"]) or 1e9) <
                                an.THRESHOLDS["tiny_rows"] for r in regs)
            if tiny:
                sev = "info"
                row["checks"].append("affected tables are under 10k rows, so the cost is small")
            col.add(Finding("SAF001", sev, "confirmed", "%s %s" % (f.rule, f.obj),
                            "Applying the recommendation for %s %s would cause: %s." % (
                                f.rule, f.obj, " | ".join(row["checks"])),
                            sorted({r["pattern"] for r in regs}), table=f.table))
            f.fix = (f.fix or "") + " See SAF001: the safety check found a side effect."
        if not row["checks"]:
            row["checks"].append("uniqueness, ON CONFLICT targets and plans unchanged")
        created = [c[1] for c in chs if c[0] == "create"]
        names = [i.name for i in (_index_of(sch, st) for st in created) if i]
        back = reintroduced(after, names, bundle, an)
        if back:
            row["checks"].append("reintroduces: " + "; ".join(back))
            row["status"] = "warn"
            col.add(Finding("SAF001", "high", "confirmed", "%s %s" % (f.rule, f.obj),
                            "The recommendation for %s %s would bring back a defect: %s." % (
                                f.rule, f.obj, "; ".join(back)), table=f.table))
        report.append(row)
        all_changes.extend(chs)

    # Everything together: catches two fixes that are each safe but not combined.
    if all_changes:
        after = apply(sch, all_changes)
        redundant = []
        combo = {"finding_rule": "ALL", "object": "all recommendations together",
                 "changes": len(all_changes), "checks": [], "status": "ok"}
        lost = lost_uniqueness(sch, after)
        if lost:
            combo["checks"].append("uniqueness lost: " + ", ".join(n for *_, n in lost))
        conf = lost_conflict_targets(sch, after, patterns)
        if conf:
            combo["checks"].append("ON CONFLICT target lost: " + ", ".join(p for p, _, _ in conf))
        combo["checks"].extend(ddl_conflicts(all_changes))
        created_all = [i.name for i in (_index_of(sch, c[1]) for c in all_changes
                                        if c[0] == "create") if i and i.name in after.indexes]
        combo["checks"].extend("reintroduces: " + x
                               for x in reintroduced(after, created_all, bundle, an))
        # A rebuilt index that another index in the final schema makes redundant.
        for n in created_all:
            a = after.indexes.get(n)
            if not a or a.unique:
                continue
            ak = [k.col for k in a.keys]
            for b in after.indexes.values():
                if b.name == n or b.table != a.table or _norm(b.where) != _norm(a.where):
                    continue
                bk = [k.col for k in b.keys]
                if len(bk) > len(ak) and bk[:len(ak)] == ak and \
                        [k.mode for k in b.keys[:len(ak)]] == [k.mode for k in a.keys]:
                    redundant.append("%s is a prefix of %s with the same predicate: drop the "
                                     "original instead of rebuilding it" % (n, b.name))
                    break
        single = {(r_pat) for row in report for c in row["checks"]
                  for r_pat in re.findall(r"\b(P\d+) on ", c)}
        regs = [r for r in regressions(base_map, access_map(after, patterns, an, ps["cbo"]),
                                       weights) if r["pattern"] not in single]
        if regs:
            combo["checks"].append("plans: " + "; ".join(
                "%s on %s %s" % (r["pattern"], r["table"], r["why"]) for r in regs))
        if redundant:
            col.add(Finding("SAF003", "low", "confirmed", "redundant rebuilds",
                            "After the recommended changes: %s." % "; ".join(redundant)))
            combo["checks"].extend("note: " + r for r in redundant)
        if any(not c.startswith("note: ") for c in combo["checks"]):
            combo["status"] = "warn"
            col.add(Finding("SAF002", "high", "confirmed", "combined recommendations",
                            "Applying all recommendations together would cause: %s." %
                            " | ".join(combo["checks"]), sorted({r["pattern"] for r in regs})))
        if combo["status"] != "warn":
            combo["checks"].insert(0, "uniqueness, ON CONFLICT targets, plans and DDL "
                                      "consistency unchanged")
        report.append(combo)
    return merged_rows + report
