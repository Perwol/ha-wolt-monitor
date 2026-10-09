"""Synthetic unified boundary checks; no endpoint/account requests."""

from unittest.mock import patch

import pytest
from test_data_transport import GetSession
from test_transport import Response

from custom_components.wolt_monitor.errors import Failure
from custom_components.wolt_monitor.transport import MAX_DATA_BYTES, UnifiedClient


@pytest.mark.parametrize(
    "key", ["a/b?#%", "opaque,identifier", "zażółć⚡", "https://evil.invalid/path"]
)
async def test_unified_literal_identity_routing_and_minimal_private_result(key):
    from urllib.parse import quote

    expected = "4ujWWng." + key
    session = GetSession(
        Response(
            body={
                "result": {
                    "order_view": {
                        "order_pdrn": expected,
                        "status": "ORDER_COMPLETE",
                        "registration_token": "private-registration-canary",
                        "edit_token": "private-edit-canary",
                        "share_token": "private-share-canary",
                    }
                }
            }
        )
    )
    client = UnifiedClient(session, timezone="Europe/Warsaw")
    result = await client.get_completion("private-bearer-canary", key)
    url, kwargs = session.calls[0]
    assert (url.scheme, url.host, url.port) == ("https", "unified-gateway.dashapi.com", 443)
    assert url.raw_path == "/order-tracking/v1/unified/marketplace/" + quote(expected, safe="")
    assert dict(url.query) == {"client_timezone": "Europe/Warsaw", "hour_cycle": "HOUR_CYCLE_H23"}
    assert kwargs["headers"]["Authorization"] == "Bearer private-bearer-canary"
    assert "canary" not in str(url) + repr(result)
    assert set(vars(result)) == {"key", "complete"}
    assert result.key == key and result.complete is True
    assert set(vars(client)) == {"_client", "_timezone"}


@pytest.mark.parametrize(
    "response,category",
    [
        (Response(302, {}, {"Location": "https://evil.invalid/private-canary"}), "unexpected"),
        (Response(401, {"error_code": 126}), "authentication"),
        (Response(429, {}, {"Retry-After": "900"}), "rate_limit"),
        (Response(error=TimeoutError("private-canary")), "timeout"),
        (Response(error=OSError("private-canary")), "connection"),
        (Response(error=RuntimeError("private-canary")), "unexpected"),
        (Response(body="private-canary"), "invalid_data"),
        (Response(body={"private": "x" * MAX_DATA_BYTES}), "invalid_data"),
        (Response(body=[]), "invalid_data"),
        (Response(body={"result": None}), "invalid_data"),
        (Response(body={"result": {"order_view": None}}), "invalid_data"),
        (
            Response(
                body={"result": {"order_view": {"order_pdrn": "4ujWWng.synthetic", "status": None}}}
            ),
            "invalid_data",
        ),
        (
            Response(
                body={"result": {"order_view": {"order_pdrn": "4ujWWng.synthetic", "status": True}}}
            ),
            "invalid_data",
        ),
        (
            Response(
                body={"result": {"order_view": {"order_pdrn": "4ujWWng.synthetic", "status": ""}}}
            ),
            "invalid_data",
        ),
        (
            Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.synthetic",
                            "status": "ORDER_COMPLETE",
                        }
                    }
                },
                headers={"Content-Type": "text/html"},
            ),
            "invalid_data",
        ),
        (Response(body='{"result": NaN}'), "invalid_data"),
    ],
)
async def test_unified_response_limits_and_sanitized_exception_chains(response, category, caplog):
    session = GetSession(response)
    with pytest.raises(Failure) as caught:
        await UnifiedClient(session).get_completion("private-bearer-canary", "synthetic")
    assert caught.value.category == category
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert caught.value.invalid_refresh is False
    assert len(session.calls) == 1
    assert response.offset <= MAX_DATA_BYTES + 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["timeout"].total == 20
    assert "canary" not in repr(caught.value) + str(caught.value) + caplog.text
    if response.status == 429:
        assert caught.value.retry_after == 900


@pytest.mark.parametrize("key", [None, True, "", ".", "..", "a\nb", "a\ud800b"])
async def test_unified_invalid_identity_fails_before_network(key):
    session = GetSession(Response(body={}))
    with pytest.raises(Failure):
        await UnifiedClient(session).get_completion("synthetic", key)
    assert not session.calls


async def test_invalid_timezone_is_rejected_before_owned_session_construction():
    with patch("custom_components.wolt_monitor.transport.aiohttp.ClientSession") as factory:
        with pytest.raises(KeyError):
            UnifiedClient(timezone="Invalid/Synthetic")
        factory.assert_not_called()


async def test_session_mutation_cannot_enable_proxy_cookies_or_default_credentials():
    session = GetSession(Response(body={}))
    client = UnifiedClient(session)
    session.trust_env = True
    with pytest.raises(Failure) as caught:
        await client.get_completion("synthetic", "synthetic")
    assert caught.value.category == "unexpected"
    assert caught.value.__context__ is None
    assert not session.calls
