"""Load the evidence bundle written by scripts/collect.sql (CSV) or hand-made JSON files.

A bundle is a directory. Every file is optional except the schema:
  schema.sql            ysql_dump --schema-only --include-yb-metadata (or any DDL)
  queries.sql           access patterns, one statement per ';' (used when no pg_stat_statements)
  ybm_pg_stats.csv      pg_stats
  ybm_pss.csv           pg_stat_statements
  ybm_reltuples.csv     pg_class relname, relkind, reltuples
  ybm_index_usage.csv   pg_stat_user_indexes
  ybm_table_usage.csv   pg_stat_user_tables
  ybm_settings.csv      pg_settings name, setting
  ybm_meta.csv          version, colocated, postmaster start, stats reset
  ybm_tablets.csv       schemaname, relname, num_tablets (yb_table_properties: cluster-wide);
                        older captures: table_name, tablets (yb_local_tablets: one node only)
Columns are matched by name, so extra columns are ignored and older releases that lack some
columns still load.
"""

import csv
import glob
import json
import os
import re

csv.field_size_limit(1 << 30)


def _rows(path):
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else data.get("rows", [])
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def _find(bundle, stem):
    for ext in (".csv", ".json"):
        p = os.path.join(bundle, stem + ext)
        if os.path.isfile(p):
            return p
    return None


def _f(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_pg_array(text):
    """Parse a text-form one-dimensional Postgres array into a list of strings."""
    if text is None or text == "":
        return None
    if isinstance(text, list):
        return [str(x) for x in text]
    s = text.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    s = s[1:-1]
    out, cur, q, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                q = False
            else:
                cur.append(c)
        elif c == '"':
            q = True
        elif c == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


class Bundle:
    def __init__(self, path):
        self.path = path
        self.ddl = ""
        self.ddl_files = []
        self.queries_sql = None
        self.stats = {}        # (table, col) -> dict
        self.pss = []          # list of dict
        self.reltuples = {}    # relname -> float
        self.index_usage = {}  # index name -> dict
        self.table_usage = {}  # table name -> dict
        self.settings = {}     # name -> setting
        self.meta = {}
        self.tablets = {}      # relname -> int
        self.tablets_cluster_wide = True  # False for a yb_local_tablets (one node) capture
        self.present = []
        self.declared_complete = False  # queries.sql says no other statement touches the schema

    @property
    def version(self):
        v = self.meta.get("version", "")
        m = re.search(r"YB-(\d+\.\d+\.\d+\.\d+)", v)
        return m.group(1) if m else (v or None)


def load(path):
    b = Bundle(path)
    if os.path.isfile(path):
        b.ddl_files = [path]
    else:
        for name in ("schema.sql", "ddl.sql", "schema.ddl"):
            p = os.path.join(path, name)
            if os.path.isfile(p):
                b.ddl_files = [p]
                break
        if not b.ddl_files:
            b.ddl_files = sorted(p for p in glob.glob(os.path.join(path, "*.sql"))
                                 if os.path.basename(p) not in ("queries.sql", "collect.sql"))
    for p in b.ddl_files:
        with open(p, encoding="utf-8", errors="replace") as fh:
            b.ddl += fh.read() + "\n"
    if os.path.isdir(path):
        q = os.path.join(path, "queries.sql")
        if os.path.isfile(q):
            with open(q, encoding="utf-8", errors="replace") as fh:
                b.queries_sql = fh.read()
            b.declared_complete = bool(re.search(r"^\s*--\s*ybm:\s*workload-complete\b",
                                                 b.queries_sql, re.M | re.I))
            b.present.append("queries.sql (declared complete)" if b.declared_complete
                             else "queries.sql")
        _load_tables(b, path)
    if b.ddl_files:
        b.present.insert(0, "schema (%s)" % ", ".join(os.path.basename(p) for p in b.ddl_files))
    return b


def _load_tables(b, path):
    p = _find(path, "ybm_pg_stats")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            if str(r.get("inherited", "f")).lower() in ("t", "true", "1"):
                continue
            key = (r["tablename"].lower(), r["attname"].lower())
            b.stats[key] = {
                "null_frac": _f(r.get("null_frac"), 0.0),
                "avg_width": int(_f(r.get("avg_width"), 0) or 0),
                "n_distinct": _f(r.get("n_distinct"), 0.0),
                "mcv": r.get("most_common_vals") or None,
                "mcf": [float(x) for x in (parse_pg_array(r.get("most_common_freqs")) or [])],
                "hist": r.get("histogram_bounds") or None,
                "correlation": _f(r.get("correlation")),
            }
    p = _find(path, "ybm_pss")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            q = r.get("query") or ""
            row = {"queryid": str(r.get("queryid", "")), "query": q,
                   "calls": _f(r.get("calls"), 0.0),
                   "total_ms": _f(r.get("total_exec_time"), _f(r.get("total_time"), 0.0)),
                   "mean_ms": _f(r.get("mean_exec_time"), _f(r.get("mean_time"), 0.0)),
                   "rows": _f(r.get("rows"), 0.0)}
            for k in ("docdb_read_rpcs", "docdb_write_rpcs", "docdb_rows_scanned",
                      "docdb_rows_returned", "docdb_seeks", "docdb_nexts", "docdb_read_time",
                      "docdb_wait_time", "max_exec_time", "stddev_exec_time",
                      "total_plan_time"):
                if k in r:
                    row[k] = _f(r.get(k))
            if not row["mean_ms"] and row["calls"]:
                row["mean_ms"] = row["total_ms"] / row["calls"]
            b.pss.append(row)
    p = _find(path, "ybm_reltuples")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            b.reltuples[r["relname"].lower()] = _f(r.get("reltuples"), -1.0)
    p = _find(path, "ybm_index_usage")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            b.index_usage[r["indexrelname"].lower()] = {
                "table": r.get("relname", "").lower(), "idx_scan": _f(r.get("idx_scan"), 0.0)}
    p = _find(path, "ybm_table_usage")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            b.table_usage[r["relname"].lower()] = {k: _f(v) for k, v in r.items()
                                                   if k != "relname" and k != "schemaname"}
    p = _find(path, "ybm_settings")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            b.settings[r["name"]] = r.get("setting")
    p = _find(path, "ybm_meta")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            if "key" in r:
                b.meta[r["key"]] = r.get("value")
            else:
                b.meta.update(r)
    p = _find(path, "ybm_tablets")
    if p:
        b.present.append(os.path.basename(p))
        for r in _rows(p):
            if "num_tablets" in r:
                b.tablets[r["relname"].lower()] = int(_f(r.get("num_tablets"), 0) or 0)
            else:  # yb_local_tablets lists only the tablets with a peer on one node
                b.tablets_cluster_wide = False
                b.tablets[r["table_name"].lower()] = int(_f(r.get("tablets"), 0) or 0)
