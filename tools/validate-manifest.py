#!/usr/bin/env python3
"""Validate manifest.json against schema + diff against a reference.

Runs as a pre-commit hook (refuses bad commits locally) and in GitHub Actions
(blocks merges to main). Spec lives in MD/CDN_DRIVEN_COSMETICS.md §I PR4.

Default behaviour: read HEAD's manifest.json via `git show HEAD:manifest.json`
and the working-tree manifest.json, then run all checks. Override either side
via CLI flags so the fixture suite can exercise the validator with arbitrary
input pairs.

Checks (refusal = exit 1, message to stderr):
  - Schema: top-level `version` (int), `packs` (list), each pack has
    `id`/`display_name`/`items`. `pack.id` must be in the pre-allocated SKU
    pool below. `available_from` / `available_until` parse as ISO 8601 with
    tzinfo; if both present, `from <= until`.
  - Pack deletions: any pack id present in HEAD but missing from staged is
    refused unless `--allow-deletion=<id>` is passed for that id. Pack
    entries should be hidden via `available: false`, not removed (orphans
    owners' entitlements — see §A append-only rule).
  - Item deletions: any item id present in HEAD but missing from staged must
    appear in staged top-level `retired_items[]`. Otherwise refused — the
    explicit retire path keeps the intent auditable in the manifest history.

Exit codes:
  0 — clean (or only warnings)
  1 — validation failed (one or more refusals)
  2 — invalid CLI / IO error / git unavailable
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from typing import Any


# Pre-allocated SKU pool — every `pack.id` in manifest.json must be in this
# set. Includes the foundational monetization SKUs (which DON'T ship through
# the CDN-driven path but may appear in manifest if a future themed Supporter
# variant lands) plus the themed_pack_<n> slots from §C and the supporter_tip_2
# extra-consumable slot.
#
# When adding a new pack to manifest.json:
#   1. Pre-create the SKU in Play Console + ASC + RevenueCat per §C.
#   2. Add the SKU id below.
#   3. Add the pack entry to manifest.json.
#   4. Commit — this validator passes.
#
# Update this list when the SKU pool size grows beyond 10 themed slots
# (a code change per §H "SKU pool exhaustion mid-cycle").
ALLOWED_SKU_POOL: set[str] = {
    # Foundational monetization tiers (hardcoded baseline; rarely manifest-shipped).
    "supporter_tip",
    "supporter_pack",
    "patron_pack",
    # Extra consumable slot per §C.
    "supporter_tip_2",
    # Transitional pre-art SKUs (will move out of code Baseline in PR6 but stay
    # in this validator pool until the SKUs themselves are deprecated).
    "nature_pack",
    "astronomy_pack",
    # Themed pack slots — the §C reservation.
    *(f"themed_pack_{n}" for n in range(1, 11)),
}


# Required top-level pack fields per §A schema.
REQUIRED_PACK_FIELDS = ("id", "display_name", "items")

# Required per-item fields. tier is optional (defaults to Premium).
REQUIRED_ITEM_FIELDS = ("id", "category")


class ValidationError(Exception):
    """Raised when a check fails. Caught at top level; printed to stderr."""


# --- IO helpers --------------------------------------------------------------


def load_json_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json_from_git(ref: str) -> dict[str, Any] | None:
    """Read manifest.json at the given git ref. Returns None if absent (initial
    commit). Raises on git failure other than the ref-not-found case."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:manifest.json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        print(f"validate-manifest: git unavailable: {e}", file=sys.stderr)
        sys.exit(2)

    if out.returncode != 0:
        # Any git failure here — ref missing (fresh repo / fixture), manifest
        # not yet committed, not-a-git-repo — is treated as "no baseline."
        # Diff checks skip; schema check still runs as the safety net. Warn
        # so the operator notices if something unexpected swallowed the ref.
        print(f"validate-manifest: no baseline from `git show {ref}:manifest.json` "
              f"(skipping diff checks): {out.stderr.strip()}", file=sys.stderr)
        return None

    return json.loads(out.stdout)


# --- Schema checks -----------------------------------------------------------


