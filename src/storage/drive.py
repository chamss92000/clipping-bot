"""Bloc 2 — Storage Google Drive + state.json.

⚠️ IMPORTANT — pourquoi OAuth et pas Service Account :
Un Service Account n'a **pas de quota de stockage** sur un Drive grand public :
tout fichier qu'il crée lui appartient, et il ne dispose pas des 15 Go gratuits
(=> 403 "storageQuotaExceeded"). Sur budget 0€, la bonne méthode est **OAuth
utilisateur avec refresh token** : les fichiers t'appartiennent et comptent sur
tes 15 Go. Consentement navigateur **une seule fois** en local (voir
`src/storage/authorize.py`), puis fonctionnement 100% headless (local + CI).

Le Service Account reste supporté en *fallback* mais ne fonctionne qu'avec un
**Shared Drive** (Google Workspace).

Ordre d'auth tenté :
  1. OAuth via env (CLIENT_ID + CLIENT_SECRET + REFRESH_TOKEN)  -> CI headless
  2. OAuth via token.json local                                -> dev après authorize
  3. Service Account (JSON brut ou fichier)                     -> Shared Drive only

Interface (contrat pour les autres blocs) :

    storage = DriveStorage.from_settings()
    state = storage.load_state(); ...; storage.save_state(state)
    fid = storage.upload(local_path, "clip.mp4", subdir="clips")
    storage.download(fid, local_path)
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("storage.drive")

# drive.file = scope NON-SENSIBLE (per-fichier) : pas de vérification Google,
# publiable en prod librement => refresh token stable (pas d'expiration 7j du
# mode Test). L'app ne voit/gère que les fichiers qu'ELLE crée — d'où le fait
# qu'on laisse l'app créer son propre dossier racine (GOOGLE_DRIVE_FOLDER_ID
# reste vide en pratique).
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_FOLDER_MIME = "application/vnd.google-apps.folder"
#: Erreurs HTTP considérées comme transitoires (on retry).
_TRANSIENT = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class State:
    """État persistant du pipeline (sérialisé dans state.json sur Drive)."""

    processed_uids: list[str] = field(default_factory=list)
    queue: list[dict[str, Any]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    uploads_this_month: dict[str, int] = field(default_factory=dict)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def is_processed(self, uid: str) -> bool:
        return uid in self.processed_uids

    def mark_processed(self, uid: str) -> None:
        if uid not in self.processed_uids:
            self.processed_uids.append(uid)

    def month_key(self, when: datetime | None = None) -> str:
        return (when or datetime.now(timezone.utc)).strftime("%Y-%m")

    def uploads_left(self, quota: int | None = None) -> int:
        quota = quota if quota is not None else settings.UPLOADPOST_MONTHLY_QUOTA
        used = self.uploads_this_month.get(self.month_key(), 0)
        return max(0, quota - used)

    def record_upload(self) -> None:
        k = self.month_key()
        self.uploads_this_month[k] = self.uploads_this_month.get(k, 0) + 1


class DriveError(RuntimeError):
    pass


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, HttpError) and getattr(exc, "status_code", None) in _TRANSIENT


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def load_credentials():
    """Résout des credentials Drive selon l'ordre documenté en tête de module."""
    # 1) OAuth via env (CI headless)
    if (
        settings.GOOGLE_OAUTH_CLIENT_ID
        and settings.GOOGLE_OAUTH_CLIENT_SECRET
        and settings.GOOGLE_OAUTH_REFRESH_TOKEN
    ):
        creds = UserCredentials(
            token=None,
            refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            token_uri=_TOKEN_URI,
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        log.info("Drive: OAuth via refresh token (env)")
        return creds

    # 2) OAuth via token.json local
    token_file = settings.GOOGLE_OAUTH_TOKEN_FILE
    if token_file and Path(token_file).exists():
        creds = UserCredentials.from_authorized_user_file(token_file, _SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                Path(token_file).write_text(creds.to_json(), encoding="utf-8")
            else:
                raise settings.ConfigError(
                    f"token.json ({token_file}) invalide/sans refresh_token — "
                    "relance `python -m src.storage.authorize`"
                )
        log.info("Drive: OAuth via token.json (local)")
        return creds

    # 3) Service Account (Shared Drive uniquement)
    sa_info: dict | None = None
    if settings.GOOGLE_DRIVE_CREDENTIALS:
        try:
            sa_info = json.loads(settings.GOOGLE_DRIVE_CREDENTIALS)
        except json.JSONDecodeError as exc:
            raise settings.ConfigError("GOOGLE_DRIVE_CREDENTIALS n'est pas un JSON valide") from exc
    elif settings.GOOGLE_DRIVE_CREDENTIALS_FILE and Path(settings.GOOGLE_DRIVE_CREDENTIALS_FILE).exists():
        sa_info = json.loads(Path(settings.GOOGLE_DRIVE_CREDENTIALS_FILE).read_text(encoding="utf-8"))
    if sa_info:
        log.warning("Drive: Service Account — ne fonctionne qu'avec un Shared Drive (Workspace)")
        return service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)

    raise settings.ConfigError(
        "Aucune credential Drive. Configure OAuth (recommandé) : "
        "`python -m src.storage.authorize`."
    )


# ---------------------------------------------------------------------------
# DriveStorage
# ---------------------------------------------------------------------------
class DriveStorage:
    def __init__(self, service, root_folder_id: str):
        self._svc = service
        self._root = root_folder_id
        self._subdir_cache: dict[str, str] = {}
        self._state_file_id: str | None = None

    @classmethod
    def from_settings(cls) -> "DriveStorage":
        creds = load_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        root = cls._resolve_root(service)
        log.info("Drive: prêt (dossier racine=%s)", root)
        return cls(service, root)

    # --- résolution du dossier racine ---
    @classmethod
    def _resolve_root(cls, service) -> str:
        if settings.GOOGLE_DRIVE_FOLDER_ID:
            return settings.GOOGLE_DRIVE_FOLDER_ID
        # Sinon on crée/retrouve un dossier nommé à la racine de "My Drive".
        name = settings.DRIVE_ROOT_FOLDER_NAME
        q = (
            f"name = '{name}' and mimeType = '{_FOLDER_MIME}' "
            f"and 'root' in parents and trashed = false"
        )
        resp = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = resp.get("files", [])
        if files:
            return files[0]["id"]
        created = service.files().create(
            body={"name": name, "mimeType": _FOLDER_MIME, "parents": ["root"]}, fields="id"
        ).execute()
        log.info("Drive: dossier racine '%s' créé (%s)", name, created["id"])
        return created["id"]

    # --- exécution robuste ---
    @retry(exceptions=(HttpError, ConnectionError, TimeoutError), attempts=4)
    def _execute(self, request):
        try:
            return request.execute()
        except HttpError as exc:
            if _is_transient(exc):
                raise
            log.error("Drive: erreur non transitoire : %s", exc)
            raise DriveError(str(exc)) from exc

    # --- sous-dossiers ---
    def _find_child(self, name: str, parent_id: str, mime: str | None = None) -> str | None:
        q = f"name = '{name}' and '{parent_id}' in parents and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        resp = self._execute(
            self._svc.files().list(
                q=q, fields="files(id, name)", pageSize=1,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            )
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def ensure_subdir(self, name: str) -> str:
        if not name:
            return self._root
        if name in self._subdir_cache:
            return self._subdir_cache[name]
        existing = self._find_child(name, self._root, _FOLDER_MIME)
        if existing:
            self._subdir_cache[name] = existing
            return existing
        meta = {"name": name, "mimeType": _FOLDER_MIME, "parents": [self._root]}
        created = self._execute(
            self._svc.files().create(body=meta, fields="id", supportsAllDrives=True)
        )
        fid = created["id"]
        self._subdir_cache[name] = fid
        log.info("Drive: sous-dossier '%s' créé (%s)", name, fid)
        return fid

    # --- upload / download ---
    def upload(self, local_path: str | Path, remote_name: str, subdir: str = "") -> str:
        local_path = Path(local_path)
        if not local_path.exists():
            raise DriveError(f"Fichier local introuvable : {local_path}")
        parent = self.ensure_subdir(subdir)
        media = MediaFileUpload(str(local_path), resumable=True)
        existing = self._find_child(remote_name, parent)
        if existing:
            file = self._execute(
                self._svc.files().update(
                    fileId=existing, media_body=media, fields="id", supportsAllDrives=True
                )
            )
            log.info("Drive: '%s' mis à jour (%s)", remote_name, file["id"])
        else:
            meta = {"name": remote_name, "parents": [parent]}
            file = self._execute(
                self._svc.files().create(
                    body=meta, media_body=media, fields="id", supportsAllDrives=True
                )
            )
            log.info("Drive: '%s' uploadé (%s)", remote_name, file["id"])
        return file["id"]

    def download(self, file_id: str, local_path: str | Path) -> Path:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        request = self._svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.FileIO(str(local_path), "wb")
        downloader = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.close()
        log.info("Drive: fichier %s téléchargé -> %s", file_id, local_path)
        return local_path

    # --- state.json ---
    def _state_id(self) -> str | None:
        if self._state_file_id is None:
            self._state_file_id = self._find_child(settings.STATE_FILENAME, self._root)
        return self._state_file_id

    def load_state(self) -> State:
        fid = self._state_id()
        if not fid:
            log.info("Drive: aucun %s existant — état initial", settings.STATE_FILENAME)
            return State()
        request = self._svc.files().get_media(fileId=fid, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        try:
            data = json.loads(buf.getvalue().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            log.error("Drive: %s corrompu — repart d'un état vide", settings.STATE_FILENAME)
            return State()
        log.info(
            "Drive: état chargé (%d traités, %d en queue)",
            len(data.get("processed_uids", [])),
            len(data.get("queue", [])),
        )
        return State.from_dict(data)

    def save_state(self, state: State) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
        fid = self._state_id()
        if fid:
            self._execute(
                self._svc.files().update(fileId=fid, media_body=media, fields="id", supportsAllDrives=True)
            )
        else:
            meta = {"name": settings.STATE_FILENAME, "parents": [self._root]}
            created = self._execute(
                self._svc.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True)
            )
            self._state_file_id = created["id"]
        log.info("Drive: état sauvegardé")


if __name__ == "__main__":  # python -m src.storage.drive  (test round-trip)
    settings.validate(["drive"])
    st = DriveStorage.from_settings()
    state = st.load_state()
    print("État chargé :", state)
    state.mark_processed("test:round-trip")
    st.save_state(state)
    print("État ré-sauvegardé sur Drive. Uploads restants ce mois :", state.uploads_left())
