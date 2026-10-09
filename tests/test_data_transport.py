"""Synthetic GET responses; sockets are blocked by the suite fixture."""

import pytest
from test_transport import Response, Session

from custom_components.wolt_monitor import transport


class GetSession(Session):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


async def test_fixed_origin_bounded_authenticated_subscription_get():
    session = GetSession(Response(body={"order_details": []}))
    assert hasattr(transport, "WoltDataClient"), "Data GET transport is missing"
    client = transport.WoltDataClient(session)
    assert await client.get("subscriptions", "synthetic-secret") == {"order_details": []}
    url, options = session.calls[0]
    assert str(url) == "https://restaurant-api.wolt.com/v2/order_details/subscriptions"
    assert options["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer synthetic-secret",
    }
    assert options["allow_redirects"] is False
    assert options["timeout"].total == 20
    assert "synthetic-secret" not in str(url)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (404, "unexpected"),
        (429, "rate_limit"),
        (503, "server"),
        (302, "unexpected"),
    ],
)
async def test_http_failure_preserves_status_and_retry_after_without_retry(status, category):
    from custom_components.wolt_monitor.errors import Failure

    session = GetSession(
        Response(
            status,
            {"error_code": 126},
            {
                "Content-Type": "application/json",
                "Retry-After": "60",
                "Location": "https://evil.invalid/secret",
            },
        )
    )
    with pytest.raises(Failure) as caught:
        await transport.WoltDataClient(session).get("subscriptions", "synthetic-secret")
    assert caught.value.category == category
    assert caught.value.http_status == status
    assert caught.value.retry_after == 60
    assert not caught.value.invalid_refresh
    assert caught.value.__context__ is None
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "response,category",
    [
        (Response(body="not-json-secret"), "invalid_data"),
        (Response(body=[]), "invalid_data"),
        (
            Response(body={"private": "secret"}, headers={"Content-Type": "text/html"}),
            "invalid_data",
        ),
        (Response(body={"private": "secret" * 200000}), "invalid_data"),
        (Response(error=TimeoutError("raw-url-secret")), "timeout"),
        (Response(error=OSError("raw-url-secret")), "connection"),
        (Response(error=ValueError("raw-url-secret")), "unexpected"),
        (Response(body='{"private": NaN}'), "invalid_data"),
    ],
)
async def test_untrusted_replies_are_bounded_and_errors_have_no_context(response, category, caplog):
    from custom_components.wolt_monitor.errors import Failure

    with pytest.raises(Failure) as caught:
        await transport.WoltDataClient(GetSession(response)).get(
            "subscriptions", "synthetic-secret"
        )
    assert caught.value.category == category
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value) + repr(caught.value) + caplog.text
    assert response.offset <= transport.MAX_DATA_BYTES + 1


@pytest.mark.parametrize(
    "resource,suffix",
    [
        ("details", "by_ids?purchases=a%2Fb%3F%23%25"),
        ("tracking", "purchase_tracking/a%2Fb%3F%23%25"),
    ],
)
async def test_literal_identifier_is_encoded_without_repair(resource, suffix):
    session = GetSession(Response(body={}))
    await transport.WoltDataClient(session).get(resource, "synthetic-secret", "a/b?#%")
    assert str(session.calls[0][0]) == transport.DATA_ORIGIN + "/v2/order_details/" + suffix


@pytest.mark.parametrize(
    "resource,key",
    [
        ("https://evil.invalid", None),
        ("tracking", ""),
        ("tracking", "."),
        ("tracking", ".."),
        ("tracking", "bad\nkey"),
        ("details", None),
        ("details", True),
        ("details", "a,b"),
        ("tracking", "bad\ud800key"),
        ("subscriptions", "extra"),
    ],
)
async def test_invalid_request_is_rejected_before_network(resource, key):
    from custom_components.wolt_monitor.errors import Failure

    session = GetSession(Response(body={}))
    with pytest.raises(Failure) as caught:
        await transport.WoltDataClient(session).get(resource, "synthetic-secret", key)
    assert caught.value.category == "invalid_data"
    assert caught.value.__context__ is None
    assert not session.calls
