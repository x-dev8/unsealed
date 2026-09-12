import traceback
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
  QFileDialog,
  QHBoxLayout,
  QLabel,
  QMessageBox,
  QPlainTextEdit,
  QPushButton,
  QVBoxLayout,
  QWidget,
)

from ...vfs import Resource
from ..content import AssetTree, ContentContext, Node, for_resource
from ..content.extract import extract_bytes


class ContentTab(QWidget):
  """One opened file: decodes it and mounts its asset tree.

  Always closable (even when it's the only tab): closing the last one
  returns to the welcome screen.

  The file is a vfs `Resource` — a loose disk file (wrapped by
  `MainWindow`) or an entry inside a mounted `.spak` — so the same
  handlers open both. Falls back to a placeholder for formats without a
  handler yet, or an error view (with traceback) when decoding fails —
  both useful when the point is to debug a file's structure.

  A header "Extract" button saves this resource to disk (decoding the
  binary `.edt`/`.dat` wrappers to CSV first — see `content/extract.py`).
  A `.spak` has no such button here: it gets its own "Extract All" in
  `SpakView`, since extracting a whole archive means a folder of files,
  not one.
  """

  def __init__(
    self,
    resource: Resource,
    output_dir: Path | None = None,
    open_file: Callable[[Resource], None] | None = None,
  ) -> None:
    super().__init__()
    self.resource = resource
    self.output_dir = output_dir
    self._open_file = open_file or (lambda _r: None)
    self.setObjectName("contentTab")

    layout = QVBoxLayout(self)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    header = self._build_header()
    if header is not None:
      layout.addWidget(header)
    layout.addWidget(self._build_body(), stretch=1)

  def _build_header(self) -> QWidget | None:
    if self.resource.suffix.lower() == ".spak":
      return None  # the archive browser has its own "Extract All"
    bar = QWidget()
    bar.setObjectName("contentHeader")
    box = QHBoxLayout(bar)
    box.setContentsMargins(10, 6, 10, 6)
    box.addStretch(1)
    button = QPushButton("Extract…")
    button.setObjectName("extractButton")
    button.setToolTip("Save this file to your computer")
    button.clicked.connect(self._extract)
    box.addWidget(button)
    return bar

  def _build_body(self) -> QWidget:
    handler = for_resource(self.resource)
    if handler is None:
      return self._placeholder(
        f"File format: {self.resource.suffix or 'this'} is not supported."
      )
    try:
      asset = handler.decode(self.resource)
      ctx = ContentContext(self.resource, self._open_file, self.output_dir)
      if handler.view is not None:
        return handler.view(asset, ctx)
      return AssetTree(Node(handler.name, asset, handler.name))
    except Exception:
      return self._error(traceback.format_exc())

  def _extract(self) -> None:
    try:
      data, suggested = extract_bytes(self.resource)
    except Exception:
      QMessageBox.critical(self, "Couldn't extract", traceback.format_exc())
      return
    disk_path = self.resource.disk_path
    base_dir = (
      self.output_dir or (disk_path.parent if disk_path else None) or Path.home()
    )
    path, _ = QFileDialog.getSaveFileName(
      self, "Extract File", str(Path(base_dir) / suggested), "All Files (*)"
    )
    if not path:
      return
    try:
      Path(path).write_bytes(data)
    except OSError as exc:
      QMessageBox.critical(self, "Couldn't extract", str(exc))

  def _placeholder(self, message: str) -> QWidget:
    label = QLabel(message)
    label.setObjectName("contentPlaceholder")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    return label

  def _error(self, tb: str) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    heading = QLabel(f"Couldn't decode {self.resource.name}")
    heading.setObjectName("errorHeading")
    trace = QPlainTextEdit(tb)
    trace.setObjectName("errorTrace")
    trace.setReadOnly(True)
    layout.addWidget(heading)
    layout.addWidget(trace, stretch=1)
    return box

  @property
  def title(self) -> str:
    return self.resource.name
