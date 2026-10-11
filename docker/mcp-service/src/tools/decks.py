"""Deck management tools.

Destructive tools (``delete_deck``) and ``rename_deck`` follow the two-gate
pattern: a boolean ``confirm`` argument (False yields a skipped
MutationResponse and touches NOTHING), then, inside the mutation closure, a
fail-closed ``pre-rename-deck``/``pre-delete-deck`` snapshot via the
registry's ``snapshot_locked`` BEFORE the destructive wheel call — if the
snapshot raises, the mutation fails and nothing is renamed or deleted.
"""

import logging
from typing import Any, Dict, List

from ..errors import CollectionToolError
from ..models import DECK_CONFIG_UPDATE_KEYS, MutationResponse, SyncStatus, UpdateDeckConfigInput
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.decks")


def _list_decks_impl(col) -> List[Dict[str, Any]]:
    return [{"name": deck.name, "id": deck.id} for deck in col.decks.all_names_and_ids()]


async def list_decks(registry, username: str) -> Dict[str, Any]:
    """Retrieve all deck names and their IDs from the user's collection."""
    logger.info("Executing list_decks for %r", username)
    decks_list = await registry.run(username, _list_decks_impl)
    return {
        "count": len(decks_list),
        "decks": decks_list,
    }


def _require_deck(col, deck_id: int) -> None:
    """Raise CollectionToolError unless ``deck_id`` is a real deck.

    ``col.decks.get`` silently returns a fallback deck dict for unknown ids,
    so existence is checked against ``all_names_and_ids`` (probed against the
    wheel), never against ``get``.
    """
    if deck_id not in {deck.id for deck in col.decks.all_names_and_ids()}:
        raise CollectionToolError(f"Deck with ID {deck_id} not found in collection.")


async def create_deck(
    sync_manager: SyncManager,
    registry,
    username: str,
    deck_name: str,
) -> MutationResponse:
    """Create a deck (or return the existing deck's ID); 'Parent::Child' makes subdecks."""
    logger.info("Executing create_deck for %r: %r", username, deck_name)

    def _create(col) -> Dict[str, Any]:
        # Creates OR resolves the existing deck: probed against the wheel,
        # re-adding an existing name returns that deck's id unchanged.
        out = col.decks.add_normal_deck_with_name(deck_name)
        return {"deck_id": int(out.id), "deck_name": deck_name}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _create)

    return await sync_manager.execute_mutation(
        username,
        operation_name="create_deck",
        mutation_fn=_mutation,
    )


async def rename_deck(
    sync_manager: SyncManager,
    registry,
    username: str,
    deck_id: int,
    new_name: str,
    confirm: bool = False,
) -> MutationResponse:
    """Rename a deck (children follow) after a fail-closed pre-rename-deck snapshot."""
    if not confirm:
        logger.info("rename_deck for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation renames a deck; pass confirm=true to proceed "
            "(a pre-rename-deck snapshot backup is taken first).",
        )
    logger.info("Executing rename_deck for %r: deck %d -> %r", username, deck_id, new_name)

    def _rename(col) -> Dict[str, Any]:
        _require_deck(col, deck_id)
        old_name = col.decks.name(deck_id)
        # Fail-closed snapshot FIRST: if it raises, the rename never runs.
        registry.snapshot_locked(col, username, "pre-rename-deck")
        col.decks.rename(deck_id, new_name)
        return {"deck_id": deck_id, "old_name": old_name, "new_name": new_name}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _rename)

    return await sync_manager.execute_mutation(
        username,
        operation_name="rename_deck",
        mutation_fn=_mutation,
    )


