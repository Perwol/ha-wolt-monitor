"""Classic Wolt token endpoint; caller owns a dedicated cookie-free session."""

import json
import math
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import aiohttp
from yarl import URL

from .auth import TokenReply
from .errors import Failure

CLASSIC_ENDPOINT = "https://authentication.wolt.com/v1/wauth2/access_token"
DATA_ORIGIN = "https://restaurant-api.wolt.com"
MAX_DATA_BYTES = 1024 * 1024


class WoltDataClient:
    """Dedicated, cookie-free fixed-origin GET transport; no automatic retries."""

    def __init__(self, session=None):
        self._client = ClassicAuthClient(session)

    async def close(self):
        await self._client.close()

    async def get(self, resource, access_token, order_key=None):
        path = self._path(resource, order_key)
        failure = None
        try:
            self._client._verify_session()
            async with self._client._session.get(
                URL(DATA_ORIGIN + "/v2/order_details/" + path, encoded=True),
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                return await self._parse_response(response)
        except Failure:
            raise
        except TimeoutError:
            failure = Failure("timeout")
        except aiohttp.ClientError, OSError:
            failure = Failure("connection")
        except Exception:
            failure = Failure("unexpected")
        raise failure

    @staticmethod
    def _path(resource, order_key):
        if resource == "subscriptions" and order_key is None:
            return "subscriptions"
        if (
            resource not in ("details", "tracking")
            or not isinstance(order_key, str)
            or not order_key
            or order_key in (".", "..")
            or any(
                ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in order_key
            )
            or (resource == "details" and "," in order_key)
        ):
            raise Failure("invalid_data")
        encoded = quote(order_key, safe="")
        return (
            "by_ids?purchases=" + encoded
            if resource == "details"
            else "purchase_tracking/" + encoded
        )

    async def _parse_response(self, response):
        status = response.status
        if status != 200:
            category = (
                "server"
                if status >= 500
                else "rate_limit"
                if status == 429
                else "authentication"
                if status == 401
                else "unexpected"
            )
            raise Failure(
                category,
                status,
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        body = None
        if response.headers.get("Content-Type", "").split(";")[0].lower() == "application/json":
            raw = bytearray()
            while len(raw) <= MAX_DATA_BYTES:
                chunk = await response.content.read(min(65536, MAX_DATA_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) <= MAX_DATA_BYTES:
                try:
                    body = json.loads(raw, parse_constant=_reject_json_constant)
                except ValueError, UnicodeDecodeError, RecursionError:
                    pass
        if not isinstance(body, dict):
            raise Failure("invalid_data", status)
        return body


class UnifiedClient(WoltDataClient):
    """Supplemental fixed-origin HTTP read; return only same-order completion."""

    def __init__(self, session=None, *, timezone="UTC"):
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
        self._timezone = timezone
        super().__init__(session)

    async def get_completion(self, access_token, order_key):
        from .state import CompletionResult

        # Public Order.fromWoltPurchaseId (modules 385696/343909 in
        # 22747-5c366388293b22ec.js): target 64, Wolt idspace 2, version 2
        # produces the verified prefix and the literal purchase ID, not a UUID rewrite.
        self._path("tracking", order_key)
        expected = "4ujWWng." + order_key
        url = URL(
            "https://unified-gateway.dashapi.com/order-tracking/v1/unified/marketplace/"
            + quote(expected, safe=""),
            encoded=True,
        ).with_query(client_timezone=self._timezone, hour_cycle="HOUR_CYCLE_H23")
        failure = None
        try:
            self._client._verify_session()
            async with self._client._session.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Pedregal-Brand": "wolt",
                    "App-Language": "en",
                    "baggage": "ptid=dashprod,platform=web",
                    "x-unified-gateway-generated-source": "v1",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                payload = await self._parse_response(response)
                result = payload.get("result")
                view = result.get("order_view") if isinstance(result, dict) else None
                if (
                    not isinstance(view, dict)
                    or view.get("order_pdrn") != expected
                    or not isinstance(view.get("status"), str)
                    or not view["status"]
                ):
                    raise Failure("invalid_data")
                return CompletionResult(order_key, view["status"] == "ORDER_COMPLETE")
        except Failure:
            raise
        except TimeoutError:
            failure = Failure("timeout")
        except aiohttp.ClientError, OSError:
            failure = Failure("connection")
        except Exception:
            failure = Failure("unexpected")
        raise failure


def _reject_json_constant(_value):
    raise ValueError("Invalid JSON constant")


def retry_after_seconds(value: str | None, *, wall_time: float | None = None) -> float:
    """Parse a server-directed delay without including header contents in errors."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except ValueError, TypeError:
        try:
            date = parsedate_to_datetime(value)  # type: ignore[arg-type]
            if date.tzinfo is None:
                return 0.0
            return max(0.0, date.timestamp() - (time.time() if wall_time is None else wall_time))
        except ValueError, TypeError, OverflowError:
            return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


class ClassicAuthClient:
    """Use a session without cookies or default authorization headers."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._owns_session = session is None
        self._session = (
            session
            if session is not None
            else aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        )
        self._verify_session()

    def _verify_session(self) -> None:
        if (
            not isinstance(self._session.cookie_jar, aiohttp.DummyCookieJar)
            or self._session.auth is not None
            or self._session.trust_env
            or "Authorization" in self._session.headers
            or "Cookie" in self._session.headers
        ):
            raise ValueError("Unsafe HTTP session")

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()

    async def refresh(self, refresh_token: str) -> TokenReply:
        failure = None
        try:
            self._verify_session()
            async with self._session.post(
                CLASSIC_ENDPOINT,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                headers={"Accept": "application/json"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                return await self._parse_response(response)
        except Failure:
            raise
        except TimeoutError:
            failure = Failure("timeout")
        except aiohttp.ClientError, OSError:
            failure = Failure("connection")
        except Exception:
            failure = Failure("unexpected")
        # Raise outside the except block so raw exceptions are not retained as context.
        raise failure

    async def _parse_response(self, response: aiohttp.ClientResponse) -> TokenReply:
        status = response.status
        retry_after = retry_after_seconds(response.headers.get("Retry-After"))
        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        body = None
        if content_type == "application/json":
            raw = bytearray()
            while len(raw) <= 16384:
                chunk = await response.content.read(16385 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) <= 16384:
                try:
                    body = json.loads(raw)
                except ValueError, UnicodeDecodeError:
                    pass
        if status != 200:
            category = (
                "server"
                if status >= 500
                else "rate_limit"
                if status == 429
                else "authentication"
                if status == 401
                else "unexpected"
            )
            invalid = (
                status == 401
                and isinstance(body, dict)
                and type(body.get("error_code")) is int
                and body["error_code"] == 126
            )
            raise Failure(category, status, retry_after=retry_after, invalid_refresh=invalid)
        if not isinstance(body, dict) or any(
            key not in body for key in ("access_token", "refresh_token", "expires_in")
        ):
            raise Failure("invalid_data", status)
        return TokenReply(body["access_token"], body["refresh_token"], body["expires_in"])
