"""Content-addressed artifact store on the local filesystem.

Every file is stored once under ``<root>/ab/cd/<sha256>``, where ``ab`` and ``cd`` are the
first two byte pairs of its SHA-256. The name *is* the hash, so any later edit to a file
is detectable by re-hashing it (``evidence-room verify``, M5). Files are written to a
temporary name, fsynced and renamed into place, then made read-only.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int
    relpath: str  # relative to the store root, e.g. "ab/cd/abcd…"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relpath_for(digest: str) -> str:
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, digest: str) -> Path:
        return self.root / relpath_for(digest)

    def put(self, data: bytes) -> StoredBlob:
        digest = sha256_hex(data)
        target = self.path_for(digest)
        # Identical content is stored once. An existing file that no longer matches its
        # name has been damaged or edited; replace it with the bytes we hold now.
        if not (target.exists() and self.verify(digest)):
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                if target.exists():
                    target.chmod(0o644)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            target.chmod(0o444)
        return StoredBlob(digest, len(data), relpath_for(digest))

    def get(self, digest: str) -> bytes:
        return self.path_for(digest).read_bytes()

    def verify(self, digest: str) -> bool:
        """True when the stored file exists and still hashes to its name."""
        path = self.path_for(digest)
        if not path.is_file():
            return False
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == digest

    def delete(self, digest: str) -> bool:
        """Remove a stored file (used by purge, M5). Returns False if it was not there."""
        path = self.path_for(digest)
        if not path.exists():
            return False
        path.chmod(0o644)
        path.unlink()
        return True
