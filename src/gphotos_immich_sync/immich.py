from dataclasses import dataclass

import requests


@dataclass
class ImmichAsset:
    id: str
    original_file_name: str
    file_created_at: str | None
    camera_make: str | None
    camera_model: str | None
    width: int | None
    height: int | None
    latitude: float | None
    longitude: float | None
    file_size: int | None
    type: str
    raw: dict


@dataclass
class ImmichAlbum:
    id: str
    name: str


class ImmichClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key,
                "Accept": "application/json",
            }
        )

    def validate(self) -> None:
        r = self.session.post(f"{self.base_url}/api/auth/validateToken")
        r.raise_for_status()
        data = r.json()
        if not data.get("authStatus"):
            raise RuntimeError("Immich API key invalid")

    def search_by_filename(self, filename: str) -> list[ImmichAsset]:
        body = {
            "originalFileName": filename,
            # Without withExif=true the response omits exifInfo, so size /
            # camera / dimensions all come back as None and disambiguation
            # has nothing to work with.
            "withExif": True,
        }
        r = self.session.post(f"{self.base_url}/api/search/metadata", json=body)
        r.raise_for_status()
        data = r.json()
        items = (data.get("assets") or {}).get("items") or []
        return [_parse_asset(a) for a in items]

    def list_albums(self) -> list[ImmichAlbum]:
        r = self.session.get(f"{self.base_url}/api/albums")
        r.raise_for_status()
        return [
            ImmichAlbum(id=a["id"], name=a.get("albumName", ""))
            for a in r.json()
        ]

    def create_album(self, name: str, description: str = "") -> ImmichAlbum:
        body = {"albumName": name, "description": description}
        r = self.session.post(f"{self.base_url}/api/albums", json=body)
        r.raise_for_status()
        data = r.json()
        return ImmichAlbum(id=data["id"], name=data.get("albumName", name))

    def add_assets(self, album_id: str, asset_ids: list[str]) -> list[dict]:
        if not asset_ids:
            return []
        body = {"ids": asset_ids}
        r = self.session.put(f"{self.base_url}/api/albums/{album_id}/assets", json=body)
        r.raise_for_status()
        return r.json()


def _parse_asset(a: dict) -> ImmichAsset:
    exif = a.get("exifInfo") or {}
    return ImmichAsset(
        id=a["id"],
        original_file_name=a.get("originalFileName", ""),
        file_created_at=a.get("fileCreatedAt"),
        camera_make=exif.get("make"),
        camera_model=exif.get("model"),
        width=exif.get("exifImageWidth"),
        height=exif.get("exifImageHeight"),
        latitude=exif.get("latitude"),
        longitude=exif.get("longitude"),
        file_size=exif.get("fileSizeInByte"),
        type=a.get("type", "IMAGE"),
        raw=a,
    )
