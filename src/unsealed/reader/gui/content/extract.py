"""Extract — turn an opened resource into file(s) saved on disk.

A single resource extracts to one output file. Most formats just save
their plain bytes (the vfs read already undoes any archive decryption),
but the two binary wrappers aren't useful raw outside the app: a `.dat`
decodes to the CSV of its table (the same grid the viewer shows), and an
`.edt` gets the same treatment when it decrypts to a `.dat` — an `.edt`
that decrypts to already-delimited `.scr`/`.tsv`/`.xml` text, or to
unrecognized binary, is saved as that plain decrypted payload instead.

A `.spak` extracts to a folder: every entry through this same per-file
logic (see `spak_view.py`'s "Extract All"). That bulk path decrypts the
whole archive up front with the bundled `bkcrack` (compiled C++, via
`SpakArchive.decrypt_copy`) rather than this module's own per-entry
ZipCrypto -- ~100x faster for the archive as a whole, measured -- then
reads every entry from that plain copy (a thread pool, tried first,
loses badly to dispatch overhead once ZipCrypto decrypt is the work
being dispatched; see the "Extract" PR notes).
"""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, List, Optional, Tuple

from ...formats.dat.format import SealDatFormat
from ...formats.edt.format import SealEdtFormat
from ...utils.spak import SpakArchive
from ...vfs import MemorySource, Resource
from .table_sources import DatTableAdapter, TableData


def _csv_bytes(data: TableData) -> bytes:
  buf = io.StringIO()
  writer = csv.writer(buf, lineterminator="\r\n")
  writer.writerow(data.columns)
  writer.writerows(data.rows)
  return buf.getvalue().encode("utf-8-sig")  # BOM: Excel reads Hangul correctly


def _dat_csv(resource: Resource) -> bytes:
  """Decode a `.dat`-named resource and render its table as CSV."""
  raw = resource.read()
  dat = SealDatFormat().decode(resource)
  adapter = DatTableAdapter(dat, raw, resource.stem)
  return _csv_bytes(adapter.apply(adapter.default_label))


def extract_bytes(resource: Resource) -> Tuple[bytes, str]:
  """The bytes to write for *resource*, and the filename to save as."""
  suffix = resource.suffix.lower()

  if suffix == ".dat":
    return _dat_csv(resource), f"{resource.stem}.csv"

  if suffix == ".edt":
    edt = SealEdtFormat().decode(resource)
    data = edt.value or b""
    ext = (edt.extension or "bin").lower()
    stem = edt.name or resource.stem
    # Keep the ".edt" marker in the output name even once converted: a
    # sibling `<stem>.dat` (or `.scr`/`.tsv`) in the same archive would
    # otherwise decode to the same output name and silently overwrite it.
    if ext == "dat":
      inner = Resource(MemorySource({f"{stem}.dat": data}), f"{stem}.dat")
      return _dat_csv(inner), f"{stem}.edt.csv"
    return data, f"{stem}.edt.{ext}"

  return resource.read(), resource.name


def _write_extracted(resource: Resource, name: str, dest_dir: Path) -> Path:
  """Run *resource* through `extract_bytes` and write it under dest_dir,
  preserving *name*'s folder structure; returns the path written."""
  data, out_name = extract_bytes(resource)
  rel_dir = PurePosixPath(name.replace("\\", "/")).parent
  out_dir = dest_dir if str(rel_dir) == "." else dest_dir / Path(str(rel_dir))
  out_dir.mkdir(parents=True, exist_ok=True)
  out_path = out_dir / out_name
  out_path.write_bytes(data)
  return out_path


def extract_entry_to(source, name: str, dest_dir: Path) -> Path:
  """Extract one archive entry under dest_dir, preserving its folder
  structure; returns the path written."""
  return _write_extracted(Resource(source, name), name, dest_dir)


def extract_entries(
  source,
  names: List[str],
  dest_dir: Path,
  on_progress: Optional[Callable[[int, int, str], None]] = None,
  should_cancel: Optional[Callable[[], bool]] = None,
) -> List[str]:
  """Extract every name in *names* from *source* under dest_dir.

  Returns the names that failed to extract (one bad entry doesn't abort
  the batch). `on_progress(done, total, name)` fires before each entry;
  `should_cancel()` is polled between entries.
  """
  failures: List[str] = []
  total = len(names)
  for i, name in enumerate(names):
    if should_cancel is not None and should_cancel():
      break
    if on_progress is not None:
      on_progress(i, total, name)
    try:
      extract_entry_to(source, name, dest_dir)
    except Exception:
      failures.append(name)
  return failures


# Sentinel `on_progress` name for the one-shot bulk-decrypt phase, so a
# caller can tell it apart from a real per-entry name (e.g. to label it
# without the "Extracting " prefix it'd otherwise get).
DECRYPTING_LABEL = "Decrypting archive…"


def extract_spak_entries(
  archive: SpakArchive,
  names: List[str],
  dest_dir: Path,
  on_progress: Optional[Callable[[int, int, str], None]] = None,
  should_cancel: Optional[Callable[[], bool]] = None,
) -> List[str]:
  """Extract every name in *names* from `archive` into dest_dir.

  `archive` is the already-mounted `SpakArchive` to extract from (a
  `SpakSource`'s `.primary`), not the `SpakSource` wrapper: reading it
  directly skips `SpakSource`'s read cache, which would otherwise retain
  every decrypted entry in memory for the tab's lifetime -- fine for
  browsing a few files, not for a bulk export of the whole archive.

  When the archive is encrypted, this decrypts the whole thing up front
  via `decrypt_copy` (the bundled `bkcrack`, compiled C++) into a plain
  temp copy, then reads every entry from THAT -- `SpakArchive.read()`
  skips its own decrypt step once an entry's encryption flag is cleared,
  so the per-file loop below is unchanged either way, just fast. Falls
  back to decrypting per-entry with this module's own (much slower, pure
  Python) ZipCrypto if bkcrack isn't available or fails for any reason,
  so extraction still works even when the native path can't be used.
  """
  if archive.key is not None:
    try:
      with tempfile.TemporaryDirectory(prefix="unsealed-extract-") as tmp:
        plain_path = Path(tmp) / "decrypted.zip"
        if on_progress is not None:
          on_progress(0, len(names), DECRYPTING_LABEL)
        if should_cancel is not None and should_cancel():
          return list(names)
        if not archive.decrypt_copy(plain_path, should_cancel=should_cancel):
          return list(names)  # cancelled before bkcrack finished
        with SpakArchive(plain_path) as plain:
          return _extract_loop(plain, names, dest_dir, on_progress, should_cancel)
    except Exception as e:
      print(f"Warning: native decrypt unavailable ({e}); using the slower fallback")
  return _extract_loop(archive, names, dest_dir, on_progress, should_cancel)


def _extract_loop(
  archive: SpakArchive,
  names: List[str],
  dest_dir: Path,
  on_progress: Optional[Callable[[int, int, str], None]],
  should_cancel: Optional[Callable[[], bool]],
) -> List[str]:
  failures: List[str] = []
  total = len(names)
  for i, name in enumerate(names):
    if should_cancel is not None and should_cancel():
      break
    if on_progress is not None:
      on_progress(i, total, name)
    try:
      resource = Resource(MemorySource({name: archive.read(name)}), name)
      _write_extracted(resource, name, dest_dir)
    except Exception:
      failures.append(name)
  return failures
