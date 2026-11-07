"""GUI de control para el kit SO-101 (LeRobot / Hugging Face).

Módulos:
    settings   -> configuración persistente (puertos, cámara, dataset) y poses guardadas
    service    -> hilo único dueño del puerto serial: teleop, sliders, poses, grabación
    recording  -> sesión de grabación de dataset apoyada en `lerobot.scripts.lerobot_record`
    camera     -> vista previa de cámara independiente (cuando el robot está desconectado)
    app        -> interfaz CustomTkinter
"""

__version__ = "1.0.0"
