"""Tests for the Energy source upgrade notice."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.pseg as integration


@pytest.mark.parametrize("legacy_id", [
    None,
    "pseg:electricity_consumption",
    "pseg:electricity_cost",
])
def test_energy_upgrade_notice_checks_legacy_ids_after_start(monkeypatch, legacy_id):
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=[])
    after_start = MagicMock()
    lookup = AsyncMock(return_value=[{"statistic_id": legacy_id}] if legacy_id else [])
    registry = MagicMock()
    monkeypatch.setattr(integration, "async_at_started", after_start)
    monkeypatch.setattr(integration, "async_list_statistic_ids", lookup)
    monkeypatch.setattr(integration, "ir", registry)

    assert asyncio.run(integration.async_setup(hass, {}))
    lookup.assert_not_awaited()
    after_start.assert_called_once()
    startup_callback = after_start.call_args.args[1]
    asyncio.run(startup_callback(hass))

    lookup.assert_awaited_once_with(
        hass, {"pseg:electricity_consumption", "pseg:electricity_cost"}
    )
    if legacy_id:
        create_issue = registry.async_create_issue
        create_issue.assert_called_once()
        assert create_issue.call_args.args == (hass, "pseg", "energy_statistics_upgrade")
        assert create_issue.call_args.kwargs["translation_key"] == "energy_statistics_upgrade"
        assert create_issue.call_args.kwargs["is_fixable"] is False
    else:
        registry.async_create_issue.assert_not_called()


def test_legacy_lookup_failure_does_not_raise_a_repair(monkeypatch, caplog):
    lookup = AsyncMock(side_effect=RuntimeError("Recorder unavailable"))
    registry = MagicMock()
    monkeypatch.setattr(integration, "async_list_statistic_ids", lookup)
    monkeypatch.setattr(integration, "ir", registry)

    asyncio.run(integration._async_check_legacy_statistics(MagicMock()))

    registry.async_create_issue.assert_not_called()
    assert "Could not check for legacy PSE&G statistics" in caplog.text
