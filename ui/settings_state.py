"""Everything the settings window decides, with no AppKit in it.

`settings_window.py` is the AppKit shell: rows of controls, a window, and
the code that turns a click into a call. This module is the part that can
be wrong in an interesting way, so it is the part that is separable and
tested — the same split `menubar_state.py` and `calibrate_state.py`
already make, for the same reason.

Five things live here:

**The form model.** `SourceForm` holds all three sources' fields at once,
because layout shows all three at once. The unselected rows are
*disabled, never hidden*, so their values must survive being unselected —
a user who types a URL, clicks the folder row to look at it, and clicks
back must find their URL still there.

**Validation at pick time.** The one place this project's
"stay silent about problems" philosophy deliberately does not apply: the
user is present and can fix it. The result strings are the plan's,
verbatim, and `TestResult.save_enabled` is what "Save disabled on
zero" actually means.

**Plain choices.** "15 minutes" is a preference; "900" is a unit
conversion. The interval list is four fixed options and the mapping in
both directions lives here.

**Login state.** Both checkboxes must reflect real `launchctl`
state, not the last-written setting. The subprocess is injected so the
decision — what a given `launchctl print` result *means* — is testable
without a launchd domain.

**The settings document.** Merging into whatever is already in
`settings.json` rather than writing a fresh one, so keys this build does
not know about survive a Save. v1 is additive-only; a settings
window that silently dropped a hand-added key would break that on the
first click.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from display import blank_schedule, image_safety, source_settings
from display.blank_schedule import BlankSchedule
from display.source_settings import SourceSettings

# -- plain choices ----------------------------------------------

#: "Rotation interval as plain choices — 1 min / 15 min / 1 hour /
#: 1 day." Ordered shortest first, which is also the order they read in.
INTERVAL_CHOICES: tuple[tuple[str, float], ...] = (
    ("1 minute", 60.0),
    ("15 minutes", 900.0),
    ("1 hour", 3600.0),
    ("1 day", 86400.0),
)

DEFAULT_INTERVAL_S = 900.0

#: "Order: shuffle or in order."
ORDER_CHOICES: tuple[tuple[str, bool], ...] = (
    ("Shuffle", True),
    ("In order", False),
)

#: The three sources, folder first and preselected.
SOURCE_ROWS: tuple[tuple[str, str], ...] = (
    (source_settings.KIND_FOLDER, "A folder on this Mac"),
    (source_settings.KIND_JSON_URL, "A web address that lists pictures"),
    (source_settings.KIND_IMAGE_SERVER, "Image server"),
)

#: Sub-labels. The JSON URL row states its contract; the image-server row
#: talks the majority of readers out of picking it.
SOURCE_SUBLABELS: dict[str, str] = {
    source_settings.KIND_FOLDER: (
        "Pictures already on this Mac. This is the right choice for "
        "almost everyone."
    ),
    source_settings.KIND_JSON_URL: (
        "Must return a JSON array of image URLs."
    ),
    source_settings.KIND_IMAGE_SERVER: (
        "A server that lists images over HTTP (advanced)."
    ),
}

SORT_ORDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("By name", "name"),
    ("Newest first", "newest"),
    ("Oldest first", "oldest"),
)

POOL_CHOICES: tuple[tuple[str, str], ...] = (
    ("Starred", "starred"),
    ("All", "all"),
)

#: Backlight note, stated once, in the settings status area. The
#: plan is specific that this belongs here and not in the menu: it is a
#: property of the hardware that explains a surprise, not a control.
BACKLIGHT_NOTE = (
    "The View's backlight stays on. In a dark room the circle will still "
    "glow faintly. Unplug it to turn it off completely."
)


def interval_index(seconds: object) -> int:
    """Which of `INTERVAL_CHOICES` a stored value corresponds to.

    Falls back to the *closest* choice rather than to a default, because
    `settings.json` is hand-editable and someone who set 600 should see
    the popup land somewhere sensible instead of silently jumping to 15
    minutes. Ties go to the shorter interval — the value is about to be
    written back on Save, and rounding a rotation down is less surprising
    than rounding it up.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return interval_index(DEFAULT_INTERVAL_S)
    value = float(seconds)
    best = 0
    for index, (_, candidate) in enumerate(INTERVAL_CHOICES):
        if abs(candidate - value) < abs(INTERVAL_CHOICES[best][1] - value):
            best = index
    return best


