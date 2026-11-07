#!/usr/bin/env python
"""Dice qué brazo (seguidor o líder) está en cada /dev/ttyACM*.

El número que asigna Linux depende del orden en que usbipd adjunta los dispositivos,
así que puede cambiar entre sesiones. Este script lee la calibración grabada en la
EEPROM de los servos de cada puerto y la compara con los archivos de calibración
guardados por LeRobot para decidir cuál es cuál.

Es de solo lectura: no cambia el torque ni escribe nada en los motores.

    conda activate lerobot && python identificar_brazos.py
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.utils.constants import HF_LEROBOT_CALIBRATION, ROBOTS, TELEOPERATORS

MOTORS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
    "gripper": 6,
}
CAMPOS = ("homing_offset", "range_min", "range_max")


def archivos_de_calibracion() -> dict[str, Path]:
    """Todas las calibraciones guardadas: 'seguidor «id»' -> ruta del json."""
    encontrados = {}
    for carpeta, etiqueta in ((ROBOTS, "seguidor"), (TELEOPERATORS, "líder")):
        base = HF_LEROBOT_CALIBRATION / carpeta
        for ruta in base.glob("*/*.json"):
            encontrados[f"{etiqueta} «{ruta.stem}»"] = ruta
    return encontrados


def leer_puerto(puerto: str) -> dict[str, dict[str, int]] | None:
    bus = FeetechMotorsBus(
        port=puerto,
        motors={n: Motor(i, "sts3215", MotorNormMode.RANGE_M100_100) for n, i in MOTORS.items()},
    )
    try:
        bus.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ no se pudo abrir: {exc}".replace("\n", " "))
        return None
    try:
        return {
            motor: {campo: getattr(cal, campo) for campo in CAMPOS}
            for motor, cal in bus.read_calibration().items()
        }
    finally:
        # disable_torque=False: no tocamos el estado del brazo, solo cerramos el puerto.
        bus.disconnect(disable_torque=False)


def coincidencias(leido: dict, archivo: Path) -> int:
    guardado = json.loads(archivo.read_text())
    return sum(
        1
        for motor, valores in leido.items()
        if motor in guardado and all(guardado[motor].get(c) == valores[c] for c in CAMPOS)
    )


def main() -> int:
    calibraciones = archivos_de_calibracion()
    if not calibraciones:
        print("No hay archivos de calibración en", HF_LEROBOT_CALIBRATION)
        return 1

    puertos = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not puertos:
        print("No hay puertos serie visibles. ¿Adjuntaste los brazos con usbipd?")
        return 1

    print("Calibraciones guardadas:", ", ".join(calibraciones))
    print()
    for puerto in puertos:
        print(puerto)
        leido = leer_puerto(puerto)
        if leido is None:
            continue
        marcador = [(coincidencias(leido, ruta), nombre) for nombre, ruta in calibraciones.items()]
        marcador.sort(reverse=True)
        mejor, nombre = marcador[0]
        total = len(MOTORS)
        if mejor == total:
            print(f"  ✓ es el {nombre}  ({mejor}/{total} motores coinciden)")
        elif mejor > 0:
            print(f"  ~ se parece al {nombre} ({mejor}/{total}); recalibra si no es el esperado")
        else:
            print("  ? no coincide con ninguna calibración guardada")
        for puntos, otro in marcador[1:]:
            print(f"      {otro}: {puntos}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
