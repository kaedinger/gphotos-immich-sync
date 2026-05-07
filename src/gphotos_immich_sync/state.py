import json
from datetime import datetime, timezone
from pathlib import Path


def _key(name: str) -> str:
    return name.casefold()


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def is_reconciled(
    state: dict, name: str, google_count: int | None, immich_count: int
) -> bool:
    if google_count is None:
        return False
    entry = state.get(_key(name))
    if not entry:
        return False
    return (
        entry.get("google_count") == google_count
        and entry.get("immich_count") == immich_count
    )


def mark_reconciled(
    state: dict, name: str, google_count: int | None, immich_count: int
) -> None:
    if google_count is None:
        return
    state[_key(name)] = {
        "name": name,
        "google_count": google_count,
        "immich_count": immich_count,
        "reconciled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def gaps(state: dict) -> list[tuple[str, int, int]]:
    """Reconciled albums where Google has more than Immich — i.e. partner-
    contributed photos the Picker API can't return. (name, google, immich)."""
    out: list[tuple[str, int, int]] = []
    for entry in state.values():
        g = entry.get("google_count")
        i = entry.get("immich_count")
        n = entry.get("name")
        if isinstance(g, int) and isinstance(i, int) and isinstance(n, str) and g > i:
            out.append((n, g, i))
    out.sort(key=lambda t: t[0].casefold())
    return out
