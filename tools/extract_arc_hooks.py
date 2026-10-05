#!/usr/bin/env python3
"""Generate the per-mod arc hooks file from the mod's scenario catalog.

Reads the per-mod catalog (common/scripted_effects/99_sandbox_scenarios.hsl)
and writes common/scripted_effects/99_sandbox_arc_hooks.hsl with the one
catalog function consumed by the shared on_actions skeleton
(core/common/on_actions/99_sandbox_core_on_actions.hsl):

  sandbox_arc_wargoal_expire_hook() - sc_goal_end per-arc tag gates

sc_justify is sampled monthly in the catalog s7 telemetry (issue 15), so no
justify hook is generated.

Both bodies are derived from the catalog itself, with arc specs filling the
gaps left by generation:
  * the aggressor tag per arc comes from sandbox_seed_actors(), or from the
    arc's spec once its branch is a generated call;
  * the target tags per arc and variant come from sandbox_set_targets(), or
    from the arc's spec once its branch is a generated call;
  * the wargoal gate is `tag(<aggressor> | <targets...>)`;
  * the justify gate is `tag(<aggressor>)` plus one `FROM->tag(<target>)` per
    target, logged as `sc_justify <aggressor>_on_<target>` (lowercased).

The output lives in common/scripted_effects/, not common/on_actions/: the
latter is parsed as an on_actions file and rejects anything that is not
`on_* = { effect = { } }`, so effect definitions there are dropped with
"Unexpected token" and every call site in the skeleton becomes an
"Invalid effect".

Three hooks cannot be derived from the catalog and stay hand-written in the
mod's catalog file:
  sandbox_arc_on_weekly()              - extra weekly fuse retries (e.g. AFG BoP)
  sandbox_arc_tick_hosts()             - extra monthly tick hosts past USA
  sandbox_delay_capped_cw_missions_mod() - extra capped-civil-war mission ids

The last one is called by the core sandbox_delay_capped_cw_missions() loop,
which keeps only the mission ids that exist in vanilla. A mod appends its own
ids there so the shared core never references content it does not have.

Usage:
  python tools/extract_arc_hooks.py <mod_dir>
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

CATALOG_REL = "common/scripted_effects/99_sandbox_scenarios.hsl"
ON_ACTIONS_REL = "common/on_actions/99_sandbox_on_actions.hsl"
OUTPUT_REL = "common/scripted_effects/99_sandbox_arc_hooks.hsl"

FUNC_RE = re.compile(r"^([A-Za-z_0-9]+)\(\):$")
ARC_RE = re.compile(r"^\s*(?:if|elif) global\.sandbox_scenario == (\d+):\s*$")
VAR_RE = re.compile(r"^\s*if global\.sandbox_target_variant == (\w+):\s*$")
ELSE_RE = re.compile(r"^\s*else:\s*$")
ADD_RE = re.compile(r"^\s*global\.&sandbox_targets\[\]\.add\((\w+)\)\s*$")
TAG_HEAD_RE = re.compile(r"^\s*([A-Z]{3}):\s*$")


def split_functions(lines: list[str]) -> dict[str, list[str]]:
    """Map each `name():` function to the lines of its body."""
    funcs: dict[str, list[str]] = {}
    cur: str | None = None
    for line in lines:
        m = FUNC_RE.match(line)
        if m:
            cur = m.group(1)
            funcs[cur] = []
        elif cur is not None:
            funcs[cur].append(line)
    return funcs


def parse_targets(body: list[str]) -> dict[int, dict[str, list[str]]]:
    """sandbox_set_targets() -> {arc: {variant: [tags]}}."""
    arcs: dict[int, dict[str, list[str]]] = {}
    arc: int | None = None
    variant: str | None = None
    for line in body:
        m = ARC_RE.match(line)
        if m:
            arc = int(m.group(1))
            arcs[arc] = {}
            variant = None
            continue
        if arc is None:
            continue
        m = VAR_RE.match(line)
        if m:
            variant = m.group(1)
            arcs[arc].setdefault(variant, [])
            continue
        if ELSE_RE.match(line):
            variant = "b"
            arcs[arc].setdefault(variant, [])
            continue
        m = ADD_RE.match(line)
        if m and variant:
            arcs[arc][variant].append(m.group(1))
    return arcs


def parse_aggressors(body: list[str]) -> dict[int, str]:
    """sandbox_seed_actors() -> {arc: aggressor tag}."""
    out: dict[int, str] = {}
    arc: int | None = None
    for line in body:
        m = ARC_RE.match(line)
        if m:
            arc = int(m.group(1))
            continue
        m = TAG_HEAD_RE.match(line)
        if m and arc is not None:
            out[arc] = m.group(1)
    return out


def load_spec_arcs(mod: Path) -> dict[int, tuple[str, dict[str, list[str]]]]:
    """Arc specs as {number: (aggressor, {variant: [tags]})}.

    A migrated arc's catalog branches are generated calls carrying no tags,
    so the specs fill those arcs in. Where a spec exists it wins: the hand
    branch holds no data to disagree with.
    """
    out: dict[int, tuple[str, dict[str, list[str]]]] = {}
    spec_dir = mod / "docs" / "scenarios"
    if not spec_dir.is_dir():
        return out
    for path in sorted(spec_dir.glob("*.toml")):
        with path.open("rb") as f:
            data = tomllib.load(f)
        number = data.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        out[number] = (
            data["aggressor"],
            {v: list(data["targets"][v]) for v in data["targets"]},
        )
    return out


def check_consistency(arcs, aggressors) -> None:
    missing_agg = sorted(set(arcs) - set(aggressors))
    missing_tgt = sorted(set(aggressors) - set(arcs))
    if missing_agg:
        raise SystemExit(
            f"error: sandbox_set_targets() has arcs {missing_agg} with no "
            "aggressor in sandbox_seed_actors()"
        )
    if missing_tgt:
        raise SystemExit(
            f"error: sandbox_seed_actors() has arcs {missing_tgt} with no "
            "targets in sandbox_set_targets()"
        )
    for arc in sorted(arcs):
        if not arcs[arc]:
            raise SystemExit(
                f"error: arc {arc} has no variants in sandbox_set_targets()"
            )


def build(arcs, aggressors) -> list[str]:
    out = [
        "# Per-mod arc hooks for the shared on_actions skeleton",
        "# (core/common/on_actions/99_sandbox_core_on_actions.hsl).",
        "# Generated by tools/extract_arc_hooks.py from this mod's scenario",
        "# catalog (common/scripted_effects/99_sandbox_scenarios.hsl).",
        "# Do not edit by hand; hand-written hooks live in the catalog:",
        "#   sandbox_arc_on_weekly()  - extra weekly fuse retries",
        "#   sandbox_arc_tick_hosts() - extra monthly tick hosts past USA",
        "",
        "# sc_goal_end per-arc tag gates (runs under phase < 3).",
        "sandbox_arc_wargoal_expire_hook():",
    ]
    for arc in sorted(arcs):
        agg = aggressors[arc]
        out.append(f"  if global.sandbox_scenario == {arc}:")
        variants = sorted(arcs[arc])
        for i, v in enumerate(variants):
            tags = arcs[arc][v]
            out.append(
                f"    if global.sandbox_target_variant == {i}:"
                if i == 0
                else f"    elif global.sandbox_target_variant == {i}:" if i < len(variants) - 1
                else "    else:"
            )
            out.append(f"      if tag({agg} | {' | '.join(tags)}):")
            out.append("        $sandbox_log_sc(sc_goal_end, goal_expired)")

    out.append("")
    return out


HANDWRITTEN = ("sandbox_arc_on_weekly", "sandbox_arc_tick_hosts")

# Called from the core sandbox_delay_capped_cw_missions() loop. The core keeps
# only the mission ids that exist in vanilla; a mod appends its own ids here,
# so every mod must define it (an empty body with `pass` is fine).
MOD_CATALOG_HOOKS = ("sandbox_delay_capped_cw_missions_mod",)


def check_handwritten_hooks(catalog_text: str) -> None:
    """Verify the catalog defines every hook the shared core calls.

    The arc hooks are called unconditionally by the on_actions skeleton:
    the expire hook is generated here, the other two must come from the mod's
    catalog or the call dangles as an invalid effect. The mod-missions hook is called by
    the core weekly mission-delay loop for the same reason. A mod with nothing
    to add writes `pass`.
    """
    missing = [n for n in HANDWRITTEN + MOD_CATALOG_HOOKS if f"{n}():" not in catalog_text]
    if missing:
        raise SystemExit(
            f"error: {CATALOG_REL} must define {', '.join(missing)} "
            "(use `pass` when there is nothing to add); the shared core calls "
            "all of them unconditionally"
        )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: extract_arc_hooks.py <mod_dir>")
    mod = Path(sys.argv[1])
    catalog = mod / CATALOG_REL
    dst = mod / OUTPUT_REL

    text = catalog.read_text(encoding="utf-8")
    funcs = split_functions(text.splitlines())

    for name in ("sandbox_set_targets", "sandbox_seed_actors"):
        if name not in funcs:
            raise SystemExit(f"error: {CATALOG_REL} has no {name}()")

    arcs = parse_targets(funcs["sandbox_set_targets"])
    aggressors = parse_aggressors(funcs["sandbox_seed_actors"])
    for number, (agg, tgts) in load_spec_arcs(mod).items():
        arcs[number] = {v: list(tgts[v]) for v in tgts}
        aggressors[number] = agg
    check_consistency(arcs, aggressors)

    check_handwritten_hooks(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(build(arcs, aggressors)), encoding="utf-8")
    print(
        f"wrote {dst} ({len(arcs)} arcs, aggressors "
        f"{', '.join(aggressors[a] for a in sorted(aggressors))})"
    )


if __name__ == "__main__":
    main()
