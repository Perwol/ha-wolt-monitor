"""HTTP adapter tests use a session double, never a real endpoint."""

import importlib
import json

import pytest


class Response:
    def __init__(self, status=200, body=None, headers=None, error=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = body or {}
        self.error = error
        self.content = self
        self.raw = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.offset = 0

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self, size):
        chunk = self.raw[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    async def json(self, **kwargs):
        return self.body


class Session:
    def __init__(self, response):
        import aiohttp

        self.response = response
        self.calls = []
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.auth = None
        self.trust_env = False
        self.headers = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


async def test_classic_refresh_omits_cookies_redirects_and_access_authorization():
    http = importlib.import_module("custom_components.wolt_monitor.transport")
    session = Session(
        Response(
            body={
                "access_token": "fake-access",
                "refresh_token": "fake-rotated",
                "expires_in": 100,
            }
        )
    )
    client = http.ClassicAuthClient(session)
    reply = await client.refresh("fake-input")
    assert reply.refresh_token == "fake-rotated"
    url, options = session.calls[0]
    assert url == "https://authentication.wolt.com/v1/wauth2/access_token"
    assert options["data"] == {"grant_type": "refresh_token", "refresh_token": "fake-input"}
    assert options["allow_redirects"] is False
    assert "Authorization" not in options["headers"]
    assert len(session.calls) == 1
    assert "fake-rotated" not in repr(reply)


@pytest.mark.parametrize(
    "status,body,category,invalid",
    [
        (401, {"error_code": 126}, "authentication", True),
        (401, {"error_code": "126"}, "authentication", False),
        (401, {"error_code": True}, "authentication", False),
        (401, {"msg": "invalid credentials"}, "authentication", False),
        (404, {"error_code": 126}, "unexpected", False),
        (429, {}, "rate_limit", False),
        (503, {}, "server", False),
        (302, {}, "unexpected", False),
        (200, {}, "invalid_data", False),
        (200, [], "invalid_data", False),
    ],
)
async def test_only_integer_126_from_classic_401_is_invalid_refresh(
    status, body, category, invalid
):
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.transport import ClassicAuthClient

    client = ClassicAuthClient(Session(Response(status, body)))
    with pytest.raises(Failure) as caught:
        await client.refresh("fake-input")
    assert caught.value.category == category
    assert caught.value.invalid_refresh is invalid
    assert caught.value.http_status == status
    assert "fake-input" not in str(caught.value)


@pytest.mark.parametrize(
    "error,category",
    [
        (TimeoutError("fake-secret"), "timeout"),
        (OSError("fake-secret"), "connection"),
        (ValueError("fake-secret"), "unexpected"),
    ],
)
async def test_transport_errors_are_sanitized(error, category, caplog):
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.transport import ClassicAuthClient

    client = ClassicAuthClient(Session(Response(error=error)))
    with pytest.raises(Failure) as caught:
        await client.refresh("fake-input")
    assert caught.value.category == category
    assert caught.value.http_status is None
    assert "fake-secret" not in str(caught.value)
    assert "fake-secret" not in repr(caught.value)
    assert "fake-secret" not in caplog.text


async def test_retry_after_is_returned_without_replaying_request():
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.transport import ClassicAuthClient

    session = Session(Response(429, {}, {"Retry-After": "120", "Content-Type": "application/json"}))
    with pytest.raises(Failure) as caught:
        await ClassicAuthClient(session).refresh("fake-input")
    assert caught.value.retry_after == 120
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (401, {"error_code": 126}, {"Content-Type": "text/html"}),
        (401, "<html>fake-secret</html>", {"Content-Type": "application/json"}),
        (200, "x" * 17000, {"Content-Type": "application/json"}),
    ],
)
async def test_untrusted_or_oversized_body_cannot_trigger_reauth(status, body, headers):
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.transport import ClassicAuthClient

    with pytest.raises(Failure) as caught:
        await ClassicAuthClient(Session(Response(status, body, headers))).refresh("fake-input")
    assert not caught.value.invalid_refresh
    assert "fake-secret" not in str(caught.value)
    assert caught.value.http_status == status


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        ("nonsense", 0),
        ("-1", 0),
        ("nan", 0),
        ("inf", 0),
        ("60", 60),
    ],
)
def test_retry_after_invalid_values_are_safe(value, expected):
    from custom_components.wolt_monitor.transport import retry_after_seconds

    assert retry_after_seconds(value) == expected


def test_retry_after_http_date_uses_wall_clock_not_monotonic():
    from custom_components.wolt_monitor.transport import retry_after_seconds

    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", wall_time=1445412420) == 60


async def test_default_session_is_cookie_free_without_environment_credentials():
    import aiohttp

    from custom_components.wolt_monitor.transport import ClassicAuthClient

    client = ClassicAuthClient()
    assert isinstance(client._session.cookie_jar, aiohttp.DummyCookieJar)
    assert client._session.auth is None
    assert not client._session.trust_env
    await client.close()
    assert client._session.closed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"Authorization": "fake-secret"}},
        {"headers": {"Cookie": "fake-secret"}},
        {"trust_env": True},
        {"cookie_jar": None},
    ],
)
async def test_unsafe_session_is_rejected_before_sending(kwargs):
    import aiohttp

    from custom_components.wolt_monitor.transport import ClassicAuthClient

    options = {"cookie_jar": aiohttp.DummyCookieJar(), **kwargs}
    async with aiohttp.ClientSession(**options) as session:
        with pytest.raises(ValueError, match="Unsafe HTTP session"):
            ClassicAuthClient(session)
