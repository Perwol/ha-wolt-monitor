"""Coalesced HA polling with independent local retention and live options."""

import asyncio
import logging
from time import monotonic

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import OrderListResult, OrderResult, resolve_order_list
from .auth import TokenManager
from .const import NAME, REFRESH_TOKEN, options_from
from .errors import Failure
from .reads import ReadSuppressed, authenticated_read
from .safe_logging import SafeLog
from .scheduler import Scheduler
from .state import CompletionResult, CourierSnapshot, OrderSnapshot, Runtime

_LOGGER = logging.getLogger(__name__)


class WoltCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api, client, *, clock=monotonic):
        super().__init__(hass, _LOGGER, name=NAME, config_entry=entry, update_interval=None)
        self.entry, self.api, self.client = entry, api, client
        self.clock = clock
        self.scheduler = Scheduler(clock=clock, options=options_from(entry.options))
        self.runtime = Runtime(
            clock=clock, retention_minutes=self.scheduler.options.retention_minutes
        )
        self.tokens = TokenManager(
            entry.data[REFRESH_TOKEN], client.refresh, self._persist, clock=clock
        )
        self.courier_supported = False
        self.reauth_reload_requested = False
        self.auth_replaced = False
        self.entities = {}
        self.safe_log = SafeLog()
        self._task = None
        self._poll_cancel = self._retention_cancel = self._eta_cancel = None
        self._stopped = False
        self._shutdown_task = None
        self._stop_unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_ha_stop)
        self._cycle_id = 0
        self._data_not_before = 0.0
        self._unified_not_before = 0.0
        self.data = self.runtime.values()

    def _persist(self, token):
        if self.auth_replaced:
            return
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, REFRESH_TOKEN: token}
        )

    async def async_config_entry_first_refresh(self):
        await self.async_refresh()
        if self.runtime.reauth_required:
            raise ConfigEntryAuthFailed("Authentication required")
        if self.runtime.sources["order"].last_successful_update is None:
            raise ConfigEntryNotReady("Unable to connect to Wolt")

    async def async_request_refresh(self):
        await self._refresh(manual=True)

    async def async_refresh(self):
        await self._refresh(manual=False)

    async def _refresh(self, *, manual):
        if self._stopped or self.auth_replaced or self.hass.is_stopping:
            return
        if self._task is not None:
            await asyncio.shield(self._task)
            return
        self._task = self.hass.async_create_task(self._cycle(manual=manual))
        try:
            await asyncio.shield(self._task)
        finally:
            if self._task is not None and self._task.done():
                self._task = None

    async def _cycle(self, *, manual):
        due = frozenset()
        try:
            if self.runtime.reauth_required or (
                self.clock() < self.tokens.not_before and not self.tokens.usable()
            ):
                return
            due = self.scheduler.begin(manual=manual)
            self.safe_log.cycle(
                manual=manual,
                due=due,
                auth_wait=max(0, self.tokens.not_before - self.clock()),
                data_wait=max(0, self._data_not_before - self.clock()),
            )
            if not due:
                return
            self._cycle_id += 1
            cycle = str(self._cycle_id)
            if "order" in due:
                previous_key = self.runtime.order.key if self.runtime.order else None
                try:
                    result = await authenticated_read(
                        self.tokens, cycle, self._order_read, can_read=self._can_read
                    )
                except Failure as error:
                    self._fail("order", error, cycle)
                else:
                    if result.retired_key is not None:
                        self.runtime.retire_key(result.retired_key)
                    self.runtime.update_order(result.snapshot, cycle)
                    self.scheduler.set_errors("order", 0)
                    self.safe_log.success("order")
                    self.courier_supported = result.courier_supported
                    active = (
                        result.snapshot is not None
                        and not self.runtime.completed
                        and self.runtime.order is not None
                    )
                    self.scheduler.set_activity(
                        active=active,
                        courier_eligible=result.courier_eligible and result.courier_supported,
                    )
                    if not self.scheduler.courier_eligible:
                        self.runtime.clear_distance()
                    if (
                        active
                        and result.courier_eligible
                        and result.courier_supported
                        and (
                            "courier" in due
                            or (
                                self.runtime.order.key != previous_key
                                and self.scheduler.courier_read_allowed
                            )
                            or self.scheduler.courier_due
                        )
                    ):
                        due |= {"courier"}
            if (
                "order" in due
                and self.clock() >= self._data_not_before
                and self.clock() >= self._unified_not_before
                and getattr(self.api, "completion_supported", False) is True
                and not self.runtime.reauth_required
                and self.tokens.usable()
                and not self.runtime.delivery_confirmed
                and self.runtime.order is not None
            ):
                key = self.runtime.order.key

                async def completion(token):
                    result = await self.api.async_completion(token, key)
                    if (
                        not isinstance(result, CompletionResult)
                        or result.key != key
                        or type(result.complete) is not bool
                    ):
                        raise Failure("invalid_data")
                    return result

                try:
                    result = await authenticated_read(
                        self.tokens, cycle, completion, can_read=self._can_read
                    )
                except Failure as error:
                    self._fail("unified", error, cycle)
                    # A rejected shared access token is not a courier read failure.
                    if not self.tokens.usable():
                        return
                else:
                    self.runtime.expire()
                    self.runtime.sources["unified"].success()
                    self.safe_log.success("unified")
                    if (
                        result.complete
                        and self.runtime.order is not None
                        and self.runtime.order.key == result.key
                        and self.runtime.order.status == "cancelled"
                    ):
                        _LOGGER.warning(
                            "Conflicting terminal evidence: "
                            "same-order completion overrides cancellation"
                        )
                    self.runtime.confirm_delivery(result)
                    if self.runtime.completed or self.runtime.order is None:
                        self.scheduler.set_activity(active=False, courier_eligible=False)
            if self._stopped or self.auth_replaced or self.hass.is_stopping:
                return
            if (
                "courier" in due
                and self.scheduler.courier_eligible
                and self.runtime.order is not None
                and not self.runtime.completed
            ):
                if self.clock() < self._data_not_before:
                    return
                key = self.runtime.order.key

                async def fetch(token):
                    distance = await self.api.async_courier(token, key)
                    value = distance.distance if isinstance(distance, CourierSnapshot) else distance
                    if value is not None and (type(value) is not int or value < 0):
                        raise Failure("invalid_data")
                    return distance

                try:
                    distance = await authenticated_read(
                        self.tokens, cycle, fetch, can_read=self._can_read
                    )
                except Failure as error:
                    self._fail("courier", error, cycle)
                else:
                    self.runtime.update_courier(distance, cycle, order_key=key)
                    self.scheduler.set_errors("courier", 0)
                    self.safe_log.success("courier")
        except ReadSuppressed:
            pass
        finally:
            self.scheduler.finish(due)
            self.async_set_updated_data(self.runtime.values())
            self._schedule()
            self._task = None

    def _can_read(self):
        return not self._stopped and not self.auth_replaced and not self.hass.is_stopping

    async def _order_read(self, token):
        result = await self.api.async_order(token)
        if isinstance(result, OrderListResult):
            self.runtime.expire()

            async def fetch_details(key):
                if not self._can_read():
                    raise ReadSuppressed()
                if not self.tokens.usable():
                    raise Failure("timeout")
                return await self.api.async_order_details(token, key)

            result = await resolve_order_list(
                result,
                previous=self.runtime.order,
                completed=self.runtime.completed,
                excluded=self.runtime.excluded_keys,
                fetch_details=fetch_details,
            )
        if not isinstance(result, OrderResult) or (
            result.snapshot is not None and not isinstance(result.snapshot, OrderSnapshot)
        ):
            raise Failure("invalid_data")
        return result

    def _fail(self, source, error, cycle):
        self.runtime.fail(source, error, cycle)
        count = self.runtime.sources[source].consecutive_errors
        self.safe_log.failure(source, error, count)
        if source == "unified":
            base = self.scheduler.options.active_seconds
            self._unified_not_before = self.clock() + max(
                min(900, base * 2 ** min(count, 5)), error.retry_after
            )
        else:
            self.scheduler.set_errors(source, count)
        if source != "unified" and error.retry_after > 0 and not error.auth_failure:
            self._data_not_before = max(self._data_not_before, self.clock() + error.retry_after)
            self.scheduler.defer_data(error.retry_after)
        if error.invalid_refresh:
            self.entry.async_start_reauth(self.hass)

    def apply_options(self):
        selected = options_from(self.entry.options)
        self.scheduler.apply_options(selected)
        self.runtime.set_retention(selected.retention_minutes)
        self.async_set_updated_data(self.runtime.values())
        self._schedule()

    def _schedule(self):
        for cancel in (self._poll_cancel, self._retention_cancel, self._eta_cancel):
            if cancel is not None:
                cancel()
        self._poll_cancel = self._retention_cancel = self._eta_cancel = None
        if self._stopped or self.auth_replaced or self.hass.is_stopping:
            return
        if not self.runtime.reauth_required:
            deadline = self.scheduler.next_deadline
            if not self.tokens.usable():
                deadline = max(deadline, self.tokens.not_before)
            self._poll_cancel = async_call_later(
                self.hass, max(0, deadline - self.clock()), self._poll
            )
        delay = self.runtime.eta_update_delay
        if delay is not None:
            self._eta_cancel = async_call_later(self.hass, delay, self._update_eta)
        deadline = self.runtime.retention_deadline
        if deadline is not None:
            self._retention_cancel = async_call_later(
                self.hass, max(0, deadline - self.clock()), self._retire
            )

    @callback
    def _update_eta(self, _):
        self.async_set_updated_data(self.runtime.values())
        self._schedule()

    async def _poll(self, _):
        await self.async_refresh()

    @callback
    def _retire(self, _):
        self.runtime.expire()
        self.async_set_updated_data(self.runtime.values())
        self._schedule()

    async def _on_ha_stop(self, _):
        # HA removes one-shot listeners before invoking them.
        self._stop_unsub = None
        await self.async_shutdown()

    async def async_shutdown(self):
        if self._shutdown_task is None:
            self._stopped = True
            self._shutdown_task = asyncio.create_task(self._close())
        await asyncio.shield(self._shutdown_task)

    async def _close(self):
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        await super().async_shutdown()
        self._schedule()
        if self._task is not None:
            await asyncio.shield(self._task)
        try:
            await self.client.close()
        finally:
            close = getattr(self.api, "close", None)
            if close is not None:
                await close()
