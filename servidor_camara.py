#!/usr/bin/env python
"""Servidor de camara que corre en WINDOWS y emite MJPEG por HTTP para WSL.

Por que existe: los brazos llegan a WSL por usbipd, y una webcam reenviada por el
mismo tunel lo satura. Las transferencias isocronas de video hacen que vhci_hcd
pierda paquetes, y los puertos serie de los servos se caen del bus.

La solucion es no meter la camara en ese tunel. Windows la abre de forma nativa
(rapida y estable) y manda los fotogramas a WSL por TCP, que si es fiable.

Uso, desde PowerShell (NO desde WSL):

    python servidor_camara.py                 # camara 0, 640x480, 30 fps
    python servidor_camara.py --camara 1      # otra camara
    python servidor_camara.py --ancho 1280 --alto 720
    python servidor_camara.py --listar        # ver que camaras hay

Deja la ventana abierta mientras uses la GUI. Para parar: Ctrl-C.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
except ImportError:
    print("Falta OpenCV en el Python de Windows. Instalalo con:")
    print("    pip install opencv-python")
    raise SystemExit(1)

LIMITE = b"--fotograma"
# Cada cuanto se revisa el ritmo de captura, en segundos.
VENTANA_S = 10.0
# Por debajo de esto se considera parada, no lentitud.
UMBRAL_PARADA = 0.5
# Minimo entre reaperturas, para no entrar en bucle de reinicios.
ENFRIAMIENTO_S = 60.0


class Captura:
    """Lee la camara en un hilo y guarda siempre el ultimo JPEG."""

    def __init__(self, indice: int, ancho: int, alto: int, fps: int, calidad: int):
        self.indice = indice
        self.ancho = ancho
        self.alto = alto
        self.fps = fps
        self.calidad = calidad

        self._cap: "cv2.VideoCapture | None" = None
        self._jpeg: bytes | None = None
        self._secuencia = 0
        self._cond = threading.Condition()
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self.leidos = 0
        self.fallos = 0
        self.reaperturas = 0
        self.real = (0, 0, 0.0)

    def abrir(self) -> None:
        # DirectShow acepta mejor los cambios de resolucion que MSMF.
        cap = cv2.VideoCapture(self.indice, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.indice)
        if not cap.isOpened():
            raise RuntimeError(f"No se pudo abrir la camara {self.indice} en Windows.")

        # El formato va primero: cambiarlo despues reinicia la resolucion.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.ancho)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.alto)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.real = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            float(cap.get(cv2.CAP_PROP_FPS)),
        )
        self._cap = cap

    def arrancar(self) -> None:
        self._hilo = threading.Thread(target=self._bucle, name="Captura", daemon=True)
        self._hilo.start()

    def parar(self) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=3)
        if self._cap is not None:
            self._cap.release()

    def _reabrir(self) -> bool:
        """Cierra y vuelve a abrir la camara. Devuelve si lo consiguio."""
        print("La camara dejo de entregar; reabriendola...")
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        time.sleep(1.0)
        try:
            self.abrir()
        except RuntimeError as exc:
            print(f"  no se pudo reabrir: {exc}")
            return False
        self.reaperturas += 1
        print(f"  reabierta: {self.real[0]}x{self.real[1]} @ {self.real[2]:.0f} fps")
        return True

    def _bucle(self) -> None:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.calidad]
        # Solo se reabre ante una parada DE VERDAD. Un ritmo bajo no es averia:
        # con poca luz la camara alarga la exposicion y baja sola a 3 o 15 fps.
        # Reabrir en ese caso corta el video una y otra vez sin arreglar nada.
        ventana_inicio = time.perf_counter()
        ventana_cuenta = 0
        ultima_reapertura = 0.0
        while not self._parar.is_set():
            ahora = time.perf_counter()
            transcurrido = ahora - ventana_inicio
            if transcurrido >= VENTANA_S:
                ritmo = ventana_cuenta / transcurrido
                parada = ritmo < UMBRAL_PARADA
                enfriado = (ahora - ultima_reapertura) > ENFRIAMIENTO_S
                if parada and enfriado:
                    print(f"Camara parada ({ritmo:.1f} fps en {transcurrido:.0f}s).")
                    self._reabrir()
                    ultima_reapertura = time.perf_counter()
                elif ritmo < self.fps * 0.5:
                    print(f"Aviso: capturando a {ritmo:.1f} fps (pedidos {self.fps}). "
                          "Suele ser falta de luz, no una averia.")
                ventana_inicio = time.perf_counter()
                ventana_cuenta = 0

            ok, cuadro = self._cap.read()
            if not ok:
                self.fallos += 1
                time.sleep(0.05)
                continue
            ok, buf = cv2.imencode(".jpg", cuadro, params)
            if not ok:
                self.fallos += 1
                continue
            ventana_cuenta += 1
            with self._cond:
                self._jpeg = buf.tobytes()
                self._secuencia += 1
                self.leidos += 1
                self._cond.notify_all()

    def esperar(self, ultima: int, timeout: float = 5.0) -> tuple[bytes, int] | None:
        """Bloquea hasta que haya un fotograma mas nuevo que `ultima`."""
        with self._cond:
            if self._secuencia <= ultima:
                self._cond.wait(timeout)
            if self._jpeg is None or self._secuencia <= ultima:
                return None
            return self._jpeg, self._secuencia


class Handler(BaseHTTPRequestHandler):
    captura: Captura = None  # lo rellena main()
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):  # silencia el log por peticion
        pass

    def do_GET(self):  # noqa: N802
        ruta = self.path.split("?")[0].rstrip("/") or "/"
        if ruta == "/stream":
            self._stream()
        elif ruta == "/frame":
            self._frame()
        elif ruta == "/estado":
            self._estado()
        elif ruta == "/":
            self._portada()
        else:
            self.send_error(404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=" + LIMITE.decode()[2:]
        )
        self.end_headers()
        ultima = 0
        try:
            while True:
                dato = self.captura.esperar(ultima)
                if dato is None:
                    continue
                jpeg, ultima = dato
                self.wfile.write(LIMITE + b"\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # el cliente cerro; normal

    def _frame(self):
        dato = self.captura.esperar(0)
        if dato is None:
            self.send_error(503, "sin fotograma")
            return
        jpeg, _ = dato
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(jpeg)

    def _estado(self):
        c = self.captura
        cuerpo = (
            '{"camara": %d, "ancho": %d, "alto": %d, "fps": %.1f, '
            '"leidos": %d, "fallos": %d, "reaperturas": %d}'
            % (c.indice, c.real[0], c.real[1], c.real[2], c.leidos, c.fallos, c.reaperturas)
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _portada(self):
        c = self.captura
        cuerpo = (
            "<!doctype html><meta charset='utf-8'><title>Camara SO-101</title>"
            "<body style='background:#111;color:#eee;font-family:sans-serif;text-align:center'>"
            f"<h2>Camara {c.indice} &middot; {c.real[0]}x{c.real[1]} @ {c.real[2]:.0f} fps</h2>"
            "<img src='/stream' style='max-width:95%'>"
            "</body>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


def listar() -> int:
    print("Buscando camaras en Windows...")
    encontradas = 0
    for i in range(6):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            ancho = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            alto = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"  camara {i}: {ancho}x{alto}")
            encontradas += 1
        cap.release()
    if not encontradas:
        print("  ninguna. Revisa que no este adjuntada a WSL: usbipd detach --busid <BUSID>")
    return 0 if encontradas else 1


def ip_para_wsl(distro: str) -> str | None:
    """IP de Windows en la red virtual de WSL, preguntandosela a la propia WSL.

    Es la puerta de enlace que ve la distro. Escuchar solo en esa direccion deja el
    video accesible desde WSL y NO desde la red local, al contrario que 0.0.0.0.
    """
    try:
        salida = subprocess.run(
            ["wsl.exe", "-d", distro, "--exec", "ip", "route"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for linea in salida.splitlines():
        partes = linea.split()
        if len(partes) >= 3 and partes[0] == "default" and partes[1] == "via":
            return partes[2]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Servidor MJPEG de camara para WSL")
    ap.add_argument("--camara", type=int, default=0, help="indice de camara en Windows")
    ap.add_argument("--puerto", type=int, default=8181)
    ap.add_argument("--ancho", type=int, default=640)
    ap.add_argument("--alto", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--calidad", type=int, default=80, help="calidad JPEG, 1 a 100")
    ap.add_argument("--distro", default="Ubuntu-22.04", help="distro de WSL a la que servir")
    ap.add_argument(
        "--host",
        default="auto",
        help="direccion donde escuchar. 'auto' = solo la interfaz virtual de WSL",
    )
    ap.add_argument("--listar", action="store_true")
    args = ap.parse_args()

    if args.listar:
        return listar()

    host = args.host
    if host == "auto":
        host = ip_para_wsl(args.distro)
        if host is None:
            print("No se pudo averiguar la IP de Windows en la red de WSL.")
            print("Abre una terminal de Ubuntu (WSL debe estar corriendo) y reintenta,")
            print("o indicala a mano:  python servidor_camara.py --host <IP>")
            return 1
    # A proposito NO se escucha en 0.0.0.0: eso publicaria la camara en toda la red local.

    captura = Captura(args.camara, args.ancho, args.alto, args.fps, args.calidad)
    try:
        captura.abrir()
    except RuntimeError as exc:
        print(exc)
        print("Si la camara esta adjuntada a WSL, devuelvela a Windows:")
        print("    usbipd detach --busid <BUSID>")
        return 1
    captura.arrancar()

    Handler.captura = captura
    servidor = ThreadingHTTPServer((host, args.puerto), Handler)
    servidor.daemon_threads = True

    ancho, alto, fps = captura.real
    print(f"Camara {args.camara} abierta en Windows: {ancho}x{alto} @ {fps:.0f} fps (MJPG)")
    print(f"Escuchando solo en {host}:{args.puerto} (interfaz de WSL, no la red local)")
    print()
    print("Pon esta URL en la GUI (Ajustes -> camara):")
    print(f"    http://{host}:{args.puerto}/stream")
    print()
    print("Para verla aqui mismo, abre en el navegador:")
    print(f"    http://{host}:{args.puerto}/")
    print()
    print("Deja esta ventana abierta. Ctrl-C para parar.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nParando...")
    finally:
        servidor.shutdown()
        captura.parar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
