"""The Growatt Datalogger integration.

Runs a TCP server that Growatt dataloggers upload to directly, replacing the vendor
cloud. No add-on, no broker, no compiled dependencies.

Note the deferred imports below. Home Assistant is pulled in inside the setup functions
rather than at module scope, so that importing this package does not itself require Home
Assistant. That is what lets ``protocol`` and ``registers`` -- which are deliberately
stdlib-only -- be imported, tested and audited on their own, since importing a subpackage
always executes its parent's ``__init__``. The type alias below is a PEP 695 alias and is
evaluated lazily, so it costs nothing at import time either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .hub import GrowattHub

type GrowattConfigEntry = ConfigEntry[GrowattHub]


async def async_setup_entry(hass: HomeAssistant, entry: GrowattConfigEntry) -> bool:
    """Bind the server, then bring up the platforms."""
    import errno

    from homeassistant.const import CONF_PORT
    from homeassistant.exceptions import ConfigEntryNotReady

    from .const import DEFAULT_PORT, PLATFORMS
    from .hub import GrowattHub
    from .services import async_register_services

    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    hub = GrowattHub(hass, entry, port)

    try:
        await hub.async_start()
    except OSError as err:
        if err.errno in (errno.EADDRINUSE, errno.EACCES):
            # Retryable on purpose. After a reload the previous socket can still be in
            # TIME_WAIT, and Home Assistant's backoff resolves that without the user
            # having to do anything.
            raise ConfigEntryNotReady(
                translation_domain=__package__ or "",
                translation_key="port_in_use",
                translation_placeholders={"port": str(port), "error": str(err)},
            ) from err
        raise

    entry.runtime_data = hub

    # The server must be listening before platforms are set up, so a record arriving
    # during setup finds its coordinator already in place.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Domain-wide rather than per-entry, and idempotent: only one entry is allowed, but
    # re-registering on a reload is harmless and keeps the services available.
    async_register_services(hass)

    # No update listener here on purpose: the options flow is an OptionsFlowWithReload,
    # which reloads the entry itself. Registering both is rejected by Home Assistant.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GrowattConfigEntry) -> bool:
    """Release the listening socket, whatever the platforms do.

    The server is stopped even if unloading a platform fails: leaking a bound port means
    the integration can never be set up again without restarting Home Assistant, which is
    a worse outcome than an untidy unload.
    """
    from .const import PLATFORMS

    hub = entry.runtime_data
    await hub.async_stop()
    # Flush the debounced device list, so a restart within the save delay still finds
    # its devices and does not come back with an empty dashboard.
    await hub.async_flush_storage()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
