"""DDL model: tables, keys and indexes as YugabyteDB will actually create them.

Sharding defaults follow the server rule (pg_yb_utils.c, YbSortOrdering): an unannotated
first key column is HASH only when yb_use_hash_splitting_by_default is on and the relation is
neither colocated nor in a tablegroup; otherwise ASC. Every other unannotated column is ASC.
"""

from .sqltok import (tokenize, split_statements, match_paren, split_top, text_of, is_word,
                     rebase)


class KeyCol:
    def __init__(self, col=None, expr=None, mode=None):
        self.col = col      # column name for a plain column key
        self.expr = expr    # normalised expression text for an expression key
        self.mode = mode    # HASH | ASC | DESC | None (unannotated; resolved later)
        self.explicit = mode is not None

    @property
    def label(self):
        return self.col if self.col else "(%s)" % self.expr

    def to_dict(self):
        return {"col": self.col, "expr": self.expr, "mode": self.mode,
                "explicit": self.explicit}


class Index:
    def __init__(self, name, table, keys, unique=False, include=None, where=None,
                 split=None, is_pk=False, method="lsm", line=0):
        self.name = name
        self.table = table
        self.keys = keys
        self.unique = unique
        self.include = include or []
        self.where = where      # normalised predicate text or None
        self.split = split      # "INTO n" | "AT VALUES" | None
        self.is_pk = is_pk
        self.method = method
        self.line = line

    @property
    def hash_cols(self):
        out = []
        for k in self.keys:
            if k.mode != "HASH":
                break
            out.append(k)
        return out

    @property
    def range_cols(self):
        return self.keys[len(self.hash_cols):]

    @property
    def key_names(self):
        return [k.col for k in self.keys if k.col]

    def signature(self):
        if self.hash_cols:
            h = "(%s) HASH" % ", ".join(k.label for k in self.hash_cols)
            rest = ["%s %s" % (k.label, k.mode) for k in self.range_cols]
            return ", ".join([h] + rest)
        return ", ".join("%s %s" % (k.label, k.mode) for k in self.keys)

    def to_dict(self):
        return {"name": self.name, "table": self.table, "unique": self.unique,
                "is_pk": self.is_pk, "method": self.method, "signature": self.signature(),
                "keys": [k.to_dict() for k in self.keys], "include": self.include,
                "where": self.where, "split": self.split}


class Table:
    def __init__(self, name, line=0):
        self.name = name
        self.cols = {}          # name -> {"type": str, "notnull": bool}
        self.col_order = []
        self.pk = None          # Index
        self.partition_by = None  # (method, [cols])
        self.partition_of = None
        self.partitions = []
        self.bound_to = None    # upper bound literal of a RANGE partition ("2027-01-01 ...")
        self.is_default = False
        self.colocated = None   # True | False | None (inherit database)
        self.tablegroup = None
        self.split = None
        self.line = line

    def to_dict(self):
        return {"name": self.name, "columns": [{"name": c, **self.cols[c]} for c in self.col_order],
                "pk": self.pk.to_dict() if self.pk else None,
                "partition_by": self.partition_by, "partition_of": self.partition_of,
                "partitions": sorted(self.partitions), "bound_to": self.bound_to,
                "is_default": self.is_default, "colocated": self.colocated,
                "tablegroup": self.tablegroup, "split": self.split}


class Schema:
    def __init__(self):
        self.tables = {}
        self.indexes = {}
        self.db_colocated = False
        self.hash_default = True
        self.parse_notes = []

    # --- lookups -------------------------------------------------------------
    def table(self, name):
        return self.tables.get(name)

    def indexes_on(self, table, own_only=False):
        """Access paths for a table. A partitioned parent with no index of its own is read
        through its partitions, so it borrows the first partition's paths (deduplicated by
        shape) for access-path analysis."""
        out = []
        t = self.tables.get(table)
        if t and t.pk:
            out.append(t.pk)
        out.extend(sorted((i for i in self.indexes.values() if i.table == table),
                          key=lambda i: i.name))
        if own_only or not t or not t.partitions:
            return out
        have = {i.signature() for i in out}
        for part in sorted(t.partitions):
            for i in self.indexes_on(part, own_only=True):
                if i.signature() not in have:
                    have.add(i.signature())
                    out.append(i)
        return out

    def is_colocated(self, table):
        t = self.tables.get(table)
        if t is None:
            return self.db_colocated
        if t.partition_of and t.colocated is None:
            return self.is_colocated(t.partition_of)
        if t.colocated is not None:
            return t.colocated
        return self.db_colocated

    def resolve_modes(self):
        """Fill in unannotated key modes the way the server does."""
        def fix(idx, table):
            t = self.tables.get(table)
            range_default = (not self.hash_default) or self.is_colocated(table) or \
                bool(t and t.tablegroup)
            for pos, k in enumerate(idx.keys):
                if k.mode is None:
                    if pos == 0 and not range_default and idx.method == "lsm":
                        k.mode = "HASH"
                    else:
                        k.mode = "ASC"
            # A column after a range column can never be hash.
            seen_range = False
            for k in idx.keys:
                if k.mode != "HASH":
                    seen_range = True
                elif seen_range:
                    k.mode = "ASC"
        for t in self.tables.values():
            if t.pk:
                fix(t.pk, t.name)
        for i in self.indexes.values():
            fix(i, i.table)

    def to_dict(self):
        return {"db_colocated": self.db_colocated, "hash_default": self.hash_default,
                "tables": [self.tables[k].to_dict() for k in sorted(self.tables)],
                "indexes": [self.indexes[k].to_dict() for k in sorted(self.indexes)],
                "parse_notes": self.parse_notes}


