import httpx
import pytest
import respx

from mastodon_admin_bot.ipinfo import (
    IpInfo,
    IpInfoClient,
    is_lookupable_ip,
    privacy_preserving_prefix,
)


def test_location_text_combines_country_and_asn_org() -> None:
    info = IpInfo(
        country="United States",
        asn_org="Google LLC",
    )

    assert info.location_text() == "United States, Google LLC"


def test_location_text_omits_missing_parts() -> None:
    assert IpInfo(country="Germany").location_text() == "Germany"
    assert IpInfo(asn_org="Example ISP").location_text() == "Example ISP"
    assert IpInfo().location_text() == ""
    assert IpInfo().has_data is False
    assert IpInfo(country="Germany").has_data is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.1", True),
        ("2001:db8::1", True),
        (" 192.0.2.1 ", True),
        ("", False),
        ("   ", False),
        ("not-an-ip", False),
        ("300.1.1.1", False),
    ],
)
def test_is_lookupable_ip(value: str, expected: bool) -> None:
    assert is_lookupable_ip(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.129", "192.0.2.0"),
        ("2001:db8:1234:5678:9abc:def0:1234:5678", "2001:db8:1234:5678:9abc::"),
        ("not-an-ip", None),
    ],
)
def test_privacy_preserving_prefix(value: str, expected: str | None) -> None:
    assert privacy_preserving_prefix(value) == expected


def test_privacy_preserving_prefix_accepts_custom_lengths() -> None:
    assert privacy_preserving_prefix("192.0.2.129", 16, 64) == "192.0.0.0"
    assert (
        privacy_preserving_prefix("2001:db8:1234:5678:9abc::1", 16, 64)
        == "2001:db8:1234:5678::"
    )


async def test_lookup_success_caches_result() -> None:
    client = IpInfoClient(base_url="https://ipip.test")
    with respx.mock(base_url="https://ipip.test") as router:
        route = router.get("/json", params={"ip": "192.0.2.0"}).respond(
            json={
                "country": "United States",
                "asn_org": "Google LLC",
            }
        )
        info = await client.lookup("192.0.2.1")
        again = await client.lookup("192.0.2.1")

    assert info == IpInfo(
        country="United States",
        asn_org="Google LLC",
    )
    assert again == info
    assert route.call_count == 1
    await client.aclose()


async def test_lookup_fail_status_returns_empty_and_caches_negatively() -> None:
    client = IpInfoClient(base_url="https://ipip.test")
    with respx.mock(base_url="https://ipip.test") as router:
        route = router.get("/json", params={"ip": "192.0.2.0"}).respond(json={})
        assert await client.lookup("192.0.2.1") == IpInfo()
        assert await client.lookup("192.0.2.1") == IpInfo()

    assert route.call_count == 1
    await client.aclose()


async def test_lookup_http_error_returns_empty() -> None:
    client = IpInfoClient(base_url="https://ipip.test")
    with respx.mock(base_url="https://ipip.test") as router:
        router.get("/json", params={"ip": "192.0.2.0"}).mock(
            side_effect=httpx.ConnectError("down")
        )
        assert await client.lookup("192.0.2.1") == IpInfo()

    await client.aclose()


async def test_lookup_coalesces_addresses_in_same_private_prefix() -> None:
    client = IpInfoClient(base_url="https://ipip.test")
    with respx.mock(base_url="https://ipip.test") as router:
        route = router.get("/json", params={"ip": "198.51.100.0"}).respond(
            json={"country": "Example"}
        )
        first = await client.lookup("198.51.100.1")
        second = await client.lookup("198.51.100.254")

    assert first == second == IpInfo(country="Example")
    assert route.call_count == 1
    await client.aclose()


async def test_lookup_uses_configured_prefix_lengths() -> None:
    client = IpInfoClient(
        base_url="https://ipip.test",
        ipv4_prefix_length=16,
        ipv6_prefix_length=64,
    )
    with respx.mock(base_url="https://ipip.test") as router:
        ipv4_route = router.get("/json", params={"ip": "198.51.0.0"}).respond(json={})
        ipv6_route = router.get(
            "/json", params={"ip": "2001:db8:1234:5678::"}
        ).respond(json={})
        await client.lookup("198.51.100.20")
        await client.lookup("2001:db8:1234:5678:9abc::1")

    assert ipv4_route.called
    assert ipv6_route.called
    await client.aclose()


async def test_lookup_invalid_ip_skips_network() -> None:
    client = IpInfoClient(base_url="https://ipip.test")
    with respx.mock(base_url="https://ipip.test", assert_all_called=False) as router:
        route = router.get("/json", params={"ip": "not-an-ip"})
        assert await client.lookup("not-an-ip") == IpInfo()
        assert await client.lookup("") == IpInfo()

    assert route.call_count == 0
    await client.aclose()