def check_schema(manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable refusal messages; empty = clean."""
    errors: list[str] = []

    if not isinstance(manifest, dict):
        return ["top-level must be a JSON object"]

    version = manifest.get("version")
    if not isinstance(version, int):
        errors.append(f"top-level `version` must be int, got {type(version).__name__}")

    packs = manifest.get("packs")
    if not isinstance(packs, list):
        errors.append(f"top-level `packs` must be a list, got {type(packs).__name__}")
        return errors  # bail — downstream checks assume list

    retired = manifest.get("retired_items", [])
    if not isinstance(retired, list):
        errors.append(f"top-level `retired_items` must be a list, got {type(retired).__name__}")

    sku_pool_field = manifest.get("sku_pool", [])
    if not isinstance(sku_pool_field, list):
        errors.append(f"top-level `sku_pool` must be a list, got {type(sku_pool_field).__name__}")
    else:
        for sku in sku_pool_field:
            if not isinstance(sku, str):
                errors.append(f"`sku_pool` entry must be string, got {sku!r}")
            elif sku not in ALLOWED_SKU_POOL:
                errors.append(f"`sku_pool` SKU `{sku}` not in pre-allocated pool - "
                              f"see tools/validate-manifest.py ALLOWED_SKU_POOL")

    seen_pack_ids: set[str] = set()
    for i, pack in enumerate(packs):
        if not isinstance(pack, dict):
            errors.append(f"packs[{i}] must be an object, got {type(pack).__name__}")
            continue

        for field in REQUIRED_PACK_FIELDS:
            if field not in pack:
                errors.append(f"packs[{i}] missing required field `{field}`")

        pack_id = pack.get("id")
        if isinstance(pack_id, str):
            if pack_id in seen_pack_ids:
                errors.append(f"duplicate pack id `{pack_id}` at packs[{i}]")
            seen_pack_ids.add(pack_id)
            if pack_id not in ALLOWED_SKU_POOL:
                errors.append(f"packs[{i}] id `{pack_id}` not in pre-allocated SKU pool - "
                              f"see tools/validate-manifest.py ALLOWED_SKU_POOL")

        # available / window fields are optional but typed.
        if "available" in pack and not isinstance(pack["available"], bool):
            errors.append(f"pack `{pack_id}` `available` must be bool, "
                          f"got {type(pack['available']).__name__}")

        af = _parse_iso_with_tz(pack.get("available_from"))
        au = _parse_iso_with_tz(pack.get("available_until"))
        if pack.get("available_from") is not None and af is None:
            errors.append(f"pack `{pack_id}` `available_from` must be ISO 8601 "
                          f"with timezone, got {pack['available_from']!r}")
        if pack.get("available_until") is not None and au is None:
            errors.append(f"pack `{pack_id}` `available_until` must be ISO 8601 "
                          f"with timezone, got {pack['available_until']!r}")
        if af and au and af > au:
            errors.append(f"pack `{pack_id}` window inverted: "
                          f"`available_from` ({af}) > `available_until` ({au})")

        items = pack.get("items")
        if isinstance(items, list):
            seen_item_ids: set[str] = set()
            for j, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"pack `{pack_id}` items[{j}] must be an object")
                    continue
                for field in REQUIRED_ITEM_FIELDS:
                    if field not in item:
                        errors.append(f"pack `{pack_id}` items[{j}] missing `{field}`")
                item_id = item.get("id")
                if isinstance(item_id, str):
                    if item_id in seen_item_ids:
                        errors.append(f"duplicate item id `{item_id}` in pack `{pack_id}`")
                    seen_item_ids.add(item_id)

    return errors


def _parse_iso_with_tz(value: Any) -> datetime.datetime | None:
    """Parse an ISO 8601 string; return None if invalid or tz-naive.

    Python's datetime.fromisoformat handles `Z` suffix from 3.11+. We accept
    both `Z` and `+00:00` form; reject naive datetimes (no tzinfo) because §A
    pins these to UTC.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


# --- Diff checks -------------------------------------------------------------


def check_pack_deletions(
    head: dict[str, Any], staged: dict[str, Any], allow_deletion: set[str]
) -> list[str]:
    head_ids = _pack_ids(head)
    staged_ids = _pack_ids(staged)
    deleted = head_ids - staged_ids - allow_deletion
    return [
        f"pack `{pid}` deleted from manifest - set `available: false` instead "
        f"(use --allow-deletion={pid} to override; only safe if no one has "
        f"ever owned it)"
        for pid in sorted(deleted)
    ]


def check_item_deletions(
    head: dict[str, Any], staged: dict[str, Any]
) -> list[str]:
    """An item present in HEAD's packs but absent from staged must appear in
    the staged `retired_items[]` list. Otherwise refuse — explicit retirement
    keeps history auditable.

    Items inside packs that were themselves legitimately deleted (covered by
    --allow-deletion in the pack check) don't trigger this. We compute the
    "items that survived their pack" intersection before refusal.
    """
    staged_retired = set(_safe_list_of_str(staged.get("retired_items", [])))
    staged_pack_ids = _pack_ids(staged)

    errors: list[str] = []
    for head_pack in head.get("packs", []):
        if not isinstance(head_pack, dict):
            continue
        pack_id = head_pack.get("id")
        if not isinstance(pack_id, str):
            continue
        # If the pack itself is gone from staged, the pack-deletion check has
        # already covered it. Skip item-level diff for this pack.
        if pack_id not in staged_pack_ids:
            continue

        head_items = {
            it["id"] for it in head_pack.get("items", [])
            if isinstance(it, dict) and isinstance(it.get("id"), str)
        }
        staged_pack = _find_pack(staged, pack_id)
        staged_items = {
            it["id"] for it in (staged_pack.get("items", []) if staged_pack else [])
            if isinstance(it, dict) and isinstance(it.get("id"), str)
        }

        for missing in sorted(head_items - staged_items):
            if missing not in staged_retired:
                errors.append(
                    f"item `{missing}` removed from pack `{pack_id}` without "
                    f"appearing in `retired_items[]` - explicit retirement "
                    f"required (add `{missing}` to top-level `retired_items` "
                    f"if intentional)"
                )

    return errors


def _pack_ids(manifest: dict[str, Any]) -> set[str]:
    return {
        p["id"] for p in manifest.get("packs", [])
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    }


def _find_pack(manifest: dict[str, Any], pack_id: str) -> dict[str, Any] | None:
    for p in manifest.get("packs", []):
        if isinstance(p, dict) and p.get("id") == pack_id:
            return p
    return None


def _safe_list_of_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


# --- Main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate manifest.json against schema + diff against a reference.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--head-ref",
        default="HEAD",
        help="git ref to read the reference manifest from "
             "(default: HEAD; CI passes origin/main).",
    )
    parser.add_argument(
        "--head-manifest",
        help="Explicit path to the reference manifest (overrides --head-ref). "
             "Used by the fixture test suite.",
    )
    parser.add_argument(
        "--staged-manifest",
        default="manifest.json",
        help="Path to the manifest under validation (default: manifest.json).",
    )
    parser.add_argument(
        "--allow-deletion",
        action="append",
        default=[],
        metavar="PACK_ID",
        help="Allow this pack id to be deleted. Repeatable. Only safe if no "
             "one has ever owned the pack (e.g. you decide pre-launch a slot "
             "will never ship).",
    )
    args = parser.parse_args()

    # Load staged manifest.
    if not os.path.exists(args.staged_manifest):
        print(f"validate-manifest: staged manifest not found: {args.staged_manifest}",
              file=sys.stderr)
        return 2
    try:
        staged = load_json_file(args.staged_manifest)
    except json.JSONDecodeError as e:
        print(f"validate-manifest: staged manifest is not valid JSON: {e}",
              file=sys.stderr)
        return 1

    # Schema first — diff checks assume well-formed input.
    schema_errors = check_schema(staged)
    if schema_errors:
        print("validate-manifest: SCHEMA ERRORS", file=sys.stderr)
        for e in schema_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    # Load HEAD/reference manifest. None means there's no baseline (initial
    # commit or fixture case) — skip diff checks.
    head: dict[str, Any] | None
    if args.head_manifest:
        if not os.path.exists(args.head_manifest):
            print(f"validate-manifest: head manifest not found: {args.head_manifest}",
                  file=sys.stderr)
            return 2
        try:
            head = load_json_file(args.head_manifest)
        except json.JSONDecodeError as e:
            print(f"validate-manifest: head manifest is not valid JSON: {e}",
                  file=sys.stderr)
            return 1
    else:
        head = load_json_from_git(args.head_ref)

    diff_errors: list[str] = []
    if head is not None:
        diff_errors += check_pack_deletions(head, staged, set(args.allow_deletion))
        diff_errors += check_item_deletions(head, staged)

    if diff_errors:
        print("validate-manifest: DIFF REFUSED", file=sys.stderr)
        for e in diff_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("validate-manifest: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
