"""
Google Drive upload helper for the production pipeline.

Supports TWO credential types (auto-detected from JSON):

1. OAuth2 Client (type: "installed" or "web") — Desktop flow
   - First run opens a browser for Google login (one-time)
   - Saves token to gdrive_token.json for future runs
   - Files uploaded as user's own account (uses user's storage quota)

2. Service Account (type: "service_account")
   - No browser login needed (headless)
   - Requires impersonation: set GDRIVE_IMPERSONATE_EMAIL env var
     to upload as a real user (service accounts have NO storage quota)

Setup for OAuth2 (recommended):
1. Google Cloud Console → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (type: Desktop App)
3. Download JSON → save in pipeline folder as credentials.json
"""
import json
import logging
import os
import time
import threading
from pathlib import Path
from datetime import datetime

from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# Thread-local storage for Drive service
_thread_local = threading.local()

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Module-level shared credentials
_shared_creds = None
_creds_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Drive file cache: batch-loads all filenames in a folder at startup
# so we can check for duplicates locally instead of 80K API calls.
# ---------------------------------------------------------------------------
class DriveFileCache:
    """Thread-safe cache of filename → file_id for a Drive folder.
    
    Loads all file names in one paginated API call (~5 seconds for 100K files),
    then checks are instant O(1) dict lookups.
    """
    def __init__(self, credentials_path: str, folder_id: str):
        self._lock = threading.Lock()
        self._files = {}  # filename -> file_id
        self._folder_id = folder_id
        self._credentials_path = credentials_path
        self._load()
    
    def _load(self):
        """Fetch all filenames in the folder via paginated API calls."""
        service = _get_service(self._credentials_path)
        query = f"'{self._folder_id}' in parents and trashed=false"
        page_token = None
        total = 0
        while True:
            resp = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=1000,  # max per page
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                self._files[f["name"]] = f["id"]
                total += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if total > 0:
            print(f"  Drive cache: {total} files already in folder (checked in 1 API call).")
    
    def exists(self, filename: str) -> bool:
        """Check if file exists (O(1) dict lookup, no API call)."""
        with self._lock:
            return filename in self._files
    
    def get_id(self, filename: str) -> str | None:
        """Get file ID by name, or None."""
        with self._lock:
            return self._files.get(filename)
    
    def add(self, filename: str, file_id: str):
        """Record a newly uploaded file."""
        with self._lock:
            self._files[filename] = file_id
    
    def remove(self, filename: str):
        """Remove a file from cache (after deletion)."""
        with self._lock:
            self._files.pop(filename, None)


# Module-level cache instance (set by init_cache)
_drive_cache: DriveFileCache | None = None


def init_cache(credentials_path: str, folder_id: str) -> DriveFileCache:
    """Initialize the Drive file cache for a folder. Call once at run start."""
    global _drive_cache
    _drive_cache = DriveFileCache(credentials_path, folder_id)
    return _drive_cache


# ---------------------------------------------------------------------------
# Auth and service helpers
# ---------------------------------------------------------------------------
def _detect_credential_type(credentials_path: str) -> str:
    """Auto-detect whether the JSON is OAuth2 client or service account."""
    with open(credentials_path) as f:
        data = json.load(f)
    if data.get("type") == "service_account":
        return "service_account"
    if "installed" in data or "web" in data:
        return "oauth2"
    raise ValueError(f"Unrecognized credential file format: {credentials_path}")


def _authenticate(credentials_path: str, token_path: str = None):
    """Authenticate based on credential type. Returns credentials object."""
    global _shared_creds
    
    with _creds_lock:
        if _shared_creds and _shared_creds.valid:
            return _shared_creds
        
        cred_type = _detect_credential_type(credentials_path)
        
        if cred_type == "service_account":
            creds = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=SCOPES
            )
            impersonate_email = os.environ.get("GDRIVE_IMPERSONATE_EMAIL")
            if impersonate_email:
                creds = creds.with_subject(impersonate_email)
        else:
            if not token_path:
                token_path = str(Path(credentials_path).parent / "gdrive_token.json")
            
            creds = None
            if Path(token_path).exists():
                creds = UserCredentials.from_authorized_user_file(token_path, SCOPES)
            
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                    creds = flow.run_local_server(port=0)
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
        
        _shared_creds = creds
        return creds


def _get_service(credentials_path: str):
    """Get or create a thread-local Drive service instance."""
    if not hasattr(_thread_local, "service"):
        creds = _authenticate(credentials_path)
        _thread_local.service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _thread_local.service


# ---------------------------------------------------------------------------
# Folder operations
# ---------------------------------------------------------------------------
def create_run_folder(credentials_path: str, parent_folder_id: str, folder_name: str = None) -> str:
    """Create a subfolder in the parent Drive folder."""
    if not folder_name:
        folder_name = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    
    service = _get_service(credentials_path)
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }
    folder = service.files().create(body=file_metadata, fields="id").execute()
    return folder.get("id")


def find_or_create_folder(credentials_path: str, parent_folder_id: str, folder_name: str) -> str:
    """Find an existing folder by name, or create it if it doesn't exist."""
    service = _get_service(credentials_path)
    query = (
        f"name='{folder_name}' and "
        f"'{parent_folder_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false"
    )
    results = service.files().list(q=query, fields="files(id,name)", pageSize=1).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    return create_run_folder(credentials_path, parent_folder_id, folder_name)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def upload_file(
    file_path: Path,
    folder_id: str,
    credentials_path: str,
    delete_after_upload: bool = True,
) -> bool:
    """Upload a file to Google Drive and optionally delete the local copy.
    
    If a file with the same name exists on Drive (checked via cache, no API call),
    it is deleted first and re-uploaded (handles partial ZIPs from interrupted runs).
    """
    if not file_path.exists():
        return False
    
    try:
        service = _get_service(credentials_path)
        
        # Check cache for existing file (instant, no API call)
        if _drive_cache and _drive_cache.exists(file_path.name):
            old_id = _drive_cache.get_id(file_path.name)
            if old_id:
                service.files().delete(fileId=old_id).execute()
                _drive_cache.remove(file_path.name)
        
        file_metadata = {
            "name": file_path.name,
            "parents": [folder_id],
        }
        
        # Retry to handle OneDrive/antivirus file locks on Windows
        media = None
        for attempt in range(3):
            try:
                media = MediaFileUpload(
                    str(file_path),
                    mimetype="application/zip",
                    resumable=True,
                    chunksize=10 * 1024 * 1024,
                )
                break
            except OSError:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise
        
        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,name,size",
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
        
        if response and response.get("id"):
            # Update cache with new file
            if _drive_cache:
                _drive_cache.add(file_path.name, response["id"])
            if delete_after_upload:
                deleted = False
                for attempt in range(3):
                    try:
                        file_path.unlink()
                        deleted = True
                        break
                    except OSError:
                        if attempt < 2:
                            time.sleep(1)
                if not deleted:
                    logging.warning(f"Uploaded {file_path.name} but failed to delete local copy (file locked)")
            return True
        return False
        
    except Exception as e:
        print(f"\n  UPLOAD ERROR ({file_path.name}): {e}")
        return False


def upload_and_cleanup(
    file_path: Path,
    folder_id: str,
    credentials_path: str,
) -> bool:
    """Upload a ZIP to Drive. Thread-safe wrapper.

    Local deletion is skipped (VM file-lock issues). Use a separate
    cleanup pass after uploads complete if disk space is needed.
    """
    return upload_file(file_path, folder_id, credentials_path, delete_after_upload=False)
