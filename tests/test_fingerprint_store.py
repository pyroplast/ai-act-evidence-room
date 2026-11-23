"""Unit tests for fingerprints and the content-addressed store. No database needed."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evidence_room.fingerprint import fingerprint, normalise_location, subject_for
from evidence_room.store import ArtifactStore


@pytest.mark.parametrize(
    ("kind", "root", "target", "subject"),
    [
        ("page", "http://h/sites/x/", "http://h/sites/x/", ""),
        ("transcripts", "/data/in", "/data/in/02-late.json", "02-late.json"),
        ("transcripts", "/data/in/", "/data/in/sub/batch.jsonl#c-04", "sub/batch.jsonl#c-04"),
        ("transcripts", "/data/in/batch.jsonl", "/data/in/batch.jsonl#c-01", "batch.jsonl#c-01"),
        ("media", "/mnt/media", "/mnt/media/hero.png", "hero.png"),
        ("media", "C:\\media", "C:\\media\\hero.png", "hero.png"),
    ],
)
def test_subject_is_relative_to_the_touchpoint(
    kind: str, root: str, target: str, subject: str
) -> None:
    assert subject_for(kind, root, target) == subject


def test_same_file_in_another_folder_has_the_same_subject() -> None:
    a = subject_for("transcripts", "/home/a/run-1/in", "/home/a/run-1/in/02.json")
    b = subject_for("transcripts", "/srv/other/in", "/srv/other/in/02.json")
    assert a == b == "02.json"


def test_location_normalisation() -> None:
    assert normalise_location("  Message   6 ") == "message 6"
    assert normalise_location("widget opened via launcher button") == "widget"
    assert normalise_location("widget opened via vendor API (Intercom)") == "widget"


def test_fingerprint_is_stable_and_separates_its_parts() -> None:
    fp = fingerprint(7, "INT-002", "02.json", "message 6")
    assert fp == fingerprint(7, "INT-002", "02.json", "Message  6")
    assert fp == hashlib.sha256(b"7\x1fINT-002\x1f02.json\x1fmessage 6").hexdigest()
    assert fp != fingerprint(8, "INT-002", "02.json", "message 6")
    assert fp != fingerprint(7, "INT-003", "02.json", "message 6")
    # Without a separator these two would collide.
    assert fingerprint(1, "A", "bc", "") != fingerprint(1, "A", "b", "c")


def test_store_is_content_addressed_and_read_only(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    blob = store.put(b"<html>evidence</html>")
    digest = hashlib.sha256(b"<html>evidence</html>").hexdigest()
    assert blob.sha256 == digest
    assert blob.relpath == f"{digest[:2]}/{digest[2:4]}/{digest}"
    path = tmp_path / blob.relpath
    assert path.read_bytes() == b"<html>evidence</html>"
    assert path.stat().st_mode & 0o222 == 0  # no write bits
    assert store.put(b"<html>evidence</html>") == blob  # stored once
    assert store.verify(digest)


def test_store_detects_and_repairs_an_edited_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    blob = store.put(b"original")
    path = tmp_path / blob.relpath
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    assert not store.verify(blob.sha256)
    store.put(b"original")
    assert store.verify(blob.sha256)
    assert store.delete(blob.sha256)
    assert not store.verify(blob.sha256)