def interval_seconds(index: object) -> float:
    if isinstance(index, bool) or not isinstance(index, int):
        return DEFAULT_INTERVAL_S
    if 0 <= index < len(INTERVAL_CHOICES):
        return INTERVAL_CHOICES[index][1]
    return DEFAULT_INTERVAL_S


def _choice_index(choices: Sequence[tuple[str, Any]], value: object, default: int = 0) -> int:
    for index, (_, candidate) in enumerate(choices):
        if candidate == value and isinstance(candidate, type(value)):
            return index
    return default


def order_index(shuffle: object) -> int:
    return 0 if bool(shuffle) else 1


def sort_order_index(value: object) -> int:
    return _choice_index(SORT_ORDER_CHOICES, value, 0)


def pool_index(value: object) -> int:
    return _choice_index(POOL_CHOICES, value, 0)


# -- the form ----------------------------------------------------------


@dataclasses.dataclass
class SourceForm:
    """Every source's fields, held simultaneously (static layout).

    Mutable, unlike most dataclasses in this project, because it *is* the
    edit buffer: the window writes each keystroke into it and reads it
    back to build a `SourceSettings`. The durable objects on either side
    of it (`SourceSettings`, `Settings`) stay frozen.
    """

    kind: str = source_settings.KIND_FOLDER
    folder: str = ""
    include_subfolders: bool = False
    sort_order: str = source_settings.DEFAULT_SORT_ORDER
    list_url: str = ""
    base_url: str = ""
    pool: str = source_settings.DEFAULT_POOL

    @classmethod
    def from_settings(cls, source: SourceSettings) -> SourceForm:
        """Seed the form from the saved source.

        The *other* two rows are seeded with their own defaults rather
        than left blank, so the disabled fields under an unselected row
        show what would be used rather than an empty box that looks
        broken. The folder row in particular is never blank: the design makes
        it the default and preselected, and a preselected row with an
        empty required field is a dead end on first run.
        """
        return cls(
            kind=source.kind,
            folder=source.folder or source_settings.default_folder(),
            include_subfolders=source.include_subfolders,
            sort_order=source.sort_order or source_settings.DEFAULT_SORT_ORDER,
            list_url=source.list_url,
            base_url=source.base_url,
            pool=source.pool or source_settings.DEFAULT_POOL,
        )

    def to_settings(self) -> SourceSettings | None:
        """The selected row as a validated `SourceSettings`, or None.

        Validation goes through `source_settings.validate_source`, the
        same function the display uses when it reads the file — so the
        settings window cannot save a document the display would then
        reject and silently fall back from. That symmetry is the point;
        a second, parallel validator here would drift.
        """
        if self.kind == source_settings.KIND_FOLDER:
            data = {
                "kind": self.kind,
                "folder": self.folder.strip(),
                "include_subfolders": bool(self.include_subfolders),
                "sort_order": self.sort_order,
            }
        elif self.kind == source_settings.KIND_JSON_URL:
            data = {"kind": self.kind, "list_url": self.list_url.strip()}
        elif self.kind == source_settings.KIND_IMAGE_SERVER:
            data = {
                "kind": self.kind,
                "base_url": self.base_url.strip(),
                "pool": self.pool,
            }
        else:
            return None
        return source_settings.validate_source(data)


# -- validation ---------------------------------------------------


class Outcome(enum.Enum):
    """Why a Test came back the way it did.

    An enum rather than a bare string so the window can branch on the
    result without matching on prose, and so the copy below has exactly
    one definition. Every member maps to one of specified strings.
    """

    OK = "ok"
    EMPTY_FOLDER = "empty_folder"
    NO_FOLDER = "no_folder"
    UNREACHABLE = "unreachable"
    NOT_A_LIST = "not_a_list"
    NO_STARRED = "no_starred"
    BAD_ADDRESS = "bad_address"
    INCOMPLETE = "incomplete"


@dataclasses.dataclass(frozen=True)
class TestResult:
    """One Test button press, reduced to what the window renders."""

    outcome: Outcome
    message: str
    count: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def save_enabled(self) -> bool:
        """"Save disabled on zero."

        Tied to the outcome rather than to `count` so that "I could not
        reach it" and "I reached it and it was empty" are both blocking,
        which is what the user needs: saving a source that returns
        nothing produces a View that shows nothing, and the settings
        window is the last place anyone will look for the reason.
        """
        return self.outcome is Outcome.OK


def _found_message(count: int) -> str:
    """`"47 pictures found"` / `"12 pictures found"`.

    Singular is special-cased. The plan only ever shows plural examples,
    but "1 pictures found" is the kind of detail that makes a careful
    reader trust the rest of the screen slightly less.
    """
    if count == 1:
        return "1 picture found"
    return f"{count} pictures found"


