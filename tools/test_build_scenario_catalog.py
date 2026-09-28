#!/usr/bin/env python3
"""Process-level tests for build_scenario_catalog.py.

Runs the CLI against throwaway fixture mod trees and asserts exit codes plus
error text. No test framework needed: plain asserts, non-zero exit on failure.

Usage:
  python test_build_scenario_catalog.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "build_scenario_catalog.py"

GRAPH = """\
# GER_root

```mermaid
flowchart TD
    n1["GER_remilitarize_the_rhineland"]
    n2["GER_anschluss"]
    n3["GER_demand_sudetenland"]
    n1 --> n2
    n2 --> n3
```
"""

INCLUDE = """\
  focus[id = GER_remilitarize_the_rhineland]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
  focus[id = GER_anschluss]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
  focus[id = GER_demand_sudetenland]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
"""

FORK_GRAPH = """\
# GER_root

```mermaid
flowchart TD
    n1["GER_fork_a"]
    n2["GER_fork_b"]
    n3["GER_fork_c"]
    n1 --> n3
    n2 --> n3
    n1 x--x n2
```
"""

FORK_SPEC = """\
id = "fork_probe"
number = 9
status = "draft"
aggressor = "GER"

targets = { a = ["CZE"], b = ["FRA"] }

paths = [
  ["GER_fork_a", "GER_fork_c"],
]

notes = "Fork probe."

[ladder]
crises_at_month = 12
peak_at_month = 24

[joiners]
select = "top_n_by_scorer"
n = 2
"""

FORK_INCLUDE = """\
  focus[id = GER_fork_a]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
  focus[id = GER_fork_b]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
  focus[id = GER_fork_c]:
    ai_will_do:
      +modifier:
        $ai_sandbox_modifier()
"""

GOOD_SPEC = """\
id = "axis_expansion"
number = 1
status = "ready"
aggressor = "GER"

targets = { a = ["CZE", "POL"], b = ["FRA", "ENG"] }

paths = [
  ["GER_remilitarize_the_rhineland", "GER_anschluss", "GER_demand_sudetenland"],
]

notes = "Rationale."

[ladder]
crises_at_month = 12
peak_at_month = 24

