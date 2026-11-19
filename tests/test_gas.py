"""Tests for New Jersey natural gas usage."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

import tests.conftest  # noqa: F401
from custom_components.pseg import coordinator as coordinator_module
from custom_components.pseg.const import DOMAIN
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.gas import parse_gas_data
from custom_components.pseg.pseg_client import (
    PsegAuthError,
    PsegClient,
    PsegConnectionError,
)
from custom_components.pseg.sensor import (
    PsegGasConsumptionSensor,
    async_setup_entry,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    """Load an anonymized or synthetic chart response."""
    return json.loads((FIXTURES / name).read_text())


def portal_response(data=None, *, status=200):
    """Return a minimal portal response."""
    response = MagicMock()
    response.status_code = status
    response.url = "https://mysmartenergy.nj.pseg.com/Dashboard/ChartData"
    response.is_redirect = status == 302
    response.raise_for_status.return_value = None
    response.json.return_value = data
    return response


def test_parse_anonymized_zero_gas_billing_periods():
    records = parse_gas_data(fixture("chartdata_gas.json"))

    assert len(records) == 3
    assert records[0] == {
        "start": date(2026, 7, 14),
        "end": date(2026, 8, 11),
        "ccf": 0.0,
        "meter": "000000000007 (PGBRSGH)",
    }
    assert records[-1]["start"] == date(2026, 9, 11)


def test_parse_prefers_positive_meter_and_keeps_its_zero_periods():
    records = parse_gas_data(fixture("chartdata_gas_nonzero.json"))

    assert [record["meter"] for record in records] == [
        "000000000042 (PGBRSGH)",
        "000000000042 (PGBRSGH)",
    ]
    assert [record["ccf"] for record in records] == [120.5, 0.0]
    assert records[1]["end"] == date(2026, 9, 10)


def test_parse_uses_latest_meter_when_all_readings_are_zero():
    chart = fixture("chartdata_gas.json")
    chart["Data"]["series"][2]["data"] = [
        {
            "x": 1793481600000,
            "y": 0,
            "hs": {"start": "10/10/2026", "end": "11/8/2026"},
        }
    ]

    assert parse_gas_data(chart)[0]["meter"] == "000000000010 (PGBRSGH)"


def test_parse_rejects_missing_series():
    with pytest.raises(TypeError, match="missing chart series"):
        parse_gas_data({})


def test_parse_identifies_gas_by_suffix_not_meter_rate_code():
    chart = fixture("chartdata_gas_nonzero.json")
    series = chart["Data"]["series"]
    series[3]["tooltip"]["valueSuffix"] = " kWh"
    series[3]["data"][0]["y"] = 200
    series[1]["name"] = "other gas meter"
    series[1]["data"][0]["y"] = 5
    series[2]["tooltip"] = {}

    records = parse_gas_data(chart)
    assert all(record["meter"] == "other gas meter" for record in records)
    assert records[0]["ccf"] == 5


def test_fetch_gas_data_selects_gas_and_restores_electric():
    client = PsegClient(cookie="MM_SID=abc")
    chart_data = fixture("chartdata_gas_nonzero.json")
    with (
        patch.object(client, "_get_verification_token", return_value="token"),
        patch.object(client.session, "get", side_effect=[
            portal_response(), portal_response(chart_data), portal_response()
        ]) as get,
        patch.object(client.session, "post", return_value=portal_response()) as post,
    ):
        records = client.fetch_gas_data()

    assert len(records) == 2
    assert get.call_args_list[0].kwargs["params"] == {"ServiceType": 4}
    assert set(get.call_args_list[1].kwargs["params"]) == {"_"}
    assert get.call_args_list[2].kwargs["params"] == {"ServiceType": 1}
    assert post.call_args.kwargs["data"]["UsageType"] == "1"
    assert post.call_args.kwargs["data"]["Start"] <= post.call_args.kwargs["data"]["End"]


def test_fetch_gas_data_restores_electric_after_chart_error():
    client = PsegClient(cookie="MM_SID=abc")
    with (
        patch.object(client, "_get_verification_token", return_value="token"),
        patch.object(client.session, "get", return_value=portal_response()) as get,
        patch.object(client.session, "post", side_effect=requests.ConnectionError("down")),
        pytest.raises(PsegConnectionError),
    ):
        client.fetch_gas_data()

    assert [call.kwargs["params"]["ServiceType"] for call in get.call_args_list] == [4, 1]


def test_fetch_gas_data_raises_auth_error_for_portal_redirect():
    client = PsegClient(cookie="MM_SID=abc")
    redirect = portal_response({"AjaxResults": [{"Action": "Redirect"}]})
    with (
        patch.object(client, "_get_verification_token", return_value="token"),
        patch.object(
            client.session, "get",
            side_effect=[portal_response(), redirect, portal_response()],
        ) as get,
        patch.object(client.session, "post", return_value=portal_response()),
        pytest.raises(PsegAuthError),
    ):
        client.fetch_gas_data()

    assert get.call_args_list[-1].kwargs["params"] == {"ServiceType": 1}


def make_coordinator(services):
    """Create a coordinator whose executor invokes portal functions inline."""
    hass = MagicMock()
    client = MagicMock()
    client.cookie = "MM_SID=abc"
    client.discover_services.return_value = services
    client.fetch_energy_data.return_value = {"hourly_records": []}

    async def execute(func, *args):
        assert coordinator._portal_lock.locked()
        return func(*args)

    hass.async_add_executor_job = execute
    coordinator = PsegDataUpdateCoordinator(hass, client, {})
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    coordinator.async_record_gas_statistics = AsyncMock()
    coordinator._dismiss_auth_issues = MagicMock()
    coordinator._store_renewed_cookie = MagicMock()
    return coordinator


@pytest.mark.parametrize("discovery_error", [PsegAuthError("expired"), PsegConnectionError("offline")])
def test_coordinator_retries_failed_discovery_and_fetches_gas(discovery_error):
    async def run():
        coordinator = make_coordinator([])
        coordinator.client.discover_services.side_effect = [
            discovery_error,
            [{"type_id": 4, "label": "Natural Gas"}],
        ]
        gas = [{"start": date(2026, 8, 1), "end": date(2026, 8, 31), "ccf": 42.0, "meter": "fake"}]
        coordinator.client.fetch_gas_data.return_value = gas

        first = await coordinator._async_update_data()
        assert "gas" not in first

        second = await coordinator._async_update_data()
        assert second["gas"] == gas
        assert coordinator.client.discover_services.call_count == 2
        coordinator.client.fetch_gas_data.assert_called_once()

    asyncio.run(run())


def test_coordinator_does_not_repeat_successful_discovery():
    async def run():
        coordinator = make_coordinator([{"type_id": 1, "label": "Electric"}])

        await coordinator._async_update_data()
        await coordinator._async_update_data()

        assert coordinator.client.discover_services.call_count == 1

    asyncio.run(run())


def test_coordinator_fetches_gas_once_a_day_and_keeps_last_data_on_failure():
    async def run():
        coordinator = make_coordinator([
            {"type_id": 1, "label": "Electric"},
            {"type_id": 4, "label": "Natural Gas"},
        ])
        gas = [{"start": date(2026, 8, 1), "end": date(2026, 8, 31), "ccf": 42.0, "meter": "fake"}]
        coordinator.client.fetch_gas_data.return_value = gas
        first = await coordinator._async_update_data()
        assert first["gas"] == gas
        second = await coordinator._async_update_data()
        assert second["gas"] == gas
        assert coordinator.client.fetch_gas_data.call_count == 1
        coordinator.client.fetch_gas_data.side_effect = PsegConnectionError("offline")
        coordinator._last_gas_attempt -= timedelta(hours=25)
        third = await coordinator._async_update_data()
        assert third["gas"] == gas
        assert third["hourly_records"] == []
        assert coordinator.client.fetch_gas_data.call_count == 2
        fourth = await coordinator._async_update_data()
        assert fourth["gas"] == gas
        assert coordinator.client.fetch_gas_data.call_count == 2

    asyncio.run(run())


def test_coordinator_does_not_fetch_gas_without_discovery():
    async def run():
        coordinator = make_coordinator([{"type_id": 1, "label": "Electric"}])
        data = await coordinator._async_update_data()
        assert "gas" not in data
        coordinator.client.fetch_gas_data.assert_not_called()

    asyncio.run(run())


def test_coordinator_gas_failure_does_not_fail_electric_update(caplog):
    async def run():
        coordinator = make_coordinator([
            {"type_id": 4, "label": "Natural Gas"},
        ])
        coordinator.client.fetch_gas_data.side_effect = PsegConnectionError("bad chart")
        data = await coordinator._async_update_data()
        assert data["hourly_records"] == []
        assert "gas" not in data

    asyncio.run(run())
    assert "Could not fetch PSE&G gas usage" in caplog.text


def test_coordinator_gas_auth_failure_keeps_data_and_retries_without_reauth(caplog):
    async def run():
        coordinator = make_coordinator([{"type_id": 4, "label": "Gas"}])
        coordinator.client.fetch_energy_data.side_effect = lambda *_: {"hourly_records": []}
        coordinator.async_reauthenticate = AsyncMock()
        old_gas = [{"ccf": 42.0}]
        new_gas = [{"ccf": 43.0}]
        coordinator.client.fetch_gas_data.side_effect = [
            old_gas, PsegAuthError("gas auth failure"), new_gas
        ]
        assert (await coordinator._async_update_data())["gas"] == old_gas
        coordinator._last_gas_attempt -= timedelta(hours=25)
        previous_attempt = coordinator._last_gas_attempt

        data = await coordinator._async_update_data()
        assert data["hourly_records"] == []
        assert data["gas"] == old_gas
        assert coordinator._last_gas_attempt == previous_attempt
        assert not coordinator.auth_required
        coordinator.async_reauthenticate.assert_not_awaited()

        assert (await coordinator._async_update_data())["gas"] == new_gas
        assert coordinator.client.fetch_gas_data.call_count == 3
        coordinator.async_reauthenticate.assert_not_awaited()

    asyncio.run(run())
    assert "Could not fetch PSE&G gas usage: gas auth failure" in caplog.text


def import_gas_statistics(records, existing):
    """Run gas import with a controlled public recorder API result."""
    coordinator = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})
    recorder = MagicMock()

    async def execute(func, *args):
        return func(*args)

    recorder.async_add_executor_job = execute
    rows = []
    metadata = {}
    with patch.object(coordinator_module, "get_instance", return_value=recorder), patch.object(
        coordinator_module, "get_last_statistics", return_value=existing
    ) as get_last, patch.object(
        coordinator_module, "StatisticData", side_effect=lambda **kwargs: kwargs
    ), patch.object(
        coordinator_module, "async_add_external_statistics",
        side_effect=lambda hass, meta, stats: (metadata.update(meta), rows.extend(stats)),
    ):
        asyncio.run(coordinator.async_record_gas_statistics(records))
        get_last.assert_called_once_with(
            coordinator.hass, 1000, "pseg:gas_consumption", True, {"sum"}
        )
    return metadata, rows


def test_gas_statistics_use_local_midnight_and_previous_sum_baseline():
    records = [
        {"start": date(2026, 12, 1), "ccf": 10.0},
        {"start": date(2027, 1, 1), "ccf": 20.0},
    ]
    existing = {
        "pseg:gas_consumption": [{
            "start": datetime(2026, 11, 1, 4, tzinfo=UTC),
            "sum": 500.0,
        }]
    }
    metadata, rows = import_gas_statistics(records, existing)

    assert metadata == {
        "has_mean": False,
        "has_sum": True,
        "mean_type": 0,
        "unit_class": "volume",
        "name": "PSE&G Gas Consumption",
        "source": "pseg",
        "statistic_id": "pseg:gas_consumption",
        "unit_of_measurement": "CCF",
    }
    assert [row["start"] for row in rows] == [
        datetime(2026, 12, 1, 5, tzinfo=UTC),
        datetime(2027, 1, 1, 5, tzinfo=UTC),
    ]
    assert [row["sum"] for row in rows] == [510.0, 530.0]
    assert [row["state"] for row in rows] == [10.0, 20.0]


def test_gas_statistics_store_summer_billing_period_at_edt_midnight():
    _, rows = import_gas_statistics(
        [{"start": date(2026, 7, 14), "ccf": 10.0}],
        {"pseg:gas_consumption": []},
    )

    assert rows[0]["start"] == datetime(2026, 7, 14, 4, tzinfo=UTC)


def test_gas_statistics_reimport_and_correction_rechain_later_periods():
    records = [
        {"start": date(2026, 12, 1), "ccf": 10.0},
        {"start": date(2027, 1, 1), "ccf": 20.0},
    ]
    existing = {"pseg:gas_consumption": [{"start": datetime(2026, 11, 1, tzinfo=UTC), "sum": 500}]}

    _, first = import_gas_statistics(records, existing)
    _, repeated = import_gas_statistics(records, existing)
    corrected = [dict(records[0], ccf=12.0), records[1]]
    _, corrected_rows = import_gas_statistics(corrected, existing)

    assert repeated == first
    assert [row["sum"] for row in corrected_rows] == [512.0, 532.0]


def test_gas_statistics_start_at_zero_without_previous_rows():
    _, rows = import_gas_statistics(
        [{"start": date(2026, 12, 1), "ccf": 10.0}],
        {"pseg:gas_consumption": []},
    )
    assert rows[0]["sum"] == 10.0


def test_gas_sensor_value_and_period_attributes():
    coordinator = MagicMock()
    coordinator.data = {
        "gas": [{
            "start": date(2026, 8, 1),
            "end": date(2026, 8, 31),
            "ccf": 42.5,
            "meter": "fake meter",
        }]
    }
    entry = MagicMock()
    entry.entry_id = "entry"
    sensor = PsegGasConsumptionSensor(coordinator, entry)

    assert sensor.native_value == 42.5
    assert sensor.extra_state_attributes == {
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "meter": "fake meter",
    }
    assert sensor._attr_state_class is None


@pytest.mark.parametrize(
    ("services", "discovered"),
    [([{"type_id": 4, "label": "Gas"}], True), ([{"type_id": 4}], True),
     ([{"type_id": 1, "label": "Natural Gas"}], False), ([], False)],
)
def test_gas_sensor_is_added_only_when_gas_was_discovered(services, discovered):
    async def run():
        coordinator = make_coordinator([])
        coordinator.discovered_services = services
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry": coordinator}}
        entry = MagicMock()
        entry.entry_id = "entry"
        entities = []
        await async_setup_entry(hass, entry, entities.extend)
        return entities

    entities = asyncio.run(run())
    assert any(isinstance(entity, PsegGasConsumptionSensor) for entity in entities) is discovered


def test_late_gas_sensor_is_added_once_by_coordinator_listener():
    coordinator = make_coordinator([])
    listener = None
    remove_listener = MagicMock()

    def add_listener(callback):
        nonlocal listener
        listener = callback
        return remove_listener

    coordinator.async_add_listener.side_effect = add_listener
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": coordinator}}
    entry = MagicMock()
    entry.entry_id = "entry"
    entities = []

    asyncio.run(async_setup_entry(hass, entry, entities.extend))
    assert not any(isinstance(entity, PsegGasConsumptionSensor) for entity in entities)
    entry.async_on_unload.assert_called_once_with(remove_listener)

    coordinator.discovered_services = [{"type_id": 4, "label": "Gas"}]
    listener()
    listener()

    gas_entities = [entity for entity in entities if isinstance(entity, PsegGasConsumptionSensor)]
    assert len(gas_entities) == 1
