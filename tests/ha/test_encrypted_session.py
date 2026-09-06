"""Telling the user when a datalogger speaks a cipher this integration cannot read.

Issue #3: a ShineWiFi stick on 2024-and-later firmware opens every connection with a
key exchange and encrypts everything after it. The connection is healthy, records
arrive and are acknowledged, and not one device or entity is created -- because
decoding never gets far enough to learn a serial. Without a repair notice the
integration simply looks broken.
"""

from __future__ import annotations

from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_frame
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN, ISSUE_ENCRYPTED_SESSION

from .conftest import settle

#: The handshake is the first frame of the connection and is *not* 16-byte aligned;
#: everything after it is. Both facts are taken from the capture on issue #3.
HANDSHAKE = build_frame(b"Password&*20240730" + bytes(66), function=0x41)
CIPHERTEXT_RECORD = build_frame(bytes(832), function=0x04)


async def _encrypted_upload(hass: HomeAssistant, device: FakeDatalogger) -> None:
    await device.send_raw(HANDSHAKE)
    await device.send_raw(CIPHERTEXT_RECORD)
    await settle(hass)
    # The stick hangs up between uploads; the repair is raised as the session ends.
    await device.close()
    await settle(hass, times=5)


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ENCRYPTED_SESSION)


async def test_an_encrypted_datalogger_raises_a_repair(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await _encrypted_upload(hass, device)

    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR


async def test_no_devices_are_created_from_an_encrypted_session(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The symptom the repair exists to explain, pinned so it stays explained."""
    await _encrypted_upload(hass, device)

    assert setup_integration.runtime_data.devices == {}


async def test_an_ordinary_datalogger_raises_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_data()
    await settle(hass)
    await device.close()
    await settle(hass, times=5)

    assert _issue(hass) is None
    assert setup_integration.runtime_data.devices != {}