[joiners]
select = "top_n_by_scorer"
n = 2
"""


def default_hsl(*scenarios: int) -> str:
    lines = ["sandbox_set_targets():"]
    for n in scenarios or (1,):
        lines.append(f"  if global.sandbox_scenario == {n}:")
        lines.append("    if global.sandbox_target_variant == a:")
        lines.append("      global.&sandbox_targets[].add(CZE)")
    lines += ["", "sandbox_scenario_s7_telemetry():", "  pass", ""]
    return "\n".join(lines)


def make_mod(spec_files: dict[str, str], graph: str = GRAPH, include: str = INCLUDE,
             hsl: str | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="spec-test-"))
    (tmp / "docs" / "scenarios").mkdir(parents=True)
    (tmp / "docs" / "gdd" / "National Focuses").mkdir(parents=True)
    (tmp / "common" / "national_focus").mkdir(parents=True)
    (tmp / "common" / "scripted_effects").mkdir(parents=True)
    for name, body in spec_files.items():
        (tmp / "docs" / "scenarios" / name).write_text(textwrap.dedent(body), encoding="utf-8")
    (tmp / "docs" / "gdd" / "National Focuses" / "germany.md").write_text(graph, encoding="utf-8")
    (tmp / "common" / "national_focus" / "germany.include").write_text(include, encoding="utf-8")
    (tmp / "common" / "scripted_effects" / "99_sandbox_scenarios.hsl").write_text(
        default_hsl() if hsl is None else hsl, encoding="utf-8")
    return tmp


def run(mod: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), str(mod), *extra],
        capture_output=True, text=True,
    )


def test_valid_spec_passes() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    r = run(mod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 spec(s) built" in r.stdout
    catalog = mod / "docs" / "gdd" / "Scenarios Catalog.md"
    assert catalog.is_file()
    text = catalog.read_text(encoding="utf-8")
    assert "Axis expansion" in text
    assert "Rationale." in text
    assert '[["GER_demand_sudetenland"]]' in text


def test_build_deterministic() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    first = (mod / "docs" / "gdd" / "Scenarios Catalog.md").read_bytes()
    assert run(mod).returncode == 0
    second = (mod / "docs" / "gdd" / "Scenarios Catalog.md").read_bytes()
    assert first == second


def test_check_catches_spec_edit() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    assert run(mod, "--check").returncode == 0
    spec = mod / "docs" / "scenarios" / "axis_expansion.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace("Rationale.", "Changed."), encoding="utf-8")
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "stale" in r.stdout.lower()


def test_splice_converges_and_idempotent() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    inc = mod / "common" / "national_focus" / "germany.include"
    text = inc.read_text(encoding="utf-8")
    assert text.count("$ai_scenario_focus_boost()") == 3
    before = inc.read_bytes()
    assert run(mod).returncode == 0
    assert inc.read_bytes() == before
    assert run(mod, "--check").returncode == 0


def test_unexpected_boost_detected() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    inc = mod / "common" / "national_focus" / "germany.include"
    text = inc.read_text(encoding="utf-8")
    text += '  focus[id = GER_stray_focus]:\n    ai_will_do:\n      +modifier:\n        $ai_sandbox_modifier()\n      +modifier:\n        $ai_scenario_focus_boost()\n'
    inc.write_text(text, encoding="utf-8")
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "GER_stray_focus" in r.stdout


def test_labels_emitted_in_catalog() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    text = (mod / "docs" / "gdd" / "Scenarios Catalog.md").read_text(encoding="utf-8")
    assert "ger_on_cze" in text
    assert "cze_on_ger" in text
    assert "Telemetry labels" in text


def test_duplicate_label_detected() -> None:
    # A label living in both the hand catalog and the generated file means
    # the old hand-written branch was not deleted; the check must fail.
    hsl = default_hsl() + '  if has_wargoal_against(X):\n    $sandbox_log_sc(sc_goal, ger_on_cze)\n'
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC}, hsl=hsl)
    assert run(mod).returncode == 0
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "both hand catalog and generated file" in r.stdout


def test_case_drift_detected() -> None:
    hsl = default_hsl() + '  if has_wargoal_against(X):\n    $sandbox_log_sc(sc_goal, GER_on_CZE)\n'
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC}, hsl=hsl)
    assert run(mod).returncode == 0
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "GER_on_CZE" in r.stdout
    assert "differs in case" in r.stdout


def test_reverse_justify_skipped() -> None:
    # A reverse-direction justify label belongs to another arc (where that
    # country is the aggressor); with no spec for it, the check skips it.
    hsl = default_hsl() + "    $sandbox_log_sc(sc_justify, cze_on_ger)\n"
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC}, hsl=hsl)
    assert run(mod).returncode == 0
    r = run(mod, "--check")
    assert r.returncode == 0, r.stdout + r.stderr


GEN_GOLDEN = """\
# Generated from docs/scenarios/*.toml by core/tools/build_scenario_catalog.py -
# do not edit by hand. Hand-written code lives in 99_sandbox_scenarios.hsl.

gen_axis_expansion_set_targets():
  if global.sandbox_target_variant == a:
    global.&sandbox_targets[].add(CZE)
    global.&sandbox_targets[].add(POL)
  else:
    global.&sandbox_targets[].add(FRA)
    global.&sandbox_targets[].add(ENG)

gen_axis_expansion_seed():
  GER:
    sandbox_seed_from_targets()

gen_axis_expansion_tick():
  if global.sandbox_scenario == 1 and global.sandbox_scenario_phase == 0 and global.sandbox_scenario_arc_months >= 12:
    global.&sandbox_scenario_phase = 1
    $sandbox_log_sc(sc_phase, crises)
    sandbox_fire_axis_crises()
  elif global.sandbox_scenario == 1 and global.sandbox_scenario_phase == 1 and global.sandbox_scenario_arc_months >= 24:
    global.&sandbox_scenario_phase = 2
    $sandbox_log_sc(sc_phase, peak)
    sandbox_fire_axis_peak()

