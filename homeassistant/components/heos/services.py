"""Services for the HEOS integration."""
import functools
import logging

from pyheos import CommandFailedError, Heos, HeosError, const
import voluptuous as vol

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import HomeAssistantType

from homeassistant.components.media_player.const import DOMAIN

from .const import (
    ATTR_PASSWORD,
    ATTR_USERNAME,
    ATTR_GROUP_MEMBERS,
    ATTR_GROUP_LEADER,
    ATTR_ENTITY_ID,
    DOMAIN as HEOS_DOMAIN,
    SERVICE_JOIN,
    SERVICE_UNJOIN,
    SERVICE_SIGN_IN,
    SERVICE_SIGN_OUT,
)
from .media_player import HeosMediaPlayer

_LOGGER = logging.getLogger(__name__)

HEOS_SIGN_IN_SCHEMA = vol.Schema(
    {vol.Required(ATTR_USERNAME): cv.string, vol.Required(ATTR_PASSWORD): cv.string}
)

HEOS_SIGN_OUT_SCHEMA = vol.Schema({})

HEOS_JOIN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_GROUP_LEADER): cv.entity_id,
        vol.Required(ATTR_GROUP_MEMBERS): cv.comp_entity_ids,
    }
)

HEOS_UNJOIN_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID, default=None): cv.entity_id}
)


def register(hass: HomeAssistantType, controller: Heos):
    """Register HEOS services."""
    hass.services.async_register(
        HEOS_DOMAIN,
        SERVICE_SIGN_IN,
        functools.partial(_sign_in_handler, controller),
        schema=HEOS_SIGN_IN_SCHEMA,
    )
    hass.services.async_register(
        HEOS_DOMAIN,
        SERVICE_SIGN_OUT,
        functools.partial(_sign_out_handler, controller),
        schema=HEOS_SIGN_OUT_SCHEMA,
    )
    hass.services.async_register(
        HEOS_DOMAIN,
        SERVICE_JOIN,
        functools.partial(_join_handler, controller, hass),
        schema=HEOS_JOIN_SCHEMA,
    )
    hass.services.async_register(
        HEOS_DOMAIN,
        SERVICE_UNJOIN,
        functools.partial(_unjoin_handler, controller, hass),
        schema=HEOS_UNJOIN_SCHEMA,
    )


def remove(hass: HomeAssistantType):
    """Unregister HEOS services."""
    hass.services.async_remove(HEOS_DOMAIN, SERVICE_SIGN_IN)
    hass.services.async_remove(HEOS_DOMAIN, SERVICE_SIGN_OUT)
    hass.services.async_remove(HEOS_DOMAIN, SERVICE_JOIN)
    hass.services.async_remove(HEOS_DOMAIN, SERVICE_UNJOIN)


async def _sign_in_handler(controller, service):
    """Sign in to the HEOS account."""
    if controller.connection_state != const.STATE_CONNECTED:
        _LOGGER.error("Unable to sign in because HEOS is not connected")
        return
    username = service.data[ATTR_USERNAME]
    password = service.data[ATTR_PASSWORD]
    try:
        await controller.sign_in(username, password)
    except CommandFailedError as err:
        _LOGGER.error("Sign in failed: %s", err)
    except HeosError as err:
        _LOGGER.error("Unable to sign in: %s", err)


async def _sign_out_handler(controller, service):
    """Sign out of the HEOS account."""
    if controller.connection_state != const.STATE_CONNECTED:
        _LOGGER.error("Unable to sign out because HEOS is not connected")
        return
    try:
        await controller.sign_out()
    except HeosError as err:
        _LOGGER.error("Unable to sign out: %s", err)


async def _join_handler(controller, hass, service):
    """Join a HEOS player to a group."""
    if controller.connection_state != const.STATE_CONNECTED:
        _LOGGER.error("Unable to join because HEOS is not connected")
        return
    leader = service.data[ATTR_GROUP_LEADER]
    members = service.data[ATTR_GROUP_MEMBERS]

    try:
        groups = await controller.get_groups(refresh=True)
    except HeosError as err:
        _LOGGER.error("Could not get HEOS group info while joining group. %s", err)

    # Get the HEOS player_id for the leader
    leader_player_id = None
    for entity in hass.data[DOMAIN].entities:
        if isinstance(entity, HeosMediaPlayer) and entity.entity_id == leader:
            leader_player_id = entity.player_id
            break
    if not leader_player_id:
        _LOGGER.error("Failed to determine HEOS player_id for %s", leader)
        return

    # Try to find out if the leader entity is already a group leader.
    # In this case we need to update the existing group.
    existing_heos_group_id = None
    existing_heos_group_members = set()
    for heos_group_id, heos_group in groups.items():
        if heos_group.leader.player_id == leader_player_id:
            existing_heos_group_id = heos_group_id
            existing_heos_group_members = set(
                [member.player_id for member in heos_group.members]
            )
            break

    # Resolve the entities passed in through `members` to HEOS player_ids.
    new_member_player_ids = set(
        [
            entity.player_id
            for entity in hass.data[DOMAIN].entities
            if isinstance(entity, HeosMediaPlayer) and entity.entity_id in members
        ]
    )
    # In case of an existing HEOS group we also need to include the existing HEOS members to
    # the group.
    all_member_player_ids = list(
        new_member_player_ids.union(existing_heos_group_members)
    )

    if existing_heos_group_id:
        _LOGGER.info("Updating HEOS group for %s with members %s", leader, members)
        try:
            await controller.update_group(existing_heos_group_id, all_member_player_ids)
        except HeosError as err:
            _LOGGER.error("Unable to update: %s", err)
    else:
        _LOGGER.info("Creating new HEOS group for %s with members %s", leader, members)
        try:
            await controller.create_group(leader_player_id, all_member_player_ids)
        except HeosError as err:
            _LOGGER.error(
                "HEOS create_group with arguments (%s, %s) failed. Error: %s",
                leader_player_id,
                members,
                err,
            )


async def _unjoin_handler(controller, hass, service):
    """Unjoin HEOS players."""
    if controller.connection_state != const.STATE_CONNECTED:
        _LOGGER.error("Unable to unjoin because HEOS is not connected")
        return

    ungroup_entity_id = service.data[ATTR_ENTITY_ID]

    groups = await controller.get_groups(refresh=True)

    _LOGGER.debug("HEOS - trying to ungroup: %s", ungroup_entity_id)

    # Get devices
    ungroup_player_id = None
    for player in hass.data[DOMAIN].entities:
        if (
            isinstance(player, HeosMediaPlayer)
            and player.entity_id == ungroup_entity_id
        ):
            ungroup_player_id = player.player_id
    if not ungroup_player_id:
        _LOGGER.error("Could not find HEOS player id for %s", ungroup_entity_id)
        return

    for heos_group_id, heos_group in groups.items():
        if heos_group.leader.player_id == ungroup_player_id:
            # destroy the group, as it's leader is removed
            try:
                _LOGGER.info("Removing HEOS group %s", heos_group_id)
                await controller.remove_group(heos_group_id)
            except HeosError as err:
                _LOGGER.error("Failed to remove HEOS group: %s", err)
            else:
                return
        else:
            member_player_ids = [member.player_id for member in heos_group.members]
            if ungroup_player_id in member_player_ids:
                member_player_ids.remove(ungroup_player_id)
                try:
                    await controller.update_group(heos_group_id, member_player_ids)
                except HeosError as err:
                    _LOGGER.error("Failed to remove player from HEOS group: %s", err)
                else:
                    return