def _extensions_phrase() -> str:
    """The extensions actually accepted, read from `image_safety` rather
    than written out here; the messages are named explicitly, and a hardcoded
    list would be the first thing to go stale if the allow-list changed."""
    names = sorted(image_safety.ALLOWED_EXTENSIONS)
    return ", ".join(names)


def probe_folder(
    folder: object,
    lister: Callable[[Path], Sequence[Path]] | None = None,
) -> TestResult:
    """Folder validation. Performs a **real directory read**.

    Real, not `Path.exists()`, and the reason is spelled out at length: anything
    under Desktop, Documents, Downloads, Pictures or an external volume
    is TCC-gated, and this is the moment to make that prompt appear —
    in the foreground, while the user is choosing the folder, rather
    than at login in the display agent where a dismissed prompt becomes
    a permanent denial.
    """
    if not isinstance(folder, str) or not folder.strip():
        return TestResult(
            Outcome.INCOMPLETE, "Choose a folder first.",
        )
    path = Path(folder).expanduser()
    read = lister if lister is not None else _list_images_in
    try:
        entries = read(path)
    except (OSError, PermissionError):
        # A denied TCC grant and a deleted folder land here together.
        # Naming the path is the only useful thing to say: the user
        # picked it, so they can tell which of the two it is.
        return TestResult(
            Outcome.NO_FOLDER,
            f"Couldn't read {path}. It may have been moved, or macOS may "
            f"be blocking access to it.",
        )
    count = len(entries)
    if count == 0:
        return TestResult(
            Outcome.EMPTY_FOLDER,
            f"No pictures in this folder. Looked for "
            f"{_extensions_phrase()} files.",
        )
    return TestResult(Outcome.OK, _found_message(count), count)


def _list_images_in(path: Path) -> list[Path]:
    """Non-recursive listing of decodable-looking files.

    Deliberately does not honour `include_subfolders`: the Test button
    answers "is there anything here", and walking a home directory
    because a checkbox was ticked would turn a button press into a
    multi-second stall. A folder whose pictures are all one level down
    reports zero and is a real thing the user should know about.
    """
    return [
        entry
        for entry in sorted(path.iterdir())
        if entry.is_file()
        and entry.suffix.lower() in image_safety.ALLOWED_EXTENSIONS
    ]


def probe_json_url(
    url: object,
    fetcher: Callable[[str], object] | None = None,
) -> TestResult:
    """URL Test: `"12 pictures found"` or the concrete failure.

    "This is also where the contract gets taught, at the moment it
    matters" — so `NOT_A_LIST` is a distinct outcome from `UNREACHABLE`,
    and its message restates what the address was supposed to return.
    The two are indistinguishable through `JsonUrlSource.list_images()`,
    which flattens every failure to `[]` by contract, which is why this
    fetches the list itself rather than going through the source.
    """
    if not isinstance(url, str) or not url.strip():
        return TestResult(Outcome.INCOMPLETE, "Enter a web address first.")
    get = fetcher if fetcher is not None else _fetch_json
    body = get(url.strip())
    if body is _UNREACHABLE:
        return TestResult(Outcome.UNREACHABLE, "Could not reach that address")
    if not isinstance(body, list):
        return TestResult(
            Outcome.NOT_A_LIST,
            "That address did not return a list of pictures",
        )
    count = sum(1 for entry in body if _looks_like_an_image_entry(entry))
    if count == 0:
        return TestResult(
            Outcome.NOT_A_LIST,
            "That address did not return a list of pictures",
        )
    return TestResult(Outcome.OK, _found_message(count), count)


def probe_image_server(
    base_url: object,
    pool: object = source_settings.DEFAULT_POOL,
    fetcher: Callable[[str], object] | None = None,
) -> TestResult:
    """Image Server Test.

    "Test additionally catches the trap Phase 2 hit for real" — a server
    that is up, reachable, and returns an empty array because everything
    in it is unstarred. That is a *success* at the network layer and a
    dead end at the product layer, and the only useful response names the
    fix: `"Connected, but no starred pictures. Try 'All'."`
    """
    if not isinstance(base_url, str) or not base_url.strip():
        return TestResult(Outcome.INCOMPLETE, "Enter a server address first.")
    chosen_pool = pool if pool in source_settings.VALID_POOLS else source_settings.DEFAULT_POOL
    root = base_url.strip().rstrip("/")
    query = "?starred=true" if chosen_pool == "starred" else ""
    get = fetcher if fetcher is not None else _fetch_json
    body = get(f"{root}/api/images{query}")
    if body is _UNREACHABLE:
        return TestResult(Outcome.UNREACHABLE, "Could not reach that address")
    if not isinstance(body, list):
        return TestResult(
            Outcome.NOT_A_LIST,
            "That address did not return a list of pictures",
        )
    if not body:
        if chosen_pool == "starred":
            return TestResult(
                Outcome.NO_STARRED,
                "Connected, but no starred pictures. Try 'All'.",
            )
        return TestResult(
            Outcome.EMPTY_FOLDER, "Connected, but there are no pictures on it."
        )
    return TestResult(Outcome.OK, _found_message(len(body)), len(body))


