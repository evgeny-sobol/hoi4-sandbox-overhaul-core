#!/usr/bin/env python3
"""Build the generated scenario catalog from per-mod TOML arc specs.

Reads `docs/scenarios/*.toml` in a mod directory, validates every spec
against the arc schema, and derives the catalog tables, the Mermaid diagrams,
the focus-boost closure and (in a later ticket) the telemetry labels.

Usage:
  python build_scenario_catalog.py <mod_dir> [--check] [--vanilla-root DIR]

`build` validates the specs, writes `docs/gdd/Scenarios Catalog.md` and
applies the focus-boost splice to the mod's `.include` files. `--check`
writes nothing and exits non-zero on any spec error or any spec-to-artifact
drift. Target tags are checked against the vanilla `common/country_tags`
registry when a vanilla root is available (default: the standard Steam
install); otherwise only the tag shape is checked.

Exit code: 0 on success, 1 on any error or drift.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

STATUSES = ("ready", "draft")
TAG_RE = re.compile(r"^[A-Z]{3}$")
FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
FOCUS_ID_RE = re.compile(r"\b[A-Z]{2,4}_[A-Za-z0-9_]+\b")
JOIN_FILTERS = frozenset({"same_ideology", "same_continent"})

# Aggressor tag -> focus-graph file stem in docs/gdd/National Focuses/.
# Falls back to the lowercased tag when the mod names the file that way.
GRAPH_ALIASES = {
    "GER": "germany",
    "SOV": "soviet",
    "JAP": "japan",
    "ITA": "italy",
    "ENG": "uk",
    "USA": "usa",
    "FRA": "france",
    "HUN": "hungary",
}

DEFAULT_VANILLA = Path(r"C:\Games\Steam\steamapps\common\Hearts of Iron IV")


def load_toml(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f), None
    except tomllib.TOMLDecodeError as e:
        return None, f"{path.name}: invalid TOML: {e}"
    except OSError as e:
        return None, f"{path.name}: unreadable: {e}"


def graph_focus_ids(graph_path: Path) -> set[str]:
    text = graph_path.read_text(encoding="utf-8", errors="replace")
    return set(FOCUS_ID_RE.findall(text))


def vanilla_tags(vanilla_root: Path | None) -> set[str] | None:
    if vanilla_root is None or not vanilla_root.is_dir():
        return None
    tags: set[str] = set()
    for p in (vanilla_root / "common" / "country_tags").glob("*.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^([A-Z]{3})\s*=", text, re.M):
            tags.add(m.group(1))
    return tags or None


def check_tag(tag: object, field: str, known: set[str] | None, errors: list[str], name: str) -> None:
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        errors.append(f"{name}: {field} {tag!r} is not a 3-letter uppercase tag")
        return
    if known is not None and tag not in known:
        errors.append(f"{name}: {field} {tag!r} is not a known country tag")


def validate_spec(name: str, data: dict, graphs_dir: Path, known_tags: set[str] | None) -> list[str]:
    errors: list[str] = []

    def req(key: str, kind: type) -> object:
        if key not in data:
            errors.append(f"{name}: missing required field {key!r}")
            return None
        if not isinstance(data[key], kind):
            errors.append(f"{name}: field {key!r} must be {kind.__name__}, got {type(data[key]).__name__}")
            return None
        return data[key]

    spec_id = req("id", str)
    status = req("status", str)
    if isinstance(status, str) and status not in STATUSES:
        errors.append(f"{name}: status {status!r} must be one of {STATUSES}")
    aggressor = req("aggressor", str)
    if isinstance(aggressor, str):
        if not TAG_RE.match(aggressor):
            errors.append(f"{name}: aggressor {aggressor!r} is not a 3-letter uppercase tag")
    if isinstance(spec_id, str) and isinstance(aggressor, str):
        # The file may be named `<id>.toml` or grouped as `<aggressor>_<id>.toml`.
        if spec_id != name and name != f"{aggressor}_{spec_id}":
            errors.append(
                f"{name}: id {spec_id!r} does not match the file name "
                f"(expected {name!r} or {aggressor}_{spec_id!r})")
    number = data.get("number", None)
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
        errors.append(f"{name}: number must be a positive integer")
    if status == "ready" and number is None:
        errors.append(f"{name}: ready arcs require a number")

    targets = req("targets", dict)
    target_tags: list[str] = []
    variants: list[str] = []
    if isinstance(targets, dict):
        variants = sorted(targets)
        if not variants:
            errors.append(f"{name}: targets must declare at least one variant")
        for variant in variants:
            lst = targets[variant]
            if not isinstance(lst, list) or not lst or any(not isinstance(t, str) for t in lst):
                errors.append(f"{name}: targets.{variant} must be a non-empty list of tags")
                continue
            if len(lst) > 4:
                errors.append(f"{name}: targets.{variant} holds {len(lst)} tags; derail arms cover 1-4")
            for t in lst:
                check_tag(t, f"targets.{variant}", known_tags, errors, name)
                target_tags.append(t)

    key = data.get("key", None)
    if key is not None and (not isinstance(key, str) or not FUNC_NAME_RE.match(key)):
        errors.append(f"{name}: key must be an identifier naming the pin trigger and pick label")

    ladder = req("ladder", dict)
    if isinstance(ladder, dict):
        for key in ("crises_at_month", "peak_at_month"):
            v = ladder.get(key, None)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errors.append(f"{name}: ladder.{key} must be a non-negative integer")
        cm, pm = ladder.get("crises_at_month"), ladder.get("peak_at_month")
        if isinstance(cm, int) and isinstance(pm, int) and not cm < pm:
            errors.append(f"{name}: ladder.peak_at_month must be after ladder.crises_at_month")
        funcs = [ladder.get(key, None) for key in ("crises_func", "peak_func")]
        if any(f is not None for f in funcs):
            if any(not isinstance(f, str) or not FUNC_NAME_RE.match(f) for f in funcs):
                errors.append(f"{name}: ladder.crises_func and ladder.peak_func come as a pair of HSL function names")

    joiners = req("joiners", dict)
    if isinstance(joiners, dict):
        if joiners.get("select") != "top_n_by_scorer":
            errors.append(f"{name}: joiners.select must be 'top_n_by_scorer'")
        n = joiners.get("n", None)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            errors.append(f"{name}: joiners.n must be a positive integer")
        invite = joiners.get("invite_event", None)
        if not isinstance(invite, str) or not invite.strip():
            errors.append(f"{name}: joiners.invite_event must be a non-empty event id")
        require = joiners.get("require", [])
        if not isinstance(require, list) or any(r not in JOIN_FILTERS for r in require):
            errors.append(f"{name}: joiners.require must be a subset of {sorted(JOIN_FILTERS)}")
        elif len(require) != len(set(require)):
            errors.append(f"{name}: joiners.require has duplicate entries")

    gate = data.get("gate", None)
    if gate is not None:
        if not isinstance(gate, dict):
            errors.append(f"{name}: gate must be a table")
        else:
            if "ideology" in gate or "at_phase" in gate:
                if not isinstance(gate.get("ideology"), str):
                    errors.append(f"{name}: gate.ideology must be a string")
                if not isinstance(gate.get("at_phase"), str):
                    errors.append(f"{name}: gate.at_phase must be a string")
            hold_keys = ("hold_ideology", "hold_max_months", "hold_reason")
            hold_present = [k for k in hold_keys if k in gate]
            if hold_present and len(hold_present) != len(hold_keys):
                errors.append(f"{name}: gate hold fields come as a set {hold_keys}")
            if "hold_ideology" in gate:
                if not isinstance(gate.get("hold_ideology"), str):
                    errors.append(f"{name}: gate.hold_ideology must be a string")
                cap = gate.get("hold_max_months")
                if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
                    errors.append(f"{name}: gate.hold_max_months must be a positive integer")
                reason = gate.get("hold_reason")
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"{name}: gate.hold_reason must be a non-empty string")
                lfuncs = data.get("ladder") or {}
                if not (isinstance(lfuncs.get("crises_func"), str) and isinstance(lfuncs.get("peak_func"), str)):
                    errors.append(f"{name}: gate hold requires ladder crises_func and peak_func")
            if not hold_present and "ideology" not in gate and "at_phase" not in gate:
                errors.append(f"{name}: gate declares neither the derail pair nor the hold set")

    paths = req("paths", list)
    key_focuses: list[str] = []
    key_after: list[str] = []
    path_entries: list[tuple[frozenset, list[str]]] = []
    if isinstance(paths, list):
        if not paths:
            errors.append(f"{name}: paths must hold at least one path")
        for i, path in enumerate(paths):
            if not isinstance(path, dict):
                errors.append(f"{name}: paths[{i}] must be a table with focuses and optional variants")
                continue
            focuses = path.get("focuses", None)
            if not isinstance(focuses, list) or not focuses or any(not isinstance(f, str) for f in focuses):
                errors.append(f"{name}: paths[{i}].focuses must be a non-empty list of focus ids")
                continue
            entry_variants = path.get("variants", None)
            if entry_variants is None:
                entry_set = frozenset(variants)
            elif (not isinstance(entry_variants, list) or not entry_variants
                    or any(not isinstance(v, str) for v in entry_variants)):
                errors.append(f"{name}: paths[{i}].variants must be a non-empty list of variant keys")
                continue
            else:
                unknown = [v for v in entry_variants if v not in variants]
                if unknown:
                    errors.append(f"{name}: paths[{i}].variants {unknown} are not targets keys")
                    continue
                entry_set = frozenset(entry_variants)
            entry_after = path.get("after", None)
            after_list: list[str] = []
            if entry_after is not None:
                if (not isinstance(entry_after, list) or not entry_after
                        or any(not isinstance(f, str) for f in entry_after)):
                    errors.append(f"{name}: paths[{i}].after must be a non-empty list of focus ids")
                    continue
                after_list = list(entry_after)
            if not entry_set:
                errors.append(f"{name}: paths[{i}] covers no variant")
                continue
            key_focuses.extend(focuses)
            key_after.extend(after_list)
            path_entries.append((entry_set, focuses, tuple(after_list)))
    covered: set[str] = set()
    for entry_set, _focuses, _after in path_entries:
        covered |= set(entry_set)
    for variant in variants:
        if variant not in covered:
            errors.append(f"{name}: targets.{variant} is covered by no path")

    notes = req("notes", str)
    if isinstance(notes, str) and not notes.strip():
        errors.append(f"{name}: notes must not be empty")

    suppress = data.get("suppress", [])
    if not isinstance(suppress, list) or any(not isinstance(f, str) for f in suppress):
        errors.append(f"{name}: suppress must be a list of focus ids")
        suppress = []
    clash = sorted(set(suppress) & (set(key_focuses) | set(key_after)))
    if clash:
        errors.append(f"{name}: suppress focuses are also in a path: {clash}")

    if isinstance(aggressor, str) and TAG_RE.match(aggressor) and (key_focuses or suppress or key_after):
        stem = GRAPH_ALIASES.get(aggressor, aggressor.lower())
        graph_path = graphs_dir / f"{stem}.md"
        if not graph_path.is_file():
            tried = stem if stem in GRAPH_ALIASES.values() or stem == aggressor.lower() else stem
            errors.append(f"{name}: no focus graph for aggressor {aggressor} (tried {tried}.md)")
        else:
            graph_ids = graph_focus_ids(graph_path)
            for fid in key_focuses:
                if fid not in graph_ids:
                    errors.append(f"{name}: key focus {fid!r} not in {graph_path.name}")
            for fid in suppress:
                if fid not in graph_ids:
                    errors.append(f"{name}: suppress focus {fid!r} not in {graph_path.name}")
            for fid in key_after:
                if fid not in graph_ids:
                    errors.append(f"{name}: after focus {fid!r} not in {graph_path.name}")

    return errors


def validate_mod(mod_dir: Path, vanilla_root: Path | None) -> tuple[int, list[str]]:
    specs, errors = load_all_specs(mod_dir)
    if errors and not specs:
        spec_dir = mod_dir / "docs" / "scenarios"
        if not spec_dir.is_dir():
            return 0, errors
    verrs_errors: list[str] = []
    count, verrs = validate_against(specs, mod_dir, vanilla_root)
    verrs_errors.extend(verrs)
    return count, errors + verrs_errors


NODE_RE = re.compile(r"^\s*(n\d+)(?:\(\(|\[\{?|\{)(.*?)(?:\)\)|\]|\})\s*$")
EDGE_RE = re.compile(r"^\s*(n\d+)\s*(-->|x--x)\s*(n\d+)")

# Aggressor tag -> country display name for the catalog grouping.
COUNTRY_NAMES = {
    "GER": "Germany",
    "SOV": "Soviet Union",
    "JAP": "Japan",
    "ITA": "Italy",
    "ENG": "United Kingdom",
    "USA": "United States",
    "FRA": "France",
    "HUN": "Hungary",
}

CATALOG_REL = Path("docs/gdd/Scenarios Catalog.md")
BOOST_ANCHOR = "      $ai_sandbox_modifier()"
BOOST_INSERT = "      +modifier:\n        $ai_scenario_focus_boost()\n"
BOOST_MARKER = "$ai_scenario_focus_boost"
# Completion-gated boost: one gate line per listed focus, the boost holds only
# once all of them are completed (docs/gdd/Scenarios.md "Paths").
GATE_MARKER = "$ai_scenario_focus_gate_after"

# Per-focus completion telemetry (issue 31): one +completion_reward block,
# appended at the end of the focus body, per focus in the boost plan. The
# builder owns it exactly as it owns the boost, so a rewire cannot ship a path
# without its sc_focus lines (docs/gdd/Scenarios.md "Hooks and telemetry").
FOCUS_LOG_MARKER = "$sandbox_log_sc_focus"


def gated_insert(variant: str) -> str:
    return f"      +modifier:\n        $ai_scenario_focus_boost_variant({variant})\n"


def after_insert(after: tuple[str, ...]) -> str:
    lines = ["      +modifier:"]
    for fid in after:
        lines.append(f"        {GATE_MARKER}({fid})")
    lines += ["        factor(5)", "        is_live_scenario_aggressor()"]
    return "\n".join(lines) + "\n"


def log_insert(focus_id: str) -> str:
    return f"    +completion_reward:\n      $sandbox_log_sc_focus({focus_id})\n"


SUPPRESS_MARKER = "$ai_scenario_focus_suppress"


def suppress_insert() -> str:
    return "      +modifier:\n        $ai_scenario_focus_suppress()\n"


def canonical_inserts() -> list[str]:
    """Every insert shape the splicer owns (plain plus any gated form)."""
    return [BOOST_INSERT]


def load_all_specs(mod_dir: Path) -> tuple[list[tuple[str, dict]], list[str]]:
    spec_dir = mod_dir / "docs" / "scenarios"
    if not spec_dir.is_dir():
        return [], [f"no spec directory: {spec_dir}"]
    out: list[tuple[str, dict]] = []
    errors: list[str] = []
    for path in sorted(spec_dir.glob("*.toml")):
        data, err = load_toml(path)
        if err is not None:
            errors.append(err)
            continue
        out.append((path.stem, data))
    return out, errors


def load_graph(graph_path: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Focus graph: prerequisite map plus mutual-exclusion pairs."""
    text = graph_path.read_text(encoding="utf-8", errors="replace")
    id_by_node: dict[str, str] = {}
    for raw in text.splitlines():
        m = NODE_RE.match(raw)
        if m:
            id_by_node[m.group(1)] = m.group(2).strip().strip('"')
    prereq: dict[str, set[str]] = {}
    excl: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        e = EDGE_RE.match(raw)
        if not e:
            continue
        a, kind, b = e.groups()
        fa, fb = id_by_node.get(a), id_by_node.get(b)
        if not fa or not fb:
            continue
        if kind == "-->":
            prereq.setdefault(fb, set()).add(fa)
        else:
            excl.add(tuple(sorted((fa, fb))))
    return prereq, excl