# --- parsing ---------------------------------------------------------------------------

def _name(toks, i):
    """Read a possibly qualified name at toks[i]; return (short_name, next_index)."""
    if i >= len(toks) or toks[i].ident is None:
        return None, i
    name = toks[i].ident
    i += 1
    while i + 1 < len(toks) and toks[i].kind == "." and toks[i + 1].ident is not None:
        name = toks[i + 1].ident
        i += 2
    return name, i


def _skip_words(toks, i, *words):
    while i < len(toks) and is_word(toks[i], *words):
        i += 1
    return i


def _parse_keys(toks):
    """Parse the inside of a key column list into [KeyCol]."""
    keys = []
    for part in split_top(toks):
        part = rebase(part)
        mode = None
        # Trailing annotations.
        tail = [t for t in part if t.depth == 0]
        for t in tail:
            if is_word(t, "HASH", "ASC", "DESC"):
                mode = t.up
        if part[0].kind == "(":
            j = match_paren(part, 0)
            inner = part[1:j]
            items = split_top(inner)
            simple = all(len(x) == 1 and x[0].ident is not None for x in items)
            if simple and (len(items) > 1 or mode == "HASH"):
                for x in items:
                    keys.append(KeyCol(col=x[0].ident, mode=mode or "HASH"))
                continue
            if simple and len(items) == 1:
                keys.append(KeyCol(col=items[0][0].ident, mode=mode))
                continue
            keys.append(KeyCol(expr=text_of(inner), mode=mode))
            continue
        if part[0].ident is not None and (len(part) == 1 or part[1].kind != "("):
            keys.append(KeyCol(col=part[0].ident, mode=mode))
            continue
        # Bare function expression, e.g. lower(email).
        stop = len(part)
        for k, t in enumerate(part):
            if t.depth == 0 and is_word(t, "HASH", "ASC", "DESC", "NULLS", "COLLATE"):
                stop = k
                break
        keys.append(KeyCol(expr=text_of(part[:stop]), mode=mode))
    return keys


def _split_clause(toks, i):
    if i < len(toks) and is_word(toks[i], "SPLIT"):
        if i + 1 < len(toks) and is_word(toks[i + 1], "INTO") and i + 2 < len(toks):
            return "INTO %s" % toks[i + 2].text
        return "AT VALUES"
    return None


def _bounds(toks, t):
    """Record FOR VALUES ... TO ('x') and DEFAULT on a partition."""
    for k, x in enumerate(toks):
        if x.depth == 0 and is_word(x, "DEFAULT") and k > 0 and \
                (is_word(toks[k - 1], "OF") or toks[k - 1].ident is not None) and \
                any(is_word(y, "PARTITION") for y in toks[:k]):
            if k + 1 >= len(toks) or not toks[k + 1].kind == "(":
                t.is_default = True
        if x.depth == 0 and is_word(x, "TO") and k + 1 < len(toks) and toks[k + 1].kind == "(":
            j = match_paren(toks, k + 1)
            lit = [y for y in toks[k + 2:j] if y.kind == "str"]
            if lit:
                t.bound_to = lit[0].text.strip("'")


def _find_top(toks, word, start=0):
    for k in range(start, len(toks)):
        if toks[k].depth == 0 and is_word(toks[k], word):
            return k
    return -1