def probe(form: SourceForm, fetcher: Callable[[str], object] | None = None) -> TestResult:
    """Test whichever row is selected. The window's single entry point."""
    if form.kind == source_settings.KIND_FOLDER:
        return probe_folder(form.folder)
    if form.kind == source_settings.KIND_JSON_URL:
        return probe_json_url(form.list_url, fetcher=fetcher)
    if form.kind == source_settings.KIND_IMAGE_SERVER:
        return probe_image_server(form.base_url, form.pool, fetcher=fetcher)
    return TestResult(Outcome.INCOMPLETE, "Pick where pictures come from.")


#: Sentinel distinguishing "the request failed" from "the response was
#: JSON `null`". A bare `None` would conflate them, and they get
#: different copy.
class _Unreachable:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unreachable>"


_UNREACHABLE = _Unreachable()

#: Bound on a list response. The same order of magnitude as
#: `json_url.MAX_LIST_BYTES`; this is a Test button, not the source, so
#: it only has to answer "roughly how many" without reading a hostile
#: 500MB body into the UI process.
MAX_PROBE_BYTES = 2 * 1024 * 1024

PROBE_TIMEOUT_S = 6.0


def _fetch_json(url: str) -> object:
    """GET `url` and decode it as JSON, through URL checks.

    Reuses `sources.net` rather than calling httpx directly, so the
    scheme allow-list, the loopback/link-local rejection, the explicit
    `follow_redirects=False`, and the credential ban all apply to the
    Test button exactly as they apply to the running source. A Test that
    could reach an address the source refuses would be worse than no
    Test — it would certify a configuration that then silently does
    nothing.

    `allow_private`/`allow_loopback` are True for the same reason
    `JsonUrlSource.list_images` sets them on the *list* URL: this is the
    address the user typed in themselves, and the LAN case is the
    normal one here. The images *inside* the response get no such
    latitude, and this function never fetches them.
    """
    from display.sources import net

    checked = net.safe_url(url, allow_private=True, allow_loopback=True)
    if checked is None:
        return _UNREACHABLE
    headers = {"Host": checked.host_header} if checked.host_header else {}
    try:
        with net.make_client(timeout_s=PROBE_TIMEOUT_S) as client:
            with client.stream(
                "GET", checked.request_url, headers=headers
            ) as resp:
                if resp.status_code >= 300:
                    return _UNREACHABLE
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_PROBE_BYTES:
                        return _UNREACHABLE
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except Exception:  # noqa: BLE001 - a Test button never raises
        # Deliberately broad. This runs on a background thread whose
        # exception would otherwise be printed to a log nobody reads
        # while the window sat showing "Testing…" forever. Every failure
        # mode here — DNS, TLS, timeout, malformed JSON, a decode error
        # — is the same answer to the user.
        return _UNREACHABLE


