"""Upload resumes to your own Google Drive.

Uses the OAuth token in GOOGLE_OAUTH_TOKEN_JSON ({client_id, client_secret,
refresh_token, token_uri}) with the drive.file scope: the program can only see
the files and folders it created, nothing else in your Drive. A service account
can't be used here because it can't own files in a personal Drive.
"""

from __future__ import annotations

import io
import json
import os

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"


def connect():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    raw = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_OAUTH_TOKEN_JSON is not set (see README: 履歷自動化)")
    info = json.loads(raw)
    info.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def folder(service, path: list[str]) -> str:
    """Id of the folder at `path` under My Drive, creating any part that's missing."""
    parent = "root"
    for name in path:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        found = service.files().list(
            q=f"name = '{safe}' and mimeType = '{FOLDER}' and '{parent}' in parents and trashed = false",
            fields="files(id)", spaces="drive").execute().get("files", [])
        if found:
            parent = found[0]["id"]
        else:
            parent = service.files().create(
                body={"name": name, "mimeType": FOLDER, "parents": [parent]}, fields="id").execute()["id"]
    return parent


def upload(service, folder_id: str, name: str, data: bytes, mime: str, as_google_doc: bool = False) -> str:
    """Upload a file and return its link. With `as_google_doc`, HTML becomes an editable Google Doc."""
    from googleapiclient.http import MediaIoBaseUpload

    body = {"name": name, "parents": [folder_id]}
    if as_google_doc:
        body["mimeType"] = GOOGLE_DOC
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    return service.files().create(body=body, media_body=media, fields="webViewLink").execute()["webViewLink"]
