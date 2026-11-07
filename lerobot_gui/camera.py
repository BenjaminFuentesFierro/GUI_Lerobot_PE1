"""Vista previa de cámara independiente.

Sólo se usa mientras el robot está desconectado: en cuanto LeRobot toma la cámara
(porque forma parte del dataset) los fotogramas llegan por `ArmService`, ya que un
mismo dispositivo /dev/videoN no se puede abrir dos veces.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)


class CameraPreview:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_id = 0
        self._error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self, index_or_path: str, width: int, height: int, fps: int, fourcc: str = ""
    ) -> None:
        self.stop()
        self._stop.clear()
        self._error = ""
        self._thread = threading.Thread(
            target=self._run,
            args=(index_or_path, width, height, fps, fourcc),
            name="CameraPreview",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            self._frame = None

    def read(self) -> tuple[np.ndarray | None, int, str]:
        with self._lock:
            return self._frame, self._frame_id, self._error

    def _run(self, index_or_path: str, width: int, height: int, fps: int, fourcc: str = "") -> None:
        import cv2

        source: int | str = int(index_or_path) if str(index_or_path).isdigit() else str(index_or_path)
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            with self._lock:
                self._error = f"No se pudo abrir la cámara '{index_or_path}'."
            return
        # El formato va primero: cambiarlo después reinicia la resolución.
        if fourcc.strip():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc.strip()[:4]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    with self._lock:
                        self._error = "La cámara dejó de entregar fotogramas."
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                with self._lock:
                    self._frame = rgb
                    self._frame_id += 1
                    self._error = ""
        finally:
            cap.release()