def _looks_like_an_image_entry(entry: object) -> bool:
    """Whether one array element could be an image URL.

    Mirrors `json_url._entry_url`'s tolerance — a bare string, or an
    object with a `url` key — without importing it, because that
    function returns a parsed pair this caller has no use for. Counting
    only the entries that would actually become pictures is what makes
    `"12 pictures found"` a number the user can trust: a 40-element
    array of which 12 are usable must not report 40.
    """
    if isinstance(entry, str):
        return entry.strip().startswith(source_settings.ALLOWED_URL_SCHEMES)
    if isinstance(entry, Mapping):
        for key in ("url", "image_url", "href"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip().startswith(
                source_settings.ALLOWED_URL_SCHEMES
            ):
                return True
    return False


# -- display picker ---------------------------------------------


@dataclasses.dataclass(frozen=True)
class DisplayOption:
    """One attached display, as the picker renders it."""

    name: str
    width: int
    height: int
    is_main: bool = False
    #: Whether the resolution heuristic thinks this is the View.
    probably_view: bool = False

    @property
    def title(self) -> str:
        """`960 x 960 — probably your View`.

        The suffix is appended, never substituted: the resolution stays
        visible on the guessed row so the user can check the guess rather
        than take it on trust.
        """
        text = f"{self.name} — {self.width} x {self.height}"
        if self.probably_view:
            text += " — probably your View"
        elif self.is_main:
            text += " — your main screen"
        return text


def display_options(
    screens: Sequence[Mapping[str, Any]],
    expected: tuple[int, int] = (960, 960),
) -> list[DisplayOption]:
    """Build the picker's list. **Never filters**.

    "Display picking never blocks... The heuristic sorts the list; it
    does not gate it." So every attached display is returned, the guess
    is a flag rather than a filter, and a machine where nothing matches
    still gets a full list to choose from by hand.

    Sorted guess-first, then non-main before main. The main screen goes
    last because it is the one display a user can be certain is *not*
    the View — they are looking at the picker on it.
    """
    options = []
    for screen in screens:
        if not isinstance(screen, Mapping):
            continue
        width = screen.get("width")
        height = screen.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        is_main = bool(screen.get("is_main"))
        options.append(
            DisplayOption(
                name=str(screen.get("name") or "Display"),
                width=width,
                height=height,
                is_main=is_main,
                probably_view=(not is_main and (width, height) == expected),
            )
        )
    options.sort(key=lambda o: (not o.probably_view, o.is_main, o.name))
    return options


def nothing_matched_note(expected: tuple[int, int] = (960, 960)) -> str:
    """"If nothing matches, the list is still populated and states
    what was looked for.\""""
    return (
        f"None of these is {expected[0]} x {expected[1]}, which is what the "
        f"View reports. Pick it by hand if you know which one it is."
    )


# -- login checkboxes -------------------------------------------


class AgentState(enum.Enum):
    """What `launchctl` says about one agent.

    Three states, not two, and the third is the reason this exists.
    the checkboxes must reflect *actual* launchctl state
    because "macOS Login Items can disable an agent behind the app's
    back, producing a checkbox that reads 'on' while nothing runs" — and
    the honest rendering of "I could not ask" is neither on nor off.
    """

    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    UNKNOWN = "unknown"

    @property
    def checked(self) -> bool:
        return self is AgentState.LOADED


def agent_state(
    label: str,
    runner: Callable[[list[str]], tuple[int, str]],
) -> AgentState:
    """Ask launchctl whether `label` is loaded in the GUI domain.

    `launchctl print` rather than parsing `launchctl list`: `list`
    reports a job that is loaded-but-not-running identically to one that
    is absent in some releases, and the checkbox is about whether the
    agent is *installed and enabled*, which is exactly what `print`
    answers. A non-zero exit means not loaded; anything that stops the
    subprocess running at all is UNKNOWN rather than a guess in either
    direction.

    The runner is injected because the alternative is a test that needs
    a live launchd domain, and because the *decision* here — which exit
    code means what — is the part worth pinning down.
    """
    try:
        code, output = runner(["print", label])
    except Exception:  # noqa: BLE001 - a checkbox never takes the window down
        return AgentState.UNKNOWN
    if code == 0:
        # A loaded job that is merely not running still counts as on:
        # `Quit` leaves the UI agent loaded, and unchecking the box for
        # that would tell the user their login item is gone when it is
        # not.
        return AgentState.LOADED
    lowered = str(output).lower()
    if "could not find" in lowered or "no such process" in lowered or code in (3, 113):
        return AgentState.NOT_LOADED
    return AgentState.UNKNOWN


def login_checkbox_note(state: AgentState) -> str:
    """The line under a checkbox whose state could not be established.

    Empty for the two definite answers: a checkbox that is simply right
    needs no caption, and a permanent explanatory line under a working
    control is the clutter the title precedence spends a paragraph avoiding.
    """
    if state is AgentState.UNKNOWN:
        return "Couldn't check this with launchctl. It may be out of date."
    return ""


# -- the settings document ---------------------------------------------


def settings_document(
    previous: Mapping[str, Any] | None,
    *,
    source: SourceSettings,
    rotation_interval_s: float,
    shuffle: bool,
    schedule: BlankSchedule,
) -> dict[str, Any]:
    """The document to write, merged over whatever is already there.

    Merge, not replace, for one reason: v1 is additive-only,
    so a key this build does not understand is a key a *newer* build
    might, and dropping it because a settings window round-tripped the
    file would break the guarantee on the first Save. It also preserves
    the two knobs the settings UI deliberately cut — `cache_max` and
    `fade_duration_s` — which "stay in the config file for the one person
    who cares" and would otherwise be silently reset by a window that
    does not show them.

    The legacy flat `image_studio_base_url`/`pool` keys are left exactly
    as they are. They are `source_settings.migrate_flat_keys`' input, and
    the explicit `source` block written here already outranks them; the source block
    puts their removal in Step 6's sweep, not here.
    """
    document: dict[str, Any] = {}
    if isinstance(previous, Mapping):
        document.update(previous)
    document["source"] = source.to_dict()
    document["rotation_interval_s"] = float(rotation_interval_s)
    document["shuffle"] = bool(shuffle)
    document["blank_schedule"] = schedule.to_dict()
    return document


def schedule_from_fields(
    enabled: bool, start_text: object, end_text: object, previous: BlankSchedule
) -> BlankSchedule:
    """Build a schedule from the two text fields.

    An unparseable field keeps the previous value rather than resetting
    to a default — the user is typing into it, and `9:` is a state every
    valid entry passes through. `blank_schedule.parse_minute` returns
    None for exactly that case, and this is the one place that decides
    what None means.
    """
    start = blank_schedule.parse_minute(start_text)
    end = blank_schedule.parse_minute(end_text)
    return BlankSchedule(
        enabled=bool(enabled),
        start_minute=previous.start_minute if start is None else start,
        end_minute=previous.end_minute if end is None else end,
    )


# -- status block -----------------------------------------------


def format_timestamp(value: object, now: float | None = None) -> str:
    """A status timestamp as "3 minutes ago".

    Relative rather than absolute because every consumer of this block
    is asking a relative question — is it working *now*, did it break
    *recently*. An absolute clock time makes the reader do the
    subtraction, and gets it wrong across midnight.
    """
    import time as _time

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return "never"
    moment = _time.time() if now is None else float(now)
    seconds = moment - float(value)
    if seconds < 0:
        return "just now"
    if seconds < 45:
        return "just now"
    if seconds < 90:
        return "a minute ago"
    minutes = seconds / 60.0
    if minutes < 45:
        return f"{round(minutes)} minutes ago"
    hours = minutes / 60.0
    if hours < 36:
        return "an hour ago" if round(hours) == 1 else f"{round(hours)} hours ago"
    return f"{round(hours / 24.0)} days ago"


def status_lines(
    status: Mapping[str, Any] | None,
    *,
    blank_state: str = "",
    now: float | None = None,
) -> list[tuple[str, str]]:
    """Status block, as label/value pairs.

    "Status: last refresh, pictures available, last error, source,
    effective blank state, and the backlight note." The
    backlight note is not in this list — it is a paragraph, not a row,
    and the window renders it as one.

    Every value degrades to a readable string rather than being omitted:
    a status block with rows that appear and disappear is one whose
    layout jumps, and the absence of a row is not something a reader can
    interpret.
    """
    data = status if isinstance(status, Mapping) else {}
    error = data.get("last_error")
    rows = [
        ("Last checked", format_timestamp(data.get("last_poll_at"), now)),
        ("Pictures available", str(_as_count(data.get("image_count")))),
        ("Coming from", str(data.get("source_label") or "Not set up yet")),
        ("Last problem", str(error) if isinstance(error, str) and error else "None"),
    ]
    if blank_state:
        rows.append(("Blanking", blank_state))
    return rows


def _as_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


__all__ = [
    "BACKLIGHT_NOTE",
    "DEFAULT_INTERVAL_S",
    "INTERVAL_CHOICES",
    "MAX_PROBE_BYTES",
    "ORDER_CHOICES",
    "POOL_CHOICES",
    "PROBE_TIMEOUT_S",
    "SORT_ORDER_CHOICES",
    "SOURCE_ROWS",
    "SOURCE_SUBLABELS",
    "AgentState",
    "DisplayOption",
    "Outcome",
    "SourceForm",
    "TestResult",
    "agent_state",
    "display_options",
    "format_timestamp",
    "interval_index",
    "interval_seconds",
    "login_checkbox_note",
    "nothing_matched_note",
    "order_index",
    "pool_index",
    "probe",
    "probe_folder",
    "probe_image_server",
    "probe_json_url",
    "schedule_from_fields",
    "settings_document",
    "sort_order_index",
    "status_lines",
]
