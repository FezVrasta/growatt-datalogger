"""Telling the user when an inverter could not be identified, and letting them fix it.

The case this exists for is issue #1: an SPH5000 behind a ShineWiFi-S reports the
storage layout on a single 0-134 group. Nothing in the record says so, and the values it
produces under Protocol II are not empty -- they are enormous. So the failure mode is a
dashboard full of confident nonsense, which is the one thing a user cannot debug.
"""

from __future__ import annotations

from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_group
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import (
    CONF_PROFILE_OVERRIDES,
    DOMAIN,
    ISSUE_UNCONFIDENT_PROFILE,
    PROFILE_AUTO,
)

from .conftest import settle

#: What the SPH5000 in issue #1 sends: one 0-based group of 135 registers. Under
#: Protocol II register 4 is PV1 current; here it is the low word of PV power.
SPH_GROUP = [build_group(0, [0] * 135)]

#: A well-behaved Protocol II device, whose 0-block ends where the documentation says.
PROTOCOL_II_GROUP = [build_group(0, [0] * 125)]


def _issue(hass: HomeAssistant, serial: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, ISSUE_UNCONFIDENT_PROFILE.format(serial=serial)
    )


async def test_unidentified_layout_raises_a_repair(
    hass: HomeAssistant, device: FakeDatalogger
) -> None:
    await device.send_data(groups=SPH_GROUP)
    await settle(hass)

    issue = _issue(hass, device.inverter_serial)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["serial"] == device.inverter_serial


async def test_recognised_layout_raises_nothing(
    hass: HomeAssistant, device: FakeDatalogger
) -> None:
    """A device we can identify must stay silent. A repair nobody can act on is noise."""
    await device.send_data(groups=PROTOCOL_II_GROUP)
    await settle(hass)

    assert _issue(hass, device.inverter_serial) is None


async def test_extended_protocol_ii_raises_nothing(
    hass: HomeAssistant, device: FakeDatalogger
) -> None:
    """MIN and MAX3 report registers past 124 in a further group.

    They are ordinary Protocol II devices and decode correctly, so demoting them would
    put a warning in front of thousands of users whose data is fine.
    """
    await device.send_data(groups=[build_group(0, [0] * 125), build_group(125, [0] * 125)])
    await settle(hass)

    assert _issue(hass, device.inverter_serial) is None


async def test_pinning_a_profile_clears_the_repair(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The whole point of the repair: it has to go away once acted on."""
    await device.send_data(groups=SPH_GROUP)
    await settle(hass)
    assert _issue(hass, device.inverter_serial) is not None

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "profiles"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {device.inverter_serial: "offgrid"}
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_PROFILE_OVERRIDES] == {device.inverter_serial: "offgrid"}

    # Saving options reloads the entry, which drops the socket and re-binds a new port.
    # The datalogger redials in reality; here it has to be redialled explicitly.
    await settle(hass)
    reconnected = FakeDatalogger(
        datalogger_serial=device.datalogger_serial, inverter_serial=device.inverter_serial
    )
    await reconnected.connect("127.0.0.1", setup_integration.runtime_data.port)
    try:
        await reconnected.send_data(groups=SPH_GROUP)
        await settle(hass)
    finally:
        await reconnected.close()

    assert _issue(hass, device.inverter_serial) is None


async def test_auto_is_stored_as_no_pin(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """ "auto" must erase the override rather than be written as a profile key.

    resolve_profile takes a key or None; a stored "auto" would match no profile and fall
    through to inference, which works by accident and reads as a bug forever after.
    """
    await device.send_data(groups=SPH_GROUP)
    await settle(hass)

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "profiles"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {device.inverter_serial: PROFILE_AUTO}
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_PROFILE_OVERRIDES] == {}


async def test_profile_step_is_hidden_until_an_inverter_appears(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing has connected yet, so there is nothing to pin -- skip straight to settings."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"


async def test_settings_step_keeps_existing_profile_pins(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The two steps write the same options dict; neither may clobber the other."""
    await device.send_data(groups=SPH_GROUP)
    await settle(hass)

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "profiles"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {device.inverter_serial: "offgrid"}
    )
    await settle(hass)

    # Saving the options reloaded the entry, so the inverter has to announce itself
    # again before the picker has anything to offer.
    await device.send_data(groups=SPH_GROUP)
    await settle(hass)

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "include_unknown": True,
            "buffered_policy": "event",
            "relay_enabled": False,
            "relay_host": "server.growatt.com",
            "relay_port": 5279,
        },
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_PROFILE_OVERRIDES] == {device.inverter_serial: "offgrid"}
    assert setup_integration.options["include_unknown"] is True
