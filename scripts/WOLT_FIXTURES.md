# Offline Wolt fixture sanitization and replay

`scripts/wolt_fixtures.py` is a local library, not an account capture client. Sanitization happens in memory; the only disk output API is sanitized-only `CaptureSession.save()`. There is no CLI, raw/file input, HTTP, browser, token lookup, auth refresh, polling or logging. Product normalization is unchanged; coordinator and replay share a narrow list/details resolution helper. Tests use synthetic inputs for capture/privacy/persistence checks and replay saved sanitized observations. `tests/fixtures/wolt/` contains 34 observed JSON artifacts: seven first-order legacy payload fixtures, nine second-order legacy fixtures plus eight unified technical allowlist summaries, and five third-order legacy fixtures plus five unified summaries. These are minimized observations, not full structural responses; unified summaries are not inputs to legacy payload replay. Session aliases, a common timestamp shift and synthetic geometry preserve replay relationships, not original GPS evidence.

## Memory API

```python
from datetime import timedelta
from scripts.wolt_fixtures import CaptureSession, replay, validate_dataset

session = CaptureSession(offset=timedelta(days=30), provenance="synthetic")
# `payload` must already exist only in memory. Never load a raw dump from disk.
clean = session.capture("subscriptions", payload)
# Other resources: "details" (order_details list), "tracking" (object + drivers).
# Use the SAME session across every endpoint/stage of one recording.
shifted_at = session.reference_time(timezone_aware_reference_datetime)
stage = {"stage": "ready", "at": shifted_at, "responses": {"subscriptions": clean}}
dataset = session.dataset([stage])
validate_dataset(dataset)
values = await replay(dataset)  # No account/token/network is accessed.
# Only for an approved recording, with a non-identifying local slug:
# path = session.save([stage], slug="recording-one")
```

Pass an explicit common `timedelta` chosen for the recording. Observed sessions reject zero offsets before processing; synthetic sessions permit zero. All offsets must be whole milliseconds (sub-millisecond offsets are rejected for both modes). Supported timestamp fields (`payment_time`, `delivery_eta`, `delivery_eta_min`, `delivery_eta_max`, `delivery_time`) and stage reference time are shifted together; native `$date` wrappers and numeric types are retained using exact millisecond addition, and aware ISO timezones are retained. `delivery_time` is preserved for chronology, not promoted to a forecast source. The offset and original reference time are not serialized. Stage times must be nondecreasing.

IDs are encounter-order `order-N` aliases, coherent within a session, without hashes or source-ID metadata. Venue strings become `Fixture venue`. Address/customer/venue IDs, URLs, arbitrary restaurant/group-order arrays, headers, cookies, tokens and every unknown field are omitted by explicit schemas. All 18 known public-client raw order status strings are retained; the nine currently unmapped strings still normalize to `unknown` in the product. Other status strings become `unknown`. Unknown scalar strings become fixed witnesses rather than raw text; invalid booleans/numbers/containers preserve safe type witnesses, not contents. Missing fields remain missing; null remains null. Native invalid numeric dates use an out-of-range witness, not epoch zero. Lists/order order and explicit courier flags are retained.

Coordinates are **fixed synthetic replacements** (destination `[0, 0]`, courier `[0.001, 0]`) only when the original value passes the integration's coordinate validator. Invalid positions never become valid. Neither original geometry, relative distance nor observed movement survives. Metadata explicitly says `synthetic-replacement`; resulting distance is adapter test behavior, never proof of live distance. Multiple true courier flags still suppress distance.

Size/type validation rejects unsupported Python objects, excessive nesting, strings, integers and collections. Unknown dictionaries are not blindly recursively copied or string-replaced. Failures have generic messages without source exception context/cause. Do not log inputs, session internals, locals, tracebacks with locals, or debugger representations: the transient alias map necessarily contains raw IDs in memory. Python does not guarantee secure erasure. Discard the session and raw references after processing. This is schema minimization, not a claim of formally irreversible anonymization; relative timing/status sequences can still be identifying in context.

## Sanitized-only atomic persistence

`session.save(stages, slug="recording-one")` rebuilds metadata from the capture session, validates exact schemas, takes a detached JSON snapshot, validates it again, then encodes canonical sorted-key compact UTF-8 JSON with `allow_nan=False`. The final byte budget is 1 MiB. No bytes are written before validation, snapshotting, slug validation and the size check succeed. There is no public writer accepting an arbitrary metadata/dataset object or raw payload.

