"""Finding fingerprints: the key that matches "the same finding" across two scans.

    fingerprint = sha256(touchpoint_id ␟ rule_id ␟ subject ␟ normalised location)

* ``subject`` is which scanned thing inside the touchpoint the finding is about:
  empty for a page touchpoint (one URL), and for transcript or media touchpoints the
  file's path relative to the touchpoint's folder (plus ``#conversation-id`` for a
  file holding several conversations). Relative, so moving the folder or scanning it
  from another machine does not change fingerprints.
* the location is lower-cased with whitespace collapsed. Browser findings say how the
  widget was opened ("widget opened via launcher button"); which launcher worked can
  differ between runs without the page changing, so that phrase normalises to
  "widget".

``␟`` is U+001F (unit separator), which none of the parts can contain.
See docs/decisions.md, D-013.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath, PureWindowsPath

SEP = "\x1f"
_SPACE = re.compile(r"\s+")


def normalise_location(location: str) -> str:
    loc = _SPACE.sub(" ", location).strip().lower()
    if loc.startswith("widget opened via"):
        return "widget"
    return loc


def subject_for(kind: str, touchpoint_target: str, result_target: str) -> str:
    if kind == "page":
        return ""
    path, sep, conversation = result_target.partition("#")
    root = touchpoint_target.rstrip("/\\")
    norm_path = path.replace("\\", "/")
    norm_root = root.replace("\\", "/")
    if norm_path == norm_root:
        rel = PurePosixPath(norm_path).name or PureWindowsPath(path).name
    elif norm_path.startswith(norm_root + "/"):
        rel = norm_path[len(norm_root) + 1 :]
    else:
        rel = PurePosixPath(norm_path).name
    return rel + (sep + conversation if sep else "")


def fingerprint(touchpoint_id: int, rule_id: str, subject: str, location: str) -> str:
    parts = (str(touchpoint_id), rule_id, subject, normalise_location(location))
    if any(SEP in p for p in parts):
        raise ValueError("fingerprint parts must not contain U+001F")
    return hashlib.sha256(SEP.join(parts).encode("utf-8")).hexdigest()
