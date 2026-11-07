#!/usr/bin/env python
"""Mide si la camara entrega video de forma estable, y cuanto aguanta.

Sirve para comparar situaciones de forma objetiva en vez de mirar la imagen:
hub USB-C contra puerto directo, o YUYV contra MJPEG.

Un fotograma YUYV con todos los bytes a cero se convierte en RGB (0,135,0),
verde puro. Contarlos distingue "la camara no entrega nada" de "entrega imagen".

    python probar_camara.py                   # 45 s, formato por defecto
    python probar_camara.py --segundos 120    # prueba larga de estabilidad
    python probar_camara.py --fourcc MJPG     # fuerza MJPEG (menos ancho de banda)
    python probar_camara.py --listar          # solo enumera las camaras visibles
"""

from __future__ import annotations

import argparse
import glob
import sys
import time
from pathlib import Path


def listar() -> int:
    from lerobot.cameras.opencv.camera_opencv import OpenCVCamera

    nodos = sorted(glob.glob("/dev/video*"))
    print("nodos presentes:", ", ".join(nodos) if nodos else "ninguno")
    if not nodos:
        print()
        print("No hay ningun /dev/video*. Dentro de WSL la camara USB no aparece sola:")
        print("  usbipd list                             (en PowerShell, anota el BUSID)")
        print("  usbipd attach --wsl --busid <BUSID>     (con Ubuntu abierto)")
        return 1

    encontradas = OpenCVCamera.find_cameras()
    print(f"camaras utilizables: {len(encontradas)}")
    for cam in encontradas:
        p = cam.get("default_stream_profile") or {}
        print(f"  {cam['id']}  {p.get('width')}x{p.get('height')} @ {p.get('fps')} fps  ({p.get('fourcc')})")
    if not encontradas:
        print("  (el nodo existe pero no se puede abrir: suele ser que otro proceso la tiene,")
        print("   por ejemplo la propia GUI con el robot conectado)")
    return 0 if encontradas else 1


def medir(ruta: str, segundos: float, fourcc: str | None, ancho: int, alto: int, fps: int) -> int:
    from lerobot.cameras.opencv.camera_opencv import OpenCVCamera
    from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig

    cfg = OpenCVCameraConfig(
        index_or_path=Path(ruta), fps=fps, width=ancho, height=alto, fourcc=fourcc
    )
    print(f"camara {ruta}  {ancho}x{alto}@{fps}  fourcc={fourcc or 'por defecto'}")
    print(f"midiendo {segundos:g} s…")

    cam = OpenCVCamera(cfg)
    try:
        cam.connect()
    except Exception as exc:
        print(f"\nNo se pudo abrir: {type(exc).__name__}: {exc}")
        return 1

    bueno = verde = negro = fallo = 0
    primer_problema = None
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < segundos:
            ahora = time.perf_counter() - t0
            try:
                f = cam.async_read()
                r = float(f[:, :, 0].mean())
                g = float(f[:, :, 1].mean())
                b = float(f[:, :, 2].mean())
                if r < 3 and b < 3 and 120 < g < 150:
                    verde += 1
                elif r < 2 and g < 2 and b < 2:
                    negro += 1
                else:
                    bueno += 1
                    continue
            except Exception as exc:
                fallo += 1
                if primer_problema is None:
                    print(f"  primer fallo de lectura a los {ahora:.1f} s: "
                          f"{type(exc).__name__}: {str(exc)[:60]}")
            if primer_problema is None:
                primer_problema = ahora
                print(f"  primer fotograma defectuoso a los {ahora:.1f} s")
            time.sleep(1 / max(1, fps))
    except KeyboardInterrupt:
        print("  interrumpido")
    finally:
        try:
            cam.disconnect()
        except Exception:
            pass

    total = bueno + verde + negro + fallo
    if total == 0:
        print("\nNo se leyo ningun fotograma.")
        return 1

    print()
    print(f"total {total} fotogramas")
    print(f"  con imagen : {bueno}")
    print(f"  verdes     : {verde}   (fotograma vacio: el stream se corta)")
    print(f"  negros     : {negro}")
    print(f"  fallos     : {fallo}")
    if primer_problema is not None:
        print(f"  aguanto {primer_problema:.1f} s antes del primer problema")

    pct = 100.0 * bueno / total
    print(f"\n{pct:.0f}% de fotogramas utiles -> ", end="")
    if pct > 98:
        print("ESTABLE")
    elif pct > 80:
        print("INESTABLE (usable a ratos, no fiable para grabar)")
    else:
        print("NO FUNCIONA")
    if pct <= 98 and not fourcc:
        print("\nSiguiente prueba: python probar_camara.py --fourcc MJPG")
        print("MJPEG baja el caudal isocrono unas diez veces, que es lo que suele")
        print("romper el tunel USB/IP de WSL.")
    return 0 if pct > 98 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnostico de la camara USB en WSL")
    ap.add_argument("--camara", default="/dev/video0", help="nodo a probar (por defecto /dev/video0)")
    ap.add_argument("--segundos", type=float, default=45.0, help="duracion de la medicion")
    ap.add_argument("--fourcc", default=None, help="formato forzado, p.ej. MJPG")
    ap.add_argument("--ancho", type=int, default=640)
    ap.add_argument("--alto", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--listar", action="store_true", help="solo enumerar camaras y salir")
    args = ap.parse_args()

    if args.listar:
        return listar()
    if not Path(args.camara).exists():
        print(f"{args.camara} no existe.")
        return listar()
    return medir(args.camara, args.segundos, args.fourcc, args.ancho, args.alto, args.fps)


if __name__ == "__main__":
    sys.exit(main())
