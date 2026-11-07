"""Sesión de grabación de datasets, equivalente a `lerobot-record` pero controlada por la GUI.

Se reutiliza `record_loop()` del propio LeRobot en vez de reimplementarlo, así que el
formato del dataset y el pipeline de procesadores son exactamente los del framework.
La diferencia es que el robot y el teleoperador ya vienen conectados (los abre el
servicio de la GUI) y los eventos de teclado se sustituyen por un diccionario que los
botones de la interfaz modifican.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.pipeline_features import (
    aggregate_pipeline_dataset_features,
    create_initial_features,
)
from lerobot.datasets.utils import combine_feature_dicts
from lerobot.datasets.video_utils import VideoEncodingManager
from lerobot.processor import make_default_processors
from lerobot.robots import Robot
from lerobot.scripts.lerobot_record import record_loop
from lerobot.teleoperators import Teleoperator
from lerobot.utils.control_utils import (
    sanity_check_dataset_name,
    sanity_check_dataset_robot_compatibility,
)

log = logging.getLogger(__name__)


def new_events() -> dict[str, bool]:
    """Mismos flags que `lerobot.utils.control_utils.init_keyboard_listener`."""
    return {"exit_early": False, "rerecord_episode": False, "stop_recording": False}


@dataclass
class RecordRequest:
    repo_id: str
    single_task: str
    num_episodes: int
    fps: int
    episode_time_s: float
    reset_time_s: float
    root: str | None = None
    push_to_hub: bool = False
    private: bool = False
    resume: bool = False
    vcodec: str = "libsvtav1"


def validate(req: RecordRequest) -> str | None:
    """Devuelve un mensaje de error si la petición no es utilizable, o None si está bien."""
    if req.repo_id.count("/") != 1 or not all(part.strip() for part in req.repo_id.split("/")):
        return "El repo_id debe tener el formato 'usuario/nombre_dataset'."
    if not req.single_task.strip():
        return "Describe la tarea a grabar (single_task)."
    if req.num_episodes < 1:
        return "El número de episodios debe ser al menos 1."
    if req.episode_time_s <= 0:
        return "La duración del episodio debe ser mayor que 0."
    if req.fps < 1:
        return "Los FPS deben ser al menos 1."
    return None


def run_recording(
    robot: Robot,
    teleop: Teleoperator | None,
    req: RecordRequest,
    events: dict[str, bool],
    on_status: Callable[[str, int, float], None],
) -> LeRobotDataset:
    """Graba `req.num_episodes` episodios usando el robot y teleoperador ya conectados.

    Args:
        on_status: callback (fase, episodio_actual, límite_en_segundos) para la interfaz.

    El robot NO se desconecta al terminar: sigue siendo propiedad del servicio.
    """
    if teleop is None:
        raise ValueError("La grabación necesita el brazo líder conectado para generar acciones.")

    teleop_action_processor, robot_action_processor, robot_observation_processor = (
        make_default_processors()
    )

    dataset_features = combine_feature_dicts(
        aggregate_pipeline_dataset_features(
            pipeline=teleop_action_processor,
            initial_features=create_initial_features(action=robot.action_features),
            use_videos=True,
        ),
        aggregate_pipeline_dataset_features(
            pipeline=robot_observation_processor,
            initial_features=create_initial_features(observation=robot.observation_features),
            use_videos=True,
        ),
    )

    root = Path(req.root).expanduser() if req.root else None
    n_cameras = len(getattr(robot, "cameras", {}) or {})

    if req.resume:
        on_status("Abriendo dataset existente", 0, 0.0)
        dataset = LeRobotDataset(req.repo_id, root=root, batch_encoding_size=1, vcodec=req.vcodec)
        if n_cameras > 0:
            dataset.start_image_writer(num_processes=0, num_threads=4 * n_cameras)
        sanity_check_dataset_robot_compatibility(dataset, robot, req.fps, dataset_features)
    else:
        on_status("Creando dataset", 0, 0.0)
        sanity_check_dataset_name(req.repo_id, None)
        dataset = LeRobotDataset.create(
            req.repo_id,
            req.fps,
            root=root,
            robot_type=robot.name,
            features=dataset_features,
            use_videos=True,
            image_writer_processes=0,
            image_writer_threads=4 * n_cameras,
            batch_encoding_size=1,
            vcodec=req.vcodec,
        )

    loop_kwargs = dict(
        robot=robot,
        events=events,
        fps=req.fps,
        teleop_action_processor=teleop_action_processor,
        robot_action_processor=robot_action_processor,
        robot_observation_processor=robot_observation_processor,
        teleop=teleop,
        single_task=req.single_task,
    )

    try:
        with VideoEncodingManager(dataset):
            recorded = 0
            while recorded < req.num_episodes and not events["stop_recording"]:
                on_status("Grabando episodio", recorded + 1, req.episode_time_s)
                record_loop(dataset=dataset, control_time_s=req.episode_time_s, **loop_kwargs)

                # Pausa sin grabar para reacomodar el entorno (se salta en el último episodio).
                if not events["stop_recording"] and (
                    recorded < req.num_episodes - 1 or events["rerecord_episode"]
                ):
                    on_status("Reiniciando entorno", recorded + 1, req.reset_time_s)
                    record_loop(dataset=None, control_time_s=req.reset_time_s, **loop_kwargs)

                if events["rerecord_episode"]:
                    on_status("Descartando episodio", recorded + 1, 0.0)
                    events["rerecord_episode"] = False
                    events["exit_early"] = False
                    dataset.clear_episode_buffer()
                    continue

                on_status("Guardando episodio", recorded + 1, 0.0)
                dataset.save_episode()
                recorded += 1
    finally:
        on_status("Cerrando dataset", 0, 0.0)
        dataset.finalize()

    if req.push_to_hub:
        on_status("Subiendo a Hugging Face Hub", 0, 0.0)
        dataset.push_to_hub(private=req.private)

    on_status("Grabación finalizada", 0, 0.0)
    return dataset
