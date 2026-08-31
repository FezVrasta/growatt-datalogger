"""Register read and write services, driven end to end against a fake device."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN
from tests.fakes.datalogger import FakeDatalogger
from tests.fakes.frames import build_frame

SERIAL = "GPG0EXAMP1"


async def _settle(hass: HomeAssistant, times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def _announced_device_id(
    hass: HomeAssistant, device: FakeDatalogger, kind: str = "logger"
) -> str:
    """Announce, then return the Home Assistant device id for the datalogger."""
    await device.send_announce()
    await device.read_frame()
    await _settle(hass)

    registry = dr.async_get(hass)
    key = f"{kind}:{SERIAL}" if kind == "logger" else f"{kind}:SML0EXAMP2"
    entry = registry.async_get_device({(DOMAIN, key)})
    assert entry is not None
    return entry.id


async def _answer(
    device: FakeDatalogger, function: int, register: int, tail: bytes, *, tries: int = 12
) -> None:
    """Play the device's side: wait for the request we care about, then reply.

    Write entities read their registers back in the background when they are added, so
    the frame arriving next is not necessarily the one the test asked for. Answer the
    others with a benign value and keep looking.
    """
    for _ in range(tries):
        request = await device.read_frame(timeout=2.0)
        got = int.from_bytes(request.body[30:32], "big")

        if request.function == function and got == register:
            body = SERIAL.encode().ljust(30, b"\x00") + register.to_bytes(2, "big") + tail
            await device.send_raw(
                build_frame(body, protocol=6, function=function, sequence=request.sequence)
            )
            return

        # Some other entity's read-back. Answer it so it does not block the lock.
        body = SERIAL.encode().ljust(30, b"\x00") + got.to_bytes(2, "big") + (0).to_bytes(2, "big")
        await device.send_raw(
            build_frame(body, protocol=6, function=request.function, sequence=request.sequence)
        )
    raise AssertionError(f"never saw a {function:#04x} request for register {register}")


async def _await_request(device: FakeDatalogger, function: int, *, tries: int = 12) -> object:
    """Return the first request with ``function``, answering anything else on the way."""
    for _ in range(tries):
        request = await device.read_frame(timeout=2.0)
        if request.function == function:
            return request
        register = int.from_bytes(request.body[30:32], "big")
        body = (
            SERIAL.encode().ljust(30, b"\x00")
            + register.to_bytes(2, "big")
            + (0).to_bytes(2, "big")
        )
        await device.send_raw(
            build_frame(body, protocol=6, function=request.function, sequence=request.sequence)
        )
    raise AssertionError(f"never saw a {function:#04x} request")


async def test_read_register_returns_the_value(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    device_id = await _announced_device_id(hass, device)

    # Register 45 deliberately: a write entity also reads register 3, and two identical
    # requests on the wire would be indistinguishable to the test's responder.
    call = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "read_register",
            {"device_id": device_id, "register": 45},
            blocking=True,
            return_response=True,
        )
    )
    await asyncio.sleep(0.05)
    await _answer(device, 0x05, 45, (1234).to_bytes(2, "big"))

    result = await asyncio.wait_for(call, 5)
    assert result["register"] == 45
    assert result["value"] == 1234


async def test_write_register_needs_confirmation_outside_the_allowlist(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """A deliberate speed bump: a wrong write can misconfigure the inverter."""
    device_id = await _announced_device_id(hass, device)

    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN,
            "write_register",
            {"device_id": device_id, "register": 1044, "value": 1},
            blocking=True,
        )


async def test_write_register_succeeds_when_the_device_accepts(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    device_id = await _announced_device_id(hass, device)

    call = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "write_register",
            {"device_id": device_id, "register": 3, "value": 80},
            blocking=True,
        )
    )
    await asyncio.sleep(0.05)
    await _answer(device, 0x06, 3, b"\x00" + (80).to_bytes(2, "big"))

    await asyncio.wait_for(call, 5)


async def test_a_rejected_write_raises(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    device_id = await _announced_device_id(hass, device)

    call = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "write_register",
            {"device_id": device_id, "register": 3, "value": 80},
            blocking=True,
        )
    )
    await asyncio.sleep(0.05)
    await _answer(device, 0x06, 3, b"\x02" + (0).to_bytes(2, "big"))

    with pytest.raises(HomeAssistantError, match="rejected"):
        await asyncio.wait_for(call, 5)


async def test_sync_time_sets_the_clock(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    device_id = await _announced_device_id(hass, device)

    call = asyncio.create_task(
        hass.services.async_call(DOMAIN, "sync_time", {"device_id": device_id}, blocking=True)
    )
    await asyncio.sleep(0.05)

    request = await _await_request(device, 0x18)
    # register 0x1f, then a 19-byte ASCII timestamp
    assert request.body[30:32] == (0x1F).to_bytes(2, "big")
    assert request.body[32:34] == (19).to_bytes(2, "big")

    body = SERIAL.encode().ljust(30, b"\x00") + (0x1F).to_bytes(2, "big") + b"\x00"
    await device.send_raw(build_frame(body, protocol=6, function=0x18, sequence=request.sequence))
    await asyncio.wait_for(call, 5)


async def test_a_command_to_a_disconnected_device_fails_fast(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Better an immediate error than queueing for a device that may never return."""
    device_id = await _announced_device_id(hass, device)
    await device.close()
    await _settle(hass, times=5)

    with pytest.raises(HomeAssistantError, match="not currently connected"):
        await hass.services.async_call(
            DOMAIN,
            "read_register",
            {"device_id": device_id, "register": 3},
            blocking=True,
            return_response=True,
        )


async def test_an_inverter_device_resolves_through_its_datalogger(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Commands always travel over the datalogger's connection."""
    # Announce once; both devices come from that single record. Announcing twice would
    # leave an extra acknowledgement on the wire for the responder to trip over.
    await _announced_device_id(hass, device)

    registry = dr.async_get(hass)
    inverter = registry.async_get_device({(DOMAIN, "inverter:SML0EXAMP2")})
    assert inverter is not None
    inverter_id = inverter.id

    call = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "read_register",
            {"device_id": inverter_id, "register": 7},
            blocking=True,
            return_response=True,
        )
    )
    await asyncio.sleep(0.05)
    await _answer(device, 0x05, 7, (42).to_bytes(2, "big"))

    result = await asyncio.wait_for(call, 5)
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
