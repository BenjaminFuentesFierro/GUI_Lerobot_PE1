"""Rutinas: reproducir en el brazo las acciones de un episodio ya grabado.

Es el equivalente a `lerobot-replay`, pero devolviendo las acciones en memoria para que
el servicio las envíe a su propio ritmo (con velocidad ajustable y repeticiones).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from lerobot.utils.constants import HF_LEROBOT_HOME

log = logging.getLogger(__name__)


@dataclass
class DatasetInfo:
    repo_id: str
    root: Path
    fps: int
    total_episodes: int
    total_frames: int
    features: list[str] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        return any(f.startswith("observation.images") for f in self.features)

    @property
    def label(self) -> str:
        segundos = self.total_frames / self.fps if self.fps else 0
        return f"{self.repo_id}  ({self.total_episodes} ep · {segundos:.0f} s · {self.fps} fps)"


@dataclass
class RoutineRequest:
    repo_id: str
    episode: int
    root: str | None = None
    repeats: int = 1
    speed: float = 1.0
    approach_s: float = 3.0  # tiempo para llegar suavemente a la pose inicial


def discover_datasets(extra_root: str | None = None) -> list[DatasetInfo]:
    """Datasets locales, buscando `<usuario>/<nombre>/meta/info.json`."""
    raices = [HF_LEROBOT_HOME]
    if extra_root:
        raices.append(Path(extra_root).expanduser())

    encontrados: dict[str, DatasetInfo] = {}
    for raiz in raices:
        if not raiz.is_dir():
            continue
        for info_path in raiz.glob("*/*/meta/info.json"):
            root = info_path.parent.parent
            repo_id = f"{root.parent.name}/{root.name}"
            if repo_id in encontrados:
                continue
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("No se pudo leer %s (%s)", info_path, exc)
                continue
            encontrados[repo_id] = DatasetInfo(
                repo_id=repo_id,
                root=root,
                fps=int(info.get("fps") or 30),
                total_episodes=int(info.get("total_episodes") or 0),
                total_frames=int(info.get("total_frames") or 0),
                features=sorted(info.get("features", {})),
            )
    return sorted(encontrados.values(), key=lambda d: d.repo_id)


def load_episode(repo_id: str, episode: int, root: str | Path | None = None) -> tuple[list[dict], int]:
    """Devuelve (acciones, fps) del episodio indicado.

    Cada acción es el mismo diccionario `{'<motor>.pos': valor}` que espera
    `robot.send_action()`.
    """
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.utils.constants import ACTION

    root = Path(root).expanduser() if root else None
    dataset = LeRobotDataset(repo_id, root=root, episodes=[episode])

    # En el formato v3.0 los episodios van en trozos compartidos, hay que filtrar.
    frames = dataset.hf_dataset.filter(lambda x: x["episode_index"] == episode)
    if len(frames) == 0:
        raise ValueError(f"El episodio {episode} no tiene fotogramas en '{repo_id}'.")

    columna = frames.select_columns(ACTION)
    nombres = dataset.features[ACTION]["names"]
    acciones = [
        {nombre: float(valor) for nombre, valor in zip(nombres, columna[i][ACTION], strict=True)}
        for i in range(len(frames))
    ]
    return acciones, int(dataset.fps)
