"""Servicio de robot: un único hilo dueño de los puertos serie.

El bus Feetech no es seguro para uso concurrente, así que **todas** las lecturas y
escrituras ocurren en el hilo de este servicio. La interfaz gráfica sólo encola
comandos y lee instantáneas (`Snapshot`) del estado.

Modos:
    IDLE    -> sólo lee posiciones y cámara
    MANUAL  -> envía continuamente los objetivos de los sliders
    TELEOP  -> el seguidor imita al líder (modo espejo)
    PRESET  -> interpola desde la pose actual hasta una pose guardada
    RECORD  -> ejecuta `record_loop` de LeRobot para grabar un dataset
"""

from __future__ import annotations

import glob
import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .recording import RecordRequest, new_events, run_recording
from .routines import RoutineRequest, load_episode
from .stream_camera import CamaraTolerante, StreamCamera, es_url
from .settings import MOTOR_ORDER, Settings

log = logging.getLogger(__name__)

# Segundos de fallos consecutivos tras los que se asume que el hardware desapareció.
HARDWARE_LOST_S = 3.0

# Tope del refresco en reposo cuando hay cámara. Por encima de esto no se gana
# fluidez visible (la GUI repinta cada 50 ms) y solo se añade tráfico al bus.
VIDEO_IDLE_CAP = 20


class Mode(str, Enum):
    IDLE = "idle"
    MANUAL = "manual"
    TELEOP = "teleop"
    PRESET = "preset"
    RECORD = "record"
    REPLAY = "replay"


MODE_LABELS = {
    Mode.IDLE: "En reposo",
    Mode.MANUAL: "Control manual",
    Mode.TELEOP: "Teleoperación",
    Mode.PRESET: "Yendo a pose",
    Mode.RECORD: "Grabando dataset",
    Mode.REPLAY: "Ejecutando rutina",
}


@dataclass
class RecordStatus:
    active: bool = False
    phase: str = ""
    episode: int = 0
    total: int = 0
    elapsed: float = 0.0
    limit: float = 0.0
    saved: int = 0
    result: str = ""


@dataclass
class ReplayStatus:
    active: bool = False
    phase: str = ""  # "Acercándose al inicio" | "Ejecutando"
    name: str = ""
    repeat: int = 0
    repeats: int = 0
    frame: int = 0
    frames: int = 0
    elapsed: float = 0.0
    duration: float = 0.0
    result: str = ""


@dataclass
class Snapshot:
    connected: bool = False
    busy: bool = False  # conectando / desconectando
    mode: Mode = Mode.IDLE
    torque: bool = False
    has_leader: bool = False
    motors: tuple[str, ...] = ()
    limits: dict[str, tuple[float, float]] = field(default_factory=dict)
    present: dict[str, float] = field(default_factory=dict)
    raw: dict[str, int] = field(default_factory=dict)
    goals: dict[str, float] = field(default_factory=dict)
    hz: float = 0.0
    unit: str = "%"
    frame: np.ndarray | None = None
    frame_id: int = 0
    status: str = "Desconectado"
    error: str = ""
    record: RecordStatus = field(default_factory=RecordStatus)
    replay: ReplayStatus = field(default_factory=ReplayStatus)


def find_serial_ports() -> list[str]:
    """Puertos serie candidatos dentro de WSL/Linux."""
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))


@contextmanager
def observation_tap(robot, callback: Callable[[dict], None]):
    """Intercepta `robot.get_observation()` para publicar estado sin duplicar lecturas.

    Se usa durante la grabación, donde el bucle de LeRobot es quien lee el hardware.
    """
    original = robot.get_observation

    def tapped(*args, **kwargs):
        obs = original(*args, **kwargs)
        try:
            callback(obs)
        except Exception:  # pragma: no cover - nunca debe romper el bucle de control
            log.exception("Fallo publicando el estado desde el tap de observación")
        return obs

    robot.get_observation = tapped  # atributo de instancia: oculta el método de clase
    try:
        yield
    finally:
        try:
            del robot.get_observation
        except AttributeError:
            pass


