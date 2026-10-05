"""Release-aware facts from rules/versions.json (built by scripts/extract-version-data.py).

The engine never assumes one release behaves like another. For the customer's release it
answers three questions:

1. What are the planner settings when the bundle does not include pg_settings? The compiled
   default, unless a deployment tool overrides it (yugabyted, or YBA for new universes). When
   the tools disagree the setting is *conditional* and findings that depend on it say so.
2. Does a feature a fix relies on exist on this release (for example merge scan streams)?
3. Is a rule's behaviour pinned by a regress test on this release?

and, for replay on a different image, which of those answers differ between the two releases.
"""

import json
import os
import re

_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "..", "rules", "versions.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _DATA = json.load(fh)
        except (OSError, ValueError):
            _DATA = {"tags": {}, "deployment_profiles": []}
    return _DATA


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def line(v):
    """Release line, e.g. 2025.2 or 2.20."""
    t = vt(v)
    return t[:2]


def resolve(version):
    """The newest known release at or below `version`, and a note when the match is loose."""
    if not version:
        return None, "release unknown"
    tags = sorted(_load()["tags"], key=vt)
    if not tags:
        return None, "no version table"
    want = vt(version)
    below = [t for t in tags if vt(t) <= want]
    if not below:
        return tags[0], "older than the version table (%s); using %s" % (version, tags[0])
    t = below[-1]
    if vt(t) == want[:len(vt(t))]:
        return t, None
    if line(t) != line(version):
        return t, "release %s not in the version table; nearest older release %s" % (version, t)
    return t, "release %s not in the version table; using %s from the same line" % (version, t)


def gucs(tag):
    return dict(_load()["tags"].get(tag, {}).get("gucs", {}))


def profiles(tag):
    """Effective GUC values per deployment tool on this release: the compiled default, and
    each tool profile extract-version-data.py found in that release's source."""
    base = gucs(tag)
    out = {"compiled default (manual install, upgraded YBA universe)": dict(base)}
    for tool, sets in sorted((_load()["tags"].get(tag, {}).get("profiles") or {}).items()):
        cur = dict(base)
        for k, v in sets.items():
            if isinstance(v, list):
                v = "/".join(v)  # the tool sets different values on different paths
            cur[k] = v
        out[tool] = cur
    return out


def setting(tag, name):
    """(value, source, per_profile): value is None when profiles disagree or it is absent."""
    if tag is None:
        return None, "release unknown", {}
    per = {k: v.get(name) for k, v in profiles(tag).items()}
    vals = set(per.values())
    if vals == {None}:
        return None, "absent on %s" % tag, per
    if len(vals) == 1:
        return vals.pop(), "default on %s for every deployment tool" % tag, per
    return None, "differs by deployment tool on %s" % tag, per


def available(tag, name):
    if tag is None:
        return None
    return gucs(tag).get(name) is not None


def pinned(tag, rule):
    """True / False if the version table knows the rule on this release, None otherwise."""
    if tag is None:
        return None
    a = _load()["tags"].get(tag, {}).get("anchors", {})
    return a.get(rule)


def drift(tag_a, tag_b, names=None, rules=None):
    """Differences between two releases: settings and rule pinning."""
    out = {"settings": {}, "rules": []}
    if not tag_a or not tag_b:
        return out
    pa, pb = profiles(tag_a), profiles(tag_b)
    keys = names or sorted(set(gucs(tag_a)) | set(gucs(tag_b)))
    for k in keys:
        va = {p: v.get(k) for p, v in pa.items()}
        vb = {p: v.get(k) for p, v in pb.items()}
        if va != vb:
            out["settings"][k] = {"customer": va, "replay": vb}
    for r in sorted(rules or []):
        if pinned(tag_a, r) != pinned(tag_b, r):
            out["rules"].append(r)
    return out


_OBS = None


def _obs():
    global _OBS
    if _OBS is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            with open(os.path.join(here, "..", "rules", "observations.json"),
                      encoding="utf-8") as fh:
                _OBS = json.load(fh)
        except (OSError, ValueError):
            _OBS = {"runs": [], "rules": {}}
    return _OBS


def observations(rule):
    """Oracle observations recorded for a rule: [{release, mode, observation}]."""
    return list(_obs().get("rules", {}).get(rule, []))


def oracle_coverage(release):
    """The oracle runs recorded for this release, or None."""
    runs = [r for r in _obs().get("runs", []) if r.get("release") == release]
    return runs or None


def oracle_nearest(release):
    rels = sorted({r["release"] for r in _obs().get("runs", [])}, key=vt)
    if not rels or not release:
        return None
    return min(rels, key=lambda r: (abs(vt(r)[0] - vt(release)[0]) * 1000 +
                                    abs(vt(r)[1] - vt(release)[1]) * 100 +
                                    abs(vt(r)[2] - vt(release)[2])))
