import json
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .google_picker import GooglePickerClient, PickedItem
from .immich import ImmichAsset, ImmichClient
from .matcher import AlbumAborted, Matcher, Prompter

try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None  # type: ignore


class QuitRequested(Exception):
    """Raised when the user asks to end the run mid-flight."""


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy via PowerShell. Silent failure if unavailable."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
            input=text,
            text=True,
            encoding="utf-8",
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
            f" (narrowed from {narrowed_from} via album context)"
            if narrowed_from != len(candidates)
            else ""
        )
        print(f"\n  [ambiguous] {item.filename}{suffix}")
        print(
            f"    Picker: created={item.create_time or '?'}  "
            f"{item.width or '?'}x{item.height or '?'}  "
            f"{item.camera_make or ''} {item.camera_model or ''}".rstrip()
        )
        for i, a in enumerate(candidates, 1):
            tail = (
                f"{self.immich_base_url}/photos/{a.id}"
                if self.immich_base_url
                else f"id:{a.id}"
            )
            print(
                f"    [{i}] created={a.file_created_at or '?'}  "
                f"{a.width or '?'}x{a.height or '?'}  "
                f"{_format_size(a.file_size)}  "
                f"{(a.camera_make or '')} {(a.camera_model or '')}".rstrip()
                + f"  {tail}"
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
    existing = {a.name.casefold() for a in immich.list_albums()}

    missing = [a for a in google_albums if a.name.casefold() not in existing]
    already = len(google_albums) - len(missing)

    print(
        f"\nGoogle albums: {len(google_albums)}    "
        f"Already in Immich: {already}    "
        f"Missing: {len(missing)}\n"
    )
    if not missing:
        print("Nothing to sync. Done.")
        return

    print(f"Will process {len(missing)} missing album(s) automatically.")
    print("Each opens the picker in your browser; the album name is copied to the")
    print("clipboard. Pick photos, then the script auto-matches and creates the")
    print("album. While waiting for picks: Ctrl+C skips this album, Q ends the run.")
    print("You'll only be prompted when a photo is missing or ambiguous.\n")

    progress = Progress()
    matcher = Matcher(immich, CliPrompter(progress, immich.base_url))

    try:
        for i, a in enumerate(missing, 1):
            print(f"[{i}/{len(missing)}] {_album_label(a)}")
            _sync_album(google, immich, matcher, progress, a)
            print()
    except QuitRequested:
        progress.clear()
        print("\nQuit requested. Stopping after current album cleanup.")

    print("Done.")


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


def _sync_album(
    google: GooglePickerClient,
    immich: ImmichClient,
    matcher: Matcher,
    progress: Progress,
    album: AlbumEntry,
) -> None:
    session = google.create_session()
    try:
        _open_picker(session.picker_uri, album.name)

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
            return

        progress.clear()
        items = google.list_picked_items(session)
        print(f"  Picked {len(items)} item(s).")
        if not items:
            print("  Nothing picked. Skipping.")
            return

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
            progress.done("  Album aborted by user. Not creating album.")
            return

        matched = result.matched
        skipped = [r for r in result.resolutions if r.status != "matched"]

        print(
            f"  Matched: {len(matched)} / {len(items)}    Skipped: {len(skipped)}"
        )
        for r in skipped[:10]:
            print(f"    - {r.item.filename}  [{r.status}]")
        if len(skipped) > 10:
            print(f"    … and {len(skipped) - 10} more")

        if not matched:
            print("  No matches; not creating album.")
            return

        album_obj = immich.create_album(album.name)
        immich.add_assets(album_obj.id, [r.asset.id for r in matched if r.asset])
        print(f"  Created album '{album_obj.name}' with {len(matched)} asset(s).")
    finally:
        google.delete_session(session)


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
