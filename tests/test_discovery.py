"""Tests for discovery from anonymized MyMeter chart responses."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.pseg.const import PORTAL_URL_LI, PORTAL_URL_NJ
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.discovery import parse_meters, parse_services
from custom_components.pseg.pseg_client import (
    PsegAuthError,
    PsegClient,
    PsegConnectionError,
    select_meter_series,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    """Load an anonymized portal response."""
    return json.loads((FIXTURES / name).read_text())


def response(data=None, *, status=200, url=f"{PORTAL_URL_NJ}/Dashboard"):
    """Make a portal response."""
    result = MagicMock()
    result.status_code = status
    result.url = url
    result.is_redirect = status == 302
    result.headers = {"Location": f"{PORTAL_URL_NJ}/Dashboard"}
    result.text = '<input name="__RequestVerificationToken" value="test-token">'
    result.json.return_value = data
    return result


def test_service_menu_and_electric_meters():
    chart = fixture("chart_electric.json")
    assert parse_services(chart) == [
        {"type_id": 1, "label": "Electric", "selected": True},
        {"type_id": 4, "label": "Natural Gas", "selected": False},
        {"type_id": 23, "label": "Billing", "selected": False},
    ]
    assert parse_meters(chart) == [
        {
            "meter_number": "000000000001",
            "internal_id": "1000002",
            "start_date": "08/23/2023",
            "end_date": None,
            "active": True,
            "rate_code": "(PEBRS - Residential Service (RS))",
        },
        {
            "meter_number": "000000000004",
            "internal_id": "1000005",
            "start_date": "03/19/2009",
            "end_date": "08/21/2023",
            "active": False,
            "rate_code": "(PEBRS - Residential Service (RS))",
        },
    ]


def test_gas_meters_match_billing_series():
    chart = fixture("chart_gas.json")
    meters = parse_meters(chart)
    series = fixture("chartdata_gas.json")["Data"]["series"]
    assert [meter["meter_number"] for meter in meters] == [
        series[1]["name"].split(" ")[0], series[2]["name"].split(" ")[0]
    ]
    assert [meter["internal_id"] for meter in meters] == ["1000008", "1000011"]
    assert all(
        meter["active"] and meter["start_date"] is None and meter["end_date"] is None
        and meter["rate_code"] == "(PGBRSGH - PGBRSGH)"
        for meter in meters
    )
    assert [s["type_id"] for s in parse_services(chart) if s["selected"]] == [4]


def test_gas_series_are_not_selected_as_electric_meter():
    series = fixture("chartdata_gas.json")["Data"]["series"]
    assert any(item.get("data") and item.get("name") for item in series)
    assert select_meter_series(series) == {}


@pytest.mark.parametrize("data", [None, {}, {"AjaxResults": None}, {"AjaxResults": [None]}])
def test_missing_chart_parts_return_empty(data):
    assert parse_services(data) == []
    assert parse_meters(data) == []


def test_discovery_switches_only_usage_services_and_restores_electric():
    client = PsegClient(cookie="MM_SID=abc")
    electric = fixture("chart_electric.json")
    gas = fixture("chart_gas.json")
    with patch.object(client.session, "get", side_effect=[
        response(), response(status=302), response(status=302)
    ]) as get, patch.object(client.session, "post", side_effect=[
        response(electric), response(gas)
    ]) as post:
        services = client.discover_services()

    assert [(s["type_id"], len(s["meters"])) for s in services] == [(1, 2), (4, 2)]
    assert [call.kwargs.get("params") for call in get.call_args_list] == [
        None, {"ServiceType": 4}, {"ServiceType": 1}
    ]
    assert all(call.args[0].endswith("/Dashboard/Chart") for call in post.call_args_list)
    assert post.call_args_list[0].kwargs["data"]["__RequestVerificationToken"] == "test-token"
    assert post.call_args_list[0].kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_discovered_electric_id_overrides_static_fallback():
    client = PsegClient(cookie="MM_SID=abc")
    chart = fixture("chart_electric.json")
    chart["AjaxResults"][0]["Value"] = chart["AjaxResults"][0]["Value"].replace(
        'data-value="1"', 'data-value="7"'
    )
    with patch.object(client.session, "get", return_value=response(status=302)) as get, patch.object(
        client, "_get_verification_token", return_value="token"
    ), patch.object(client.session, "post", side_effect=[
        response(chart), requests.ConnectionError("down")
    ]):
        with pytest.raises(PsegConnectionError):
            client.discover_services()
    assert client.service_type == 7
    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == [4, 7]


def test_discovery_when_gas_was_selected():
    client = PsegClient(cookie="MM_SID=abc")
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", side_effect=[
            response(fixture("chart_gas.json")), response(fixture("chart_electric.json"))
        ]
    ), patch.object(client.session, "get", return_value=response(status=302)) as get:
        services = client.discover_services()
    assert [(s["type_id"], len(s["meters"])) for s in services] == [(1, 2), (4, 2)]
    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == [1, 1]


@pytest.mark.parametrize("portal, fallback", [(PORTAL_URL_NJ, 1), (PORTAL_URL_LI, None)])
def test_no_service_menu_keeps_portal_fallback(portal, fallback):
    client = PsegClient(cookie="MM_SID=abc", portal_url=portal)
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", return_value=response({})
    ), patch.object(client.session, "get", return_value=response(status=302)) as get:
        assert client.discover_services() == []
    assert client.service_type == fallback
    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == (
        [fallback] if fallback is not None else []
    )


def test_no_electric_service_does_not_switch_unrestorable_service():
    client = PsegClient(cookie="MM_SID=abc", portal_url=PORTAL_URL_LI)
    chart = fixture("chart_electric.json")
    chart["AjaxResults"][0]["Value"] = chart["AjaxResults"][0]["Value"].replace(
        'setServiceTypeChartButton" data-value="1"',
        'otherButton" data-value="1"',
    )
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", return_value=response(chart)
    ), patch.object(client.session, "get", return_value=response(status=302)) as get:
        assert client.discover_services() == []
    get.assert_not_called()


def test_discovery_rejects_unsuccessful_switch_and_restores():
    client = PsegClient(cookie="MM_SID=abc")
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", return_value=response(fixture("chart_electric.json"))
    ) as post, patch.object(client.session, "get", side_effect=[
        response(status=404), response(status=302)
    ]) as get:
        with pytest.raises(PsegConnectionError, match="HTTP 404"):
            client.discover_services()
    assert post.call_count == 1
    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == [4, 1]


def test_discovery_rejects_chart_still_showing_other_service():
    client = PsegClient(cookie="MM_SID=abc")
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", return_value=response(fixture("chart_electric.json"))
    ), patch.object(client.session, "get", return_value=response(status=302)) as get:
        with pytest.raises(PsegConnectionError, match="did not switch"):
            client.discover_services()
    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == [4, 1]


def test_restore_http_failure_is_non_strict(caplog):
    client = PsegClient(cookie="MM_SID=abc")
    with patch.object(client, "_get_verification_token", return_value="token"), patch.object(
        client.session, "post", return_value=response({})
    ), patch.object(client.session, "get", return_value=response(status=503)):
        assert client.discover_services() == []
    assert "HTTP 503" in caplog.text


@pytest.mark.parametrize("discovery_error", [None, PsegAuthError("expired")])
@pytest.mark.parametrize("restore_error", [PsegConnectionError("down"), RuntimeError("unexpected")])
def test_restore_exception_does_not_mask_discovery(discovery_error, restore_error, caplog):
    client = PsegClient(cookie="MM_SID=abc")
    with patch.object(client, "_get_verification_token", side_effect=discovery_error or ["token"]), \
        patch.object(client.session, "post", return_value=response({})), \
        patch.object(client, "_select_service_type", side_effect=restore_error) as select:
        if discovery_error:
            with pytest.raises(PsegAuthError, match="expired"):
                client.discover_services()
        else:
            assert client.discover_services() == []
    select.assert_called_once_with()
    assert "Could not restore the electric service" in caplog.text


@pytest.mark.parametrize("error", [None, PsegConnectionError("down"), PsegAuthError("expired")])
def test_coordinator_discovers_once_under_portal_lock(error):
    async def run():
        hass = MagicMock()
        client = MagicMock()
        client.cookie = "MM_SID=abc"
        coordinator = PsegDataUpdateCoordinator(hass, client, {})
        discovered = [{"type_id": 1, "meters": []}]

        async def execute(func, *args):
            assert coordinator._portal_lock.locked()
            return func(*args)

        hass.async_add_executor_job = execute
        client.discover_services.side_effect = error or (lambda: discovered)
        client.fetch_energy_data.return_value = {}
        await coordinator._async_update_data()
        await coordinator._async_update_data()
        assert coordinator.discovered_services == ([] if error else discovered)
        assert client.discover_services.call_count == 1
        assert client.fetch_energy_data.call_count == 2

    asyncio.run(run())
