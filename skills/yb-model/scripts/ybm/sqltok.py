"""Small SQL tokenizer shared by the DDL and query parsers. Standard library only.

It is not a SQL parser. It produces a flat token list with parenthesis depth so the callers
can find clauses at the top level of a statement. Comments are dropped; string, dollar-quoted
and quoted-identifier tokens are kept intact.
"""

import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUM = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?")
_PARAM = re.compile(r"\$\d+")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_OPS = ("::", "<=", ">=", "<>", "!=", "||", "->>", "->", "#>>", "#>", "~~*", "!~~", "~~",
        "@>", "<@", "&&", "=", "<", ">", "+", "-", "*", "/", "%", "~", "^", "|", "&", "@", "#")


class Tok:
    __slots__ = ("kind", "text", "depth", "pos")

    def __init__(self, kind, text, depth, pos):
        self.kind = kind  # word | qident | str | num | param | op | ( | ) | , | ; | . | [ | ]
        self.text = text
        self.depth = depth
        self.pos = pos

    @property
    def up(self):
        return self.text.upper() if self.kind == "word" else self.text

    @property
    def ident(self):
        """Identifier value: lower-cased unless quoted."""
        if self.kind == "qident":
            return self.text[1:-1].replace('""', '"')
        if self.kind == "word":
            return self.text.lower()
        return None

    def __repr__(self):
        return "Tok(%s,%r,%d)" % (self.kind, self.text, self.depth)


def tokenize(sql):
    toks, i, n, depth = [], 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "'" or ((c in "eEbBxXnN") and i + 1 < n and sql[i + 1] == "'"):
            start = i
            if c != "'":
                i += 1
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c in "eE":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("str", sql[start:j + 1], depth, start))
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("qident", sql[i:j + 1], depth, i))
            i = j + 1
            continue
        if c == "$":
            m = _PARAM.match(sql, i)
            if m:
                toks.append(Tok("param", m.group(0), depth, i))
                i = m.end()
                continue
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                j = n if j == -1 else j + len(tag)
                toks.append(Tok("str", sql[i:j], depth, i))
                i = j
                continue
        m = _WORD.match(sql, i)
        if m:
            toks.append(Tok("word", m.group(0), depth, i))
            i = m.end()
            continue
        m = _NUM.match(sql, i)
        if m:
            toks.append(Tok("num", m.group(0), depth, i))
            i = m.end()
            continue
        if c == "(":
            toks.append(Tok("(", c, depth, i))
            depth += 1
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            toks.append(Tok(")", c, depth, i))
            i += 1
            continue
        if c in ",;.[]":
            toks.append(Tok(c, c, depth, i))
            i += 1
            continue
        for op in _OPS:
            if sql.startswith(op, i):
                toks.append(Tok("op", op, depth, i))
                i += len(op)
                break
        else:
            toks.append(Tok("op", c, depth, i))
            i += 1
    return toks


def split_statements(toks):
    """Split a token list on top-level semicolons."""
    out, cur = [], []
    for t in toks:
        if t.kind == ";" and t.depth == 0:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def rebase(toks):
    """Return a copy of toks with depth relative to the first token's depth."""
    if not toks:
        return []
    base = min(t.depth for t in toks)
    return [Tok(t.kind, t.text, t.depth - base, t.pos) for t in toks]


def match_paren(toks, i):
    """toks[i] is '('; return the index of its matching ')'."""
    d = toks[i].depth
    for j in range(i + 1, len(toks)):
        if toks[j].kind == ")" and toks[j].depth == d:
            return j
    return len(toks) - 1


def split_top(toks, sep=","):
    """Split tokens on separators at the lowest depth present."""
    if not toks:
        return []
    d = min(t.depth for t in toks)
    parts, cur = [], []
    for t in toks:
        if t.kind == sep and t.depth == d:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def text_of(toks):
    """Normalised text of a token run, used to compare expressions."""
    out = []
    for t in toks:
        if t.kind == "word":
            out.append(t.text.lower())
        elif t.kind == "qident":
            out.append(t.ident)
        else:
            out.append(t.text)
    s = " ".join(out)
    s = re.sub(r"\s*([().,:\[\]])\s*", r"\1", s)
    return s


def is_word(t, *words):
    return t is not None and t.kind == "word" and t.up in words
