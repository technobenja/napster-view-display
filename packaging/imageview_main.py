"""Single bundle executable — argv dispatch.

`setup(app=[a, b])` produces two separate `.app` bundles, not one bundle
with two binaries. There is no py2app option for the shape an earlier
draft assumed, so the one executable branches on `sys.argv` instead:

    Contents/MacOS/ImageView              -> menu bar UI
    Contents/MacOS/ImageView --display    -> display agent
    Contents/MacOS/ImageView --selftest   -> build verification, exits

The LaunchAgent's `ProgramArguments` becomes that path plus `--display`.
`open -a` stays ruled out: it returns immediately, so launchd sees a job
that instantly exits, and LaunchServices refuses to start a second
instance of a running bundle ID.

`argv_emulation` is `False` in setup.py — it works via Carbon Apple
Events, hangs at startup under launchd, and would corrupt the
`--display` flag this module dispatches on.

Both branches are now real: Step 2 built the menu bar, so the no-arg
path runs it. The spike's "UI not built yet" placeholder is gone.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _add_source_dirs() -> None:
    """Make the `display` and `ui` packages importable from a checkout.

    These are real packages, so what has to be on `sys.path`
    is the **repo root** — the directory that *contains* them — not the
    two directories themselves. Putting `display/` on the path instead
    would resurrect the flat imports the conversion removed and shadow
    the package form.

    Inside the bundle this does nothing: py2app byte-compiles both
    packages into `Contents/Resources/lib/python3NN.zip`, which is
    already on `sys.path`, and no repo root exists next to the frozen
    executable. Outside the bundle it is what makes

        display/.venv/bin/python3 packaging/imageview_main.py

    behave identically to the shipped binary — which matters, because
    that is how the dispatch gets exercised without a five-minute build
    in between.
    """
    repo = Path(__file__).resolve().parent.parent
    if (repo / "display" / "__init__.py").is_file() and str(repo) not in sys.path:
        sys.path.insert(0, str(repo))


def _run_display() -> int:
    """Run the display agent unchanged (`display/app.py:main`)."""
    from display import app

    app.main()
    return 0


def _run_ui() -> int:
    """Run the menu bar (`ui/menubar.py:main`).

    This is the no-arg path, which means it is what a double-click and
    what the `dev.viewlab.imageview.ui` LaunchAgent both get. `menubar.main`
    takes its own single-instance lock on `~/.viewlab/ui.lock` and
    returns 0 if another menu bar already holds it, so those two launch
    routes cannot produce two status items.
    """
    from ui import menubar

    return menubar.main()


def _run_selftest() -> int:
    """Prove the bundled interpreter can do HTTPS.

    This is the `_ssl` question: the build Python is Homebrew's, whose
    `_ssl...so` links `/opt/homebrew/opt/openssl@3/...`. macholib
    usually copies and rewrites those, and when it misses one it fails
    only on a machine without Homebrew — and it fails on HTTPS, which is
    the whole point of the URL sources. `certifi`'s `cacert.pem` is a
    data file rather than a module, so a missing recipe shows up here as
    a certificate error and no other symptom.
    """
    import ssl

    print(f"python:  {sys.version.split()[0]}")
    print(f"prefix:  {sys.prefix}")
    print(f"openssl: {ssl.OPENSSL_VERSION}")

    import certifi

    ca = certifi.where()
    print(f"certifi: {ca}")

    from pathlib import Path

    if not Path(ca).is_file():
        print("FAIL: certifi cacert.pem is not present in the bundle")
        return 1

    import httpx

    response = httpx.get("https://www.apple.com/library/test/success.html", timeout=30.0)
    print(f"https:   {response.status_code} ({len(response.content)} bytes)")

    # PyObjC's lazy framework loading defeats modulegraph's static
    # analysis, so confirm the explicitly-named modules really landed.
    import AppKit
    import Foundation
    import objc

    print(f"objc:    {objc.__version__}")
    print(f"appkit:  {AppKit.NSApplication is not None}")
    print(f"found'n: {Foundation.NSObject is not None}")

    # fifth root: read-only seed config shipped inside the bundle.
    from display import paths

    seed = paths.bundled_config_dir()
    print(f"seed:    {seed} exists={seed.is_dir()}")

    if response.status_code != 200:
        print(f"FAIL: unexpected HTTPS status {response.status_code}")
        return 1

    print("SELFTEST OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    _add_source_dirs()

    if "--display" in args:
        return _run_display()
    if "--selftest" in args:
        return _run_selftest()
    return _run_ui()


if __name__ == "__main__":
    sys.exit(main())
