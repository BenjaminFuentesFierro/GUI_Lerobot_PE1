"""Punto de entrada: `python -m lerobot_gui`."""

from __future__ import annotations

import sys


def _check_dependencies() -> None:
    faltantes = []
    for module, paquete in (
        ("customtkinter", "customtkinter"),
        ("PIL", "pillow"),
        ("cv2", "opencv-python-headless"),
        ("lerobot", "lerobot"),
    ):
        try:
            __import__(module)
        except ImportError:
            faltantes.append(paquete)
    if faltantes:
        print("Faltan dependencias:", ", ".join(faltantes), file=sys.stderr)
        print(f"Instálalas con:\n    pip install {' '.join(faltantes)}", file=sys.stderr)
        raise SystemExit(1)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "Tkinter no está disponible en este intérprete.\n"
            "En Ubuntu/WSL:  sudo apt install python3-tk",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    _check_dependencies()
    from .app import main

    main()
