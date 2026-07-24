"""Support for Verisure alarm control panels."""

import asyncio
from typing import override

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALARM_STATE_TO_HA, CONF_GIID, DOMAIN, LOGGER
from .coordinator import VerisureConfigEntry, VerisureDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VerisureConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Verisure alarm control panel from a config entry."""
    async_add_entities([VerisureAlarm(coordinator=entry.runtime_data)])


class VerisureAlarm(
    CoordinatorEntity[VerisureDataUpdateCoordinator], AlarmControlPanelEntity
):
    """Representation of a Verisure alarm status."""

    _attr_code_format = CodeFormat.NUMBER
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    )

    def __init__(self, coordinator: VerisureDataUpdateCoordinator) -> None:
        """Initialize the Verisure alarm control panel."""
        super().__init__(coordinator)
        # True while armed away with a device bypassed; overrides the state
        # normally derived from the coordinator's polled arm status, since
        # Verisure itself only ever reports DISARMED/ARMED_HOME/ARMED_AWAY and
        # has no bypass state of its own to read back.
        self._bypass_active = False

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        return DeviceInfo(
            name="Verisure Alarm",
            manufacturer="Verisure",
            model="VBox",
            identifiers={(DOMAIN, self.coordinator.config_entry.data[CONF_GIID])},
            configuration_url="https://mypages.verisure.com",
        )

    @property
    @override
    def unique_id(self) -> str:
        """Return the unique ID for this entity."""
        return self.coordinator.config_entry.data[CONF_GIID]

    async def _async_set_arm_state(
        self,
        state: str,
        command_data: dict[str, str | dict[str, str]],
        failure_translation_key: str = "arm_state_failed",
    ) -> None:
        """Send set arm state command."""
        arm_state = await self.hass.async_add_executor_job(
            self.coordinator.verisure.request, command_data
        )
        LOGGER.debug("Verisure set arm state %s", state)
        if arm_state is None or "data" not in arm_state:
            await self.coordinator.async_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="arm_state_failed",
            )
        result = None
        attempts = 0
        while result is None:
            if attempts == 30:
                break
            if attempts > 1:
                await asyncio.sleep(0.5)
            attempts += 1
            transaction = await self.hass.async_add_executor_job(
                self.coordinator.verisure.request,
                self.coordinator.verisure.poll_arm_state(
                    list(arm_state["data"].values())[0], state
                ),
            )
            if transaction is None:
                continue
            result = (
                transaction.get("data", {})
                .get("installation", {})
                .get("armStateChangePollResult", {})
                .get("result")
            )
            LOGGER.debug("Result is %s", result)
        if result == "OK":
            self._attr_alarm_state = ALARM_STATE_TO_HA.get(state)
            self.async_write_ha_state()
            return
        # The poll never confirmed the change. Refresh so the entity reflects
        # the real (unchanged) state instead of staying stuck on ARMING, and
        # raise so the user is told the attempt failed rather than it failing
        # silently.
        await self.coordinator.async_refresh()
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=failure_translation_key,
        )

    @override
    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        self._attr_alarm_state = AlarmControlPanelState.DISARMING
        self.async_write_ha_state()
        await self._async_set_arm_state(
            "DISARMED", self.coordinator.verisure.disarm(code)
        )

    @override
    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command.

        Always forces: arm home already tolerates devices such as interior
        motion sensors being inactive by design, so forcing here is
        consistent with what the mode already means.
        """
        self._attr_alarm_state = AlarmControlPanelState.ARMING
        self.async_write_ha_state()
        self._bypass_active = False
        await self._async_set_arm_state(
            "ARMED_HOME", self.coordinator.verisure.arm_home(code, force_arm=True)
        )

    @override
    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command.

        Does not force: arm away is meant to fully secure the home, so a
        device that is out of place should block arming rather than being
        silently bypassed. Use the bypass action to arm anyway.
        """
        self._attr_alarm_state = AlarmControlPanelState.ARMING
        self.async_write_ha_state()
        self._bypass_active = False
        await self._async_set_arm_state(
            "ARMED_AWAY",
            self.coordinator.verisure.arm_away(code),
            failure_translation_key="arm_away_requires_bypass",
        )

    @override
    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Arm away, forcing past any device that is out of place."""
        self._attr_alarm_state = AlarmControlPanelState.ARMING
        self.async_write_ha_state()
        await self._async_set_arm_state(
            "ARMED_AWAY", self.coordinator.verisure.arm_away(code, force_arm=True)
        )
        self._bypass_active = True
        self._attr_alarm_state = AlarmControlPanelState.ARMED_CUSTOM_BYPASS
        self.async_write_ha_state()

    def _update_alarm_attributes(self) -> None:
        """Update alarm state and changed by from coordinator data."""
        status_type = self.coordinator.data["alarm"]["statusType"]
        if self._bypass_active and status_type == "ARMED_AWAY":
            # Verisure has no bypass state to read back; keep reporting the
            # bypass state for as long as it's still armed away.
            self._attr_alarm_state = AlarmControlPanelState.ARMED_CUSTOM_BYPASS
        else:
            self._bypass_active = False
            self._attr_alarm_state = ALARM_STATE_TO_HA.get(status_type)
        self._attr_changed_by = self.coordinator.data["alarm"].get("name")

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_alarm_attributes()
        super()._handle_coordinator_update()

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self._update_alarm_attributes()