The destination is fixed to this repository's `tests/fixtures/wolt/`; there is no caller-supplied path or external base argument. Slugs match `[a-z0-9][a-z0-9-]{0,63}` and receive the `.json` suffix. Use non-identifying slugs, never original IDs or other account information. Absolute paths, traversal, separators and symlink directories (including ancestors) fail closed. Descriptor-relative no-follow directory walks, exclusive mode-0600 temporary creation, file fsync, atomic no-replace hard-link publication, owned-temp cleanup and directory fsync protect the sanitized snapshot. Existing targets, including symlinks, are never overwritten. Filesystem failures expose only a generic error with no source context/cause. A failure after publication (for example directory fsync failure) may leave the complete validated final file; inspect rather than retry under the same slug. Unsupported filesystem primitives fail closed. This is not a power-loss durability certification; creation of missing parent directories and abrupt process death can leave directories or a sanitized temporary file.

`dataset()`, replay and save reject raw/private additions, but **validation is not an attestation of real observation**. The `observed` label comes only from a session explicitly configured with a nonzero common offset, not caller metadata. The operator must use the same session's `capture()` and `reference_time()` results for every stage and truthfully label the source. A hand-constructed alias-shaped payload or already-shifted-looking ISO date cannot be distinguished from real capture by schema validation. Do not relabel synthetic examples as observations, and do not serialize the transient session/alias map. No fixture is written to the repository merely to demonstrate this API.

## Future manually approved observation protocol (recipe only)

No live work is authorized by this document. Obtain fresh, scoped approval for the account, exact stage and GET requests before any access; authentication/refresh requires separate explicit approval. Never retrieve credentials or refresh automatically.

Plan a bounded four-to-five-stage natural-order recording (received/acknowledged, production, ready, courier delivery, delivered; refund only if actually observed and separately approved). For each user-notified and specifically approved stage:

1. One GET `/v2/order_details/subscriptions`.
2. Choose the order in memory; one GET `/v2/order_details/by_ids?purchases=<literal-encoded-chosen-ID>`.
3. Only if explicitly supported for that chosen order, one GET `/v2/order_details/purchase_tracking/<literal-encoded-chosen-ID>`.

At most three GETs per approved stage; tracking may be omitted when unsupported. No background polling, retries, automated monitoring, UI/account automation, refresh or extra endpoints. Record the reference time in memory and shift it with the same session offset. Sanitize each reply immediately, discard raw references, assemble only sanitized stages, validate, then use the approved `CaptureSession.save()` persistence path. Notify the user of the result and wait for the next stage notification. HTTP failures are not captured as arbitrary raw error bodies; this version supports successful payload shape replay only.

## Verification and gaps

Run `.venv/bin/pytest tests/test_fixture_capture.py -q`, then the repository's full pytest/coverage, Ruff and local metadata checks. Replay feeds JSON-roundtripped sanitized structures into the actual `WoltDataAPI` with a synthetic memory transport, shared coordinator list/details resolution, actual `select_order`, and actual `Runtime`. Controlled wall and monotonic time verify received/preparing/ready/on-the-way/delivered/retention, a single upper-bound forecast (not separate Min/Max entities), ceil-based nonnegative ETA from that same timestamp, and explicit delivery evidence. Tests cover private canaries, malformed positions/native dates, timestamp relationships, reversed forecasts, unknown types, coherent aliases and schema rejection.

This is not a live endpoint-contract validation, HA server/entity/coordinator capture, or automatic account pipeline. Runtime source-success diagnostic timestamps still use the product's real clock; replay controls business ETA/retention clocks, not those diagnostics. Initial estimate units remain unverified; the approved bounded ordinary-home-delivery assumption treats eligible `client_pre_estimate` ranges as minutes, anchored to `payment_time` plus the upper bound, never to the current time. Scheduled/preorder/day-unit and unsupported variants are excluded; valid later dynamic forecasts take precedence. Paired synthetic tests exercise the actual coordinator cycle and replay for unused details, immediate terminal replacement and retirement, capability loss clearing tracking, and expiry-before-selection. Replay reads tracking only for active eligible orders when a tracking response is supplied; it does not reproduce scheduler/backoff/auth failures or absent tracking reads. Saved natural-order stages are partial evidence: the second order includes a historical unified `ORDER_COMPLETE` observation before the current adapter integration; the third order has five captured checkpoints but no terminal capture. Refund observations and chronology/replacement races remain unverified. Neither these fixtures nor synthetic tests establish live acceptance of the current unified integration or owner HA acceptance. Existing transport and HA synthetic tests remain separate evidence. Configured repository coverage measures product code, not this scripts module.
