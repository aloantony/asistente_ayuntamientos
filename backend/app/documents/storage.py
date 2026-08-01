from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings

ALLOWED_DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}

# Imágenes de marca (escudo del municipio). Van al mismo almacén que los
# documentos pero bajo su propio prefijo, sin pasar por el modelo Document:
# éste exige proyecto y su control de acceso es el del proyecto.
ALLOWED_BRAND_IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/svg+xml",
    "image/webp",
}

# Archivo del Ayuntamiento: fototeca, crónicas, himno y documentos históricos.
# Comparte el volumen bajo su propio prefijo, sin pasar por el modelo Document,
# que exige proyecto. Ver ADR-034.
ALLOWED_ARCHIVE_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "audio/mpeg",
    "audio/ogg",
}

DEFAULT_EXTENSIONS_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}

CHUNK_SIZE_BYTES = 1024 * 1024


class DocumentStorageError(Exception):
    pass


class UnsupportedDocumentContentTypeError(DocumentStorageError):
    pass


class DocumentTooLargeError(DocumentStorageError):
    pass


class EmptyDocumentError(DocumentStorageError):
    pass


class InvalidStorageKeyError(DocumentStorageError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    original_filename: str
    stored_filename: str
    storage_backend: str
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str


class LocalStorageService:
    storage_backend = "local"

    def __init__(self, root: str | Path | None = None) -> None:
        configured_root = root if root is not None else settings.document_storage_root
        self.root = Path(configured_root).expanduser().resolve()

    def save_upload_file(
        self,
        upload_file: UploadFile,
        *,
        organization_id: int,
        project_id: int,
        max_bytes: int,
    ) -> StoredUpload:
        return self._save_upload(
            upload_file,
            allowed_content_types=ALLOWED_DOCUMENT_CONTENT_TYPES,
            key_prefix=f"organizations/{organization_id}/projects/{project_id}",
            max_bytes=max_bytes,
        )

    def save_branding_file(
        self,
        upload_file: UploadFile,
        *,
        organization_id: int,
        max_bytes: int,
    ) -> StoredUpload:
        return self._save_upload(
            upload_file,
            allowed_content_types=ALLOWED_BRAND_IMAGE_CONTENT_TYPES,
            key_prefix=f"organizations/{organization_id}/brand",
            max_bytes=max_bytes,
        )

    def save_archive_file(
        self,
        upload_file: UploadFile,
        *,
        organization_id: int,
        max_bytes: int,
    ) -> StoredUpload:
        return self._save_upload(
            upload_file,
            allowed_content_types=ALLOWED_ARCHIVE_CONTENT_TYPES,
            key_prefix=f"organizations/{organization_id}/archive",
            max_bytes=max_bytes,
        )

    def _save_upload(
        self,
        upload_file: UploadFile,
        *,
        allowed_content_types: set[str],
        key_prefix: str,
        max_bytes: int,
    ) -> StoredUpload:
        content_type = normalize_content_type(upload_file.content_type)
        if content_type not in allowed_content_types:
            raise UnsupportedDocumentContentTypeError(content_type)

        original_filename = normalize_original_filename(upload_file.filename)
        stored_filename = build_stored_filename(original_filename, content_type)
        storage_key = build_storage_key(
            key_prefix=key_prefix,
            stored_filename=stored_filename,
        )
        destination = self.resolve_storage_key(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)

        digest = sha256()
        size_bytes = 0

        try:
            upload_file.file.seek(0)
        except OSError:
            pass

        try:
            with destination.open("wb") as output_file:
                while chunk := upload_file.file.read(CHUNK_SIZE_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise DocumentTooLargeError

                    digest.update(chunk)
                    output_file.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        if size_bytes == 0:
            destination.unlink(missing_ok=True)
            raise EmptyDocumentError

        return StoredUpload(
            original_filename=original_filename,
            stored_filename=stored_filename,
            storage_backend=self.storage_backend,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum_sha256=digest.hexdigest(),
        )

    def open_file(self, storage_key: str):
        return self.resolve_storage_key(storage_key).open("rb")

    def delete_file(self, storage_key: str) -> None:
        self.resolve_storage_key(storage_key).unlink(missing_ok=True)

    def resolve_storage_key(self, storage_key: str) -> Path:
        key_path = PurePosixPath(storage_key)
        if key_path.is_absolute() or any(part in {"", ".", ".."} for part in key_path.parts):
            raise InvalidStorageKeyError(storage_key)

        path = (self.root / Path(*key_path.parts)).resolve()
        if not path.is_relative_to(self.root):
            raise InvalidStorageKeyError(storage_key)

        return path


def normalize_content_type(content_type: str | None) -> str:
    return (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()


def normalize_original_filename(filename: str | None) -> str:
    raw_filename = (filename or "document").replace("\x00", "").strip()
    posix_name = PurePosixPath(raw_filename).name
    windows_name = PureWindowsPath(posix_name).name
    normalized = windows_name.strip() or "document"
    return normalized[:255]


def build_stored_filename(original_filename: str, content_type: str) -> str:
    suffix = Path(original_filename).suffix.lower()
    if len(suffix) > 20:
        suffix = ""
    if not suffix:
        suffix = DEFAULT_EXTENSIONS_BY_CONTENT_TYPE.get(content_type, "")

    return f"{uuid4().hex}{suffix}"


def build_storage_key(*, key_prefix: str, stored_filename: str) -> str:
    safe_stored_filename = PurePosixPath(stored_filename).name
    if safe_stored_filename != stored_filename or not safe_stored_filename:
        raise InvalidStorageKeyError(stored_filename)

    return f"{key_prefix}/{safe_stored_filename}"
