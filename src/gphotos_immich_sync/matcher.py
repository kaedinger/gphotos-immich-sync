from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .google_picker import PickedItem
from .immich import ImmichAsset, ImmichClient

# Offsets considered when comparing picker time to Immich time during the
# strict auto-match check.
#   0:     exact / minute-cut alignment
#   ±3600: CET/CEST DST flip, or one side stored in local while the other
#          is one hour off (common when EXIF is taken as UTC)
#   ±7200: UTC stored as CEST or vice versa
_AUTO_MATCH_OFFSETS_S = (0, 3600, -3600, 7200, -7200)

# Cap for the ambiguous display window.
_AMBIGUOUS_TIME_WINDOW = timedelta(days=1)


class AlbumAborted(Exception):
    """Raised by a prompter to abandon the current album mid-resolution."""


@dataclass
class Resolution:
    item: PickedItem
    asset: ImmichAsset | None
    status: str  # "matched" | "user_skipped"


@dataclass
class ResolutionResult:
    resolutions: list[Resolution] = field(default_factory=list)

    @property
    def matched(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.status == "matched" and r.asset]


class Prompter(Protocol):
    # no_candidates returns "skip" or "abort".
    def no_candidates(self, item: PickedItem) -> str: ...
    # disambiguate returns the chosen asset, None to skip the photo,
    # or the string "abort" to abort the whole album.
    def disambiguate(
        self,
        item: PickedItem,
        candidates: list[ImmichAsset],
        narrowed_from: int,
    ) -> "ImmichAsset | None | str": ...


class Matcher:
    def __init__(self, immich: ImmichClient, prompter: Prompter):
        self.immich = immich
        self.prompter = prompter

    def resolve(
        self,
        items: list[PickedItem],
        progress=None,
        on_summary=None,
    ) -> ResolutionResult:
        # Phase 1: search Immich for every item.
        prelim: list[tuple[PickedItem, list[ImmichAsset]]] = []
        for idx, item in enumerate(items):
            if progress:
                progress(idx, len(items), item.filename)
            prelim.append((item, self.immich.search_by_filename(item.filename)))
        if progress:
            progress(len(items), len(items), None)

        # Phase 2: classify each item.
        auto_matched: list[tuple[PickedItem, ImmichAsset]] = []
        no_match: list[PickedItem] = []
        ambiguous: list[tuple[PickedItem, list[ImmichAsset], int]] = []

        for item, cands in prelim:
            if not cands:
                no_match.append(item)
                continue

            strict = _strict_match(item, cands)
            if len(strict) == 1:
                auto_matched.append((item, strict[0]))
                continue

            shown = _ambiguous_candidates(item, cands)
            if not shown:
                no_match.append(item)
                continue

            shown = sorted(shown, key=lambda a: _candidate_distance(item, a))
            ambiguous.append((item, shown, len(cands)))

        if on_summary:
            on_summary(len(auto_matched), len(no_match), len(ambiguous))

        # Phase 3: prompt for the unresolved.
        result = ResolutionResult()
        for item, asset in auto_matched:
            result.resolutions.append(Resolution(item, asset, "matched"))

        for item in no_match:
            action = self.prompter.no_candidates(item)
            if action == "abort":
                raise AlbumAborted()
            result.resolutions.append(Resolution(item, None, "user_skipped"))

        for item, shown, narrowed_from in ambiguous:
            pick = self.prompter.disambiguate(item, shown, narrowed_from=narrowed_from)
            if pick == "abort":
                raise AlbumAborted()
            if pick is None:
                result.resolutions.append(Resolution(item, None, "user_skipped"))
            else:
                result.resolutions.append(Resolution(item, pick, "matched"))

        return result


def _camera_present(make: str | None, model: str | None) -> bool:
    return bool((make or "").strip() or (model or "").strip())


