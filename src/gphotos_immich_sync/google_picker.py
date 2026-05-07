import time
from dataclasses import dataclass
from pathlib import Path

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

PICKER_BASE = "https://photospicker.googleapis.com/v1"
SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]


@dataclass
class PickerSession:
    id: str
    picker_uri: str
    poll_interval_s: float
    expire_time: str | None


@dataclass
class PickedItem:
    id: str
    filename: str
    create_time: str | None
    mime_type: str
    width: int | None
    height: int | None
    camera_make: str | None
    camera_model: str | None
    media_type: str
    raw: dict


class GooglePickerClient:
    def __init__(self, credentials_path: Path, token_path: Path):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._creds: Credentials | None = None

    def authenticate(self) -> None:
        creds: Credentials | None = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if not creds or not creds.valid:
            refreshed = False
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    refreshed = True
                except RefreshError:
                    # Refresh token expired or revoked. Apps in OAuth
                    # "Testing" publishing status get 7-day refresh tokens,
                    # so this hits routinely. Drop the stale cache and
                    # fall through to a fresh interactive flow.
                    print(
                        "  Cached Google token rejected (likely expired — "
                        "Test-mode apps get 7-day refresh tokens). "
                        "Re-authenticating…"
                    )
                    self.token_path.unlink(missing_ok=True)
                    creds = None
            if not refreshed:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            self.token_path.write_text(creds.to_json())
        self._creds = creds

    def _auth_headers(self) -> dict:
        if not self._creds:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        if not self._creds.valid and self._creds.expired and self._creds.refresh_token:
            self._creds.refresh(Request())
            self.token_path.write_text(self._creds.to_json())
        return {"Authorization": f"Bearer {self._creds.token}"}

    def create_session(self) -> PickerSession:
        r = requests.post(f"{PICKER_BASE}/sessions", headers=self._auth_headers(), json={})
        r.raise_for_status()
        data = r.json()
        return PickerSession(
            id=data["id"],
            picker_uri=data["pickerUri"],
            poll_interval_s=_parse_duration_seconds(
                data.get("pollingConfig", {}).get("pollInterval"), default=5.0
            ),
            expire_time=data.get("expireTime"),
        )

    def get_session(self, session_id: str) -> dict:
        r = requests.get(f"{PICKER_BASE}/sessions/{session_id}", headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    def wait_for_picks(self, session: PickerSession, on_tick=None) -> None:
        poll_interval = max(session.poll_interval_s, 5.0)
        start = time.monotonic()
        last_poll = -poll_interval  # poll once on first iteration
        while True:
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                data = self.get_session(session.id)
                if data.get("mediaItemsSet"):
                    return
                last_poll = now
            if on_tick:
                on_tick(now - start)
            time.sleep(0.25)

    def list_picked_items(self, session: PickerSession) -> list[PickedItem]:
        items: list[PickedItem] = []
        page_token: str | None = None
        while True:
            params: dict = {"sessionId": session.id, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(
                f"{PICKER_BASE}/mediaItems", headers=self._auth_headers(), params=params
            )
            r.raise_for_status()
            data = r.json()
            for it in data.get("mediaItems", []):
                items.append(_parse_item(it))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    def delete_session(self, session: PickerSession) -> None:
        try:
            requests.delete(
                f"{PICKER_BASE}/sessions/{session.id}", headers=self._auth_headers()
            )
        except requests.RequestException:
            pass


def _parse_item(it: dict) -> PickedItem:
    mf = it.get("mediaFile", {}) or {}
    meta = mf.get("mediaFileMetadata", {}) or {}
    return PickedItem(
        id=it.get("id", ""),
        filename=mf.get("filename", ""),
        create_time=it.get("createTime"),
        mime_type=mf.get("mimeType", ""),
        width=_to_int(meta.get("width")),
        height=_to_int(meta.get("height")),
        camera_make=meta.get("cameraMake"),
        camera_model=meta.get("cameraModel"),
        media_type=it.get("type", "PHOTO"),
        raw=it,
    )


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_duration_seconds(s: str | None, default: float) -> float:
    if not s:
        return default
    try:
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return default