def _parse_create_table(st, schema, line):
    i = 1
    i = _skip_words(st, i, "GLOBAL", "LOCAL", "TEMP", "TEMPORARY", "UNLOGGED")
    if not is_word(st[i] if i < len(st) else None, "TABLE"):
        return
    i += 1
    if is_word(st[i], "IF"):
        i += 3
    name, i = _name(st, i)
    if not name:
        return
    t = schema.tables.get(name) or Table(name, line)
    schema.tables[name] = t
    if i < len(st) and is_word(st[i], "PARTITION") and is_word(st[i + 1], "OF"):
        parent, i = _name(st, i + 2)
        t.partition_of = parent
        _bounds(st, t)
        if parent in schema.tables:
            schema.tables[parent].partitions.append(name)
        else:
            schema.parse_notes.append("partition %s declared before parent %s" % (name, parent))
    body_cols = []
    if i < len(st) and st[i].kind == "(":
        j = match_paren(st, i)
        body_cols = split_top(st[i + 1:j])
        i = j + 1
    for el in body_cols:
        el = rebase(el)
        head = el[0]
        if is_word(head, "CONSTRAINT"):
            el = el[2:]
            head = el[0] if el else None
        if head is None:
            continue
        if is_word(head, "PRIMARY") and len(el) > 2 and el[2].kind == "(":
            j = match_paren(el, 2)
            t.pk = Index(name + "_pkey", name, _parse_keys(el[3:j]), unique=True, is_pk=True,
                         line=line)
            continue
        if is_word(head, "UNIQUE") and len(el) > 1 and el[1].kind == "(":
            j = match_paren(el, 1)
            iname = "%s_%s_key" % (name, "_".join(text_of([x]) for x in el[2:j] if x.ident))
            schema.indexes[iname] = Index(iname, name, _parse_keys(el[2:j]), unique=True,
                                          line=line)
            continue
        if is_word(head, "CHECK", "FOREIGN", "EXCLUDE", "LIKE"):
            continue
        if head.ident is None:
            continue
        cname = head.ident
        typ_toks, k = [], 1
        while k < len(el) and not (el[k].depth == 0 and is_word(
                el[k], "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "CHECK", "REFERENCES",
                "CONSTRAINT", "GENERATED", "COLLATE")):
            typ_toks.append(el[k])
            k += 1
        rest = [x.up for x in el[k:] if x.depth == 0 and x.kind == "word"]
        notnull = False
        for a, b in zip(rest, rest[1:]):
            if a == "NOT" and b == "NULL":
                notnull = True
        if "PRIMARY" in rest:
            notnull = True
            mode = None
            for x in el[k:]:
                if is_word(x, "HASH", "ASC", "DESC"):
                    mode = x.up
            t.pk = Index(name + "_pkey", name, [KeyCol(col=cname, mode=mode)], unique=True,
                         is_pk=True, line=line)
        if "UNIQUE" in rest:
            iname = "%s_%s_key" % (name, cname)
            schema.indexes[iname] = Index(iname, name, [KeyCol(col=cname)], unique=True,
                                          line=line)
        if cname not in t.cols:
            t.col_order.append(cname)
        t.cols[cname] = {"type": text_of(typ_toks), "notnull": notnull}
    if t.pk:
        for k in t.pk.keys:
            if k.col in t.cols:
                t.cols[k.col]["notnull"] = True
    # Trailing clauses.
    k = _find_top(st, "PARTITION", i)
    if k != -1 and k + 2 < len(st) and is_word(st[k + 1], "BY"):
        method = st[k + 2].up
        if k + 3 < len(st) and st[k + 3].kind == "(":
            j = match_paren(st, k + 3)
            cols = [p[0].ident for p in split_top(st[k + 4:j]) if p and p[0].ident]
            t.partition_by = (method, cols)
    k = _find_top(st, "WITH", i)
    if k != -1 and k + 1 < len(st) and st[k + 1].kind == "(":
        j = match_paren(st, k + 1)
        opts = text_of(st[k + 2:j]).replace(" ", "")
        if "colocation=false" in opts or "colocated=false" in opts:
            t.colocated = False
        elif "colocation=true" in opts or "colocated=true" in opts:
            t.colocated = True
    k = _find_top(st, "TABLEGROUP", i)
    if k != -1:
        t.tablegroup = st[k + 1].ident
    k = _find_top(st, "SPLIT", i)
    if k != -1:
        t.split = _split_clause(st, k)
    if t.pk and t.pk.keys:
        t.pk.split = t.split


