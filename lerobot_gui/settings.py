"""Configuración persistente de la GUI y almacén de poses predefinidas.

Los archivos se guardan en `gui_data/` junto al proyecto para que sobrevivan entre
sesiones sin tocar nada dentro de `~/.cache/huggingface/lerobot`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "gui_data"
SETTINGS_FILE = DATA_DIR / "ajustes.json"
POSES_FILE = DATA_DIR / "poses.json"
ROUTINES_FILE = DATA_DIR / "rutinas.json"

# Orden físico de las articulaciones del SO-101 (coincide con los IDs 1..6).
MOTOR_ORDER = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)

# Etiquetas legibles para la interfaz.
MOTOR_LABELS = {
    "shoulder_pan": "1 · Base (giro)",
    "shoulder_lift": "2 · Hombro",
    "elbow_flex": "3 · Codo",
    "wrist_flex": "4 · Muñeca (inclinación)",
    "wrist_roll": "5 · Muñeca (rotación)",
    "gripper": "6 · Pinza",
}


@dataclass
class CameraSettings:
    """Cámara USB usada para la vista previa y para el dataset."""

    enabled: bool = False
    name: str = "front"
    # Índice (`0`) o ruta (`/dev/video0`). Se guarda como texto y se convierte al usar.
    index_or_path: str = "0"
    width: int = 640
    height: int = 480
    fps: int = 30
    # Formato de captura (FOURCC). Vacío = el que elija la cámara.
    # "MJPG" reduce unas diez veces el caudal isócrono, imprescindible cuando la
    # cámara llega por usbipd: en YUYV el túnel USB/IP de WSL entrega fotogramas vacíos.
    fourcc: str = "MJPG"


@dataclass
class DatasetSettings:
    repo_id: str = "usuario/mi_dataset"
    single_task: str = "Tomar el objeto y colocarlo en la caja"
    num_episodes: int = 5
    episode_time_s: float = 30.0
    reset_time_s: float = 10.0
    root: str = ""  # vacío => ruta por defecto de LeRobot (~/.cache/huggingface/lerobot)
    push_to_hub: bool = False
    private: bool = False
    resume: bool = False
    vcodec: str = "libsvtav1"


@dataclass
class Settings:
    follower_port: str = "/dev/ttyACM0"
    follower_id: str = "my_awesome_follower_arm"
    leader_port: str = "/dev/ttyACM1"
    leader_id: str = "my_awesome_leader_arm"
    use_leader: bool = True

    fps: int = 30  # frecuencia de los bucles activos (teleop / sliders / grabación)
    idle_fps: int = 10  # frecuencia de refresco cuando el brazo está en reposo
    use_degrees: bool = False  # False => unidades normalizadas (-100..100 y 0..100)
    # Limita el salto máximo por comando (en unidades normalizadas). None = sin límite.
    max_relative_target: float | None = None
    preset_duration_s: float = 3.0  # duración del movimiento hacia una pose guardada

    camera: CameraSettings = field(default_factory=CameraSettings)
    dataset: DatasetSettings = field(default_factory=DatasetSettings)

    # ------------------------------------------------------------------ IO --
    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "Settings":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("No se pudo leer %s (%s). Se usan valores por defecto.", path, exc)
            return cls()
        return _from_dict(cls, raw)

    def save(self, path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")


_NESTED = {"camera": CameraSettings, "dataset": DatasetSettings}


def _from_dict(cls, raw: dict):
    """Construye un dataclass ignorando claves desconocidas (tolerante a versiones)."""
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        value = raw[f.name]
        nested = _NESTED.get(f.name)
        if nested is not None and isinstance(value, dict):
            kwargs[f.name] = _from_dict(nested, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


class PoseStore:
    """Poses predefinidas: nombre -> {motor: valor normalizado}."""

    def __init__(self, path: Path = POSES_FILE):
        self.path = path
        self.poses: dict[str, dict[str, float]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self.poses = _default_poses()
            self.save()  # deja el archivo listo para editarlo a mano
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.poses = {str(k): {str(m): float(v) for m, v in p.items()} for k, p in data.items()}
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("No se pudo leer %s (%s). Se usan poses por defecto.", self.path, exc)
            self.poses = _default_poses()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.poses, indent=2, ensure_ascii=False), encoding="utf-8")

    def names(self) -> list[str]:
        return list(self.poses)

    def get(self, name: str) -> dict[str, float] | None:
        return self.poses.get(name)

    def put(self, name: str, values: dict[str, float]) -> None:
        self.poses[name] = {k: round(float(v), 2) for k, v in values.items()}
        self.save()

    def remove(self, name: str) -> None:
        self.poses.pop(name, None)
        self.save()


class RoutineStore:
    """Rutinas guardadas: nombre -> episodio de un dataset con sus opciones de ejecución."""

    def __init__(self, path: Path = ROUTINES_FILE):
        self.path = path
        self.routines: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self.routines = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.routines = {str(k): dict(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("No se pudo leer %s (%s).", self.path, exc)
            self.routines = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.routines, indent=2, ensure_ascii=False), encoding="utf-8")

    def names(self) -> list[str]:
        return list(self.routines)

    def get(self, name: str) -> dict | None:
        return self.routines.get(name)

    def put(self, name: str, data: dict) -> None:
        self.routines[name] = dict(data)
        self.save()

    def remove(self, name: str) -> None:
        self.routines.pop(name, None)
        self.save()


def _default_poses() -> dict[str, dict[str, float]]:
    """Poses iniciales.

    "Centro" deja cada articulación en el punto medio de su rango calibrado, que es
    la única pose que se puede afirmar segura sin conocer el montaje real. "Reposo"
    nace como copia y está pensada para reescribirse con «Capturar pose actual».
    """
    centro = {m: (0.0 if m != "gripper" else 50.0) for m in MOTOR_ORDER}
    return {"Centro": centro, "Reposo": dict(centro)}