def full_closure(seeds: list[str], prereq: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    frontier = list(seeds)
    while frontier:
        f = frontier.pop()
        for p in prereq.get(f, ()):
            if p not in seen:
                seen.add(p)
                frontier.append(p)
    return seen


def boost_set(keys: list[str], prereq: dict[str, set[str]], excl: set[tuple[str, str]]) -> set[str]:
    """Boost closure for one arc: keys plus every prerequisite ancestor.

    On a mutually exclusive fork where exactly one side is a key focus, only
    that side is boosted; a fork with no key side boosts both. A key focus is
    never dropped.
    """
    keyset = set(keys)
    need = set(keyset) | full_closure(keys, prereq)
    for a, b in excl:
        if a in need and b in need and (a in keyset) != (b in keyset):
            need.discard(b if a in keyset else a)
    return need


def graph_path_for(mod_dir: Path, aggressor: str) -> Path:
    stem = GRAPH_ALIASES.get(aggressor, aggressor.lower())
    return mod_dir / "docs" / "gdd" / "National Focuses" / f"{stem}.md"


def variant_keys(data: dict) -> list[str]:
    """Declared target variants, sorted. Non-empty for valid specs."""
    return sorted(data["targets"])


def vidx(data: dict, variant: str) -> int:
    """Numeric id of a variant. HoI4 variables are floats: bare-word enum
    literals (a/b) do not survive a write/read round trip, so the engine
    only ever sees these indices. Letters stay in log labels only."""
    return variant_keys(data).index(variant)


def path_entries(data: dict) -> list[tuple[frozenset, list[str], tuple[str, ...]]]:
    """(variants, focuses, after) per path. A missing variants field means
    shared; a missing after field means the boost is unconditional."""
    keys = variant_keys(data)
    out = []
    for path in data["paths"]:
        vs = path.get("variants", None)
        after = path.get("after", None)
        out.append((
            frozenset(keys) if vs is None else frozenset(vs),
            list(path["focuses"]),
            tuple(after) if isinstance(after, list) else (),
        ))
    return out


def boost_plan(data: dict, prereq: dict[str, set[str]], excl: set[tuple[str, str]]) -> tuple[dict[str, tuple[frozenset | None, tuple[str, ...]]], list[str]]:
    """Focus -> (variant gate or None, completion gate tuple), plus errors.

    Closure runs per path. A focus used in every variant with no path gate
    stays plain; a focus in a strict subset of variants takes the variant
    gate; a focus whose paths all carry `after` takes the completion gate
    (every listed focus completed). A focus in any ungated path stays plain.
    Two distinct gates on one focus are ambiguous and reported.
    """
    keys = variant_keys(data)
    uses: dict[str, set[str]] = {}
    ungated: set[str] = set()
    gates: dict[str, set[tuple[str, ...]]] = {}
    for variants, focuses, after in path_entries(data):
        keep = boost_set(focuses, prereq, excl)
        for fid in keep:
            uses.setdefault(fid, set()).update(variants)
            if after:
                gates.setdefault(fid, set()).add(after)
            else:
                ungated.add(fid)
    errors: list[str] = []
    out: dict[str, tuple[frozenset | None, tuple[str, ...]]] = {}
    for fid, vs in uses.items():
        variant = None if set(vs) >= set(keys) else frozenset(vs)
        if fid in ungated or not gates.get(fid):
            after: tuple[str, ...] = ()
        elif len(gates[fid]) > 1:
            errors.append(f"{data.get('id', '?')}: conflicting after gates on {fid!r}: {sorted(gates[fid])}")
            after = ()
        else:
            after = next(iter(gates[fid]))
        if variant is not None and after:
            errors.append(f"{data.get('id', '?')}: {fid!r} is both variant-gated and after-gated (unsupported)")
        out[fid] = (variant, after)
    return out, errors


def path_tail(data: dict, variant: str) -> str:
    """Last focus of the first path listing the variant (file order)."""
    for variants, focuses, _after in path_entries(data):
        if variant in variants:
            return focuses[-1]
    raise KeyError(f"no path covers variant {variant!r}")


def arc_title(spec_id: str) -> str:
    return spec_id.replace("_", " ").capitalize()


def short_focus(fid: str, aggressor: str) -> str:
    prefix = aggressor + "_"
    return fid[len(prefix):] if fid.startswith(prefix) else fid


def render_diagram(number: int, keys: list[str], prereq: dict[str, set[str]],
                   excl: set[tuple[str, str]]) -> list[str]:
    """Mermaid diagram over the boost set, so it cannot disagree with it."""
    keyset = set(keys)
    keep = boost_set(keys, prereq, excl)
    kept_edges = [(p, b) for b in keep for p in prereq.get(b, ()) if p in keep]
    has_parent = {b for _, b in kept_edges}
    rootset = {f for f in keep if f not in has_parent}
    out = ["```mermaid", "flowchart TD", f"    subgraph arc{number}"]
    for fid in sorted(keep):
        if fid in rootset:
            out.append(f'        {fid}(["{fid}"])')
        elif fid in keyset:
            out.append(f'        {fid}[["{fid}"]]')
        else:
            out.append(f'        {fid}["{fid}"]')
    for p, b in sorted(set(kept_edges)):
        out.append(f"        {p} --> {b}")
    for a, b in sorted(excl):
        if a in keep and b in keep:
            out.append(f"        {a} x--x {b}")
    out.append("    end")
    out.append("```")
    return out


def render_catalog(specs: list[tuple[str, dict]], graphs: dict[str, tuple[dict, set]]) -> str:
    """Full catalog text: tables, notes and diagrams, deterministic."""
    by_agg: dict[str, list[tuple[str, dict]]] = {}
    for name, data in specs:
        by_agg.setdefault(data["aggressor"], []).append((name, data))
    for arcs in by_agg.values():
        arcs.sort(key=lambda nd: (nd[1].get("number") or 0, nd[0]))
    ordered_aggs = sorted(by_agg, key=lambda a: min(d.get("number") or 0 for _, d in by_agg[a]))

    out = [
        "# Scenarios Catalog",
        "",
        "Generated from `docs/scenarios/*.toml` by `core/tools/build_scenario_catalog.py` -",
        "do not edit by hand. The arc schema lives in `docs/gdd/Scenarios.md`.",
        "",
        "## Reading a diagram",
        "",
        "- `([id])` rounded - a branch entry / path root.",
        "- `[[id]]` double-bordered - a key focus the director boosts and logs.",
        "- `[id]` plain - an intermediate prerequisite, boosted as part of the path closure.",
        "- `A --> B` - B requires A.",
        "- `A x--x B` - mutually exclusive: taking one hides the other.",
        "",
    ]
    for agg in ordered_aggs:
        arcs = by_agg[agg]
        stem = GRAPH_ALIASES.get(agg, agg.lower())
        country = COUNTRY_NAMES.get(agg, stem.capitalize())
        out.append(f"## {country}")
        out.append("")
        vkeys = sorted({v for _, d in arcs for v in d["targets"]})
        out.append("| # | Aggressor | Arc | " + " | ".join(f"Variant {v}" for v in vkeys) + " | Key focuses | Status |")
        out.append("|" + "---|" * (5 + len(vkeys)))
        for name, data in arcs:
            keys = [f for _, plist, _after in path_entries(data) for f in plist]
            shorts = ", ".join(f"`{short_focus(f, agg)}`" for f in keys)
            cols = " | ".join(", ".join(data["targets"].get(v, ())) for v in vkeys)
            out.append(
                f"| {data.get('number', '-')} | {agg} | {arc_title(data['id'])} "
                f"| {cols} | {shorts} | {data['status']} |"
            )
        out.append("")
        prereq, excl = graphs[agg]
        for name, data in arcs:
            keys = [f for _, plist, _after in path_entries(data) for f in plist]
            out.append(f"### Arc {data.get('number', '-')}: {arc_title(data['id'])}")
            out.append("")
            out.append(data["notes"].strip())
            out.append("")
            out.append(render_labels(specs, data["id"]))
            out.append("")
            out.extend(render_diagram(data.get("number") or 0, keys, prereq, excl))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def split_include_blocks(text: str) -> list[tuple[str, int, int]]:
    """Split an .include file into (focus id, body start, body end) spans."""
    heads = [(m.group(1), m.start()) for m in re.finditer(r"^  focus\[id = ([A-Za-z0-9_]+)\]:", text, re.M)]
    spans = []
    for i, (fid, start) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(text)
        spans.append((fid, start, end))
    return spans


def required_inserts(gate: tuple[frozenset | None, tuple[str, ...]]) -> list[str]:
    """Canonical splice lines for one focus: plain when shared, one gated
    modifier per variant in the set, or a completion-gated modifier."""
    variant, after = gate
    if after:
        return [after_insert(after)]
    if variant is None:
        return [BOOST_INSERT]
    return [gated_insert(v) for v in sorted(variant)]


CANONICAL_RE = re.compile(
    r"      \+modifier:\n(?:"
    r"        \$ai_scenario_focus_boost(?:_variant)?\([^)\n]*\)"
    r"|"
    r"(?:        \$ai_scenario_focus_gate_after\([^)\n]*\)\n)+        factor\(5\)\n        is_live_scenario_aggressor\(\)"
    r")\n"
)
FOCUS_LOG_RE = re.compile(r"    \+completion_reward:\n      \$sandbox_log_sc_focus\([^)\n]*\)\n")
SUPPRESS_RE = re.compile(r"      \+modifier:\n        \$ai_scenario_focus_suppress\(\)\n")


def boost_shape_label(gate: tuple[frozenset | None, tuple[str, ...]]) -> str:
    variant, after = gate
    if after:
        return f"after {sorted(after)}"
    if variant is None:
        return "plain"
    return f"gated {sorted(variant)}"


def check_splice_file(path: Path, expected: dict[str, tuple[frozenset | None, tuple[str, ...]]], suppressed: set[str]) -> tuple[set[str], set[str], list[str]]:
    """Return (missing, unexpected, errors) for one .include file.

    Three owned splices: the boost modifier, the sc_focus completion_reward
    block, and the suppress modifier (factor 0 for the AI while the arc is
    live). A focus in the boost plan must carry its boost shape and one
    sc_focus block; a focus in the suppress set must carry the suppress
    modifier; a focus outside both carrying any marker is unexpected. A marker
    the canonical regex does not own is an error, never converged silently.
    """
    text = path.read_text(encoding="utf-8")
    spans = {fid: (s, e) for fid, s, e in split_include_blocks(text)}
    boosted: set[str] = set()
    logged: set[str] = set()
    suppressed_present: set[str] = set()
    errors: list[str] = []
    for fid, start, end in split_include_blocks(text):
        block = text[start:end]
        if BOOST_MARKER in block or GATE_MARKER in block:
            boosted.add(fid)
        if FOCUS_LOG_MARKER in block:
            logged.add(fid)
        if SUPPRESS_MARKER in block:
            suppressed_present.add(fid)
    for fid in sorted(expected):
        if fid not in spans:
            continue
        block = text[spans[fid][0]:spans[fid][1]]
        for ins in required_inserts(expected[fid]):
            if ins not in block:
                want = boost_shape_label(expected[fid])
                errors.append(f"{path.name}: {fid!r} lacks the {want} boost; run build")
        if log_insert(fid) not in block:
            errors.append(f"{path.name}: {fid!r} lacks the sc_focus log; run build")
        if SUPPRESS_MARKER in block:
            errors.append(f"{path.name}: {fid!r} is both boosted and suppressed; run build")
    for fid in sorted(suppressed):
        if fid not in spans:
            errors.append(f"{path.name}: suppressed focus {fid!r} not found")
            continue
        block = text[spans[fid][0]:spans[fid][1]]
        if suppress_insert() not in block:
            errors.append(f"{path.name}: {fid!r} lacks the suppress modifier; run build")
        if BOOST_MARKER in block or GATE_MARKER in block or FOCUS_LOG_MARKER in block:
            errors.append(f"{path.name}: suppressed focus {fid!r} is still boosted; run build")
    for fid in sorted(boosted | logged | suppressed_present):
        if fid not in expected and fid not in suppressed:
            continue
        block = text[spans[fid][0]:spans[fid][1]]
        if (BOOST_MARKER in block or GATE_MARKER in block) and CANONICAL_RE.search(block) is None:
            errors.append(f"{path.name}: non-canonical boost in {fid!r}; run build or fix by hand")
        if FOCUS_LOG_MARKER in block and FOCUS_LOG_RE.search(block) is None:
            errors.append(f"{path.name}: non-canonical sc_focus in {fid!r}; run build or fix by hand")
        if SUPPRESS_MARKER in block and SUPPRESS_RE.search(block) is None:
            errors.append(f"{path.name}: non-canonical suppress in {fid!r}; run build or fix by hand")
    unexpected = (boosted | logged | suppressed_present) - set(expected) - set(suppressed)
    return set(expected) - boosted, unexpected, errors


def apply_splice_file(path: Path, expected: dict[str, tuple[frozenset | None, tuple[str, ...]]], suppressed: set[str]) -> tuple[int, int, list[str]]:
    """Converge one .include file to the expected boost + sc_focus + suppress plan.

    Returns (added, removed, errors). For a boosted focus the required boost
    inserts go after the sandbox-modifier anchor and one sc_focus block is
    appended at the end of the body; a suppressed focus gets the factor-0
    modifier after the same anchor; canonical inserts a focus should no longer
    carry come out. A focus outside both sets is stripped of all three splices
    (issue 30: no migration mode). Anything the canonical shapes do not own is
    an error, never silently rewritten.
    """
    text = path.read_text(encoding="utf-8")
    added, removed = 0, 0
    errors: list[str] = []

    def spans() -> dict[str, tuple[int, int]]:
        return {fid: (s, e) for fid, s, e in split_include_blocks(text)}

    for fid in sorted(set(expected) | suppressed):
        if fid not in spans():
            errors.append(f"{path.name}: expected focus {fid!r} not found")
            continue
        start, end = spans()[fid]
        block = text[start:end]
        if fid in expected:
            for ins in required_inserts(expected[fid]):
                if ins in block:
                    continue
                pos = block.find(BOOST_ANCHOR + "\n")
                if pos < 0:
                    errors.append(f"{path.name}: no splice anchor in {fid!r}")
                    continue
                at = start + pos + len(BOOST_ANCHOR) + 1
                text = text[:at] + ins + text[at:]
                added += 1
                start, end = spans()[fid]
                block = text[start:end]
            if log_insert(fid) not in block:
                if FOCUS_LOG_MARKER in block and FOCUS_LOG_RE.search(block) is None:
                    errors.append(f"{path.name}: non-canonical sc_focus in {fid!r}; remove by hand")
                else:
                    body = block.rstrip("\n")
                    trailing = block[len(body):] or "\n"
                    text = text[:start] + body + "\n" + log_insert(fid) + trailing + text[end:]
                    added += 1
                    start, end = spans()[fid]
                    block = text[start:end]
            want = set(required_inserts(expected[fid]))
            found = set(CANONICAL_RE.findall(block))
            for stale in sorted(found - want):
                text = text[:start] + block.replace(stale, "", 1) + text[spans()[fid][1]:]
                removed += 1
                start, end = spans()[fid]
                block = text[start:end]
        else:
            # Suppressed (or dropped): strip any canonical boost/log still on it.
            # Suppress wins over boost: a suppressed OR alternative is not
            # boosted (expected_boosts already dropped it).
            new_block, n1 = CANONICAL_RE.subn("", block)
            new_block, n2 = FOCUS_LOG_RE.subn("", new_block)
            if n1 or n2:
                if BOOST_MARKER in new_block or GATE_MARKER in new_block or FOCUS_LOG_MARKER in new_block:
                    errors.append(f"{path.name}: non-canonical boost/log in {fid!r}; remove by hand")
                else:
                    text = text[:start] + new_block + text[end:]
                    removed += n1 + n2
                    start, end = spans()[fid]
                    block = text[start:end]
        if fid in suppressed:
            if suppress_insert() not in block:
                pos = block.find(BOOST_ANCHOR + "\n")
                if pos < 0:
                    errors.append(f"{path.name}: no splice anchor in {fid!r}")
                else:
                    at = start + pos + len(BOOST_ANCHOR) + 1
                    text = text[:at] + suppress_insert() + text[at:]
                    added += 1
                    start, end = spans()[fid]
                    block = text[start:end]
        elif SUPPRESS_MARKER in block:
            new_block, n = SUPPRESS_RE.subn("", block)
            if n == 0 or SUPPRESS_MARKER in new_block:
                errors.append(f"{path.name}: non-canonical suppress in {fid!r}; remove by hand")
            else:
                text = text[:start] + new_block + text[end:]
                removed += 1
    for _fid in [fid for fid, _s, _e in split_include_blocks(text)]:
        start, end = spans()[_fid]
        block = text[start:end]
        if _fid not in expected and _fid not in suppressed and (
                BOOST_MARKER in block or GATE_MARKER in block or FOCUS_LOG_MARKER in block or SUPPRESS_MARKER in block):
            new_block, n1 = CANONICAL_RE.subn("", block)
            new_block, n2 = FOCUS_LOG_RE.subn("", new_block)
            new_block, n3 = SUPPRESS_RE.subn("", new_block)
            if ((n1 == 0 and n2 == 0 and n3 == 0)
                    or BOOST_MARKER in new_block or GATE_MARKER in new_block or FOCUS_LOG_MARKER in new_block or SUPPRESS_MARKER in new_block):
                errors.append(f"{path.name}: non-canonical boost/log/suppress in {_fid!r}; remove by hand")
                continue
            text = text[:start] + new_block + text[end:]
            removed += 1
    if added or removed:
        path.write_text(text, encoding="utf-8")
    return added, removed, errors


def include_path_for(mod_dir: Path, aggressor: str) -> Path:
    stem = GRAPH_ALIASES.get(aggressor, aggressor.lower())
    return mod_dir / "common" / "national_focus" / f"{stem}.include"


SCENARIO_HSL_REL = Path("common/scripted_effects/99_sandbox_scenarios.hsl")
GEN_HSL_REL = Path("common/scripted_effects/99_sandbox_scenarios_gen.hsl")
LABEL_RE = re.compile(r"\$sandbox_log_sc\((sc_goal|sc_justify),\s*([A-Za-z_0-9]+)\)")


def expected_labels(specs: list[tuple[str, dict]]) -> dict[str, set[str]]:
    """Expected telemetry labels derived from the specs.

    `sc_goal` covers every declared pair in both directions; `sc_justify`
    covers the aggressor-to-target direction. Always lowercase.
    """
    out = {"sc_goal": set(), "sc_justify": set()}
    for _, data in specs:
        agg = data["aggressor"].lower()
        for variant in data["targets"]:
            for tgt in data["targets"][variant]:
                t = tgt.lower()
                out["sc_goal"].add(f"{agg}_on_{t}")
                out["sc_goal"].add(f"{t}_on_{agg}")
                out["sc_justify"].add(f"{agg}_on_{t}")
    return out


FUNC_HEAD_RE = re.compile(r"^(sandbox_\w+)\(\):$")
ARC_NUM_RE = re.compile(r"global\.sandbox_scenario == (\d+)")


def label_occurrences(mod_dir: Path) -> list[tuple[str, str, str, int | None]]:
    """Every sc_goal/sc_justify log line as (line, label, source file, arc).

    Both the hand catalog and the generated sibling are scanned, without
    deduplication. The arc comes from the enclosing `sandbox_scenario == N`
    guard, so a label shared by two arcs (each arc logs its own direction of
    a common pair) is attributed to the arc that logs it; lines outside any
    guard carry None. A case-drifted copy must not hide behind a correct one.
    """
    out: list[tuple[str, str, str, int | None]] = []
    for rel in (SCENARIO_HSL_REL, GEN_HSL_REL):
        path = mod_dir / rel
        if not path.is_file():
            continue
        arc: int | None = None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if FUNC_HEAD_RE.match(line):
                arc = None
                continue
            if line.startswith(("if", "elif")):
                m = ARC_NUM_RE.search(line)
                if m:
                    arc = int(m.group(1))
            for kind, label in LABEL_RE.findall(raw):
                if kind in ("sc_goal", "sc_justify"):
                    out.append((kind, label, rel.as_posix(), arc))
    return out


def check_labels(mod_dir: Path, specs: list[tuple[str, dict]]) -> list[str]:
    errors: list[str] = []
    expected = expected_labels(specs)
    spec_numbers = {d["number"] for _, d in specs if isinstance(d.get("number"), int)}
    occurrences = label_occurrences(mod_dir)
    seen: dict[str, set[str]] = {"sc_goal": set(), "sc_justify": set()}
    in_gen: dict[str, set[str]] = {"sc_goal": set(), "sc_justify": set()}
    for line, exact, source, _ in occurrences:
        lowered = exact.lower()
        seen[line].add(lowered)
        if source == GEN_HSL_REL.as_posix():
            in_gen[line].add(lowered)
        if lowered in expected[line]:
            if exact != lowered:
                errors.append(f"{line} label {exact!r} differs in case; want {lowered!r} ({source})")
        elif source == GEN_HSL_REL.as_posix():
            # The generated file mirrors the specs exactly: any label there
            # that no spec expects is a rogue hand-edit, named on the spot.
            # Hand-catalog labels outside the inventory belong to arcs without
            # specs (mid-migration) and are skipped.
            errors.append(f"{line} label {exact!r} in generated file not expected from any spec")
    for line in ("sc_goal", "sc_justify"):
        for label in sorted(expected[line] - seen[line]):
            errors.append(f"{line} label {label!r} expected from specs but missing")
    for line, exact, source, arc in occurrences:
        lowered = exact.lower()
        if (
            source != GEN_HSL_REL.as_posix()
            and arc in spec_numbers
            and lowered in expected[line]
            and lowered in in_gen[line]
        ):
            errors.append(
                f"{line} label {exact!r} logged in both hand catalog and generated file; "
                "delete the hand-written branch"
            )
    return errors


def render_labels(specs: list[tuple[str, dict]], spec_id: str) -> str:
    """One-line label inventory for a single arc's catalog section."""
    expected = expected_labels([(n, d) for n, d in specs if d["id"] == spec_id])
    goal = ", ".join(sorted(expected["sc_goal"]))
    justify = ", ".join(sorted(expected["sc_justify"]))
    return f"**Telemetry labels**: `sc_goal`: {goal}; `sc_justify`: {justify}."


def gen_function_names(spec_id: str) -> dict[str, str]:
    """Generated per-arc function names for every mechanical aspect.

    Only set_targets, seed, tick, telemetry, derail and pick aspects exist
    yet; later tickets add any remaining aspects under the same contract.
    """
    return {
        "set_targets": f"gen_{spec_id}_set_targets",
        "seed": f"gen_{spec_id}_seed",
        "tick": f"gen_{spec_id}_tick",
        "telemetry": f"gen_{spec_id}_telemetry",
        "derail": f"gen_{spec_id}_derail",
        "pin": f"gen_{spec_id}_pin",
        "eligible": f"gen_{spec_id}_eligible",
        "pick_log": f"gen_{spec_id}_pick_log",
        "roll_variant": f"gen_{spec_id}_roll_variant",
        "log_variant": f"gen_{spec_id}_log_variant",
    }


def render_peak_gate(names: dict[str, str], number: int, aggressor: str,
                     data: dict, peak_month: int, peak_func: str) -> list[str]:
    """Peak rung gated on the live path tail (AI aggressors only).

    The rung waits in crises until the AI completes the last focus of the
    live variant's path; human aggressors and a fallback threshold
    (peak + 12 months) proceed on schedule. Only completed focuses count:
    a bypassed tail stalls to the fallback.
    """
    out = [
        f"  elif global.sandbox_scenario == {number} and global.sandbox_scenario_phase == 1 and global.sandbox_scenario_arc_months >= {peak_month}:",
    ]
    keys = sorted(data["targets"])
    for i, variant in enumerate(keys):
        tail = path_tail(data, variant)
        out.append(f"    if global.sandbox_target_variant == {i}:" if i == 0
                   else f"    elif global.sandbox_target_variant == {i}:" if i < len(keys) - 1
                   else "    else:")
        out.append(f"      {aggressor}:")
        out.append(f"        if has_completed_focus({tail}):")
        out.extend(_advance_peak(number, peak_func))
        out.append(f"        elif {aggressor}->is_ai(no):")
        out.extend(_advance_peak(number, peak_func))
        out.append(f"        elif global.sandbox_scenario_arc_months >= {peak_month + 12}:")
        out.extend(_advance_peak(number, peak_func))
    return out


def _advance_peak(number: int, peak_func: str) -> list[str]:
    """Phase advance plus peak content, nested one level deeper than usual."""
    return [
        "          global.&sandbox_scenario_phase = 2",
        "          $sandbox_log_sc(sc_phase, peak)",
        f"          {peak_func}()",
    ]


def render_roll_variant(names: dict[str, str], number: int, data: dict) -> list[str]:
    """Variant roll over the spec's declared keys (any count)."""
    keys = sorted(data["targets"])
    out = [
        f"{names['roll_variant']}():",
        f"  if global.sandbox_scenario == {number}:",
        f"    variant_roll = randi(0, {len(keys) - 1})",
    ]
    for i, variant in enumerate(keys):
        out.append(f"    if variant_roll == {i}:" if i == 0
                   else f"    elif variant_roll == {i}:" if i < len(keys) - 1
                   else "    else:")
        out.append(f"      global.&sandbox_target_variant = {i}")
    return out


def render_log_variant(names: dict[str, str], number: int, data: dict) -> list[str]:
    """Variant line for every declared key."""
    keys = sorted(data["targets"])
    out = [f"{names['log_variant']}():"]
    for i, variant in enumerate(keys):
        out.append(f"  if global.sandbox_scenario == {number} and global.sandbox_target_variant == {i}:" if i == 0
                   else f"  elif global.sandbox_scenario == {number} and global.sandbox_target_variant == {i}:")
        out.append(f"    $sandbox_log_sc(sc_variant, {variant})")
    return out


def render_telemetry(number: int, aggressor: str, targets: dict[str, list[str]]) -> list[str]:
    """Monthly s7 sampling for one arc: power per live actor, goal and
    justify lines per declared pair, both label directions where the
    convention requires. Mirrors the hand-written shape it replaces."""
    agg = aggressor
    out = []
    keys = sorted(targets)
    for i, variant in enumerate(keys):
        out.append(
            f"  if global.sandbox_target_variant == {i}:"
            if i == 0
            else f"  elif global.sandbox_target_variant == {i}:"
            if i < len(keys) - 1
            else "  else:"
        )
        out += [
            f"    {agg}:",
            "      sc_div = num_divisions",
            "      sc_fab = num_of_factories",
            "      $sandbox_log_sc_power()",
        ]
        for tgt in targets[variant]:
            out += [
                f"    if country_exists({tgt}):",
                f"      {tgt}:",
                "        sc_div = num_divisions",
                "        sc_fab = num_of_factories",
                "        $sandbox_log_sc_power()",
                f"        if has_wargoal_against({agg}) or is_justifying_wargoal_against({agg}):",
                f"          $sandbox_log_sc(sc_goal, {tgt.lower()}_on_{agg.lower()})",
            ]
        out.append(f"    {agg}:")
        for tgt in targets[variant]:
            out += [
                f"      if has_wargoal_against({tgt}) or is_justifying_wargoal_against({tgt}):",
                f"        $sandbox_log_sc(sc_goal, {agg.lower()}_on_{tgt.lower()})",
                f"      if is_justifying_wargoal_against({tgt}):",
                f"        $sandbox_log_sc(sc_justify, {agg.lower()}_on_{tgt.lower()})",
            ]
    return out


def render_derail(number: int, aggressor: str, targets: dict[str, list[str]], gate: dict | None = None) -> list[str]:
    """Gone and capitulated derail arms for one arc, then the per-variant
    target arms. A hold gate (gate.hold_ideology) prepends the regime-hold
    block: while the aggressor is not yet on the required ideology the arc
    clock stays at zero and a held-month counter runs; past
    gate.hold_max_months the arc parks with gate.hold_reason. The block lives
    here, not in the tick, so the park fires inside the derail pass and a
    repick still triggers."""
    agg = aggressor
    gone = f"{agg.lower()}_gone"
    capitulated = f"{agg.lower()}_capitulated"
    chain = [
        f"  if not country_exists({agg}):",
        "    global.&sandbox_scenario_phase = 3",
        f"    $sandbox_log_sc(sc_derail, {gone})",
        f"    $sandbox_log_sc(sc_end, {gone})",
        f"  elif {agg}->has_capitulated():",
        "    global.&sandbox_scenario_phase = 3",
        f"    $sandbox_log_sc(sc_derail, {capitulated})",
        f"    $sandbox_log_sc(sc_end, {capitulated})",
        "  else:",
    ]
    dkeys = sorted(targets)
    for i, variant in enumerate(dkeys):
        chain.append(f"    if global.sandbox_target_variant == {i}:" if i == 0
                     else f"    elif global.sandbox_target_variant == {i}:" if i < len(dkeys) - 1
                     else "    else:")
        chain.append(f"      $sandbox_check_targets_derail{len(targets[variant])}({agg}, {', '.join(targets[variant])})")
    hold = gate.get("hold_ideology") if isinstance(gate, dict) else None
    if not hold:
        return chain
    cap, reason = gate["hold_max_months"], gate["hold_reason"]
    out = [
        f"  if country_exists({agg}) and not {agg}->has_government({hold}):",
        "    global.&sandbox_scenario_hold_months += 1",
        "    global.&sandbox_scenario_arc_months = 0",
        f"    if global.sandbox_scenario_hold_months >= {cap}:",
        "      global.&sandbox_scenario_phase = 3",
        f"      $sandbox_log_sc(sc_derail, {reason})",
        f"      $sandbox_log_sc(sc_end, {reason})",
        "  else:",
        "    global.&sandbox_scenario_hold_months = 0",
        "  if global.sandbox_scenario_phase < 3:",
    ]
    out += ["  " + line for line in chain]
    return out


def render_pick(names: dict[str, str], number: int, aggressor: str, key: str) -> list[str]:
    """Pin branch, eligibility entry and pick log for one arc. Three small
    functions because the hand dispatcher hosts them at three positions."""
    return [
        f"{names['pin']}():",
        f"  if sandbox_scenario_pin_is_{key}():",
        "    global.&sandbox_scenario_pin = 1",
        f"    global.&sandbox_scenario = {number}",
        "",
        f"{names['eligible']}():",
        f"  if country_exists({aggressor}):",
        f"    eligible[].add({number})",
        "",
        f"{names['pick_log']}():",
        f"  if global.sandbox_scenario == {number}:",
        f"    $sandbox_log_sc(sc_pick, {key})",
    ]


def render_joiners(data: dict) -> list[str]:
    """Generated join-selection function for one arc (docs/adr/0005).

    Sets the per-call join-filter globals from the spec's `require`, scores the
    shared candidate pool, and fires the arc's invite event at the top-n
    candidates with a positive score, then clears the flags. Named
    `sandbox_select_<key>_joiners` so the hand-written peak function calls it
    unchanged.
    """
    key = data["key"]
    joiners = data["joiners"]
    n = joiners["n"]
    invite = joiners["invite_event"]
    require = joiners.get("require", [])
    out = [f"sandbox_select_{key}_joiners():"]
    out.append(f"  global.&scenario_join_same_ideology = {1 if 'same_ideology' in require else 0}")
    out.append(f"  global.&scenario_join_same_continent = {1 if 'same_continent' in require else 0}")
    out.append("  sandbox_snapshot_join_industry()")
    out.append("  get_sorted_scored_countries(scenario_join_scorer, scenario_join_candidates[], scenario_join_scores[])")
    for i in range(n):
        out.append(f"  if scenario_join_scores[{i}] > 0:")
        out.append(f"    var:scenario_join_candidates[{i}]:")
        out.append("      country_event:")
        out.append(f"        id({invite})")
        out.append("      $sandbox_log_sc(sc_offer, invited)")
    out.append("  global.&scenario_join_same_ideology = 0")
    out.append("  global.&scenario_join_same_continent = 0")
    return out


def render_gen_hsl(specs: list[tuple[str, dict]]) -> str:
    """Generated mechanics sibling: per-arc functions derived from specs.

    Only specs with a number are emitted (a draft without a number cannot be
    wired into the dispatcher). The tick branch is emitted only when the spec
    declares both ladder content functions; aspects beyond
    set_targets/seed/tick/telemetry arrive in later tickets.
    """
    out = [
        "# Generated from docs/scenarios/*.toml by core/tools/build_scenario_catalog.py -",
        "# do not edit by hand. Hand-written code lives in 99_sandbox_scenarios.hsl.",
        "",
    ]
    for name, data in sorted(specs, key=lambda nd: (nd[1].get("number") or 0, nd[0])):
        number = data.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        agg = data["aggressor"]
        names = gen_function_names(data["id"])
        out.append(f"{names['set_targets']}():")
        tkeys = sorted(data["targets"])
        for i, variant in enumerate(tkeys):
            out.append(f"  if global.sandbox_target_variant == {i}:" if i == 0
                       else f"  elif global.sandbox_target_variant == {i}:" if i < len(tkeys) - 1
                       else "  else:")
            for tgt in data["targets"][variant]:
                out.append(f"    global.&sandbox_targets[].add({tgt})")
        out.append("")
        out.append(f"{names['seed']}():")
        out.append(f"  {agg}:")
        out.append("    sandbox_seed_from_targets()")
        out.append("")
        ladder = data.get("ladder") or {}
        crises_func, peak_func = ladder.get("crises_func"), ladder.get("peak_func")
        if isinstance(crises_func, str) and isinstance(peak_func, str):
            cm, pm = ladder["crises_at_month"], ladder["peak_at_month"]
            out.append(f"{names['tick']}():")
            out.append(f"  if global.sandbox_scenario == {number} and global.sandbox_scenario_phase == 0 and global.sandbox_scenario_arc_months >= {cm}:")
            out.append("    global.&sandbox_scenario_phase = 1")
            out.append("    $sandbox_log_sc(sc_phase, crises)")
            out.append(f"    {crises_func}()")
            out.extend(render_peak_gate(names, number, agg, data, pm, peak_func))
            out.append("")
        out.extend(render_roll_variant(names, number, data))
        out.append("")
        out.extend(render_log_variant(names, number, data))
        out.append("")
        out.append(f"{names['telemetry']}():")
        out.extend(render_telemetry(number, agg, data["targets"]))
        out.append("")
        out.append(f"{names['derail']}():")
        out.extend(render_derail(number, agg, data["targets"], data.get("gate")))
        out.append("")
        key = data.get("key", None)
        if isinstance(key, str):
            out.extend(render_pick(names, number, agg, key))
            out.append("")
            if isinstance(data.get("joiners"), dict) and data["joiners"].get("invite_event"):
                out.extend(render_joiners(data))
                out.append("")
    return "\n".join(out).rstrip() + "\n"


def stale_graph_errors(mod_dir: Path, specs: list[tuple[str, dict]]) -> list[str]:
    """Fail when an aggressor's focus graph predates its specs."""
    newest: dict[str, float] = {}
    for toml in (mod_dir / "docs" / "scenarios").glob("*.toml"):
        mtime = toml.stat().st_mtime
        for _, data in specs:
            agg = data["aggressor"]
            newest[agg] = max(newest.get(agg, 0.0), mtime)
    errors = []
    for agg in sorted(newest):
        graph = graph_path_for(mod_dir, agg)
        if graph.is_file() and graph.stat().st_mtime < newest[agg]:
            errors.append(
                f"stale focus graph for {agg}: export focus graphs first "
                f"({graph.name} predates the specs)"
            )
    return errors


def expected_boosts(mod_dir: Path, specs: list[tuple[str, dict]]) -> tuple[dict[str, dict[str, tuple[frozenset | None, tuple[str, ...]]]], list[str]]:
    """Aggressor -> boost plan (focus -> (variant gate, after gate))."""
    errors: list[str] = []
    graphs: dict[str, tuple[dict, set]] = {}
    for _, data in specs:
        agg = data["aggressor"]
        if agg not in graphs:
            graph = graph_path_for(mod_dir, agg)
            if not graph.is_file():
                errors.append(f"no focus graph for aggressor {agg}")
                continue
            graphs[agg] = load_graph(graph)
    out: dict[str, dict[str, tuple[frozenset | None, tuple[str, ...]]]] = {}
    for _, data in specs:
        agg = data["aggressor"]
        if agg not in graphs:
            continue
        prereq, excl = graphs[agg]
        plan, plan_errors = boost_plan(data, prereq, excl)
        errors.extend(plan_errors)
        merged = out.setdefault(agg, {})
        for fid, gate in plan.items():
            if fid not in merged:
                merged[fid] = gate
                continue
            pv, pa = merged[fid]
            gv, ga = gate
            variant = None if pv is None or gv is None else pv | gv
            if not pa or not ga:
                after: tuple[str, ...] = ()
            elif pa == ga:
                after = pa
            else:
                errors.append(f"{agg}: conflicting after gates on {fid!r}: {sorted(pa)} vs {sorted(ga)}")
                after = ()
            merged[fid] = (variant, after)
    # A suppressed focus is never boosted: suppressing an OR alternative is
    # legal because the path reaches its goal through the other branch. The
    # closure is author-trusted; suppressing a path's key focus is still
    # rejected by validate_spec.
    for agg, sup in expected_suppress(specs).items():
        merged = out.get(agg)
        if not merged:
            continue
        for fid in sup:
            merged.pop(fid, None)
    return out, errors


def expected_suppress(specs: list[tuple[str, dict]]) -> dict[str, set[str]]:
    """Aggressor -> focus ids whose AI pick is closed for the arc (issue 33)."""
    out: dict[str, set[str]] = {}
    for _, data in specs:
        sup = data.get("suppress", [])
        if not isinstance(sup, list):
            continue
        out.setdefault(data["aggressor"], set()).update(s for s in sup if isinstance(s, str))
    return out


SET_TARGETS_HEAD_RE = re.compile(r"^sandbox_set_targets\(\):$")
FUNC_RE = re.compile(r"^([A-Za-z_0-9]+)\(\):$")
ARC_GUARD_RE = re.compile(r"^\s*(?:if|elif) global\.sandbox_scenario == (\d+):\s*$")


def code_arc_numbers(hsl_text: str) -> set[int]:
    """Arc numbers the code dispatcher knows, read from sandbox_set_targets."""
    lines = hsl_text.splitlines()
    start = next((i for i, l in enumerate(lines) if SET_TARGETS_HEAD_RE.match(l)), None)
    if start is None:
        return set()
    out: set[int] = set()
    for line in lines[start + 1:]:
        if FUNC_RE.match(line):
            break
        m = ARC_GUARD_RE.match(line)
        if m:
            out.add(int(m.group(1)))
    return out


def coverage(mod_dir: Path, specs: list[tuple[str, dict]]) -> list[str]:
    """Compare spec numbers against the code dispatcher.

    A spec number the code does not know is an error. Arcs the code knows
    without a spec (the disabled 5-6) are noted for context; they no longer
    relax the splice, so an out-of-plan boost fails like any other (issue 30).
    """
    hsl_path = mod_dir / SCENARIO_HSL_REL
    if not hsl_path.is_file():
        return [f"missing scenario catalog ({SCENARIO_HSL_REL.as_posix()})"]
    code = code_arc_numbers(hsl_path.read_text(encoding="utf-8", errors="replace"))
    spec_numbers = {d["number"] for _, d in specs if isinstance(d.get("number"), int)}
    errors = [f"spec {n}: number {num} matches no arc in the code dispatcher"
              for n, d in specs
              for num in [d.get("number")]
              if isinstance(num, int) and num not in code]
    missing = sorted(code - spec_numbers)
    if missing:
        print(f"note: arcs without specs (not splice-checked): {missing}")
    return errors


def build_mod(mod_dir: Path, vanilla_root: Path | None) -> tuple[int, list[str]]:
    specs, errors = load_all_specs(mod_dir)
    if errors:
        return 0, errors
    count, verrs = validate_against(specs, mod_dir, vanilla_root)
    errors.extend(verrs)
    if errors:
        return count, errors
    errors.extend(stale_graph_errors(mod_dir, specs))
    if errors:
        return count, errors
    graphs = {agg: load_graph(graph_path_for(mod_dir, agg)) for agg in {d["aggressor"] for _, d in specs}}
    catalog = render_catalog(specs, graphs)
    (mod_dir / CATALOG_REL).write_text(catalog, encoding="utf-8")
    gen_text = render_gen_hsl(specs)
    (mod_dir / GEN_HSL_REL).write_text(gen_text, encoding="utf-8")
    expected, gerrs = expected_boosts(mod_dir, specs)
    errors.extend(gerrs)
    suppressed = expected_suppress(specs)
    if errors:
        return count, errors
    cov_errs = coverage(mod_dir, specs)
    errors.extend(cov_errs)
    if errors:
        return count, errors
    changed = 0
    for agg in sorted(set(expected) | set(suppressed)):
        inc = include_path_for(mod_dir, agg)
        if not inc.is_file():
            errors.append(f"no include file for aggressor {agg} ({inc.name})")
            continue
        added, removed, serrs = apply_splice_file(inc, expected.get(agg, {}), suppressed.get(agg, set()))
        errors.extend(serrs)
        changed += added + removed
    return count, errors


def validate_against(specs: list[tuple[str, dict]], mod_dir: Path, vanilla_root: Path | None) -> tuple[int, list[str]]:
    graphs_dir = mod_dir / "docs" / "gdd" / "National Focuses"
    known_tags = vanilla_tags(vanilla_root)
    if known_tags is None:
        print("note: no vanilla registry; target tags checked by shape only", file=sys.stderr)
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_numbers: dict[int, str] = {}
    for name, data in specs:
        if isinstance(data.get("id"), str):
            if data["id"] in seen_ids:
                errors.append(f"{name}: duplicate spec id {data['id']!r} (also in {seen_ids[data['id']]})")
            else:
                seen_ids[data["id"]] = name + ".toml"
        number = data.get("number", None)
        if isinstance(number, int) and not isinstance(number, bool):
            if number in seen_numbers:
                errors.append(f"{name}: duplicate number {number} (also in {seen_numbers[number]})")
            else:
                seen_numbers[number] = name + ".toml"
        errors.extend(validate_spec(name, data, graphs_dir, known_tags))
    return len(specs), errors


def check_mod(mod_dir: Path, vanilla_root: Path | None) -> tuple[int, list[str]]:
    specs, errors = load_all_specs(mod_dir)
    if errors:
        return 0, errors
    count, verrs = validate_against(specs, mod_dir, vanilla_root)
    errors.extend(verrs)
    if errors:
        return count, errors
    errors.extend(stale_graph_errors(mod_dir, specs))
    if errors:
        return count, errors
    graphs = {agg: load_graph(graph_path_for(mod_dir, agg)) for agg in {d["aggressor"] for _, d in specs}}
    want_catalog = render_catalog(specs, graphs)
    have_path = mod_dir / CATALOG_REL
    if not have_path.is_file():
        errors.append(f"missing generated catalog ({CATALOG_REL.as_posix()}); run build")
    elif have_path.read_text(encoding="utf-8") != want_catalog:
        errors.append(f"stale generated catalog ({CATALOG_REL.as_posix()}); run build")
    want_gen = render_gen_hsl(specs)
    have_gen = mod_dir / GEN_HSL_REL
    if not have_gen.is_file():
        errors.append(f"missing generated mechanics ({GEN_HSL_REL.as_posix()}); run build")
    elif have_gen.read_text(encoding="utf-8") != want_gen:
        errors.append(f"stale generated mechanics ({GEN_HSL_REL.as_posix()}); run build")
    expected, gerrs = expected_boosts(mod_dir, specs)
    errors.extend(gerrs)
    suppressed = expected_suppress(specs)
    errors.extend(coverage(mod_dir, specs))
    for agg in sorted(set(expected) | set(suppressed)):
        inc = include_path_for(mod_dir, agg)
        if not inc.is_file():
            errors.append(f"no include file for aggressor {agg} ({inc.name})")
            continue
        missing, unexpected, serrs = check_splice_file(inc, expected.get(agg, {}), suppressed.get(agg, set()))
        errors.extend(serrs)
        for fid in sorted(missing):
            errors.append(f"{inc.name}: missing boost on {fid!r}; run build")
        for fid in sorted(unexpected):
            errors.append(f"{inc.name}: unexpected boost/log/suppress on {fid!r}; run build to converge")
    errors.extend(check_labels(mod_dir, specs))
    return count, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the scenario catalog from arc specs.")
    parser.add_argument("mod_dir", type=Path, help="mod directory holding docs/scenarios/")
    parser.add_argument("--check", action="store_true", help="verify only; write nothing")
    parser.add_argument("--vanilla-root", type=Path, default=DEFAULT_VANILLA,
                        help="vanilla game root for the country-tag registry")
    args = parser.parse_args(argv)
    if args.check:
        count, errors = check_mod(args.mod_dir, args.vanilla_root)
        for e in errors:
            print(f"error: {e}")
        if errors:
            print(f"{count} spec(s), {len(errors)} error(s)")
            return 1
        print(f"{count} spec(s), artifacts in sync")
        return 0
    count, errors = build_mod(args.mod_dir, args.vanilla_root)
    for e in errors:
        print(f"error: {e}")
    if errors:
        print(f"{count} spec(s), {len(errors)} error(s)")
        return 1
    print(f"{count} spec(s) built")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
