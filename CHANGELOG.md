# Changelog

Notable changes to Wolt Monitor are documented here.

## 1.0.5 — 2026-11-09

### Fixed

- Avoid blocking timezone-data disk reads during Home Assistant setup by removing redundant `ZoneInfo` validation from the supplemental HTTP client (#28).
- Add a regression check for constructor-time timezone lookups; preserve the timezone forwarded to the API.

### Maintenance

- Refresh the pinned hassfest validator image and update `setup-uv`, `upload-artifact` and `download-artifact` GitHub Actions (#24–27).
- The minimum supported Home Assistant version remains 2026.3.1. No changes to entity IDs, configuration, polling defaults or order-status mapping.

## 1.0.4 — 2026-11-05

### Documentation and maintenance

- Replace the public README's legacy store instructions with the built-in Home Assistant Marketplace, including repository links, installation and updates; retain manual installation for older supported Home Assistant versions.
- Remove obsolete README migration instructions for never-public development entities and clarify setup and polling behavior.
- Move current-stable HA testing to the official `pytest-homeassistant` package; retain a separate minimum-version test environment.
- Initialize Home Assistant's frame helper in the fresh-process lifecycle test harness.
- Add the official catalog and hassfest GitHub Actions for Marketplace submission, alongside the existing pinned validators.
- Include the MIT copyright and permission notice in the installable ZIP.
- No integration behavior changes. The minimum supported Home Assistant version remains 2026.3.1; the built-in Marketplace requires Home Assistant 2026.11 or newer.
- Catalog submission does not imply acceptance or official support by Home Assistant.

## 1.0.3 — 2026-11-02

### Maintenance

- Update CI's `astral-sh/setup-uv` action from 10.2.0 to 10.3.2 (#14).
- Refresh the pinned hassfest validator image from `c79061c` to `33c6946` (#15).
- Update the development Ruff lock from 0.16.10 to 0.17.3 (#16).
- Update `pytest-homeassistant-custom-component` from 0.13.371 to 0.13.375 and its transitive development lock, including Home Assistant 2026.10.4 (#17).
- No new integration runtime fixes or features beyond 1.0.2. The minimum supported Home Assistant version remains 2026.3.1; validator and test-environment versions are tooling, not runtime requirements.

## 1.0.2 — 2026-11-02

### Fixed

- Prefer known-active orders over stale subscriptions with unknown statuses, including when replacing an unknown selection; preserve conservative status mapping and known-order stickiness.
- Import device tracker types from the supported Home Assistant public module instead of the deprecated config-entry module.

### Added

- Reproducible, validated flat HACS release ZIPs with SHA256 sidecars and a manually dispatched workflow that attaches assets only to an existing empty draft.
- Explicit HACS release-asset metadata, packaging regressions, and release/download-count documentation. Existing tags and assets remain unchanged.

## 1.0.1 — 2026-10-09

### Fixed

- Use an absolute HTTPS URL for the README banner so HACS can display the existing image.
- No integration logic or banner-centering changes.

## 1.0.0 — 2026-10-09

### Added

- First version: one Wolt account and one selected order, grouped in one Home Assistant device.
- Eight entities: order status, restaurant, estimated delivery time, ETA, courier distance, GPS courier position, Delivery In Progress and Delivered.
- English and Polish setup, field descriptions, entity names and localized status labels. Native states remain stable for automations.
- Matching setup and Options settings: idle polling (default 2 minutes), active-order and courier polling (default 30 seconds each), and completed-order retention (default 10 minutes).
- HTTP order and tracking reads, plus isolated same-order delivery confirmation from a supplemental HTTP source. No WebSockets.
- Configurable terminal retention, expiry without network access, stable order selection and independent source backoff with Retry-After support.
- Refresh-token authentication and reauthentication, validation before token replacement, memory-only access tokens and redacted diagnostics.
- Courier location through the built-in Home Assistant Map card and whole-metre suggested distance precision.
- Owner-supplied light/dark Home Assistant artwork; README always uses the owner's light banner. Editable PXD projects remain repository sources, not installable runtime files.
- Manual installation instructions and HACS custom-repository metadata for Home Assistant 2026.3.1 or later.

### Limitations

- Unofficial interfaces can change. Classic Wolt authentication is the supported scope; other providers are not verified.
- List fallback does not guarantee the newest order. Initial arrival estimates assume minutes for qualifying ordinary home deliveries; estimates are not guarantees and zero ETA does not prove delivery.
- Direct-leg indication is not a physical-pickup detector. Distance is straight-line, not road distance; GPS freshness is not guaranteed.
- Recorder may retain coordinates independently of integration retention. Token rotation cannot guarantee crash-safe recovery.

### Updating from development packages

- Replace the complete integration folder and restart Home Assistant; retain saved options and credentials.
- Old Min/Max sensors and the earlier binary-sensor IDs have no migration or compatibility aliases. Only installations using those obsolete IDs require the documented manual removal/re-addition and card/automation updates. Back up first and retain private access to the refresh token and desired settings; Recorder history is not migrated or deleted.