def _parse_create_index(st, schema, line):
    i = 1
    unique = False
    if is_word(st[i], "UNIQUE"):
        unique = True
        i += 1
    if not is_word(st[i], "INDEX"):
        return
    i += 1
    i = _skip_words(st, i, "CONCURRENTLY", "NONCONCURRENTLY")
    if is_word(st[i], "IF"):
        i += 3
    iname = None
    if not is_word(st[i], "ON"):
        iname, i = _name(st, i)
    if not is_word(st[i], "ON"):
        return
    i += 1
    i = _skip_words(st, i, "ONLY")
    tname, i = _name(st, i)
    method = "lsm"
    if is_word(st[i], "USING"):
        method = st[i + 1].text.lower()
        i += 2
    if i >= len(st) or st[i].kind != "(":
        return
    j = match_paren(st, i)
    keys = _parse_keys(st[i + 1:j])
    i = j + 1
    include, where, split = [], None, None
    k = _find_top(st, "INCLUDE", i)
    if k != -1 and st[k + 1].kind == "(":
        e = match_paren(st, k + 1)
        include = [p[0].ident for p in split_top(st[k + 2:e]) if p and p[0].ident]
    k = _find_top(st, "SPLIT", i)
    if k != -1:
        split = _split_clause(st, k)
    k = _find_top(st, "WHERE", i)
    if k != -1:
        end = len(st)
        s2 = _find_top(st, "SPLIT", k)
        if s2 != -1:
            end = s2
        where = text_of(st[k + 1:end])
    if not iname:
        iname = "%s_%s_idx" % (tname, "_".join(kc.col or "expr" for kc in keys))
    if method not in ("lsm", "btree", "hash"):
        # gin / ybgin / ybhnsw and friends are not modelled as key-ordered access paths.
        schema.parse_notes.append("index %s uses %s; not modelled for scan analysis"
                                  % (iname, method))
    schema.indexes[iname] = Index(iname, tname, keys, unique=unique, include=include,
                                  where=where, split=split, method=method if method != "btree"
                                  else "lsm", line=line)


def _parse_alter_table(st, schema, line):
    i = 2
    i = _skip_words(st, i, "IF", "EXISTS", "ONLY")
    tname, i = _name(st, i)
    if not tname:
        return
    k = _find_top(st, "ATTACH", i)
    if k != -1 and is_word(st[k + 1], "PARTITION"):
        child, _ = _name(st, k + 2)
        if child in schema.tables:
            schema.tables[child].partition_of = tname
            _bounds(st[k:], schema.tables[child])
        if tname in schema.tables and child not in schema.tables[tname].partitions:
            schema.tables[tname].partitions.append(child)
        return
    k = _find_top(st, "ADD", i)
    if k == -1:
        return
    j = k + 1
    cname = None
    if is_word(st[j], "CONSTRAINT"):
        cname = st[j + 1].ident
        j += 2
    if is_word(st[j], "PRIMARY") and st[j + 2].kind == "(":
        e = match_paren(st, j + 2)
        t = schema.tables.get(tname)
        if t:
            t.pk = Index(cname or tname + "_pkey", tname, _parse_keys(st[j + 3:e]),
                         unique=True, is_pk=True, line=line)
            for kc in t.pk.keys:
                if kc.col in t.cols:
                    t.cols[kc.col]["notnull"] = True
    elif is_word(st[j], "UNIQUE") and st[j + 1].kind == "(":
        e = match_paren(st, j + 1)
        iname = cname or "%s_key" % tname
        schema.indexes[iname] = Index(iname, tname, _parse_keys(st[j + 2:e]), unique=True,
                                      line=line)


def parse(sql, db_colocated=None, hash_default=True):
    schema = Schema()
    schema.hash_default = hash_default
    # psql meta-commands (backslash lines such as if/connect) are not SQL and would glue
    # onto the next statement.
    sql = "\n".join("" if ln.lstrip().startswith("\\") else ln for ln in sql.splitlines())
    toks = tokenize(sql)
    for st in split_statements(toks):
        st = rebase(st)
        if not st:
            continue
        line = sql.count("\n", 0, st[0].pos) + 1
        try:
            if is_word(st[0], "CREATE"):
                if any(is_word(x, "DATABASE") for x in st[1:3]):
                    txt = text_of(st).replace(" ", "")
                    if "colocation=true" in txt or "colocated=true" in txt:
                        schema.db_colocated = True
                elif any(is_word(x, "TABLE") for x in st[1:4]):
                    _parse_create_table(st, schema, line)
                elif any(is_word(x, "INDEX") for x in st[1:3]):
                    _parse_create_index(st, schema, line)
            elif is_word(st[0], "ALTER") and len(st) > 1 and is_word(st[1], "TABLE"):
                _parse_alter_table(st, schema, line)
        except IndexError:
            schema.parse_notes.append("could not parse statement at line %d" % line)
    if db_colocated is not None:
        schema.db_colocated = db_colocated
    # Partitions inherit columns and PK shape from the parent when they declare none.
    for t in schema.tables.values():
        if t.partition_of and t.partition_of in schema.tables:
            parent = schema.tables[t.partition_of]
            if not t.cols:
                t.cols = dict(parent.cols)
                t.col_order = list(parent.col_order)
            if t.pk is None and parent.pk is not None:
                t.pk = Index(t.name + "_pkey", t.name,
                             [KeyCol(k.col, k.expr, k.mode) for k in parent.pk.keys],
                             unique=True, is_pk=True, line=t.line)
    schema.resolve_modes()
    return schema
