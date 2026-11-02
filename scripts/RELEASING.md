# Release assets and HACS migration

Version 1.0.2 introduces release ZIP distribution. Packaging and asset attachment
do not publish a release, change old tags, or replace any existing asset.

## Local packaging

```sh
python3 scripts/package_release.py --tag v1.0.3
python3 scripts/package_release.py --tag v1.0.3 --validate dist/wolt_monitor.zip
(cd dist && shasum -a 256 --check wolt_monitor.zip.sha256)
```

The ZIP contains integration contents directly: `manifest.json`, Python modules,
`translations/` and `brand/`. There is **no** enclosing `wolt_monitor/` or
`custom_components/` directory. HACS extracts release ZIPs straight into
`<config>/custom_components/wolt_monitor`.

Only tracked files in the explicit runtime allowlist are accepted. Untracked
files are ignored; unexpected tracked files, missing files, and symlinks (including
parent directories) fail closed. New runtime modules/resources require updating
the allowlist. Sorted entries, fixed timestamps and permissions make repeated
builds reproducible in the same Python/zlib environment. This is not a secret
scanner: review approved runtime file contents before release.

The tag must be exactly `v` plus the version shared by manifest, runtime constant,
project metadata and lockfile. HACS metadata must name exactly `wolt_monitor.zip`,
with `zip_release: true` and `content_in_root: false`. `hide_default_branch: true`
is intentional: ZIP installation takes the release-asset path even for branch
selection, and no main-branch release asset exists.

## Authorized release procedure

1. Update version declarations for the approved release, merge reviewed changes,
   and pass CI first. Version 1.0.2 is the first ZIP-enabled release.
2. Create the matching tag on a commit reachable from main and an **empty draft**
   GitHub release for it. Do not publish it yet.
3. Manually run **Prepare release assets** from main with that tag. It uses
   trusted main tooling to inspect/package target bytes without executing target
   code. The read-only job checks tag ancestry, versions, metadata, contents and
   checksum. A separate write-enabled job only downloads the same-run artifact
   and attaches it to the same still-empty draft. No release is created or
   published by this workflow, and no assets are clobbered.
4. Review the two attached assets and release notes, then separately approve and
   publish the draft. Test a real HACS install/update before declaring rollout
   verified. Local archive validation is not a live HACS install.

GitHub's two asset uploads are not transactional. A failure may leave one asset
on the **draft**, never an automatically published partial release. Inspect and
resolve that draft manually; retries intentionally fail if any asset exists.
Do not delete/re-upload published assets: that loses their download counters.
Maintainers must not publish or edit the draft concurrently with attachment.

## Download count semantics and compatibility

Verified against HACS integration commit
[`a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8`](https://github.com/hacs/integration/tree/a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8):

- [`base.py` lines 570–625](https://github.com/hacs/integration/blob/a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8/custom_components/hacs/repositories/base.py#L570-L625)
  downloads the configured asset and uses `extractall(self.content.path.local)`;
  [`integration.py`](https://github.com/hacs/integration/blob/a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8/custom_components/hacs/repositories/integration.py)
  defines that local directory as `<config>/custom_components/<domain>`.
- [`base.py` lines 1129–1136](https://github.com/hacs/integration/blob/a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8/custom_components/hacs/repositories/base.py#L1129-L1136)
  sets downloads from the target asset of the release whose tag equals the
  selected ref; **it does not sum all versions**.
- [`base.py` lines 1397–1425](https://github.com/hacs/integration/blob/a15e87ec0d1c82e58f1ddbaf17669d3eaa261eb8/custom_components/hacs/repositories/base.py#L1397-L1425)
  first matches `data.file_name`, then category-specific names, then the exact
  manifest `filename`, falling back to the first asset if nothing matches.
  Installation itself uses the configured exact filename, not this fallback.

Every new ZIP-enabled release must attach the exact stable name
`wolt_monitor.zip`; the version belongs in the tag, not in the asset filename.
The checksum is `wolt_monitor.zip.sha256`. HACS's selected-release count is not a
cross-version total. To obtain that total separately, paginate the GitHub releases
API and sum only intended ZIP asset `download_count` values, never checksum counts.
Downloads include manual downloads, updates and reinstalls; they are neither
unique users nor active installations. Historic HACS source downloads cannot be
recovered from release-asset counts.

Existing v1.0.0/v1.0.1 tags have `zip_release: false` and versioned ZIP assets;
leave them unchanged. HACS reads metadata for the selected version, so those tags
retain their old source-based installation behavior and do not gain ZIP-asset
tracking retroactively. Merely merging the new main-branch manifest does not
convert old releases. A new release containing this configuration and matching
assets is needed for the new mechanism. Do not retag 1.0.1 to roll it out.

Official manifest reference: https://www.hacs.xyz/docs/publish/start/#hacsjson.
