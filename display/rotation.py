"""Rotation state machine.

Pure logic: an opaque set of image ids in, a shuffled walk order out. No
AppKit, no PIL, no filesystem image access - window.py and app.py
(Steps 5/6) own drawing and image validation; this module only owns
"which id is current, and what's next."

Persisted to `~/.viewlab/state/rotation_state.json` as
{order, position, pool_hash}. On construction, resumes at `position` if pool_hash matches
the current pool; otherwise reshuffles fresh. A missing or corrupt state
file is treated identically to "pool changed" - never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from display import paths
from display.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# Relocated out of the source tree in Step -1: inside a
# py2app bundle `Path(__file__).parent / "state"` is read-only, and
# writing there would invalidate the ad-hoc code signature.
DEFAULT_STATE_PATH = paths.rotation_state_path()


def _pool_hash(ids: Iterable[str]) -> str:
    """Order-independent fingerprint of a pool's membership."""
    joined = "|".join(sorted(ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class Rotation:
    """A seeded, persisted shuffle over a pool of opaque image ids.

    "manual override seam" is now built (phase-3 Step 1). Three
    entry points walk the order, and which one a caller uses encodes
    *why* it is moving:

    - `next_image()` — the rotation **timer**. A no-op while pinned,
      which is what makes Pause hold even if a caller forgets to stop the
      timer. One enforcement point, not two.
    - `step_forward()` / `previous()` — an explicit **user** step (Next/Previous). Always move, and carry the pin with them, because
      Next-while-paused must move the pause rather than
      cancel it.

    The pin is deliberately **not** persisted here. `paused`/
    `paused_on_id` live in the UI-owned command file, which is
    already durable desired state; persisting the pin a second time in
    `rotation_state.json` would create two sources of truth that
    disagree the moment one of them is hand-edited. `app.py` re-pins from
    the command file on startup.
    """

    def __init__(
        self,
        pool_ids: Iterable[str],
        state_path: Path | str = DEFAULT_STATE_PATH,
        rng: random.Random | None = None,
        is_valid: Callable[[str], bool] | None = None,
        shuffle: bool = True,
    ) -> None:
        self._state_path = Path(state_path)
        self._rng = rng or random.Random()
        # "Order: shuffle or in order". False walks the pool in
        # the order the *source* listed it — which for a folder is the
        # configured sort order, and for the HTTP sources is whatever the
        # server returned. Defaults True because that is what this class
        # has always done, so nothing that does not pass the argument
        # changes behaviour.
        self._shuffle = bool(shuffle)
        #: The pool in source order (see `sync_pool`). Populated there;
        #: declared here so `set_shuffle` before a first sync is safe.
        self._pool_ids: list[str] = []
        self._order: list[str] = []
        self._position: int = 0
        self._pool_hash: str = ""
        self._pinned: bool = False
        self.sync_pool(pool_ids, is_valid=is_valid)

    # -- persistence --------------------------------------------------

    def _load_state(self) -> dict[str, Any] | None:
        try:
            raw = self._state_path.read_text()
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "rotation.py: %s is not valid JSON; starting fresh.",
                self._state_path,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "rotation.py: %s has an unexpected shape; starting fresh.",
                self._state_path,
            )
            return None
        return data

    def _save_state(self) -> None:
        atomic_write_json(
            self._state_path,
            {
                "order": self._order,
                "position": self._position,
                "pool_hash": self._pool_hash,
            },
        )

    # -- pool sync / reshuffle ------------------------------------------

    def sync_pool(
        self,
        pool_ids: Iterable[str],
        is_valid: Callable[[str], bool] | None = None,
    ) -> None:
        """Reconcile against a (possibly changed) pool.

        Bad-image handling: `is_valid`, if given, is applied here,
        at queue-build time - a candidate that fails it never enters
        `_order`, so it's never a display candidate; the skip is invisible
        by construction rather than discovered later at render time. The
        concrete decodability/aspect-ratio check is Step 5/6's to supply
        once window.py has settled how images actually get loaded.

        Reshuffles iff the resulting valid pool's membership differs from
        what's currently loaded; otherwise resumes at the persisted
        position unchanged (or is a no-op if already in sync).
        """
        ids = list(pool_ids)
        if is_valid is not None:
            ids = [i for i in ids if is_valid(i)]
        new_hash = _pool_hash(ids)
        # The pool in the order the *source* listed it, retained so that
        # "in order" has something to order by. `_order` cannot
        # serve: it is the shuffled permutation, so re-deriving source
        # order from it is impossible once a shuffle has happened. Set
        # before the early return below, because a `set_shuffle` can
        # arrive at any time after an already-in-sync poll.
        self._pool_ids = ids

        if new_hash == self._pool_hash and self._order:
            return  # already in sync, nothing to do

        # A reshuffle resets `_position` to 0, which would silently move
        # a *paused* View onto a different picture the next time a poll
        # noticed one new file in the folder — "restoring shows
        # the same picture you left" applies to Pause just as much. Held
        # across the reshuffle below and re-pinned if the picture
        # survived the pool change; if it did not, the pin lands wherever
        # the fresh order starts, which is the only thing left to do.
        held = self.current() if self._pinned else None

        # Only consult the persisted file on first sync (construction);
        # once we have a live in-memory order, a pool change during the
        # run always reshuffles fresh rather than resurrecting stale disk
        # state from a previous, different pool.
        state = self._load_state() if not self._order else None
        if state is not None and state.get("pool_hash") == new_hash:
            order = state.get("order") or []
            position = state.get("position", 0)
            if order and set(order) == set(ids):
                self._order = order
                self._position = position % len(order) if isinstance(position, int) else 0
                self._pool_hash = new_hash
                self._restore_pin(held)
                return

        self._reshuffle(ids, new_hash)
        self._restore_pin(held)

    def _restore_pin(self, held: str | None) -> None:
        """Put a held pin back on its picture after a pool change."""
        if held is None or not self._order:
            return
        try:
            index = self._order.index(held)
        except ValueError:
            logger.info(
                "rotation.py: the paused picture is no longer in the pool; "
                "the pause now holds %r.",
                self.current(),
            )
            return
        if index != self._position:
            self._position = index
            self._save_state()

    def set_shuffle(self, shuffle: bool) -> None:
        """Switch between shuffled and source order at runtime.

        Re-orders immediately rather than waiting for the next pool
        change, because the next pool change may be thirty minutes away
        and the user is looking at the settings window now.

        The currently-shown picture is held across the re-order and the
        position moved to wherever it landed, so changing the *order* of
        the rotation never changes what is *on* the View. Doing otherwise
        would make this checkbox behave like a Next button, which is not
        what it says it does.
        """
        shuffle = bool(shuffle)
        if shuffle == self._shuffle:
            return
        self._shuffle = shuffle
        if not self._order:
            return
        held = self.current()
        # From the *source* order, not from `_order`: re-ordering the
        # existing permutation would leave "in order" showing whatever
        # sequence the last shuffle happened to produce.
        self._reshuffle(list(self._pool_ids or self._order), self._pool_hash)
        self._restore_pin(held)

    def _reshuffle(self, ids: list[str], pool_hash: str) -> None:
        """Rebuild the walk order. Named for its original and still
        commonest behaviour; with `shuffle=False` it is a re-*order* into
        the source's own sequence, which is the same operation minus the
        permutation."""
        order = list(ids)
        if self._shuffle:
            self._rng.shuffle(order)
        self._order = order
        self._position = 0
        self._pool_hash = pool_hash
        self._save_state()

    # -- walking --------------------------------------------------------

    def current(self) -> str | None:
        if not self._order:
            return None
        return self._order[self._position]

    def position(self) -> int:
        """1-based position in the current permutation, for `Picture 12 of 47` fallback label. 0 on an empty pool."""
        if not self._order:
            return 0
        return self._position + 1

    def next_image(self) -> str | None:
        """The rotation timer's entry point. Advance to and return the
        next id. Reshuffles (a fresh permutation of the same pool) when
        the current permutation is exhausted, rather than looping the
        identical order every cycle. Returns None, never raises, on an
        empty pool.

        **A no-op while pinned** (Pause). Enforcing the pause here
        rather than only by stopping the timer means a rotation tick that
        slips through — a timer left scheduled, a hot-reload that
        rebuilds one — cannot move a paused picture. `step_forward()` is
        the entry point for a user-initiated Next, which does move."""
        if not self._order:
            return None
        if self._pinned:
            return self.current()
        return self._advance_one()

    def _advance_one(self) -> str | None:
        self._position += 1
        if self._position >= len(self._order):
            self._reshuffle(self._order, self._pool_hash)
        else:
            self._save_state()
        return self.current()

    def step_forward(self) -> str | None:
        """An explicit user Next. Moves even while pinned, and
        takes the pin with it: "Next/Previous while paused move
        that id one step; they never clear `paused`"."""
        if not self._order:
            return None
        return self._advance_one()

    def previous(self) -> str | None:
        """An explicit user Previous. Step back one, **floored at
        0**.

        Two deliberate non-behaviours, both of which a naive
        implementation would get wrong:

        - It does **not** wrap. At position 0 this is a no-op returning
          the same id. Wrapping to the end of the order would show a
          picture the user has not seen yet and call it "back", which is
          the opposite of what the control means.
        - It does **not** un-reshuffle. The permutation that preceded the
          current one is gone — `_reshuffle` replaces it in place — so
          "previous" is only ever defined *within* the current
          permutation. Reconstructing an earlier one would mean
          persisting a history nothing else needs.

        Like `step_forward()`, this moves while pinned and carries the
        pin with it."""
        if not self._order:
            return None
        if self._position > 0:
            self._position -= 1
            self._save_state()
        return self.current()

    def step(self, steps: int) -> str | None:
        """Apply a signed step count from the control channel's clamped
        `advance` delta in one call. Positive is Next, negative is
        Previous, zero is a no-op that still returns the current id."""
        for _ in range(abs(int(steps))):
            if steps > 0:
                self.step_forward()
            else:
                self.previous()
        return self.current()

    # -- pinning (Pause) -----------------------------------------

    @property
    def is_pinned(self) -> bool:
        return self._pinned

    def pinned_id(self) -> str | None:
        """The id the pause is currently holding, or None when running."""
        return self.current() if self._pinned else None

    def pin(self, image_id: str | None = None) -> str | None:
        """Hold on one picture. With no argument, holds whatever is
        current; with an id, moves to that id first (if it is in the
        current order) and holds there.

        An id that is not in the order pins the current picture instead
        of failing: the caller's alternative is a paused View that is not
        actually paused, and `Paused` is a state the user is
        told about — a label that lies is worse than pausing one picture
        away from the requested one. Returns the pinned id."""
        if not self._order:
            self._pinned = True
            return None
        if image_id is not None:
            try:
                index = self._order.index(image_id)
            except ValueError:
                logger.debug(
                    "rotation.py: pin(%r) — not in the current order; "
                    "pinning the current picture instead.",
                    image_id,
                )
            else:
                if index != self._position:
                    self._position = index
                    self._save_state()
        self._pinned = True
        return self.current()

    def unpin(self) -> None:
        """Resume rotation. Idempotent, and deliberately does not move —
        Resume continues from the picture you paused on."""
        self._pinned = False

    def __len__(self) -> int:
        return len(self._order)