class ArmService:
    """Hilo de control. La GUI interactúa sólo mediante los métodos públicos."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cmds: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._thread = threading.Thread(target=self._run, name="ArmService", daemon=True)

        # Estado interno (sólo lo toca el hilo del servicio, salvo _events).
        self._robot = None
        self._leader = None
        self._mode = Mode.IDLE
        self._goals: dict[str, float] = {}
        self._preset: dict[str, Any] | None = None
        self._replay: dict[str, Any] | None = None
        # Instante del primer fallo de una racha, para detectar pérdida de hardware.
        self._fail_since: float | None = None
        self._events = new_events()  # lo modifica también la GUI (flags booleanos)
        self._record_req: RecordRequest | None = None
        self._frame_id = 0
        # Último array publicado: evita reconvertir a imagen un fotograma repetido.
        self._last_frame_obj = None
        self._episode_started = 0.0
        self._teleop_procs = None

    # ------------------------------------------------------------- ciclo de vida --
    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout: float = 15.0) -> None:
        self._events["stop_recording"] = True
        self._events["exit_early"] = True
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ---------------------------------------------------------------- comandos --
    def connect(self) -> None:
        self._push("connect", None)

    def disconnect(self) -> None:
        self._push("disconnect", None)

    def set_mode(self, mode: Mode) -> None:
        self._push("mode", mode)

    def set_goal(self, motor: str, value: float) -> None:
        self._push("goal", (motor, float(value)))

    def set_torque(self, enabled: bool) -> None:
        self._push("torque", bool(enabled))

    def goto_pose(self, target: dict[str, float], duration: float) -> None:
        self._push("preset", (dict(target), float(duration)))

    def start_recording(self, req: RecordRequest) -> None:
        self._push("record", req)

    def start_routine(self, req: RoutineRequest, name: str = "") -> None:
        self._push("routine", (req, name))

    def stop_routine(self) -> None:
        self._push("stop_routine", None)

    def emergency_stop(self) -> None:
        """Corta cualquier bucle activo y suelta el torque de inmediato."""
        self._events["stop_recording"] = True
        self._events["exit_early"] = True
        self._push("estop", None)

    def record_event(self, name: str) -> None:
        """`exit_early`, `rerecord_episode` o `stop_recording` (los lee `record_loop`)."""
        if name == "rerecord_episode":
            self._events["rerecord_episode"] = True
            self._events["exit_early"] = True
        elif name == "stop_recording":
            self._events["stop_recording"] = True
            self._events["exit_early"] = True
        else:
            self._events["exit_early"] = True

    def snapshot(self) -> Snapshot:
        with self._lock:
            return replace(self._snap)

    def _push(self, kind: str, payload: Any) -> None:
        self._cmds.put((kind, payload))

    # ------------------------------------------------------------------ bucle --
    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            self._drain()
            if self._stop.is_set():
                break

            if self._robot is not None:
                try:
                    self._tick()
                    self._fail_since = None
                except Exception as exc:  # noqa: BLE001
                    log.exception("Error en el bucle de control")
                    self._fail(f"Error de control: {exc}")
                    if self._fail_since is None:
                        self._fail_since = time.perf_counter()
                    elif time.perf_counter() - self._fail_since > HARDWARE_LOST_S:
                        self._abort_connection(exc)

            period = 1.0 / self._loop_rate()
            dt = time.perf_counter() - t0
            time.sleep(max(0.0, period - dt))
            self._update(hz=1.0 / max(1e-6, time.perf_counter() - t0))

        self._teardown()

    def _abort_connection(self, exc: Exception) -> None:
        """Suelta la conexión cuando el hardware deja de responder de forma sostenida."""
        log.error("Se pierde la conexión con el hardware: %s", exc)
        self._fail_since = None
        self._teardown()
        self._update(
            connected=False,
            busy=False,
            mode=Mode.IDLE,
            torque=False,
            has_leader=False,
            motors=(),
            limits={},
            present={},
            raw={},
            goals={},
            frame=None,
            status="Desconectado",
            error=(
                "Se perdió la conexión con el hardware: dejó de responder durante "
                f"{HARDWARE_LOST_S:g} s.\n"
                "En WSL la causa habitual es que usbipd soltó los dispositivos. "
                "Compruébalo con 'ls /dev/ttyACM*' y vuelve a adjuntarlos:\n"
                "    usbipd attach --wsl --busid <BUSID>"
            ),
        )

    def _loop_rate(self) -> float:
        """Frecuencia del bucle según el modo, acotada a algo razonable."""
        if self._mode is Mode.IDLE:
            base = max(1, self.settings.idle_fps)
            # Con cámara, en reposo manda el vídeo: a 10 Hz la imagen se ve a tirones
            # aunque la cámara entregue más. El coste extra es un sync_read por ciclo.
            if self._robot is not None and getattr(self._robot, "cameras", None):
                return max(base, min(self.settings.camera.fps, VIDEO_IDLE_CAP))
            return base
        if self._mode is Mode.REPLAY and self._replay is not None:
            # Al acelerar una rutina hay que muestrear más rápido o se pierden fotogramas.
            return min(120.0, max(self.settings.fps, self._replay["fps"] * self._replay["speed"]))
        return max(1, self.settings.fps)

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(kind, payload)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error atendiendo el comando %s", kind)
                self._fail(str(exc))

    def _handle(self, kind: str, payload: Any) -> None:
        if kind == "connect":
            self._do_connect()
        elif kind == "disconnect":
            self._do_disconnect()
        elif kind == "mode":
            self._set_mode(payload)
        elif kind == "goal":
            motor, value = payload
            self._goals[motor] = value
        elif kind == "torque":
            self._set_torque(payload)
        elif kind == "preset":
            target, duration = payload
            self._start_preset(target, duration)
        elif kind == "record":
            self._do_record(payload)
        elif kind == "routine":
            req, name = payload
            self._do_routine(req, name)
        elif kind == "stop_routine":
            self._stop_routine("Rutina detenida por el usuario")
        elif kind == "estop":
            self._do_estop()

    # ------------------------------------------------------------- conexión --
    def _do_connect(self) -> None:
        if self._robot is not None:
            return
        from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
        from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

        s = self.settings
        self._update(busy=True, status="Conectando…", error="")

        cameras = {}
        if s.camera.enabled:
            cameras[s.camera.name] = OpenCVCameraConfig(
                index_or_path=_as_index_or_path(s.camera.index_or_path),
                fps=s.camera.fps,
                width=s.camera.width,
                height=s.camera.height,
                fourcc=s.camera.fourcc.strip() or None,
            )

        camara_stream = s.camera.enabled and es_url(s.camera.index_or_path)
        robot = SO101Follower(
            SO101FollowerConfig(
                port=s.follower_port,
                id=s.follower_id,
                use_degrees=s.use_degrees,
                max_relative_target=s.max_relative_target,
                cameras=cameras,
            )
        )
        leader = None
        if s.use_leader:
            leader = SO101Leader(
                SO101LeaderConfig(port=s.leader_port, id=s.leader_id, use_degrees=s.use_degrees)
            )

        if camara_stream:
            # Fuera del tunel USB/IP: el video llega por TCP desde Windows, asi que
            # no compite con los puertos serie de los brazos.
            robot.cameras = {
                s.camera.name: StreamCamera(
                    s.camera.index_or_path, s.camera.width, s.camera.height, s.camera.fps
                )
            }

        # Los brazos mandan: una camara que falle no debe tumbar la conexion.
        robot.cameras = {
            nombre: CamaraTolerante(cam, s.camera.height, s.camera.width)
            for nombre, cam in robot.cameras.items()
        }

        try:
            _connect_device(robot, "brazo seguidor", "robot", "so101_follower")
            if leader is not None:
                _connect_device(leader, "brazo líder", "teleop", "so101_leader")
        except Exception as exc:  # noqa: BLE001
            _safe_disconnect(leader)
            _safe_disconnect(robot)
            self._update(busy=False, connected=False, status="Desconectado", error=_explain(exc, s))
            return

        from lerobot.processor import make_default_processors

        self._robot = robot
        self._leader = leader
        # Mismos pipelines que usa `lerobot-teleoperate` (identidad por defecto).
        self._teleop_procs = make_default_processors()
        self._mode = Mode.IDLE
        self._goals = {}
        motors = tuple(_ordered(robot.bus.motors))
        self._update(
            busy=False,
            connected=True,
            torque=True,
            has_leader=leader is not None,
            motors=motors,
            limits=_motor_limits(robot),
            unit="°" if s.use_degrees else "%",
            mode=Mode.IDLE,
            status="Conectado",
            error="",
        )

    def _do_disconnect(self) -> None:
        self._teardown()
        self._update(
            connected=False,
            busy=False,
            mode=Mode.IDLE,
            torque=False,
            has_leader=False,
            motors=(),
            limits={},
            present={},
            raw={},
            goals={},
            frame=None,
            status="Desconectado",
        )

    def _teardown(self) -> None:
        _safe_disconnect(self._leader)
        _safe_disconnect(self._robot)
        self._leader = None
        self._robot = None
        self._mode = Mode.IDLE
        self._preset = None
        self._replay = None

    # ----------------------------------------------------------------- modos --
    def _set_mode(self, mode: Mode) -> None:
        if self._robot is None or mode is self._mode:
            return
        if mode is Mode.TELEOP and self._leader is None:
            self._fail("El brazo líder no está conectado: no se puede teleoperar.")
            return

        if mode in (Mode.MANUAL, Mode.TELEOP):
            self._set_torque(True)
        if mode is Mode.MANUAL:
            # Arranca desde la posición actual para que no haya saltos bruscos.
            self._goals = dict(self._read_present())
        self._preset = None
        self._replay = None
        self._mode = mode
        self._update(mode=mode, goals=dict(self._goals), status=MODE_LABELS[mode])

    def _set_torque(self, enabled: bool) -> None:
        if self._robot is None:
            return
        if enabled:
            self._robot.bus.enable_torque()
        else:
            if self._mode in (Mode.MANUAL, Mode.TELEOP, Mode.PRESET, Mode.REPLAY):
                self._mode = Mode.IDLE
                self._preset = None
                self._replay = None
            self._robot.bus.disable_torque()
        self._update(torque=enabled, mode=self._mode)

    def _do_estop(self) -> None:
        self._mode = Mode.IDLE
        self._preset = None
        self._replay = None
        self._events = new_events()
        if self._robot is not None:
            try:
                self._robot.bus.disable_torque()
            except Exception:  # noqa: BLE001
                log.exception("No se pudo desactivar el torque en el paro de emergencia")
        self._update(mode=Mode.IDLE, torque=False, status="PARO DE EMERGENCIA · torque liberado")

    def _start_preset(self, target: dict[str, float], duration: float) -> None:
        if self._robot is None:
            return
        self._set_torque(True)
        start = dict(self._read_present())
        target = {m: float(v) for m, v in target.items() if m in start}
        if not target:
            self._fail("La pose guardada no coincide con los motores del brazo.")
            return
        self._replay = None
        self._preset = {"start": start, "target": target, "t0": time.perf_counter(), "dur": max(0.2, duration)}
        self._mode = Mode.PRESET
        self._update(mode=Mode.PRESET, status="Moviendo a la pose…")

    # ------------------------------------------------------------------ tick --
    def _tick(self) -> None:
        obs = self._robot.get_observation()
        self._publish_observation(obs)

        if self._mode is Mode.TELEOP:
            teleop_proc, robot_proc, _ = self._teleop_procs
            raw_action = self._leader.get_action()
            action = robot_proc((teleop_proc((raw_action, obs)), obs))
            self._robot.send_action(action)
        elif self._mode is Mode.MANUAL:
            if self._goals:
                self._robot.send_action({f"{m}.pos": v for m, v in self._goals.items()})
        elif self._mode is Mode.PRESET:
            self._tick_preset()
        elif self._mode is Mode.REPLAY:
            self._tick_replay()

    def _tick_preset(self) -> None:
        p = self._preset
        if p is None:
            self._mode = Mode.IDLE
            return
        alpha = min(1.0, (time.perf_counter() - p["t0"]) / p["dur"])
        # Suavizado coseno: acelera y frena de forma progresiva.
        smooth = 0.5 - 0.5 * np.cos(np.pi * alpha)
        goal = {m: p["start"][m] + (t - p["start"][m]) * smooth for m, t in p["target"].items()}
        self._robot.send_action({f"{m}.pos": v for m, v in goal.items()})
        if alpha >= 1.0:
            self._goals = dict(p["target"])
            self._preset = None
            self._mode = Mode.IDLE
            self._update(mode=Mode.IDLE, goals=dict(self._goals), status="Pose alcanzada")

    # ---------------------------------------------------------------- rutinas --
    def _do_routine(self, req: RoutineRequest, name: str) -> None:
        if self._robot is None:
            self._fail("Conecta el robot antes de ejecutar una rutina.")
            return
        if self._mode in (Mode.RECORD, Mode.TELEOP):
            self._fail("Detén la teleoperación o la grabación antes de ejecutar una rutina.")
            return

        etiqueta = name or f"{req.repo_id} · episodio {req.episode}"
        self._update(
            status="Cargando episodio…",
            error="",
            replay=ReplayStatus(active=True, phase="Cargando episodio", name=etiqueta),
        )
        try:
            acciones, fps = load_episode(req.repo_id, req.episode, req.root)
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudo cargar el episodio")
            self._update(
                status="Conectado",
                error=f"No se pudo cargar el episodio: {exc}",
                replay=ReplayStatus(result="Carga fallida"),
            )
            return

        faltantes = {k.removesuffix(".pos") for k in acciones[0]} - set(self._robot.bus.motors)
        if faltantes:
            self._update(
                status="Conectado",
                error=f"El episodio usa motores que este brazo no tiene: {', '.join(sorted(faltantes))}",
                replay=ReplayStatus(result="Episodio incompatible"),
            )
            return

        self._set_torque(True)
        self._replay = {
            "actions": acciones,
            "fps": fps,
            "speed": max(0.1, min(4.0, req.speed)),
            "repeat": 1,
            "repeats": max(1, req.repeats),
            "approach_s": max(0.5, req.approach_s),
            "name": etiqueta,
            "phase": "approach",
            "approach": None,
            "t0": 0.0,
            "index": 0,
        }
        self._begin_approach()
        self._mode = Mode.REPLAY
        self._update(mode=Mode.REPLAY, status=f"Ejecutando rutina «{etiqueta}»")

    def _begin_approach(self) -> None:
        """Lleva el brazo suavemente hasta la primera pose del episodio.

        Sin esto, el primer `send_action` sería un salto brusco desde donde esté el brazo
        hasta el inicio de la rutina.
        """
        r = self._replay
        objetivo = {k.removesuffix(".pos"): v for k, v in r["actions"][0].items()}
        r["approach"] = {
            "start": dict(self._read_present()),
            "target": objetivo,
            "t0": time.perf_counter(),
            "dur": r["approach_s"],
        }
        r["phase"] = "approach"

    def _tick_replay(self) -> None:
        r = self._replay
        if r is None:
            self._mode = Mode.IDLE
            return

        total = len(r["actions"])
        duracion = total / (r["fps"] * r["speed"])

        if r["phase"] == "approach":
            a = r["approach"]
            alpha = min(1.0, (time.perf_counter() - a["t0"]) / a["dur"])
            smooth = 0.5 - 0.5 * np.cos(np.pi * alpha)
            goal = {m: a["start"].get(m, t) + (t - a["start"].get(m, t)) * smooth for m, t in a["target"].items()}
            self._robot.send_action({f"{m}.pos": v for m, v in goal.items()})
            self._publish_replay(r, "Acercándose al inicio", 0, total, alpha * a["dur"], a["dur"])
            if alpha >= 1.0:
                r["phase"] = "playing"
                r["t0"] = time.perf_counter()
            return

        # Reproducción guiada por reloj: si un ciclo se retrasa, se salta al fotograma
        # que toca en vez de acumular deriva.
        transcurrido = (time.perf_counter() - r["t0"]) * r["speed"]
        indice = int(transcurrido * r["fps"])

        if indice >= total:
            if r["repeat"] >= r["repeats"]:
                self._stop_routine(f"Rutina «{r['name']}» completada ({r['repeats']}×)")
                return
            r["repeat"] += 1
            self._begin_approach()  # vuelve al inicio con suavidad antes de repetir
            return

        r["index"] = indice
        self._robot.send_action(r["actions"][indice])
        self._publish_replay(r, "Ejecutando", indice + 1, total, transcurrido / r["speed"], duracion)

    def _publish_replay(self, r, fase: str, frame: int, frames: int, elapsed: float, duration: float) -> None:
        self._update(
            replay=ReplayStatus(
                active=True,
                phase=fase,
                name=r["name"],
                repeat=r["repeat"],
                repeats=r["repeats"],
                frame=frame,
                frames=frames,
                elapsed=elapsed,
                duration=duration,
            )
        )

    def _stop_routine(self, mensaje: str) -> None:
        if self._replay is None and self._mode is not Mode.REPLAY:
            return
        self._replay = None
        if self._mode is Mode.REPLAY:
            self._mode = Mode.IDLE
        self._update(mode=self._mode, status="Conectado", replay=ReplayStatus(result=mensaje))

    def _read_present(self) -> dict[str, float]:
        return self._robot.bus.sync_read("Present_Position")

    def _publish_observation(self, obs: dict) -> None:
        present = {k.removesuffix(".pos"): float(v) for k, v in obs.items() if k.endswith(".pos")}
        raw = {m: t for m in present if (t := raw_ticks(self._robot, m, present[m])) is not None}
        frame = next(
            (v for v in obs.values() if isinstance(v, np.ndarray) and v.ndim == 3),
            None,
        )
        fields: dict[str, Any] = {"present": present, "raw": raw}
        # `async_read` devuelve el mismo array mientras no llegue uno nuevo: si el
        # bucle va más rápido que la cámara, no hay que repintar lo ya pintado.
        if frame is not None and frame is not self._last_frame_obj:
            self._last_frame_obj = frame
            self._frame_id += 1
            fields["frame"] = frame
            fields["frame_id"] = self._frame_id
        self._update(**fields)

    # ------------------------------------------------------------ grabación --
    def _do_record(self, req: RecordRequest) -> None:
        if self._robot is None:
            self._fail("Conecta el robot antes de grabar.")
            return
        if self._leader is None:
            self._fail("La grabación necesita el brazo líder conectado.")
            return

        self._events = new_events()
        self._record_req = req
        self._replay = None
        self._mode = Mode.RECORD
        self._set_torque(True)
        status = RecordStatus(active=True, phase="Preparando", total=req.num_episodes)
        self._update(mode=Mode.RECORD, record=status, status="Grabando dataset", error="")

        def on_status(phase: str, episode: int, limit: float) -> None:
            self._episode_started = time.perf_counter()
            with self._lock:
                rec = self._snap.record
                saved = rec.saved + 1 if phase == "Guardando episodio" else rec.saved
                self._snap = replace(
                    self._snap,
                    record=RecordStatus(
                        active=True,
                        phase=phase,
                        episode=episode,
                        total=req.num_episodes,
                        elapsed=0.0,
                        limit=limit,
                        saved=saved,
                    ),
                )

        def on_obs(obs: dict) -> None:
            self._publish_observation(obs)
            with self._lock:
                rec = self._snap.record
                self._snap = replace(
                    self._snap,
                    record=replace(rec, elapsed=time.perf_counter() - self._episode_started),
                )

        result = ""
        try:
            with observation_tap(self._robot, on_obs):
                dataset = run_recording(self._robot, self._leader, req, self._events, on_status)
            result = f"Dataset guardado en {dataset.root}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Fallo durante la grabación")
            result = f"Grabación interrumpida: {exc}"
            self._update(error=str(exc))
        finally:
            self._events = new_events()
            self._mode = Mode.IDLE
            with self._lock:
                rec = self._snap.record
                self._snap = replace(
                    self._snap,
                    mode=Mode.IDLE,
                    status="Conectado",
                    record=replace(rec, active=False, phase="", elapsed=0.0, result=result),
                )

    # ----------------------------------------------------------------- utils --
    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._snap = replace(self._snap, **fields)

    def _fail(self, message: str) -> None:
        self._update(error=message)


# --------------------------------------------------------------------- helpers --
def _as_index_or_path(value: str) -> int | Path | str:
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    # Una URL se deja tal cual: Path() la deformaria a "http:/host".
    return value if es_url(value) else Path(value)


def _connect_device(device, label: str, cli_key: str, cli_type: str) -> None:
    """Conecta un brazo sin usar `input()`.

    `SOFollower.connect()` / `SOLeader.connect()` preguntan por consola cuando la
    calibración de los motores no coincide con el archivo guardado, lo que colgaría
    la interfaz. Aquí se repiten los mismos pasos (bus -> calibración -> cámaras ->
    configure) escribiendo directamente el archivo de calibración existente.
    """
    _connect_bus_with_retry(device.bus)
    if not device.bus.is_calibrated:
        if not device.calibration:
            raise RuntimeError(
                f"No hay archivo de calibración para el {label} con id '{device.id}'.\n"
                f"Ejecútala primero en la terminal:\n"
                f"    lerobot-calibrate --{cli_key}.type={cli_type} "
                f"--{cli_key}.port={device.bus.port} --{cli_key}.id={device.id}"
            )
        # El torque bloquea la EEPROM (registro Lock), hay que soltarlo para escribir.
        device.bus.disable_torque()
        device.bus.write_calibration(device.calibration)
    for nombre, cam in getattr(device, "cameras", {}).items():
        try:
            cam.connect()
        except Exception as exc:  # noqa: BLE001
            # Sin video se puede trabajar; sin brazos no. Se sigue adelante.
            log.warning("La camara '%s' no pudo abrirse (%s); se continua sin video", nombre, exc)
    device.configure()


def _connect_bus_with_retry(bus, intentos: int = 3, espera: float = 0.4) -> None:
    """Abre el bus reintentando el handshake.

    Sobre WSL el puerto serie viaja por usbipd y de vez en cuando se corrompe la respuesta
    del `broadcast_ping`: aparecen motores «ausentes» o con números de modelo imposibles.
    Es transitorio, así que se reintenta antes de molestar al usuario con el error.
    """
    ultimo: Exception | None = None
    for intento in range(1, intentos + 1):
        try:
            bus.connect()
            if intento > 1:
                log.info("Bus %s conectado en el intento %d", bus.port, intento)
            return
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            log.warning("Handshake fallido en %s (intento %d/%d)", bus.port, intento, intentos)
            # El puerto queda abierto si falló el handshake, hay que cerrarlo para reintentar.
            try:
                if bus.is_connected:
                    bus.disconnect(disable_torque=False)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo cerrar el puerto entre reintentos")
            if intento < intentos:
                time.sleep(espera)
    raise ultimo  # type: ignore[misc]


def _safe_disconnect(device) -> None:
    """Suelta cámaras y puerto serie por separado.

    No se usa `device.disconnect()` porque `is_connected` exige que bus *y* cámaras estén
    conectados: si la conexión falló a medias, el puerto se quedaría abierto y el siguiente
    intento fallaría por dispositivo ocupado.
    """
    if device is None:
        return
    for cam in getattr(device, "cameras", {}).values():
        try:
            if cam.is_connected:
                cam.disconnect()
        except Exception:  # noqa: BLE001
            log.exception("Error cerrando la cámara")

    bus = getattr(device, "bus", None)
    if bus is None:
        return
    try:
        if bus.is_connected:
            bus.disconnect(getattr(device.config, "disable_torque_on_disconnect", True))
    except Exception:  # noqa: BLE001
        log.exception("Error cerrando el puerto %s", getattr(bus, "port", "?"))


def _motor_limits(robot) -> dict[str, tuple[float, float]]:
    """Rango útil de cada slider según el modo de normalización de cada motor."""
    from lerobot.motors import MotorNormMode

    limits: dict[str, tuple[float, float]] = {}
    for name in _ordered(robot.bus.motors):
        motor = robot.bus.motors[name]
        if motor.norm_mode is MotorNormMode.RANGE_0_100:
            limits[name] = (0.0, 100.0)
        elif motor.norm_mode is MotorNormMode.DEGREES:
            cal = robot.calibration.get(name)
            span = ((cal.range_max - cal.range_min) * 360.0 / 4095.0 / 2.0) if cal else 180.0
            limits[name] = (-span, span)
        else:
            limits[name] = (-100.0, 100.0)
    return limits


def _ordered(names) -> list[str]:
    """Motores en orden físico (base -> pinza); los desconocidos van al final."""
    known = [m for m in MOTOR_ORDER if m in names]
    return known + [m for m in names if m not in MOTOR_ORDER]


def raw_ticks(robot, motor: str, normalized: float) -> int | None:
    """Convierte un valor normalizado al conteo bruto del encoder (0..4095).

    Replica `FeetechMotorsBus._unnormalize` sin ocupar el bus, para poder mostrar
    en pantalla los mismos números que se ven durante la calibración.
    """
    from lerobot.motors import MotorNormMode

    cal = robot.calibration.get(motor)
    if cal is None or cal.range_max == cal.range_min:
        return None
    lo, hi = cal.range_min, cal.range_max
    mode = robot.bus.motors[motor].norm_mode
    if mode is MotorNormMode.RANGE_0_100:
        value = min(100.0, max(0.0, normalized))
        return int((value / 100) * (hi - lo) + lo)
    if mode is MotorNormMode.RANGE_M100_100:
        value = min(100.0, max(-100.0, normalized))
        return int(((value + 100) / 200) * (hi - lo) + lo)
    return int((normalized * 4095.0 / 360.0) + (lo + hi) / 2)


def _explain(exc: Exception, settings: Settings) -> str:
    """Traduce los fallos de conexión más habituales en WSL a algo accionable."""
    # LeRobot envuelve el error original en ConnectionError, hay que mirar la cadena.
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = "\n".join(str(e) for e in chain)

    if any(isinstance(e, PermissionError) for e in chain) or "Permission denied" in text:
        return (
            "Permiso denegado sobre el puerto. Ejecuta en la terminal de WSL:\n"
            "    sudo chmod 666 /dev/ttyACM*"
        )
    if "No such file or directory" in text or "Could not connect on port" in text:
        disponibles = ", ".join(find_serial_ports()) or "ninguno"
        return (
            f"No se pudo abrir el puerto. Puertos visibles ahora: {disponibles}.\n"
            f"Revisa que usbipd tenga los dispositivos adjuntados "
            f"(seguidor={settings.follower_port}, líder={settings.leader_port})."
        )
    return str(exc)