def _camera_match(item: PickedItem, asset: ImmichAsset) -> bool:
    if not _camera_present(item.camera_make, item.camera_model):
        return False
    if not _camera_present(asset.camera_make, asset.camera_model):
        return False
    return (
        (item.camera_make or "").strip() == (asset.camera_make or "").strip()
        and (item.camera_model or "").strip() == (asset.camera_model or "").strip()
    )


def _pixel_match(item: PickedItem, asset: ImmichAsset) -> bool:
    if not (item.width and item.height and asset.width and asset.height):
        return False
    return item.width == asset.width and item.height == asset.height


def _time_match_strict(picker_dt: datetime, asset_dt: datetime) -> bool:
    """True if asset matches picker under any allowed offset, with either
    exact-second precision OR minute-cut alignment (one side has its
    seconds zeroed)."""
    a = asset_dt.replace(microsecond=0)
    a_min = a.replace(second=0)
    for offset in _AUTO_MATCH_OFFSETS_S:
        p = (picker_dt + timedelta(seconds=offset)).replace(microsecond=0)
        if p == a:
            return True
        if (p.second == 0 or a.second == 0) and p.replace(second=0) == a_min:
            return True
    return False


def _strict_match(item: PickedItem, cands: list[ImmichAsset]) -> list[ImmichAsset]:
    """Candidates qualifying for auto-match: camera + pixel + strict time.

    When neither the picker item nor the candidate has any camera info
    (e.g. old AVIs, screenshots), the camera requirement is vacuously
    satisfied — pixel + strict-time + uniqueness still have to hold."""
    if not item.create_time:
        return []
    try:
        picker_dt = _parse_iso(item.create_time)
    except ValueError:
        return []
    item_has_camera = _camera_present(item.camera_make, item.camera_model)
    out: list[ImmichAsset] = []
    for a in cands:
        asset_has_camera = _camera_present(a.camera_make, a.camera_model)
        if item_has_camera or asset_has_camera:
            if not _camera_match(item, a):
                continue
        if not _pixel_match(item, a):
            continue
        if not a.file_created_at:
            continue
        try:
            asset_dt = _parse_iso(a.file_created_at)
        except ValueError:
            continue
        if _time_match_strict(picker_dt, asset_dt):
            out.append(a)
    return out


def _ambiguous_candidates(
    item: PickedItem, cands: list[ImmichAsset]
) -> list[ImmichAsset]:
    """Candidates to surface in the disambiguate prompt: camera matches
    (when both sides have camera info) and within ±1 day of the picker
    timestamp. The 1-day cap drops same-filename collisions from unrelated
    dates; the camera filter is waived if either side lacks camera info
    (e.g. screenshots, videos with stripped EXIF) so the prompt still has
    something to show."""
    item_has_camera = _camera_present(item.camera_make, item.camera_model)

    picker_dt: datetime | None = None
    if item.create_time:
        try:
            picker_dt = _parse_iso(item.create_time)
        except ValueError:
            pass

    out: list[ImmichAsset] = []
    for a in cands:
        if item_has_camera and _camera_present(a.camera_make, a.camera_model):
            if not _camera_match(item, a):
                continue
        if picker_dt is not None and a.file_created_at:
            try:
                asset_dt = _parse_iso(a.file_created_at)
            except ValueError:
                continue
            if abs((asset_dt - picker_dt).total_seconds()) > _AMBIGUOUS_TIME_WINDOW.total_seconds():
                continue
        out.append(a)
    return out


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _candidate_distance(
    item: PickedItem, asset: ImmichAsset
) -> tuple[float, float]:
    """Sort key for ambiguous candidates: closest timestamp first, then
    closest pixel count. Picker API doesn't expose file size, so we use
    width*height as the size proxy."""
    ts_dist = float("inf")
    if item.create_time and asset.file_created_at:
        try:
            ts_dist = abs(
                (_parse_iso(asset.file_created_at) - _parse_iso(item.create_time))
                .total_seconds()
            )
        except ValueError:
            pass
    px_dist = float("inf")
    if item.width and item.height and asset.width and asset.height:
        px_dist = abs(item.width * item.height - asset.width * asset.height)
    return (ts_dist, px_dist)