gen_axis_expansion_telemetry():
  if global.sandbox_target_variant == a:
    GER:
      sc_div = num_divisions
      sc_fab = num_of_factories
      $sandbox_log_sc_power()
    if country_exists(CZE):
      CZE:
        sc_div = num_divisions
        sc_fab = num_of_factories
        $sandbox_log_sc_power()
        if has_wargoal_against(GER) or is_justifying_wargoal_against(GER):
          $sandbox_log_sc(sc_goal, cze_on_ger)
    if country_exists(POL):
      POL:
        sc_div = num_divisions
        sc_fab = num_of_factories
        $sandbox_log_sc_power()
        if has_wargoal_against(GER) or is_justifying_wargoal_against(GER):
          $sandbox_log_sc(sc_goal, pol_on_ger)
    GER:
      if has_wargoal_against(CZE) or is_justifying_wargoal_against(CZE):
        $sandbox_log_sc(sc_goal, ger_on_cze)
      if is_justifying_wargoal_against(CZE):
        $sandbox_log_sc(sc_justify, ger_on_cze)
      if has_wargoal_against(POL) or is_justifying_wargoal_against(POL):
        $sandbox_log_sc(sc_goal, ger_on_pol)
      if is_justifying_wargoal_against(POL):
        $sandbox_log_sc(sc_justify, ger_on_pol)
  else:
    GER:
      sc_div = num_divisions
      sc_fab = num_of_factories
      $sandbox_log_sc_power()
    if country_exists(FRA):
      FRA:
        sc_div = num_divisions
        sc_fab = num_of_factories
        $sandbox_log_sc_power()
        if has_wargoal_against(GER) or is_justifying_wargoal_against(GER):
          $sandbox_log_sc(sc_goal, fra_on_ger)
    if country_exists(ENG):
      ENG:
        sc_div = num_divisions
        sc_fab = num_of_factories
        $sandbox_log_sc_power()
        if has_wargoal_against(GER) or is_justifying_wargoal_against(GER):
          $sandbox_log_sc(sc_goal, eng_on_ger)
    GER:
      if has_wargoal_against(FRA) or is_justifying_wargoal_against(FRA):
        $sandbox_log_sc(sc_goal, ger_on_fra)
      if is_justifying_wargoal_against(FRA):
        $sandbox_log_sc(sc_justify, ger_on_fra)
      if has_wargoal_against(ENG) or is_justifying_wargoal_against(ENG):
        $sandbox_log_sc(sc_goal, ger_on_eng)
      if is_justifying_wargoal_against(ENG):
        $sandbox_log_sc(sc_justify, ger_on_eng)

gen_axis_expansion_derail():
  if not country_exists(GER):
    global.&sandbox_scenario_phase = 3
    $sandbox_log_sc(sc_derail, ger_gone)
    $sandbox_log_sc(sc_end, ger_gone)
  elif GER->has_capitulated():
    global.&sandbox_scenario_phase = 3
    $sandbox_log_sc(sc_derail, ger_capitulated)
    $sandbox_log_sc(sc_end, ger_capitulated)
  else:
    if global.sandbox_target_variant == a:
      $sandbox_check_targets_derail2(GER, CZE, POL)
    else:
      $sandbox_check_targets_derail2(GER, FRA, ENG)

gen_axis_expansion_pin():
  if sandbox_scenario_pin_is_axis():
    global.&sandbox_scenario_pin = 1
    global.&sandbox_scenario = 1

gen_axis_expansion_eligible():
  if country_exists(GER):
    eligible[].add(1)

gen_axis_expansion_pick_log():
  if global.sandbox_scenario == 1:
    $sandbox_log_sc(sc_pick, axis)
