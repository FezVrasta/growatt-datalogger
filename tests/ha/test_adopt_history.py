"""Adopting the history of whatever integration was reading the inverter before.

The migration rests on a Home Assistant guarantee -- history and long-term statistics
are keyed by entity id, and renaming an entity carries both across -- so these tests
drive the real recorder rather than mocking it. A test against a fake would prove
nothing about the one behaviour the feature depends on.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_group
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    async_list_statistic_ids,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.growatt_datalogger.const import DOMAIN

OLD_ENTITY = "sensor.growatt_lifetime_energy_output"

#: Registers 3051-3052 hold the lifetime output counter, in tenths of a kWh.
_ENERGY_GROUP = build_group(3000, [0] * 51 + [0, 12345])

_UNIQUE_ID = f"{DOMAIN}_inverter:SML0EXAMP2_output_energy_total"


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder) -> None:
    """Prepare the recorder's database before Home Assistant starts.

    Home Assistant installs recorder's entity-registry listener during setup and
    refuses to do so if anything else is already listening, so a recorder brought up
    afterwards is not the same thing as one that was there all along.
    """


async def _settle(hass: HomeAssistant, times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def _energy_entity(hass: HomeAssistant, device: FakeDatalogger) -> str:
    """Report a lifetime energy counter, and return the entity it created."""
    await device.send_data(groups=[_ENERGY_GROUP])
    await _settle(hass)

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, _UNIQUE_ID)
    assert entity_id is not None
    return entity_id


async def _record_old_statistics(
    hass: HomeAssistant, statistic_id: str = OLD_ENTITY, unit: str = "kWh"
) -> None:
    """Leave behind the kind of series a departed integration leaves behind."""
    unit_class = "energy" if unit in ("kWh", "Wh") else "power"
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    async_import_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_class": unit_class,
            "unit_of_measurement": unit,
        },
        [
            {"start": start + timedelta(hours=hour), "state": 1230.0 + hour, "sum": 100.0 + hour}
            for hour in range(3)
        ],
    )
    await async_wait_recording_done(hass)


async def test_the_growatt_entity_takes_over_the_old_id(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """The point of the whole exercise: dashboards and automations keep working."""
    entity_id = await _energy_entity(hass, device)
    await _record_old_statistics(hass)

    result = await hass.services.async_call(
        DOMAIN,
        "adopt_history",
        {"target_entity": entity_id, "source_entity_id": OLD_ENTITY},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert result["entity_id"] == OLD_ENTITY
    assert result["previous_entity_id"] == entity_id

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, _UNIQUE_ID) == OLD_ENTITY
    assert hass.states.get(OLD_ENTITY) is not None
    assert hass.states.get(entity_id) is None


async def test_the_adopted_series_survives_the_rename(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """Recorder refuses to move the target's series onto an id that already has one.

    That refusal is the outcome we want -- the long history is what must survive -- but
    it is recorder's internal behaviour rather than ours, so it is worth pinning.
    """
    entity_id = await _energy_entity(hass, device)
    await _record_old_statistics(hass)

    await hass.services.async_call(
        DOMAIN,
        "adopt_history",
        {"target_entity": entity_id, "source_entity_id": OLD_ENTITY},
        blocking=True,
    )
    await async_wait_recording_done(hass)

    ids = {meta["statistic_id"] for meta in await async_list_statistic_ids(hass, {OLD_ENTITY})}
    assert OLD_ENTITY in ids


async def test_discarding_removes_the_series_left_behind(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """What the target compiled under its own id is unreachable once it is renamed."""
    entity_id = await _energy_entity(hass, device)
    await _record_old_statistics(hass)
    await _record_old_statistics(hass, statistic_id=entity_id)

    result = await hass.services.async_call(
        DOMAIN,
        "adopt_history",
        {
            "target_entity": entity_id,
            "source_entity_id": OLD_ENTITY,
            "discard_target_statistics": True,
        },
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)

    assert result["orphaned_statistics"] is None
    ids = {
        meta["statistic_id"]
        for meta in await async_list_statistic_ids(hass, {entity_id, OLD_ENTITY})
    }
    assert ids == {OLD_ENTITY}


async def test_an_id_that_is_still_taken_is_refused(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """An id has one owner. Renaming into a live one would fail deep inside HA."""
    entity_id = await _energy_entity(hass, device)
    await _record_old_statistics(hass)
    hass.states.async_set(OLD_ENTITY, "1234.5")

    with pytest.raises(ServiceValidationError, match="still exists"):
        await hass.services.async_call(
            DOMAIN,
            "adopt_history",
            {"target_entity": entity_id, "source_entity_id": OLD_ENTITY},
            blocking=True,
        )


async def test_an_unconvertible_unit_is_refused(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """Recorder would silently stop compiling statistics for the entity.

    A mismatch it cannot convert is not an error there -- it is a warning in the log and
    a series that quietly stops growing -- which is exactly the kind of failure a
    migration must not be able to cause.
    """
    entity_id = await _energy_entity(hass, device)
    await _record_old_statistics(hass, unit="W")

    with pytest.raises(ServiceValidationError, match="not convertible"):
        await hass.services.async_call(
            DOMAIN,
            "adopt_history",
            {"target_entity": entity_id, "source_entity_id": OLD_ENTITY},
            blocking=True,
        )


async def test_an_id_with_no_history_is_refused(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    """Nothing to adopt almost always means a typo, and a typo must not rename anything."""
    entity_id = await _energy_entity(hass, device)

    with pytest.raises(ServiceValidationError, match="No long-term statistics"):
        await hass.services.async_call(
            DOMAIN,
            "adopt_history",
            {"target_entity": entity_id, "source_entity_id": "sensor.mistyped"},
            blocking=True,
        )

    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, _UNIQUE_ID) == entity_id


async def test_only_this_integration_can_adopt(
    recorder_mock,
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
) -> None:
    await _record_old_statistics(hass)

    with pytest.raises(ServiceValidationError, match="not a Growatt"):
        await hass.services.async_call(
            DOMAIN,
            "adopt_history",
            {"target_entity": "sensor.somebody_else", "source_entity_id": OLD_ENTITY},
            blocking=True,
        )
