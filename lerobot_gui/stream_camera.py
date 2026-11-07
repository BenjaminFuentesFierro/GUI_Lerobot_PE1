"""Cámara que llega por un stream MJPEG en vez de por USB.

`servidor_camara.py` corre en Windows, abre la webcam de forma nativa y emite los
fotogramas por HTTP. Aquí se consumen desde WSL.

El motivo es medido: una webcam reenviada por usbipd comparte el túnel con los
brazos, y su tráfico isócrono tira los puertos serie del bus. Sacándola del túnel
se duplica además el ritmo (30 fps frente a 15) y se reduce la dispersión.

La clase imita la interfaz de las cámaras de LeRobot (`connect`, `disconnect`,
`async_read`, `is_connected`) para poder ocupar su lugar dentro del robot.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# Si no llega un fotograma nuevo, se reutiliza el último hasta esta antigüedad.
# Holgado a propósito: con poca luz la cámara baja sola a pocos fps, y eso no es
# una avería. Un hueco aislado tampoco debe abortar una grabación entera.
MAX_ANTIGUEDAD_S = 5.0


def es_url(valor: object) -> bool:
    texto = str(valor).strip().lower()
    return texto.startswith("http://") or texto.startswith("https://")


class StreamCamera:
    """Lee un MJPEG por HTTP en un hilo y entrega siempre el último fotograma."""

    def __init__(self, url: str, width: int = 640, height: int = 480, fps: int = 30):
        self.url = str(url).strip()
        self.width = width
        self.height = height
        self.fps = fps

        self._cap = None
        self._frame: np.ndarray | None = None
        self._sello = 0.0
        self._secuencia = 0
        self._lock = threading.Lock()
        self._nuevo = threading.Condition(self._lock)
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._conectada = False
        # Última secuencia entregada, para contar repeticiones.
        self._entregado = -1
        # Veces que se reutilizó el fotograma anterior por no llegar uno nuevo.
        self.repetidos = 0

    def __str__(self) -> str:
        return f"StreamCamera({self.url})"

    @property
    def is_connected(self) -> bool:
        return self._conectada

    def connect(self, warmup: bool = True) -> None:
        import cv2

        if self._conectada:
            return
        cap = cv2.VideoCapture(self.url)
        if not cap.isOpened():
            raise ConnectionError(
                f"No se pudo abrir el stream {self.url}.\n"
                "¿Está corriendo servidor_camara.py en Windows? Ábrelo con:\n"
                "    python servidor_camara.py"
            )
        self._cap = cap
        self._parar.clear()
        self._conectada = True
        self._hilo = threading.Thread(target=self._bucle, name="StreamCamera", daemon=True)
        self._hilo.start()

        if warmup:
            limite = time.perf_counter() + 5.0
            while time.perf_counter() < limite:
                with self._lock:
                    if self._frame is not None:
                        return
                time.sleep(0.05)
            self.disconnect()
            raise ConnectionError(f"El stream {self.url} no entregó ningún fotograma.")

    def _bucle(self) -> None:
        import cv2

        while not self._parar.is_set():
            ok, cuadro = self._cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            # LeRobot espera RGB; OpenCV entrega BGR.
            rgb = cv2.cvtColor(cuadro, cv2.COLOR_BGR2RGB)
            with self._nuevo:
                self._frame = rgb
                self._sello = time.perf_counter()
                self._secuencia += 1
                self._nuevo.notify_all()

    def async_read(self, timeout_ms: float = 200) -> np.ndarray:
        """Devuelve el último fotograma disponible, sin esperar a que llegue otro.

        Es deliberado que no bloquee. El bucle de control llama a esto en cada
        ciclo, antes de mover los motores: si esperase un fotograma nuevo, la
        teleoperación heredaría el ritmo de la cámara. Medido, esperar dejaba el
        bucle en 5 Hz en vez de 30.

        Solo espera cuando todavía no hay ningún fotograma, y falla si el que hay
        es demasiado viejo (la cámara se cayó de verdad).
        """
        with self._nuevo:
            if self._frame is None:
                self._nuevo.wait(timeout_ms / 1000.0)
                if self._frame is None:
                    raise TimeoutError(f"{self} no ha entregado ningún fotograma.")

            antiguedad = time.perf_counter() - self._sello
            if antiguedad > MAX_ANTIGUEDAD_S:
                raise TimeoutError(f"{self} lleva {antiguedad:.1f} s sin fotogramas nuevos.")

            if self._secuencia == self._entregado:
                self.repetidos += 1
            self._entregado = self._secuencia
            return self._frame

    def read(self, color_mode=None) -> np.ndarray:
        return self.async_read(timeout_ms=1000)

    def disconnect(self) -> None:
        self._parar.set()
        if self._hilo is not None and self._hilo.is_alive():
            self._hilo.join(timeout=3)
        self._hilo = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                log.exception("Error cerrando el stream")
            self._cap = None
        self._conectada = False
        with self._lock:
            self._frame = None


class CamaraTolerante:
    """Envuelve una cámara para que sus fallos nunca tumben la conexión del robot.

    El robot es la prioridad. Sin esto, un servidor de vídeo caído o una webcam
    desenchufada hacen fallar `get_observation()` en cada ciclo, el servicio
    interpreta que se perdió el hardware y suelta también los brazos.

    Con el envoltorio, un fallo de cámara degrada el vídeo y nada más: se entrega
    el último fotograma conocido, o uno negro si nunca hubo ninguno.
    """

    # Cada cuánto se reintenta una cámara que está fallando.
    REINTENTO_S = 1.0

    def __init__(self, camara, alto: int = 480, ancho: int = 640):
        self._camara = camara
        self._ultimo: np.ndarray | None = None
        self._negro = np.zeros((alto, ancho, 3), dtype=np.uint8)
        self._proximo_intento = 0.0
        self.fallos = 0
        self.ultimo_error = ""

    def __str__(self) -> str:
        return f"CamaraTolerante({self._camara})"

    @property
    def sin_senal(self) -> bool:
        return self.fallos > 0

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self._camara.is_connected)
        except Exception:  # noqa: BLE001
            return False

    def connect(self, warmup: bool = True) -> None:
        self._camara.connect(warmup)

    def async_read(self, timeout_ms: float = 200) -> np.ndarray:
        ahora = time.perf_counter()

        # Mientras la cámara esté caída no se paga su timeout en cada ciclo: eso
        # dejaba la teleoperación en 4 Hz. Se reintenta de vez en cuando, y corto.
        if self.fallos and ahora < self._proximo_intento:
            return self._respaldo()

        try:
            fotograma = self._camara.async_read(20 if self.fallos else timeout_ms)
        except Exception as exc:  # noqa: BLE001
            self.fallos += 1
            self.ultimo_error = str(exc)
            self._proximo_intento = ahora + self.REINTENTO_S
            if self.fallos == 1:
                log.warning("La cámara dejó de entregar: %s", exc)
            return self._respaldo()

        if self.fallos:
            log.info("La cámara volvió tras %d fallos", self.fallos)
            self.fallos = 0
            self.ultimo_error = ""
        self._ultimo = fotograma
        return fotograma

    def _respaldo(self) -> np.ndarray:
        return self._ultimo if self._ultimo is not None else self._negro

    def read(self, color_mode=None) -> np.ndarray:
        return self.async_read(timeout_ms=1000)

    def disconnect(self) -> None:
        try:
            self._camara.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("Error desconectando la cámara")


def comprobar(url: str, timeout: float = 3.0) -> dict | None:
    """Consulta /estado del servidor. Devuelve el JSON, o None si no responde."""
    import json
    import urllib.error
    import urllib.request

    base = url.rstrip("/")
    for sufijo in ("/stream", "/frame"):
        if base.endswith(sufijo):
            base = base[: -len(sufijo)]
            break
    try:
        with urllib.request.urlopen(base + "/estado", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def url_por_defecto(puerto: int = 8181) -> str | None:
    """URL probable del servidor: la puerta de enlace de WSL es el host Windows."""
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for linea in f.readlines()[1:]:
                campos = linea.split()
                if len(campos) > 2 and campos[1] == "00000000":
                    crudo = int(campos[2], 16)
                    ip = ".".join(str((crudo >> (8 * i)) & 0xFF) for i in range(4))
                    return f"http://{ip}:{puerto}/stream"
    except OSError:
        pass
    return None