"""


def test_gen_hsl_golden() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_scenario_catalog as b
    import tomllib
    spec_path = Path("docs/scenarios/axis_expansion.toml")
    if not spec_path.is_file():
        print("SKIP golden: not a mod checkout")
        return
    with spec_path.open("rb") as f:
        data = tomllib.load(f)
    assert b.render_gen_hsl([("axis_expansion", data)]) == GEN_GOLDEN


def test_fork_prefers_path_side() -> None:
    mod = make_mod({"fork_probe.toml": FORK_SPEC}, graph=FORK_GRAPH, include=FORK_INCLUDE,
                   hsl=default_hsl(9))
    r = run(mod)
    assert r.returncode == 0, r.stdout + r.stderr
    inc = (mod / "common" / "national_focus" / "germany.include").read_text(encoding="utf-8")
    assert "$ai_scenario_focus_boost()" in inc.split("GER_fork_a")[1].split("focus[id")[0]
    assert "$ai_scenario_focus_boost()" in inc.split("GER_fork_c")[1].split("focus[id")[0]
    assert "$ai_scenario_focus_boost()" not in inc.split("GER_fork_b")[1].split("focus[id")[0]


def test_stale_graph_fails_with_export_message() -> None:
    import os
    import time
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    graph = mod / "docs" / "gdd" / "National Focuses" / "germany.md"
    old = time.time() - 100
    os.utime(graph, (old, old))
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "export focus graphs first" in r.stdout


def test_spec_number_missing_from_code_fails() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC}, hsl=default_hsl(2))
    r = run(mod, "--check")
    assert r.returncode == 1, r.stdout
    assert "matches no arc" in r.stdout


def test_migration_mode_is_additive() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC}, hsl=default_hsl(1, 2))
    inc = mod / "common" / "national_focus" / "germany.include"
    text = inc.read_text(encoding="utf-8")
    text += '  focus[id = GER_stray_focus]:\n    ai_will_do:\n      +modifier:\n        $ai_sandbox_modifier()\n      +modifier:\n        $ai_scenario_focus_boost()\n'
    inc.write_text(text, encoding="utf-8")
    assert run(mod).returncode == 0
    r = run(mod, "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "belongs to an arc without a spec" in r.stdout


def test_check_mode_passes() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    assert run(mod).returncode == 0
    r = run(mod, "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_unknown_focus_fails_and_names_field() -> None:
    bad = GOOD_SPEC.replace("GER_demand_sudetenland", "GER_no_such_focus")
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "GER_no_such_focus" in r.stdout


def test_missing_field_fails() -> None:
    bad = "\n".join(l for l in GOOD_SPEC.splitlines() if not l.startswith("status"))
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "status" in r.stdout


def test_ladder_content_funcs_come_as_a_pair() -> None:
    bad = GOOD_SPEC.replace("peak_at_month = 24",
                            'peak_at_month = 24\ncrises_func = "sandbox_fire_axis_crises"')
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "come as a pair" in r.stdout


def test_duplicate_id_fails() -> None:
    mod = make_mod({"axis_expansion.toml": GOOD_SPEC})
    (mod / "docs" / "scenarios" / "axis_expansion.toml").rename(mod / "docs" / "scenarios" / "zz.toml")
    (mod / "docs" / "scenarios" / "axis_expansion.toml").write_text(GOOD_SPEC, encoding="utf-8")
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "duplicate" in r.stdout.lower()


def test_bad_key_fails() -> None:
    bad = GOOD_SPEC.replace('aggressor = "GER"', 'aggressor = "GER"\nkey = "has space"')
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "key must be an identifier" in r.stdout


def test_too_many_targets_fails() -> None:
    bad = GOOD_SPEC.replace('a = ["CZE", "POL"]', 'a = ["CZE", "POL", "FRA", "ENG", "SOV"]')
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod)
    assert r.returncode == 1, r.stdout
    assert "derail arms cover 1-4" in r.stdout


def test_bad_target_tag_fails() -> None:
    bad = GOOD_SPEC.replace('"CZE", "POL"', '"CZE", "XX"')
    mod = make_mod({"axis_expansion.toml": bad})
    r = run(mod, "--vanilla-root", str(mod / "no-such-vanilla"))
    assert r.returncode == 1, r.stdout
    assert "XX" in r.stdout


def test_real_vanilla_spec_passes() -> None:
    mod = Path(__file__).resolve().parents[2]
    spec = mod / "docs" / "scenarios" / "axis_expansion.toml"
    if not spec.is_file():
        print("SKIP real spec: not a mod checkout")
        return
    r = run(mod, "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
