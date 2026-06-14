#!/usr/bin/env python3
"""Fixture-based test suite for tools/validate-manifest.py.

Each test names a (head, staged) fixture pair and the expected exit code.
The runner shells out to validate-manifest.py with --head-manifest and
--staged-manifest so git state never matters.

Run: python tools/run-validator-tests.py
Exits 0 if all tests pass, 1 if any fail.

Wired into .github/workflows/validate-manifest.yml so PRs that break the
validator's own contract fail CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass


HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
VALIDATOR = os.path.join(HERE, "validate-manifest.py")


@dataclass
class Case:
    name: str
    head: str | None              # filename in fixtures/, or None for "no baseline"
    staged: str                   # filename in fixtures/
    expected_exit: int            # 0 = pass, 1 = refused
    allow_deletion: tuple[str, ...] = ()
    must_contain: tuple[str, ...] = ()   # substrings expected in stderr on refusal


CASES: list[Case] = [
    # --- Clean paths ---------------------------------------------------------
    Case(
        name="clean: extend pack + add new pack",
        head="head_with_one_pack.json",
        staged="staged_clean_extend.json",
        expected_exit=0,
    ),
    Case(
        name="clean: retire item via retired_items[]",
        head="head_with_one_pack.json",
        staged="staged_clean_retire_item.json",
        expected_exit=0,
    ),
    Case(
        name="clean: seasonal window with both bounds",
        head=None,                                       # no baseline (initial commit case)
        staged="staged_clean_seasonal_window.json",
        expected_exit=0,
    ),

    # --- Refused: diff violations -------------------------------------------
    Case(
        name="refused: pack deleted without --allow-deletion",
        head="head_with_one_pack.json",
        staged="staged_bad_pack_deletion.json",
        expected_exit=1,
        must_contain=("pack `themed_pack_1` deleted",),
    ),
    Case(
        name="allowed: pack deletion with explicit override",
        head="head_with_one_pack.json",
        staged="staged_bad_pack_deletion.json",
        expected_exit=0,
        allow_deletion=("themed_pack_1",),
    ),
    Case(
        name="refused: item removed without entry in retired_items[]",
        head="head_with_one_pack.json",
        staged="staged_bad_item_deletion.json",
        expected_exit=1,
        must_contain=("item `botanical_back_02` removed",),
    ),

    # --- Refused: schema violations -----------------------------------------
    Case(
        name="refused: missing top-level packs",
        head=None,
        staged="staged_bad_schema_missing_packs.json",
        expected_exit=1,
        must_contain=("`packs` must be a list",),
    ),
    Case(
        name="refused: pack id outside ALLOWED_SKU_POOL",
        head=None,
        staged="staged_bad_unknown_sku.json",
        expected_exit=1,
        must_contain=("`rogue_pack_xyz` not in pre-allocated SKU pool",),
    ),
    Case(
        name="refused: available_until before available_from",
        head=None,
        staged="staged_bad_inverted_window.json",
        expected_exit=1,
        must_contain=("window inverted",),
    ),
    Case(
        name="refused: timestamp without timezone",
        head=None,
        staged="staged_bad_naive_timestamp.json",
        expected_exit=1,
        must_contain=("must be ISO 8601 with timezone",),
    ),
]


def run_case(case: Case) -> tuple[bool, str]:
    cmd = [sys.executable, VALIDATOR, "--staged-manifest", os.path.join(FIXTURES, case.staged)]
    if case.head is not None:
        cmd += ["--head-manifest", os.path.join(FIXTURES, case.head)]
    else:
        # Force the validator to skip the git-show path even outside a repo —
        # pointing at a non-existent ref returns None (no baseline) cleanly.
        cmd += ["--head-ref", "definitely-not-a-real-ref-3d7250"]
    for pid in case.allow_deletion:
        cmd += ["--allow-deletion", pid]

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")

    if result.returncode != case.expected_exit:
        return False, (
            f"expected exit {case.expected_exit}, got {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}"
            f"--- stderr ---\n{result.stderr}"
        )

    for needle in case.must_contain:
        if needle not in output:
            return False, (
                f"expected stderr to contain {needle!r}, did not.\n"
                f"--- stderr ---\n{result.stderr}"
            )

    return True, ""


def main() -> int:
    passed = 0
    failed = 0
    for case in CASES:
        ok, message = run_case(case)
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {case.name}")
        if ok:
            passed += 1
        else:
            failed += 1
            for line in message.splitlines():
                print(f"           {line}")

    print()
    print(f"  {passed} passed, {failed} failed out of {len(CASES)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
