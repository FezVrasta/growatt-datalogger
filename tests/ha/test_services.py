"""Register read and write services, driven end to end against a fake device."""

from __future__ import annotations

import pytest
import voluptuous as vol
from growatt_protocol.testing import FakeDatalogger, FakeInverter, request_register, request_values
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN

from .conftest import INVERTER, SERIAL, device_id, settle


async def _announced(
    hass: HomeAssistant, device: FakeDatalogger, entry: MockConfigEntry, key: str | None = None
) -> str:
    """Announce, then return the Home Assistant device id for the logger or inverter."""
    await device.send_announce()
    await settle(hass)
    return device_id(hass, entry, key or f"logger:{SERIAL}")


async def test_read_register_returns_the_value(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    inverter.values[45] = 1234
    target = await _announced(hass, device, setup_integration)

    result = await hass.services.async_call(
        DOMAIN,
        "read_register",
        {"device_id": target, "register": 45},
        blocking=True,
        return_response=True,
    )

    assert result["register"] == 45
    assert result["value"] == 1234


async def test_write_register_needs_confirmation_outside_the_allowlist(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """A deliberate speed bump: a wrong write can misconfigure the inverter."""
    target = await _announced(hass, device, setup_integration)

    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN,
            "write_register",
            {"device_id": target, "register": 1044, "value": 1},
            blocking=True,
        )


async def test_write_register_succeeds_when_the_device_accepts(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    target = await _announced(hass, device, setup_integration)

    await hass.services.async_call(
        DOMAIN, "write_register", {"device_id": target, "register": 3, "value": 80}, blocking=True
    )

    assert inverter.values[3] == 80


async def test_a_raw_write_is_never_widened_to_the_whole_slot(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """The entities treat a charge window as one setting. This service does not.

    Someone reaching for the raw escape hatch on register 1080 means that register and
    no other, so widening the write would be the service doing something unasked.
    """
    target = await _announced(hass, device, setup_integration)

    await hass.services.async_call(
        DOMAIN,
        "write_register",
        {"device_id": target, "register": 1080, "value": 0x0600, "confirm": True},
        blocking=True,
    )

    written = await inverter.wait_for(0x06, 1080)
    assert written is not None
    assert not [r for r in inverter.requests if r.function == 0x10]


async def test_a_rejected_write_says_whether_the_register_exists(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """The raw service is where an unfamiliar register is most likely to be refused, so
    it gets the same diagnosis the entities do rather than a bare status byte."""
    inverter.result = 2
    inverter.missing = {3}
    target = await _announced(hass, device, setup_integration)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "write_register",
            {"device_id": target, "register": 3, "value": 80},
            blocking=True,
        )

    assert "no such register" in str(raised.value)
    assert "does not have it" in str(raised.value)


async def test_sync_time_sets_the_clock(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    target = await _announced(hass, device, setup_integration)

    await hass.services.async_call(DOMAIN, "sync_time", {"device_id": target}, blocking=True)

    request = await inverter.wait_for(0x18)
    # register 0x1f, then a 19-byte ASCII timestamp
    assert request_register(request) == 0x1F
    assert int.from_bytes(request.body[32:34], "big") == 19


async def test_a_command_to_a_disconnected_device_fails_fast(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """Better an immediate error than queueing for a device that may never return."""
    target = await _announced(hass, device, setup_integration)
    await device.close()
    await settle(hass, times=5)

    with pytest.raises(HomeAssistantError, match="not currently connected"):
        await hass.services.async_call(
            DOMAIN,
            "read_register",
            {"device_id": target, "register": 3},
            blocking=True,
            return_response=True,
        )


async def test_an_inverter_device_resolves_through_its_datalogger(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """Commands always travel over the datalogger's connection."""
    inverter.values[7] = 42
    target = await _announced(hass, device, setup_integration, f"inverter:{INVERTER}")

    result = await hass.services.async_call(
        DOMAIN,
        "read_register",
        {"device_id": target, "register": 7},
        blocking=True,
        return_response=True,
    )

    assert result["value"] == 42


async def test_an_unknown_device_is_rejected(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "read_register",
            {"device_id": "does-not-exist", "register": 3},
            blocking=True,
            return_response=True,
        )


async def test_write_registers_sends_one_multi_register_command(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """A run must go out as a single 0x10, not as several 0x06 writes.

    Writing a multi-register value one register at a time leaves a window in which the
    inverter holds a mix of the old and new values.
    """
    target = await _announced(hass, device, setup_integration)

    result = await hass.services.async_call(
        DOMAIN,
        "write_registers",
        {
            "device_id": target,
            "start_register": 1100,
            "values": [1560, 1740, 1],
            "confirm": True,
        },
        blocking=True,
        return_response=True,
    )

    request = await inverter.wait_for(0x10)
    assert request_register(request) == 1100
    assert int.from_bytes(request.body[32:34], "big") == 1102  # start + len - 1
    assert request_values(request) == (1560, 1740, 1)

    assert result["start_register"] == 1100
    assert result["count"] == 3


async def test_write_registers_always_requires_confirmation(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """There is no allowlist here: a range is not one documented setting."""
    target = await _announced(hass, device, setup_integration)

    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN,
            "write_registers",
            {"device_id": target, "start_register": 3, "values": [80]},
            blocking=True,
        )


async def test_write_registers_rejects_an_empty_list(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    target = await _announced(hass, device, setup_integration)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "write_registers",
            {"device_id": target, "start_register": 3, "values": [], "confirm": True},
            blocking=True,
        )
