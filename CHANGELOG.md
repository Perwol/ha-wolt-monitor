# Changelog

Notable changes to Wolt Monitor are documented here.

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
