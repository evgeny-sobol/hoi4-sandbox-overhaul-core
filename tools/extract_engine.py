#!/usr/bin/env python3
"""Extract engine functions from the r56 scenario director into the core file.

Engine = mod-agnostic functions (no arc ids, no focus/event ids):
  sandbox_seed_from_targets, sandbox_snapshot_join_industry,
  sandbox_scenario_s7_telemetry is CATALOG (arc ids) -> stays in mods.
  sandbox_log_scenario_success is CATALOG (arc labels) -> stays in mods.
  sandbox_scenario_ignite, sandbox_ignite_if_at_war,
  sandbox_scenario_check_civil_war_derail dispatches on arc ids -> stays.

So the true engine extract is: seed_from_targets, snapshot_join_industry,
ignite, ignite_if_at_war. The dispatchers (check_ignite_by_war,
check_civil_war_derail, check_derail, tick, pick, repick, seed_actors,
set_targets, log_variant, success) are catalog: generated per mod.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENGINE = {
    "sandbox_seed_from_targets",
    "sandbox_snapshot_join_industry",
    "sandbox_scenario_ignite",
    "sandbox_ignite_if_at_war",
}

FUNC_RE = re.compile(r"^([A-Za-z_0-9]+)\(\):$")


def split_functions(lines: list[str]) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_body: list[str] = []
    pre: list[str] = []
    for line in lines:
        m = FUNC_RE.match(line)
        if m:
            if cur_name is not None:
                out.append((cur_name, cur_body))
            cur_name = m.group(1)
            cur_body = []
        else:
            if cur_name is None:
                pre.append(line)
            else:
                cur_body.append(line)
    if cur_name is not None:
        out.append((cur_name, cur_body))
    return out


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    lines = src.read_text(encoding="utf-8").splitlines()
    funcs = split_functions(lines)
    engine = [(n, b) for n, b in funcs if n in ENGINE]
    missing = ENGINE - {n for n, _ in engine}
    if missing:
        print(f"missing engine functions: {sorted(missing)}")
        sys.exit(1)
    parts = [
        "# docs/gdd/Scenarios.md: shared scenario engine (mod-agnostic).",
        "# Catalog dispatchers (pick/repick/tick/seed_actors/set_targets/success,",
        "# per-arc fire/select/derail, ignite/derail dispatchers) live in each mod's",
        "# 99_sandbox_scenarios.hsl next to the arc table. This file holds only",
        "# functions with no arc ids and no focus/event ids.",
        "",
    ]
    for name, body in engine:
        # strip leading blank lines; also strip trailing comment-only lines:
        # comments between functions belong to the NEXT function in the source,
        # so keeping them would leave dangling text at the end of this body.
        while body and body[0].strip() == "":
            body.pop(0)
        while body and (body[-1].strip() == "" or body[-1].lstrip().startswith("#")):
            body.pop()
        parts.append(f"{name}():")
        parts.extend(body)
        parts.append("")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {dst} ({len(chr(10).join(parts).splitlines())} lines)")


if __name__ == "__main__":
    main()
