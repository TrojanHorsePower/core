"""Tests for the Verisure alarm control panel."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.verisure.const import DEFAULT_SCAN_INTERVAL
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_ALARM_ARM_AWAY,
    SERVICE_ALARM_ARM_CUSTOM_BYPASS,
    SERVICE_ALARM_ARM_HOME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import OVERVIEW

from tests.common import MockConfigEntry, async_fire_time_changed

ALARM_ENTITY_ID = "alarm_control_panel.verisure_alarm"
ARM_TRANSACTION = {"data": {"armStateChangeTransactionId": "txn"}}
POLL_OK = {"data": {"installation": {"armStateChangePollResult": {"result": "OK"}}}}
POLL_FAILED = {
    "data": {"installation": {"armStateChangePollResult": {"result": "FAILED"}}}
}


def _overview(status_type: str) -> list:
    """Build a minimal overview payload reporting the given arm status."""
    return [
        {
            "data": {
                "installation": {
                    "armState": {"status": status_type, "statusType": status_type},
                }
            }
        }
    ]


async def _async_setup(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Set up the Verisure integration with the alarm control panel platform."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.verisure.PLATFORMS", [Platform.ALARM_CONTROL_PANEL]
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_arm_home_always_forces(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
) -> None:
    """Arming home always passes force_arm=True.

    Arm home already tolerates some sensors being inactive by design, so
    forcing here is consistent with what the mode already means.
    """
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [ARM_TRANSACTION, POLL_OK]

    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_HOME,
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )

    mock_verisure.arm_home.assert_called_once_with("1234", force_arm=True)
    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_home"


async def test_arm_away_does_not_force(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
) -> None:
    """Arming away does not force, so an out-of-place device blocks it."""
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [ARM_TRANSACTION, POLL_OK]

    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_AWAY,
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )

    mock_verisure.arm_away.assert_called_once_with("1234")
    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_away"


async def test_arm_away_failure_raises_and_refreshes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
) -> None:
    """A failed arm away raises instead of leaving the panel stuck on arming."""
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [ARM_TRANSACTION, POLL_FAILED, OVERVIEW]

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            ALARM_DOMAIN,
            SERVICE_ALARM_ARM_AWAY,
            {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
            blocking=True,
        )

    assert exc_info.value.translation_key == "arm_away_requires_bypass"
    assert hass.states.get(ALARM_ENTITY_ID).state == "disarmed"


async def test_arm_custom_bypass_forces_and_reports_bypass_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
) -> None:
    """The bypass action forces the arm away and reports armed_custom_bypass."""
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [ARM_TRANSACTION, POLL_OK]

    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_CUSTOM_BYPASS,
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )

    mock_verisure.arm_away.assert_called_once_with("1234", force_arm=True)
    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_custom_bypass"


async def test_bypass_state_persists_while_still_armed_away(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """armed_custom_bypass survives a poll that still reports armed away.

    Verisure has no bypass state of its own to read back; the coordinator
    keeps reporting ARMED_AWAY, and the entity must not let that overwrite
    the bypass state while still armed.
    """
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [
        ARM_TRANSACTION,
        POLL_OK,
        _overview("ARMED_AWAY"),
    ]

    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_CUSTOM_BYPASS,
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )
    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_custom_bypass"

    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_custom_bypass"


async def test_bypass_state_clears_once_disarmed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """armed_custom_bypass clears once Verisure reports disarmed."""
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [
        ARM_TRANSACTION,
        POLL_OK,
        _overview("DISARMED"),
    ]

    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_CUSTOM_BYPASS,
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )
    assert hass.states.get(ALARM_ENTITY_ID).state == "armed_custom_bypass"

    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(ALARM_ENTITY_ID).state == "disarmed"


async def test_disarm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_verisure: MagicMock,
) -> None:
    """Disarming is unaffected by the force-arm changes."""
    await _async_setup(hass, mock_config_entry)
    mock_verisure.request.side_effect = [ARM_TRANSACTION, POLL_OK]

    await hass.services.async_call(
        ALARM_DOMAIN,
        "alarm_disarm",
        {ATTR_ENTITY_ID: ALARM_ENTITY_ID, "code": "1234"},
        blocking=True,
    )

    mock_verisure.disarm.assert_called_once_with("1234")
    assert hass.states.get(ALARM_ENTITY_ID).state == "disarmed"
