# FreeCell-CDN

Cosmetic-pack manifest + asset payload for the FreeCell Unlocked client.
Served from Azure Static Web Apps; the client polls `manifest.json` and
pulls pack PNGs by the paths declared inside.

Design lives in the game repo at `MD/CDN_DRIVEN_COSMETICS.md`.

## Validating manifest.json

Every commit that touches `manifest.json` must pass
`tools/validate-manifest.py` — both locally (pre-commit hook) and in CI
(`.github/workflows/validate-manifest.yml`).

What it checks:

- **Schema sanity** — `version` is int, `packs` is a list, each pack has
  `id` / `display_name` / `items`, every `pack.id` is in the pre-allocated
  SKU pool, `available_from` / `available_until` parse as ISO 8601 with
  timezone, window order `from <= until` if both set.
- **Pack deletions** — refuses any pack id present in HEAD but missing from
  the staged manifest. Hide packs with `"available": false` instead;
  deletion orphans owners' entitlements. Override with
  `--allow-deletion=<pack_id>` only when no one has ever owned the pack
  (typically pre-launch pool reshuffles).
- **Item deletions** — refuses any item id present in HEAD's packs but
  missing from staged unless it appears in top-level `retired_items[]`.

### Install the pre-commit hook (per clone)

```bash
ln -s ../../tools/pre-commit-manifest-guard.sh .git/hooks/pre-commit
```

Or copy it if your shell can't symlink:

```bash
cp tools/pre-commit-manifest-guard.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

The hook only fires when `manifest.json` is staged, so README / asset
commits are unaffected.

### Run the validator manually

```bash
python tools/validate-manifest.py                        # diff against HEAD
python tools/validate-manifest.py --head-ref origin/main # diff against main
python tools/run-validator-tests.py                      # fixture suite
```
