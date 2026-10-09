<p align="center">
  <img src="custom_components/wolt_monitor/brand/logo.png" width="640" alt="Wolt Monitor">
</p>
<h1 align="center">Wolt Monitor</h1>
<p align="center">Unofficial Wolt order tracking integration for Home Assistant.</p>

Track a Wolt order's status, restaurant, estimated arrival and courier delivery. Supports one account, with an English or Polish interface.

**Version 1.0.0** — one account, eight entities and configurable HTTP polling.

Wolt Monitor is unofficial and community-developed, not affiliated with, endorsed by or supported by Wolt or Home Assistant.
Unofficial Wolt interfaces may change or stop working without notice.

## Installation

Requires **Home Assistant 2026.3.1+**. Choose one installation method, then complete setup below.

### HACS — after publication

With [HACS installed](https://www.hacs.xyz/docs/use/), use **Add Custom Repository to HACS**
below to add this custom repository. It does **not** download or install the integration.

[![Add Custom Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Perwol&repository=ha-wolt-monitor&category=integration)

Alternatively, in HACS select **⋮ → Custom repositories**, enter `https://github.com/Perwol/ha-wolt-monitor`, and choose **Integration**.
Then download Wolt Monitor and fully restart Home Assistant.

**The repository is not published yet.** The button and GitHub links are for future publication and may return 404 until then.
Adding it requires a published repository compatible with HACS, not a default HACS listing.

### Manual installation

1. Obtain the current source or the supplied full integration package and unpack it locally.
2. Copy the complete `custom_components/wolt_monitor` folder into your HA configuration directory, including `translations` and `brand`.
   The final path must be `<config>/custom_components/wolt_monitor/manifest.json`.
   File editor or another file-access tool can be used; uploading only a ZIP is not installation.
3. Fully restart HA. For updates, replace all files, keep the configuration entry and refresh your browser after restarting.

### Set up Wolt Monitor

In **Settings → Devices & services → Add integration**, choose **Wolt Monitor** and enter a **Refresh token**.
Initial setup requires a token; in **Options**, a replacement is optional: **leave blank to keep the current refresh token**.
Both forms offer the same four settings below.

### Configuration fields

| Field | Description | Input unit | Default | Allowed range / step |
|---|---|---|---|---|
| **Refresh token** | Authenticates your Wolt account. Required initially; optional in Options, where blank keeps the current token. See [instructions](#refresh-token). | Token text (masked) | None | Not applicable |
| **No active order polling interval** | How often to look for orders when none is selected. | Minutes | **2 minutes** | 1–10 minutes / 1 minute |
| **Active order polling interval** | How often to refresh the current order data. | Seconds | **30 seconds** | 30–300 seconds / 30 seconds |
| **Courier polling interval** | How often to refresh courier tracking during delivery. | Seconds | **30 seconds** | 30–300 seconds / 30 seconds |
| **Completed order retention** | How long to keep a delivered or cancelled order visible; zero clears it immediately. | Minutes | **10 minutes** | 0–60 minutes / 1 minute |

Defaults apply to new setup or missing options; existing saved intervals are preserved.
Idle polling displays minutes without changing saved durations. Order and courier polling are separate.
Settings-only changes apply without reload. Replacement tokens are validated before saving; failure changes neither token nor settings.
Successful replacement reloads the integration, preserving identities but clearing in-memory order state.
Blank token input in Options causes no authentication request or reload.

### Refresh token

Sign in at `https://wolt.com` in a desktop browser, open **Developer Tools → Console**,
and run the snippet below. It only reads the local cookie and prints the token as plain text,
without quotes; it makes no requests and does not rotate the token. Copy the output into
**Refresh token**. If the cookie is missing, sign in again.
Do not bypass a browser's paste warning blindly: read and understand the snippet first.

```javascript
(() => {
  try {
    if (location.origin !== "https://wolt.com") {
      console.log("Open https://wolt.com and sign in first.");
      return;
    }
    const cookie = document.cookie.split(";")
      .map(part => part.trim())
      .find(part => part.startsWith("__wrtoken="));
    const token = cookie
      ? JSON.parse(decodeURIComponent(cookie.slice("__wrtoken=".length)))
      : null;
    if (typeof token !== "string" || !token || /\s/.test(token)) {
      console.log("Refresh token not found. Sign in to Wolt and try again.");
      return;
    }
    console.log(token);
  } catch {
    console.log("Unable to read the refresh token. Sign in to Wolt and try again.");
  }
})();
```

**Treat the token like a password. Never share it, console screenshots, or authentication cookies.**
Keep the token private when copying it into Home Assistant.

## Entities

All eight entities are created at setup: five sensors, two binary sensors and one GPS device tracker.
Courier Distance and Courier Position are available only while Delivery In Progress is on,
and their required location data is valid; otherwise they are unavailable.
Default entity IDs use
`sensor.wolt_monitor_<key>`, `binary_sensor.wolt_monitor_<key>` or
`device_tracker.wolt_monitor_<key>`; HA entity IDs may be customized.

| Entity | Identity key | Meaning |
|---|---|---|
| **Latest Order Status** | `latest_order_status` | Normalized order stage for automations. |
| **Latest Order Restaurant** | `latest_order_restaurant` | The selected order's restaurant. |
| **Latest Order Estimated Delivery Time** | `latest_order_delivery_time` | One predicted arrival date and time. |
| **Latest Order ETA** | `latest_order_eta` | Whole minutes remaining to that same arrival. |
| **Latest Order Courier Distance** | `latest_order_courier_distance` | Always present; available only while Delivery In Progress is on with valid courier/destination locations. Straight-line metres, not road distance. |
| **Latest Order Courier Position** | `latest_order_courier_position` | GPS device tracker for the same unambiguously selected courier. Numeric GPS coordinates are in its `latitude` and `longitude` attributes, available only while Delivery In Progress is on with valid courier coordinates. Its state (`home`, `not_home` or a zone name) describes a zone, not a delivery stage. |
| **Latest Order Delivery In Progress** | `latest_order_delivery_in_progress` | On when a ready order has a valid current direct-leg indication to you. Icon: `mdi:moped`. |
| **Latest Order Delivered** | `latest_order_delivered` | On after confirmed delivery during retention; off for confirmed ongoing or cancelled orders. |

Status uses English/Polish display labels; raw enum values used by automations are unchanged.
Courier Distance suggests zero display decimals in HA; its numeric value and metre unit are unchanged.
This is a display default, not an override of an existing user precision preference.
Binary sensors use delivery-specific English/Polish labels; automation values remain `on`/`off`.
To show the courier, add Home Assistant's built-in **Map** card and select
`device_tracker.wolt_monitor_latest_order_courier_position` (or your customized entity ID).
**Upgrading from Min/Max sensors:** one timestamp and one ETA replace the old pairs. Update affected automations.
There is no automatic migration of names, history or automations, compatibility aliases or registry cleanup.
**Uniform binary sensor IDs:** `binary_sensor.wolt_monitor_delivery_in_progress` and
`binary_sensor.wolt_monitor_latest_order_is_delivered` are replaced by
`binary_sensor.wolt_monitor_latest_order_delivery_in_progress` and
`binary_sensor.wolt_monitor_latest_order_delivered`. No registry migration or aliases are provided.
Only when upgrading from those older binary IDs, remove and re-add the integration manually,
then update cards and automations that reference the old IDs. The shared courier-availability
gate itself needs only replacement of the complete integration files and a full HA restart,
not removal or re-addition. The five sensor keys are unchanged; Recorder history is not deleted.

## Order behavior and limitations

### Selection and availability

One selected order is tracked, without a separate history. Selection stays stable when Wolt reorders its list.
**“Latest Order” does not guarantee the newest order**: the list fallback uses the first eligible order without reliable chronology.
Disappearance or a source failure does not prove delivery, cancellation or absence.

Status values are `received`, `acknowledged`, `scheduled`, `preparing`, `ready`, `on_the_way`,
`delivered` and `cancelled`. `no_active_order` means confirmed absence with no retained order.
`unknown` means the stage cannot be interpreted reliably; `unavailable` means an entity lacks
reliable data under its availability/error rules. Valid estimates can remain available with
an unknown stage; courier distance and position cannot. Tracking alone cannot turn delivery on. The Delivered flag
is unavailable when delivery cannot be determined reliably. With no selected or retained
order, both binary sensors and the other data sensors are unavailable.

### Arrival estimates

Estimated Delivery Time and ETA share one prediction: a valid range's upper end or a single forecast without an added margin.
Estimates can move earlier or later. ETA rounds up to whole minutes, never below zero. **Zero ETA does not prove delivery.**

Without a reliable direct forecast, qualifying ordinary home deliveries may use the initial estimate's upper bound
added to the fixed payment time—not the latest refresh time. This bounded fallback is approximate:
payment time approximates order placement, and minutes are assumed, not verified Wolt units.
Scheduled/preorder, day-based, takeaway and unrecognized delivery/stage variants are excluded.
Missing or invalid inputs leave the estimate unavailable; reversed bounds are not silently repaired.
Later valid direct forecasts take precedence. These are predictions, not actual delivery times.

### Delivery and courier tracking

A `ready` order with a valid, unambiguous **current direct-leg indication for the same order**
sets Status to `on_the_way` and Delivery In Progress to on together. If the indication becomes
false, they return to `ready`/off. Assignment alone, or a true indicator during preparation,
is insufficient. No pickup history is required or remembered. These are API indications,
not proof of physical handover; a transient indicator may disagree with the courier's real route.
Missing or ambiguous tracking cannot establish a direct leg; cache/error rules still apply.

Both courier entities require Delivery In Progress to be on. A false or unavailable flag
makes both unavailable, including a return to preparation or an unknown stage.
Distance additionally requires valid courier/destination locations.
Missing positions or ambiguity make it unavailable; the nearest courier is not guessed. Delivery In Progress may remain on without distance.
Courier Position uses the same courier selection as distance but does not require a destination location.
It adds no API requests. Neither position nor distance alone proves pickup or delivery.
The GPS tracker uses standard HA zone states (such as `home` / `not_home`), not delivery stages,
and is not assigned to a person by the integration. No location timestamp is supplied.
Location freshness is not guaranteed. Missing supported-delivery metadata can leave tracking, distance and position unavailable.
Position becomes unavailable immediately on delivery or cancellation, and after expiry or order replacement;
unavailable states do not expose the previous GPS point. Changes are applied when data is
refreshed; polling and eligible cached data do not guarantee an immediate physical pickup or route transition.

Either of two HTTP sources can confirm delivery of the exact same selected order; both are not required.
Supplemental-source failures do not disable healthy primary data. Confirmation wins over conflicting cancellation
without extending retention or carrying over to another order. HTTP polling is used, not WebSockets.

### Completed orders

Status and Restaurant remain visible for the configured retention period:

| Situation | Estimated Delivery Time | ETA | Courier Distance | Delivery In Progress | Delivered |
|---|---|---|---|---|---|
| Confirmed delivery, during retention | Unavailable | 0 minutes | Unavailable | Off | On |
| Confirmed cancellation, during retention | Unavailable | Unavailable | Unavailable | Off | Off |
| No selected order after expiry | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |

Retention starts at first local completion detection and is not renewed by repeated responses.
Expiry is enforced even during failures; zero retention clears the order immediately.
A new active order replaces the retained one without inheriting its data or returning to it later.
Completed orders are not redisplayed after expiry in the same runtime session. Restarts,
reloads and successful reauthentication reset memory-only order/retention state, not Recorder history.
A refund alone proves neither cancellation nor delivery and does not extend retention or undo
already confirmed delivery. Authentication and order-error availability rules still take precedence.

## Authentication, troubleshooting and privacy

Access tokens stay in memory; rotating refresh tokens are saved through Home Assistant.
A masked input is not an encryption guarantee. A crash or failed disk write during rotation
can leave an unusable old token on disk; recovery is not guaranteed. If the refresh token is
confirmed invalid, Home Assistant requests reauthentication. Only classic Wolt authentication
is targeted; other providers are not verified.

With previous valid data, the first two consecutive source failures retain that source's cache.
The third order failure makes all entities unavailable; the third tracking failure makes
Courier Distance, Courier Position and Delivery In Progress unavailable. Without prior valid data, affected
entities are unavailable immediately. Cached data is not a fresh read; inconclusive tracking
cannot extend old evidence. Confirmed completion takes precedence over tracking failures.
Retries slow down after errors; server-requested waits are respected. A manual
`homeassistant.update_entity` refresh does not bypass authentication, failed-source backoff
or server-imposed waits.

No orders are placed or account settings changed; authentication may rotate tokens.
Logs and diagnostics omit secrets, raw responses and identifying order/location data.
HA Recorder may retain restaurant names, delivery times and **courier coordinates** independently
of this integration's retention. The GPS coordinates are exposed as HA state attributes for map use;
logs and integration diagnostics never include them. If you do not want future coordinate history,
manually merge the following into your existing Recorder configuration (use your actual entity ID
if customized), then apply it according to HA's instructions. This integration does not change Recorder
or erase previously recorded data.

```yaml
recorder:
  exclude:
    entities:
      - device_tracker.wolt_monitor_latest_order_courier_position
```
Redact tokens and personal data before reporting an issue.

## Development and support

For local development, use **Python 3.14.2+** and `uv`:

```sh
uv sync --locked
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/check_metadata.py
.venv/bin/pytest --cov --cov-report=term-missing -q
```

Tests use synthetic data and anonymized observation fixtures with outbound sockets blocked.
After publication, report redacted, reproducible problems via [GitHub Issues](https://github.com/Perwol/ha-wolt-monitor/issues).

Optional support: [Buy Me a Coffee](https://www.buymeacoffee.com/perwol). The integration is free and open source.

## License

[MIT](LICENSE) — Copyright (c) 2026 Maciej Perłakowski.
