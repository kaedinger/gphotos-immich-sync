import json
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import state
from .config import Config
from .google_picker import GooglePickerClient, PickedItem
from .immich import ImmichAlbum, ImmichAsset, ImmichClient
from .matcher import AlbumAborted, Matcher, Prompter, ResolutionResult

try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None  # type: ignore


class QuitRequested(Exception):
    """Raised when the user asks to end the run mid-flight."""


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy via PowerShell. Silent failure if unavailable."""
    # PowerShell stdin defaults to the active Windows code page (cp1252
    # on most German/English installs), which mangles UTF-8 — ä becomes
    # Ã¤, em-dashes become â€”, etc. Force InputEncoding to UTF-8 so
    # non-ASCII album names land on the clipboard intact.
    ps = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
        "$input | Set-Clipboard"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            input=text.encode("utf-8"),
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False


def _check_quit_key() -> None:
    """Drain the keyboard buffer; raise QuitRequested on 'q'/'Q'."""
    if not msvcrt:
        return
    while msvcrt.kbhit():
        try:
            ch = msvcrt.getwch()
        except OSError:
            return
        if ch and ch.lower() == "q":
            raise QuitRequested()


@dataclass(frozen=True)
class AlbumEntry:
    name: str
    count: int | None
    shared: bool
    unknown: bool = False


# Single-line in-place progress. The prompter must call clear() before
# any print/input so its output isn't tangled with a stale progress line.
class Progress:
    def __init__(self) -> None:
        self._active = False
        self._last_len = 0

    def write(self, text: str) -> None:
        pad = max(0, self._last_len - len(text))
        sys.stdout.write("\r" + text + (" " * pad))
        sys.stdout.flush()
        self._last_len = len(text)
        self._active = True

    def clear(self) -> None:
        if self._active:
            sys.stdout.write("\r" + (" " * self._last_len) + "\r")
            sys.stdout.flush()
            self._active = False
            self._last_len = 0

    def done(self, text: str = "") -> None:
        self.clear()
        if text:
            print(text)


class CliPrompter(Prompter):
    def __init__(self, progress: Progress, immich_base_url: str = "") -> None:
        self.progress = progress
        self.immich_base_url = immich_base_url.rstrip("/")

    def no_candidates(self, item: PickedItem) -> str:
        self.progress.clear()
        print(f"  [no match] {item.filename}")
        camera = (
            f"{(item.camera_make or '').strip()} "
            f"{(item.camera_model or '').strip()}"
        ).strip()
        parts = [
            "    Picker:",
            f"created={item.create_time or '?'}",
            f"{item.width or '?'}x{item.height or '?'}",
        ]
        if camera:
            parts.append(camera)
        gphoto_url = _gphoto_date_url(item.create_time)
        if gphoto_url:
            parts.append(gphoto_url)
        print("  ".join(parts))
        choice = input("    [s]kip / [a]bort album? [s] ").strip().lower()
        return "abort" if choice == "a" else "skip"

    def disambiguate(
        self,
        item: PickedItem,
        candidates: list[ImmichAsset],
        narrowed_from: int,
    ) -> "ImmichAsset | None | str":
        self.progress.clear()
        suffix = (
            f" (narrowed from {narrowed_from} by camera/time filter)"
            if narrowed_from != len(candidates)
            else ""
        )
        print(f"\n  [ambiguous] {item.filename}{suffix}")

        gphoto_url = _gphoto_date_url(item.create_time) or ""
        picker_camera = (
            f"{(item.camera_make or '').strip()} "
            f"{(item.camera_model or '').strip()}"
        ).strip()
        # (label, created, pixels, size, camera, url)
        rows: list[tuple[str, str, str, str, str, str]] = [(
            "Picker:",
            item.create_time or "?",
            f"{item.width or '?'}x{item.height or '?'}",
            "",
            picker_camera,
            gphoto_url,
        )]
        for i, a in enumerate(candidates, 1):
            url = (
                f"{self.immich_base_url}/photos/{a.id}"
                if self.immich_base_url
                else f"id:{a.id}"
            )
            cam = (
                f"{(a.camera_make or '').strip()} "
                f"{(a.camera_model or '').strip()}"
            ).strip()
            rows.append((
                f"[{i}]",
                a.file_created_at or "?",
                f"{a.width or '?'}x{a.height or '?'}",
                _format_size(a.file_size),
                cam,
                url,
            ))

        label_w = max(len(r[0]) for r in rows)
        created_w = max(len(r[1]) for r in rows)
        pixels_w = max(len(r[2]) for r in rows)
        size_w = max(len(r[3]) for r in rows)
        camera_w = max(len(r[4]) for r in rows)

        for label, created, pixels, size, cam, url in rows:
            print(
                f"    {label:<{label_w}}  "
                f"created={created:<{created_w}}  "
                f"{pixels:<{pixels_w}}  "
                f"{size:<{size_w}}  "
                f"{cam:<{camera_w}}  "
                f"{url}"
            )
        print("    [s] skip this photo  [a] abort album")
        while True:
            raw = input(f"    Pick [1-{len(candidates)}/s/a]: ").strip().lower()
            if raw == "s":
                return None
            if raw == "a":
                return "abort"
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(candidates):
                    return candidates[idx - 1]
            print("    Invalid choice.")


def run() -> None:
    try:
        cfg = Config.load()
    except (RuntimeError, FileNotFoundError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(2)

    if not cfg.google_albums_file.exists():
        print(_albums_file_missing_message(cfg.google_albums_file), file=sys.stderr)
        sys.exit(2)

    try:
        google_albums = _load_album_list(cfg.google_albums_file)
    except (json.JSONDecodeError, ValueError) as e:
        print(
            f"Could not parse {cfg.google_albums_file}: {e}", file=sys.stderr
        )
        sys.exit(2)

    google = GooglePickerClient(cfg.google_credentials_path, cfg.google_token_path)
    print("Authenticating with Google…")
    google.authenticate()

    print("Validating Immich API key…")
    immich = ImmichClient(cfg.immich_url, cfg.immich_api_key)
    immich.validate()

    print("Loading existing Immich albums…")
    existing_by_name: dict[str, ImmichAlbum] = {}
    for ia in immich.list_albums():
        existing_by_name.setdefault(ia.name.casefold(), ia)

    state_path = cfg.google_token_path.parent / "album-state.json"
    album_state = state.load(state_path)

    # Process everything in alphabetical order so resuming a partial run is
    # predictable.
    google_albums.sort(key=lambda a: a.name.casefold())

    # Partition into:
    #   to_process: albums that need work in the main pass — either missing
    #     from Immich (create), or present but with a count mismatch (resync
    #     to add photos that became available in Immich since last run).
    #   reconciled: counts mismatch but a prior run confirmed the gap is
    #     unrecoverable (partner-contributed photos the Picker can't return).
    #     Skipped silently; gaps surface in the end-of-run manifest.
    #   counts_match: present in Immich with matching counts; skipped in the
    #     main pass but available for the optional id-check after.
    to_process: list[tuple[AlbumEntry, ImmichAlbum | None]] = []
    reconciled: list[tuple[AlbumEntry, ImmichAlbum]] = []
    counts_match: list[tuple[AlbumEntry, ImmichAlbum]] = []
    for a in google_albums:
        ia = existing_by_name.get(a.name.casefold())
        if ia is None:
            to_process.append((a, None))
        elif a.count is None or a.count != ia.asset_count:
            if state.is_reconciled(album_state, a.name, a.count, ia.asset_count):
                reconciled.append((a, ia))
            else:
                to_process.append((a, ia))
        else:
            counts_match.append((a, ia))

    create_n = sum(1 for _, ia in to_process if ia is None)
    resync_n = len(to_process) - create_n

    print(
        f"\nGoogle albums: {len(google_albums)}    "
        f"Missing: {create_n}    "
        f"Count mismatch: {resync_n}    "
        f"Reconciled (gap): {len(reconciled)}    "
        f"Counts match: {len(counts_match)}\n"
    )

    progress = Progress()
    matcher = Matcher(immich, CliPrompter(progress, immich.base_url))

    if to_process:
        print(
            f"Will process {len(to_process)} album(s) alphabetically "
            f"({create_n} to create, {resync_n} to resync for missing photos)."
        )
        print("Each opens the picker in your browser; the album name is copied to the")
        print("clipboard. Pick photos, then the script auto-matches and either creates")
        print("the album or adds newly-available photos to it. While waiting for picks:")
        print("Ctrl+C skips this album, Q ends the run. You'll only be prompted for")
        print("missing or ambiguous photos.\n")

        try:
            for i, (a, ia) in enumerate(to_process, 1):
                if ia is None:
                    print(f"[{i}/{len(to_process)}] {_album_label(a)}  (create)")
                    _sync_album(
                        google, immich, matcher, progress, a,
                        album_state, state_path,
                    )
                else:
                    g = a.count if a.count is not None else "?"
                    print(
                        f"[{i}/{len(to_process)}] {_album_label(a)}  "
                        f"(resync; Google:{g}  Immich:{ia.asset_count})"
                    )
                    _resync_album(
                        google, immich, matcher, progress, a, ia,
                        album_state, state_path,
                    )
                print()
        except QuitRequested:
            progress.clear()
            print("\nQuit requested. Stopping after current album cleanup.")
            _print_manual_download_manifest(album_state)
            print("Done.")
            return
    else:
        print("Nothing actionable in the main pass.\n")

    # Optional after-pass: counts can match while contents differ (e.g. one
    # photo swapped for another). Offer a deeper id-level check against the
    # picker for albums that looked clean by count alone.
    if counts_match:
        answer = input(
            f"Also id-check {len(counts_match)} album(s) where counts already match? [y/N] "
        ).strip().lower()
        if answer == "y":
            print()
            try:
                for i, (a, ia) in enumerate(counts_match, 1):
                    print(
                        f"[{i}/{len(counts_match)}] {_album_label(a)}  "
                        f"(id-check; Google:{a.count}  Immich:{ia.asset_count})"
                    )
                    _resync_album(
                        google, immich, matcher, progress, a, ia,
                        album_state, state_path,
                    )
                    print()
            except QuitRequested:
                progress.clear()
                print("\nQuit requested. Stopping after current album cleanup.")

    _print_manual_download_manifest(album_state)
    print("Done.")


def _print_manual_download_manifest(album_state: dict) -> None:
    gaps = state.gaps(album_state)
    if not gaps:
        return
    print("\nReconciled albums with download gaps")
    print("(partner-contributed photos the Picker API can't return — download")
    print(" the full albums manually from Google Photos and import to Immich):")
    name_w = max(len(n) for n, _, _ in gaps)
    for name, g, i in gaps:
        print(
            f"  {name:<{name_w}}  Google:{g}  Immich:{i}  Missing:{g - i}"
        )


def _gphoto_date_url(create_time: str | None) -> str | None:
    # Google Photos search refuses to honor literal filenames (tokenizes
    # and drops middle parts), but a YYYY-MM-DD path jumps to that day's
    # grid where the user can spot the photo by eye.
    if not create_time or len(create_time) < 10:
        return None
    date = create_time[:10]
    if date[4] != "-" or date[7] != "-":
        return None
    return f"https://photos.google.com/search/{date}"


def _format_size(b: int | None) -> str:
    if b is None:
        return "?"
    n = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _album_label(a: AlbumEntry) -> str:
    parts = [f"'{a.name}'"]
    if a.unknown:
        parts.append("(unknown — open URL to inspect)")
    elif a.count is not None:
        parts.append(
            f"({a.count} item{'s' if a.count != 1 else ''}"
            f"{', shared' if a.shared else ''})"
        )
    elif a.shared:
        parts.append("(shared)")
    return " ".join(parts)


def _pick_and_match(
    google: GooglePickerClient,
    matcher: Matcher,
    progress: Progress,
    album_name: str,
    abort_message: str,
    known_member_ids: set[str] | None = None,
) -> ResolutionResult | None:
    """Open picker, wait for picks, and resolve them against Immich.

    Returns the full ResolutionResult on success, or None if the user
    skipped (Ctrl+C), aborted (AlbumAborted), or picked nothing.
    Manages the picker session lifecycle.
    """
    session = google.create_session()
    try:
        _open_picker(session.picker_uri, album_name)

        spin_chars = "|/-\\"

        def on_tick(elapsed: float) -> None:
            _check_quit_key()
            idx = int(elapsed * 4) % len(spin_chars)
            mm, ss = divmod(int(elapsed), 60)
            progress.write(
                f"  {spin_chars[idx]}  waiting for picks  ({mm:02d}:{ss:02d})"
            )

        try:
            google.wait_for_picks(session, on_tick=on_tick)
        except KeyboardInterrupt:
            progress.done("  Skipped (Ctrl+C).")
            return None

        progress.clear()
        items = google.list_picked_items(session)
        print(f"  Picked {len(items)} item(s).")
        if not items:
            print("  Nothing picked. Skipping.")
            return None

        def match_progress(idx: int, total: int, filename: str | None) -> None:
            if filename is None:
                progress.done(f"  Searched {total} filename(s) in Immich.")
            else:
                label = filename if len(filename) <= 50 else "…" + filename[-49:]
                progress.write(f"  Searching {idx + 1}/{total}: {label}")

        def match_summary(matched: int, missing: int, ambiguous: int) -> None:
            print(
                f"  Auto-matched: {matched}    "
                f"No match: {missing}    "
                f"Ambiguous: {ambiguous}"
            )
            unresolved = missing + ambiguous
            if unresolved > 0:
                print(f"  Will now prompt for {unresolved} photo(s).")

        try:
            result = matcher.resolve(
                items, progress=match_progress, on_summary=match_summary
            )
        except AlbumAborted:
            progress.done(abort_message)
            return None

        matched = result.matched
        skipped = [r for r in result.resolutions if r.status != "matched"]

        print(
            f"  Matched: {len(matched)} / {len(items)}    Skipped: {len(skipped)}"
        )
        for r in skipped[:10]:
            print(f"    - {r.item.filename}  [{r.status}]")
        if len(skipped) > 10:
            print(f"    … and {len(skipped) - 10} more")

        return result
    finally:
        google.delete_session(session)


def _sync_album(
    google: GooglePickerClient,
    immich: ImmichClient,
    matcher: Matcher,
    progress: Progress,
    album: AlbumEntry,
    album_state: dict,
    state_path: Path,
) -> None:
    result = _pick_and_match(
        google, matcher, progress, album.name,
        abort_message="  Album aborted by user. Not creating album.",
    )
    if result is None:
        return
    matched = result.matched
    if not matched:
        print("  No matches; not creating album.")
        return

    album_obj = immich.create_album(album.name)
    immich.add_assets(album_obj.id, [r.asset.id for r in matched if r.asset])
    print(f"  Created album '{album_obj.name}' with {len(matched)} asset(s).")

    skipped_n = len(result.resolutions) - len(matched)
    if skipped_n == 0:
        state.mark_reconciled(album_state, album.name, album.count, len(matched))
        state.save(state_path, album_state)


def _resync_album(
    google: GooglePickerClient,
    immich: ImmichClient,
    matcher: Matcher,
    progress: Progress,
    album: AlbumEntry,
    existing: ImmichAlbum,
    album_state: dict,
    state_path: Path,
) -> None:
    existing_ids = immich.get_album_asset_ids(existing.id)
    print(f"  Immich album currently has {len(existing_ids)} asset(s).")

    result = _pick_and_match(
        google, matcher, progress, album.name,
        abort_message="  Album aborted by user. Not modifying album.",
        known_member_ids=existing_ids,
    )
    if result is None:
        return
    matched = result.matched
    if not matched:
        print("  No matches; nothing to add.")
        return

    to_add = [r for r in matched if r.asset and r.asset.id not in existing_ids]
    already_present = len(matched) - len(to_add)
    print(
        f"  Already in album: {already_present}    "
        f"To add: {len(to_add)}"
    )
    if to_add:
        immich.add_assets(existing.id, [r.asset.id for r in to_add if r.asset])
        print(f"  Added {len(to_add)} asset(s) to '{existing.name}'.")
    else:
        print("  Album already contains all matched photos.")

    skipped_n = len(result.resolutions) - len(matched)
    if to_add == [] and skipped_n == 0:
        state.mark_reconciled(
            album_state, album.name, album.count, len(existing_ids)
        )
        state.save(state_path, album_state)


def _open_picker(picker_uri: str, album_name: str) -> None:
    copied = _copy_to_clipboard(album_name)
    opened = False
    try:
        opened = webbrowser.open(picker_uri, new=2)
    except webbrowser.Error:
        pass
    if opened:
        line = "  Browser opened to picker."
    else:
        line = f"  Picker URL: {picker_uri}"
    if copied:
        line += "  Album name on clipboard."
    print(line)
    print("  Pick the album in Google Photos.  [Ctrl+C] skip album   [Q] quit")


def _load_album_list(path: Path) -> list[AlbumEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    seen: set[str] = set()
    out: list[AlbumEntry] = []
    for raw in data:
        if isinstance(raw, str):
            entry = AlbumEntry(name=raw.strip(), count=None, shared=False)
        elif isinstance(raw, dict) and isinstance(raw.get("name"), str):
            count = raw.get("count")
            entry = AlbumEntry(
                name=raw["name"].strip(),
                count=count if isinstance(count, int) else None,
                shared=bool(raw.get("shared", False)),
                unknown=bool(raw.get("unknown", False)),
            )
        else:
            raise ValueError(
                "expected each entry to be a string or "
                "{'name': str, 'count'?: int, 'shared'?: bool, 'unknown'?: bool}"
            )
        if not entry.name or entry.name.casefold() in seen:
            continue
        seen.add(entry.name.casefold())
        out.append(entry)
    return out


def _albums_file_missing_message(path: Path) -> str:
    snippet = Path(__file__).resolve().parents[2] / "scripts" / "list_google_albums.js"
    return (
        f"Album list not found at {path}.\n"
        "\n"
        "To generate it:\n"
        "  1. Open https://photos.google.com/albums in your browser.\n"
        "  2. Open DevTools (F12) -> Console.\n"
        f"  3. Paste the contents of {snippet} and press Enter.\n"
        "     The script auto-scrolls and collects album names.\n"
        f"  4. Save the clipboard contents as {path.name} in {path.parent}.\n"
        "\n"
        "Override the path with the GOOGLE_ALBUMS_FILE env var."
    )
