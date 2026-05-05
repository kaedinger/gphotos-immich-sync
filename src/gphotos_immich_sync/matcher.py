from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Protocol

from .google_picker import PickedItem
from .immich import ImmichAsset, ImmichClient

CONTEXT_DATE_PADDING = timedelta(days=7)

# Candidate offsets (in seconds) considered when looking for an
# album-wide time skew. ±1h covers the common case of one side storing
# local time as UTC, including DST flips that push a whole import off by
# an hour.
ALBUM_OFFSET_CANDIDATES_S = (3600, -3600)

# Don't bother offering the offset shortcut for tiny ambiguous sets — a
# couple of incidental matches under a shift aren't a pattern.
ALBUM_OFFSET_THRESHOLD = 5


class AlbumAborted(Exception):
    """Raised by a prompter to abandon the current album mid-resolution."""


@dataclass
class Resolution:
    item: PickedItem
    asset: ImmichAsset | None
    status: str  # "matched" | "skipped" | "no_candidates" | "user_skipped"


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
    # offer_offset_resolution returns True to apply the offset to the
    # ambiguous items in this album, False to fall through to per-item
    # prompts.
    def offer_offset_resolution(
        self, offset_seconds: int, would_resolve: int, total_ambiguous: int
    ) -> bool: ...


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

        # Phase 2: classify each item without prompting yet. Narrowing
        # happens here so the summary reflects post-narrowing counts.
        ctx = _build_context([c[0] for _, c in prelim if len(c) == 1])

        auto_matched: list[tuple[PickedItem, ImmichAsset]] = []
        no_candidates: list[PickedItem] = []
        ambiguous: list[tuple[PickedItem, list[ImmichAsset], int]] = []

        for item, cands in prelim:
            if len(cands) == 1:
                auto_matched.append((item, cands[0]))
                continue
            if not cands:
                no_candidates.append(item)
                continue
            narrowed = _narrow(cands, ctx, item)
            if len(narrowed) == 1:
                auto_matched.append((item, narrowed[0]))
                continue
            shown = narrowed if narrowed else cands
            shown = sorted(shown, key=lambda a: _candidate_distance(item, a))
            ambiguous.append((item, shown, len(cands)))

        # Phase 2.5: a consistent ±1h skew between picker and Immich
        # timestamps (typically a timezone mismatch on one side) leaves
        # whole albums stuck on per-photo prompts. If many ambiguous
        # items would resolve uniquely under the same shift, offer it
        # once for this album.
        if ambiguous:
            detected = _detect_album_time_offset(ambiguous)
            if detected is not None:
                offset_seconds, resolutions = detected
                if len(resolutions) >= ALBUM_OFFSET_THRESHOLD and self.prompter.offer_offset_resolution(
                    offset_seconds, len(resolutions), len(ambiguous)
                ):
                    resolved_by_id = {id(item): asset for item, asset in resolutions}
                    kept: list[tuple[PickedItem, list[ImmichAsset], int]] = []
                    for item, shown, narrowed_from in ambiguous:
                        asset = resolved_by_id.get(id(item))
                        if asset is not None:
                            auto_matched.append((item, asset))
                        else:
                            kept.append((item, shown, narrowed_from))
                    ambiguous = kept

        if on_summary:
            on_summary(len(auto_matched), len(no_candidates), len(ambiguous))

        # Phase 3: prompt for the unresolved.
        result = ResolutionResult()
        for item, asset in auto_matched:
            result.resolutions.append(Resolution(item, asset, "matched"))

        for item in no_candidates:
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


def _build_context(assets: Iterable[ImmichAsset]) -> dict:
    dates: list[datetime] = []
    cameras: set[tuple[str, str]] = set()
    for a in assets:
        if a.file_created_at:
            try:
                dates.append(_parse_iso(a.file_created_at))
            except ValueError:
                pass
        make = (a.camera_make or "").strip()
        model = (a.camera_model or "").strip()
        if make or model:
            cameras.add((make, model))
    return {
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "cameras": cameras,
    }


def _narrow(
    cands: list[ImmichAsset], ctx: dict, item: PickedItem | None = None
) -> list[ImmichAsset]:
    out = list(cands)

    if ctx.get("min_date") and ctx.get("max_date"):
        lo = ctx["min_date"] - CONTEXT_DATE_PADDING
        hi = ctx["max_date"] + CONTEXT_DATE_PADDING
        in_range: list[ImmichAsset] = []
        for a in out:
            if not a.file_created_at:
                continue
            try:
                dt = _parse_iso(a.file_created_at)
            except ValueError:
                continue
            if lo <= dt <= hi:
                in_range.append(a)
        if in_range:
            out = in_range

    if ctx.get("cameras"):
        cams = ctx["cameras"]
        same_cam = [
            a
            for a in out
            if ((a.camera_make or "").strip(), (a.camera_model or "").strip()) in cams
        ]
        if same_cam:
            out = same_cam

    # Per-item timestamp narrowing: prefer candidates whose Immich
    # fileCreatedAt matches the picked item's createTime down to the
    # second. Sub-second precision differs between Google and Immich (and
    # rounds inconsistently), so we truncate to the second.
    if item and item.create_time:
        try:
            target = _parse_iso(item.create_time).replace(microsecond=0)
        except ValueError:
            target = None
        if target is not None:
            same_second: list[ImmichAsset] = []
            for a in out:
                if not a.file_created_at:
                    continue
                try:
                    a_dt = _parse_iso(a.file_created_at).replace(microsecond=0)
                except ValueError:
                    continue
                if a_dt == target:
                    same_second.append(a)
            if same_second:
                out = same_second

    return out


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _detect_album_time_offset(
    ambiguous: list[tuple[PickedItem, list[ImmichAsset], int]],
) -> tuple[int, list[tuple[PickedItem, ImmichAsset]]] | None:
    """Find the ± shift that uniquely resolves the most ambiguous items.

    For each candidate offset, count items where shifting the picker
    timestamp by that offset hits exactly one candidate at second
    precision. Return the dominant offset and its resolutions, or None
    if no offset resolves anything.
    """
    by_offset: dict[int, list[tuple[PickedItem, ImmichAsset]]] = {
        o: [] for o in ALBUM_OFFSET_CANDIDATES_S
    }

    for item, shown, _narrowed_from in ambiguous:
        if not item.create_time:
            continue
        try:
            picker_ts = _parse_iso(item.create_time).replace(microsecond=0)
        except ValueError:
            continue

        for offset in ALBUM_OFFSET_CANDIDATES_S:
            target = picker_ts + timedelta(seconds=offset)
            unique: ImmichAsset | None = None
            ambiguous_at_target = False
            for a in shown:
                if not a.file_created_at:
                    continue
                try:
                    a_dt = _parse_iso(a.file_created_at).replace(microsecond=0)
                except ValueError:
                    continue
                if a_dt == target:
                    if unique is not None:
                        ambiguous_at_target = True
                        break
                    unique = a
            if unique is not None and not ambiguous_at_target:
                by_offset[offset].append((item, unique))

    best_offset = max(ALBUM_OFFSET_CANDIDATES_S, key=lambda o: len(by_offset[o]))
    best = by_offset[best_offset]
    if not best:
        return None
    return best_offset, best


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