async def delete_deck(
    sync_manager: SyncManager,
    registry,
    username: str,
    deck_id: int,
    confirm: bool = False,
) -> MutationResponse:
    """Delete a deck, its subdecks and their cards after a fail-closed pre-delete-deck snapshot."""
    if deck_id == 1:
        # Refused before the confirm gate: even confirm=true never deletes Default.
        logger.info("delete_deck for %r refused: deck 1 is the Default deck", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="Default deck cannot be deleted.",
        )
    if not confirm:
        logger.info("delete_deck for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently deletes the deck, its subdecks and all their "
            "cards; pass confirm=true to proceed (a pre-delete-deck snapshot backup is "
            "taken first).",
        )
    logger.info("Executing delete_deck for %r: deck %d", username, deck_id)

    def _delete(col) -> Dict[str, Any]:
        _require_deck(col, deck_id)
        deck_name = col.decks.name(deck_id)
        # child_ids walks the whole subtree (probed: 'A' with A::B::C yields both).
        deck_ids = [deck_id, *col.decks.child_ids(deck_name)]
        placeholders = ", ".join("?" for _ in deck_ids)
        cards_removed = int(
            col.db.scalar(f"SELECT COUNT() FROM cards WHERE did IN ({placeholders})", *deck_ids) or 0
        )
        # Fail-closed snapshot FIRST: if it raises, nothing is deleted.
        registry.snapshot_locked(col, username, "pre-delete-deck")
        col.decks.remove([deck_id])
        return {"deck_id": deck_id, "deck_name": deck_name, "cards_removed": cards_removed}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _delete)

    return await sync_manager.execute_mutation(
        username,
        operation_name="delete_deck",
        mutation_fn=_mutation,
    )


# -- deck config presets (roadmap phase 4) ------------------------------------------


async def get_deck_config(registry, username: str, deck_id: int) -> Dict[str, Any]:
    """Return a deck's full scheduling config dict (read-only)."""
    logger.info("Executing get_deck_config for %r: deck %d", username, deck_id)

    def _get(col) -> Dict[str, Any]:
        _require_deck(col, deck_id)
        # Plain JSON dict (probed wheel API): name, maxTaken, new{...},
        # rev{...}, autoplay, timer, ... A dynamic (filtered) deck has no
        # preset of its own — config_dict_for_deck_id then returns the deck
        # itself with its embedded conf, which is still honest to show.
        conf = col.decks.config_dict_for_deck_id(deck_id)
        return {"deck_id": deck_id, "deck_name": col.decks.name(deck_id), "config": dict(conf)}

    return await registry.run(username, _get)


async def update_deck_config(
    sync_manager: SyncManager,
    registry,
    username: str,
    deck_id: int,
    updates: UpdateDeckConfigInput,
) -> MutationResponse:
    """Apply whitelisted scheduling updates to a deck's config preset.

    Not destructive (scheduling preferences, same no-snapshot class as
    update_note_fields) — but the preset is SHARED: every deck pointing at
    the same config id changes scheduling behavior with it, which the tool
    description spells out.
    """
    logger.info(
        "Executing update_deck_config for %r: deck %d, keys=%r",
        username, deck_id,
        sorted(key for key in DECK_CONFIG_UPDATE_KEYS if getattr(updates, key) is not None),
    )

    def _update(col) -> Dict[str, Any]:
        _require_deck(col, deck_id)
        deck_name = col.decks.name(deck_id)
        conf = col.decks.config_dict_for_deck_id(deck_id)
        if conf.get("dyn"):
            # A filtered deck's config is embedded in the deck itself, not a
            # shared preset — update_config would write a config with the
            # deck's shape. Refused in Python before any backend call.
            raise CollectionToolError(
                f"Deck {deck_id} ({deck_name!r}) is a filtered (dynamic) deck: its config is "
                "embedded in the deck and cannot be edited as a preset."
            )
        requested = [
            key for key in DECK_CONFIG_UPDATE_KEYS if getattr(updates, key) is not None
        ]
        # Whitelisted mapping only (the input model refused everything else):
        # apply to the dict, then persist the whole mutated dict.
        if "new_per_day" in requested:
            conf["new"]["perDay"] = updates.new_per_day
        if "reviews_per_day" in requested:
            conf["rev"]["perDay"] = updates.reviews_per_day
        if "max_answer_secs" in requested:
            conf["maxTaken"] = updates.max_answer_secs
        if "autoplay_audio" in requested:
            conf["autoplay"] = updates.autoplay_audio
        col.decks.update_config(conf)
        return {
            "deck_id": deck_id,
            "deck_name": deck_name,
            "updated_keys": sorted(requested),
            "config": dict(conf),
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _update)

    return await sync_manager.execute_mutation(
        username,
        operation_name="update_deck_config",
        mutation_fn=_mutation,
    )
