#!/usr/bin/env python3
"""Extract per-mod arc hooks from 99_sandbox_on_actions.hsl.

Reads the mod's central on_actions file and writes 99_sandbox_arc_hooks.hsl
with the four catalog functions:
  sandbox_arc_on_weekly()           - extra weekly fuse retries
  sandbox_arc_tick_hosts()          - extra monthly tick hosts past USA
  sandbox_arc_wargoal_expire_hook() - sc_goal_end per-arc tag gates
  sandbox_arc_justify_hook()        - sc_justify per-arc FROM gates

The wargoal/justify blocks are taken verbatim from the mod file (all lines
between the phase guard and the end of the on_wargoal_expire /
on_justifying_wargoal_pulse effect). The weekly extras are the retry calls
present in the mod but absent from the core skeleton; the tick hosts are the
elif chain past USA.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CORE_WEEKLY = [
    "sandbox_retry_ita_civil_war_fuse()",
    "sandbox_retry_eth_civil_war_fuse()",
    "sandbox_retry_sov_civil_war_fuse()",
    "sandbox_retry_gre_civil_war_fuse()",
    "sandbox_retry_per_civil_war_fuse()",
    "sandbox_retry_est_civil_war_fuse()",
]


def extract_block(lines: list[str], start_pat: str, end_pats: list[str]) -> list[str]:
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if re.search(start_pat, line):
                inside = True
            continue
        if any(re.search(p, line) for p in end_pats):
            break
        out.append(line)
    return out


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    lines = src.read_text(encoding="utf-8").splitlines()

    # --- weekly extras: retry calls not in the core skeleton ---
    weekly_retries = []
    for line in lines:
        m = re.match(r"\s*(sandbox_retry_\w+\(\))", line)
        if m and m.group(1) not in CORE_WEEKLY and m.group(1) not in weekly_retries:
            weekly_retries.append(m.group(1))

    # --- tick hosts past USA: re-chain under else: as nested if/elif ---
    tick_hosts: list[str] = []
    cap = False
    for i, line in enumerate(lines):
        if cap:
            s = line.strip()
            if s.startswith("elif not country_exists"):
                cond = s[len("elif ") : -1] if s.endswith(":") else s[len("elif ") :]
                kw = "if" if not tick_hosts else "elif"
                tick_hosts.append(f"    {kw} {cond}:")
                tick_hosts.append("      sandbox_scenario_tick()")
            elif s == "" or re.match(r"^  on_", line):
                if tick_hosts:
                    break
            continue
        if "tag(USA):" in line:
            window = "\n".join(lines[max(0, i - 1) : i + 2])
            if "sandbox_scenario_tick" in window:
                cap = True
                continue

    # --- wargoal expire hook body: inside on_wargoal_expire effect ---
    wg_lines = extract_block(
        lines, r"on_wargoal_expire:", [r"^  on_justifying_wargoal_pulse:"]
    )
    # drop the effect:/phase-guard wrapper lines, keep the arc gates
    wg_body = [l for l in wg_lines if not re.match(r"\s*(effect:|if global\.sandbox_scenario_phase < 3:)\s*$", l)]

    # --- justify hook body: arc gates after the honor drip ---
    ju_lines = extract_block(
        lines, r"on_justifying_wargoal_pulse:", [r"^  # ROOT = leaving", r"^  on_leave_faction:"]
    )
    ju_body: list[str] = []
    copying = False
    for line in ju_lines:
        if re.match(r"\s*# s7 diagnostic", line):
            copying = True
            continue
        if copying:
            # skip the two comment lines, keep code
            if re.match(r"\s*#", line):
                continue
            ju_body.append(line)
    # trim leading/trailing blanks
    while ju_body and ju_body[0].strip() == "":
        ju_body.pop(0)
    while ju_body and ju_body[-1].strip() == "":
        ju_body.pop()
    while wg_body and wg_body[0].strip() == "":
        wg_body.pop(0)
    while wg_body and wg_body[-1].strip() == "":
        wg_body.pop()

    parts = [
        "# Per-mod arc hooks for the shared on_actions skeleton",
        "# (core/common/on_actions/99_sandbox_core_on_actions.hsl).",
        "# Regenerate with tools/extract_arc_hooks.py after editing the arc table.",
        "",
        "sandbox_arc_on_weekly():",
    ]
    if weekly_retries:
        for r in weekly_retries:
            parts.append(f"  {r}")
    else:
        parts.append("  pass")
    parts += [
        "",
        "sandbox_arc_tick_hosts():",
    ]
    if tick_hosts:
        for t in tick_hosts:
            parts.append(t)
    else:
        parts.append("  pass")

    def reindent(block: list[str], base: int = 8) -> list[str]:
        """Shift a block taken from inside effect:/phase-guard to function level."""
        fixed: list[str] = []
        for line in block:
            if line.strip() == "":
                fixed.append("")
                continue
            indent = len(line) - len(line.lstrip(" "))
            fixed.append(" " * max(0, indent - base + 2) + line.strip())
        return fixed

    parts += [
        "",
        "# sc_goal_end per-arc tag gates (runs under phase < 3).",
        "sandbox_arc_wargoal_expire_hook():",
    ]
    parts += reindent(wg_body) if wg_body else ["  pass"]
    parts += [
        "",
        "# sc_justify per-arc FROM gates (daily while active).",
        "sandbox_arc_justify_hook():",
    ]
    parts += reindent(ju_body) if ju_body else ["  pass"]
    parts.append("")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {dst} ({len(parts)} lines)")


if __name__ == "__main__":
    main()
