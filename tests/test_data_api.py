"""Verified Wolt shapes filled exclusively with synthetic values."""

import pytest
from test_data_transport import GetSession
from test_transport import Response

from custom_components.wolt_monitor import api, transport


def details(key="synthetic", **fields):
    return {"order_id": key, "status": "ready", "venue_name": "synthetic venue", **fields}


async def test_subscription_transport_to_normalized_order_without_unproven_units():
    session = GetSession(
        Response(
            body={
                "order_details": [
                    details(payment_time={"$date": 1791374400000}, client_pre_estimate="10-20")
                ],
                "group_orders": [],
                "expires_in_seconds": 15,
            }
        )
    )
    assert hasattr(api, "WoltDataAPI"), "Real DataAPI adapter is missing"
    result = await api.WoltDataAPI(transport.WoltDataClient(session)).async_order(
        "synthetic-secret"
    )
    assert result.orders[0].key == "synthetic"
    assert result.orders[0].status == "ready"
    assert result.orders[0].restaurant == "synthetic venue"
    assert result.orders[0].estimated_delivery_time is None
    assert "synthetic" not in repr(result)


@pytest.mark.parametrize(
    "fields,supported",
    [
        ({"delivery_method": "homedelivery", "is_marketplace_v2": False}, True),
        (
            {
                "delivery_method": "homedelivery",
                "is_marketplace_v2": True,
                "self_delivery": {"is_tracking_enabled": True},
            },
            True,
        ),
        ({"delivery_method": "homedelivery", "is_marketplace_v2": True}, False),
        ({"delivery_method": "takeaway", "is_marketplace_v2": False}, False),
        ({"delivery_method": "homedelivery"}, False),
        ({"delivery_method": "homedelivery", "is_marketplace_v2": "false"}, False),
        (
            {
                "delivery_method": "homedelivery",
                "is_marketplace_v2": True,
                "self_delivery": {"is_tracking_enabled": 1},
            },
            False,
        ),
    ],
)
async def test_tracking_metadata_belongs_to_each_order(fields, supported):
    payload = {
        "order_details": [
            details("chosen", **fields),
            details("other", delivery_method="homedelivery", is_marketplace_v2=False),
        ]
    }
    result = await api.WoltDataAPI(
        transport.WoltDataClient(GetSession(Response(body=payload)))
    ).async_order("synthetic-secret")
    selected = result.for_order(result.orders[0])
    assert selected.courier_supported is supported
    assert selected.courier_eligible is supported
    assert result.for_order(result.orders[1]).courier_supported is True
    assert result.for_order(None).courier_supported is False
    assert result.courier_supported is False  # no global capability leaks


async def test_selected_details_use_verified_by_ids_purchase_query():
    session = GetSession(
        Response(
            body={
                "order_details": [
                    details("chosen", delivery_method="homedelivery", is_marketplace_v2=False)
                ]
            }
        )
    )
    adapter = api.WoltDataAPI(transport.WoltDataClient(session))
    assert hasattr(adapter, "async_order_details"), "Chosen details read is missing"
    result = await adapter.async_order_details("synthetic-secret", "chosen")
    assert result.snapshot.key == "chosen"
    assert result.courier_supported
    assert result.courier_eligible
    assert str(session.calls[0][0]).endswith("/by_ids?purchases=chosen")


async def test_tracking_uses_nested_selected_destination_without_caching_payload():
    payload = {
        "order_details": details(
            delivery_location={"coordinates": {"type": "Point", "coordinates": [21, 52]}}
        ),
        "drivers": [{"delivering_your_order": True, "location": [21, 52]}],
        "expires_in_seconds": 5,
    }
    session = GetSession(Response(body=payload))
    adapter = api.WoltDataAPI(transport.WoltDataClient(session))
    assert hasattr(adapter, "async_courier"), "Tracking mapping is missing"
    result = await adapter.async_courier("synthetic-secret", "synthetic")
    assert result.distance == 0
    assert result.delivery_in_progress is True
    assert str(session.calls[0][0]).endswith("/purchase_tracking/synthetic")
    assert set(vars(adapter)) == {"_client", "_unified_client"}


@pytest.mark.parametrize(
    "method,payload",
    [
        ("async_order", {}),
        ("async_order", {"order_details": {}}),
        ("async_order", {"order_details": [None]}),
        ("async_order", {"order_details": [details(), details()]}),
        ("async_order_details", {"order_details": []}),
        ("async_order_details", {"order_details": [details("wrong-secret-id")]}),
        ("async_order_details", {"order_details": [details(), details()]}),
        ("async_courier", {"order_details": [details()], "drivers": []}),
        ("async_courier", {"order_details": details("wrong-secret-id"), "drivers": []}),
        ("async_courier", {"order_details": details(), "drivers": "malformed-secret"}),
    ],
)
async def test_malformed_nested_data_and_identity_mismatch_are_private_failures(
    method, payload, caplog
):
    from custom_components.wolt_monitor.errors import Failure

    adapter = api.WoltDataAPI(transport.WoltDataClient(GetSession(Response(body=payload))))
    args = ("synthetic-secret",) if method == "async_order" else ("synthetic-secret", "synthetic")
    with pytest.raises(Failure) as caught:
        await getattr(adapter, method)(*args)
    assert caught.value.category == "invalid_data"
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value) + repr(caught.value) + caplog.text


@pytest.mark.parametrize(
    "drivers,flag,distance",
    [
        (None, None, None),
        ([], None, None),
        ([{}], None, None),
        ([{"delivering_your_order": False}], False, None),
        ([{"delivering_your_order": True}], True, None),
        (
            [
                {"delivering_your_order": True, "location": [21, 52]},
                {"delivering_your_order": True},
            ],
            None,
            None,
        ),
    ],
)
async def test_valid_incomplete_tracking_is_not_a_transport_failure(drivers, flag, distance):
    payload = {"order_details": details(), **({"drivers": drivers} if drivers is not None else {})}
    result = await api.WoltDataAPI(
        transport.WoltDataClient(GetSession(Response(body=payload)))
    ).async_courier("synthetic-secret", "synthetic")
    assert result.delivery_in_progress is flag
    assert result.distance is distance


async def test_missing_chosen_details_404_does_not_confirm_absence():
    from custom_components.wolt_monitor.errors import Failure

    session = GetSession(Response(404, {}))
    with pytest.raises(Failure) as caught:
        await api.WoltDataAPI(transport.WoltDataClient(session)).async_order_details(
            "synthetic-secret", "synthetic"
        )
    assert caught.value.http_status == 404
    assert len(session.calls) == 1


async def test_factory_constructs_owned_cookie_free_adapter_without_request():
    from types import SimpleNamespace

    adapter = api.create_data_api(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")))
    try:
        assert isinstance(adapter, api.WoltDataAPI)
        assert not adapter._client._client._session.trust_env
        assert hasattr(adapter, "close"), "Adapter owns lifecycle closure"
    finally:
        if hasattr(adapter, "close"):
            await adapter.close()
    assert adapter._client._client._session.closed
