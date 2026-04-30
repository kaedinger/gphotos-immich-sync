import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    immich_url: str
    immich_api_key: str
    google_credentials_path: Path
    google_token_path: Path
    google_albums_file: Path

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()

        immich_url = _required("IMMICH_URL").rstrip("/")
        immich_api_key = _required("IMMICH_API_KEY")

        creds = Path(os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")).expanduser()
        if not creds.is_absolute():
            creds = (Path.cwd() / creds).resolve()
        if not creds.exists():
            raise FileNotFoundError(
                f"Google OAuth credentials not found at {creds}. "
                "See .env.example for setup instructions."
            )

        token_env = os.environ.get("GOOGLE_TOKEN")
        if token_env:
            token = Path(token_env).expanduser()
        else:
            token = Path.home() / ".gphotos-immich-sync" / "token.json"
        token.parent.mkdir(parents=True, exist_ok=True)

        albums_file = Path(
            os.environ.get("GOOGLE_ALBUMS_FILE", "albums.json")
        ).expanduser()
        if not albums_file.is_absolute():
            albums_file = (Path.cwd() / albums_file).resolve()

        return cls(immich_url, immich_api_key, creds, token, albums_file)


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val
