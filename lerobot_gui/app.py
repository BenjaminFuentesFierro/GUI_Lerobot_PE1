"""Interfaz gráfica CustomTkinter para el kit SO-101 sobre LeRobot."""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from .camera import CameraPreview
from .recording import RecordRequest, validate
from .routines import DatasetInfo, RoutineRequest, discover_datasets
from .service import MODE_LABELS, ArmService, Mode, Snapshot, find_serial_ports
from .stream_camera import comprobar as comprobar_stream
from .stream_camera import url_por_defecto
from .settings import MOTOR_LABELS, PoseStore, RoutineStore, Settings

log = logging.getLogger(__name__)

REFRESH_MS = 50
PREVIEW_W = 420

COLOR_OK = "#2e7d32"
COLOR_WARN = "#ed6c02"
COLOR_ERR = "#b3261e"
COLOR_MUTED = "gray60"


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LeRobot SO-101 · Panel de control")
        self.geometry("1500x900")
        self.minsize(1200, 780)

        self.settings = Settings.load()
        self.poses = PoseStore()
        self.routines = RoutineStore()
        self._datasets: list[DatasetInfo] = []
        self.service = ArmService(self.settings)
        self.service.start()
        self.preview = CameraPreview()

        self._sliders: dict[str, ctk.CTkSlider] = {}
        self._slider_values: dict[str, ctk.CTkLabel] = {}
        self._built_motors: tuple[str, ...] = ()
        self._syncing = False
        # Interruptores en espera de confirmación del servicio (evita parpadeos).
        self._pending: dict[str, tuple[object, float]] = {}
        # Último `configure()` aplicado a cada widget: redibujar 20 veces por segundo
        # widgets que no cambiaron cuesta CPU sin aportar nada.
        self._cfg_cache: dict[object, dict[str, object]] = {}
        self._photo: ImageTk.PhotoImage | None = None
        self._last_frame_id = -1
        self._last_error = ""
        self._last_status = ""
        self._last_record_result = ""
        self._last_routine_result = ""
        self._cam_scan_running = False
        # Un escaneo silencioso rellena la lista sin abrir diálogos ni pisar ajustes.
        self._cam_scan_silent = False
        # Resultado del escaneo de cámaras: lo publica el hilo, lo recoge _apply.
        self._cam_result: tuple[list, str] | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(REFRESH_MS, self._refresh)

    # =============================================================== interfaz ==
    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_joints()
        self._build_center()
        self._build_right()
        self._build_statusbar()
        # Al final: necesitan el panel de registro, que crea `_build_right`.
        self._reload_datasets()
        self._detect_cameras(silencioso=True)

    # ------------------------------------------------------------- cabecera --
    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", padx=0, pady=(0, 8))
        bar.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(
            bar, text="SO-101 · LeRobot", font=ctk.CTkFont(size=18, weight="bold")
        ).grid(row=0, column=0, padx=(16, 20), pady=12)

        self.btn_connect = ctk.CTkButton(bar, text="Conectar", width=130, command=self._toggle_connection)
        self.btn_connect.grid(row=0, column=1, padx=6, pady=12)

        self.lbl_conn = ctk.CTkLabel(bar, text="● Desconectado", text_color=COLOR_MUTED)
        self.lbl_conn.grid(row=0, column=2, padx=10)

        self.lbl_mode = ctk.CTkLabel(bar, text="", text_color=COLOR_MUTED)
        self.lbl_mode.grid(row=0, column=3, padx=10)

        self.btn_estop = ctk.CTkButton(
            bar,
            text="■  PARO DE EMERGENCIA",
            width=230,
            height=40,
            fg_color=COLOR_ERR,
            hover_color="#8c1d18",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._estop,
        )
        self.btn_estop.grid(row=0, column=5, padx=16, pady=8, sticky="e")

    # ---------------------------------------------------------- articulaciones --
    def _build_joints(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=(0, 6))
        panel.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            panel, text="Articulaciones", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))

        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 8))

        self.var_manual = ctk.BooleanVar(value=False)
        self.sw_manual = ctk.CTkSwitch(
            controls, text="Control manual", variable=self.var_manual, command=self._toggle_manual
        )
        self.sw_manual.grid(row=0, column=0, padx=6, pady=4, sticky="w")

        self.var_torque = ctk.BooleanVar(value=False)
        self.sw_torque = ctk.CTkSwitch(
            controls, text="Torque", variable=self.var_torque, command=self._toggle_torque
        )
        self.sw_torque.grid(row=0, column=1, padx=16, pady=4, sticky="w")

        self.frm_sliders = ctk.CTkScrollableFrame(panel, label_text="")
        self.frm_sliders.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 14))
        self.frm_sliders.grid_columnconfigure(0, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        self.lbl_no_motors = ctk.CTkLabel(
            self.frm_sliders,
            text="Conecta el brazo seguidor para ver los motores.",
            text_color=COLOR_MUTED,
        )
        self.lbl_no_motors.grid(row=0, column=0, pady=30)

    def _build_sliders(self, motors: tuple[str, ...], limits: dict[str, tuple[float, float]]) -> None:
        for widget in self.frm_sliders.winfo_children():
            widget.destroy()
        self._sliders.clear()
        self._slider_values.clear()
        self._cfg_cache.clear()  # los widgets cacheados acaban de desaparecer

        for row, motor in enumerate(motors):
            lo, hi = limits.get(motor, (-100.0, 100.0))
            block = ctk.CTkFrame(self.frm_sliders, fg_color="transparent")
            block.grid(row=row, column=0, sticky="ew", pady=(6, 10))
            block.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                block, text=MOTOR_LABELS.get(motor, motor), font=ctk.CTkFont(size=13, weight="bold")
            ).grid(row=0, column=0, sticky="w", padx=4)

            value = ctk.CTkLabel(block, text="—", text_color=COLOR_MUTED, font=ctk.CTkFont(size=12))
            value.grid(row=0, column=1, sticky="e", padx=4)
            self._slider_values[motor] = value

            slider = ctk.CTkSlider(
                block,
                from_=lo,
                to=hi,
                command=lambda v, m=motor: self._on_slider(m, v),
            )
            slider.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
            slider.configure(state="disabled")
            self._sliders[motor] = slider

            scale = ctk.CTkLabel(
                block,
                text=f"{lo:g} … {hi:g}",
                text_color=COLOR_MUTED,
                font=ctk.CTkFont(size=10),
            )
            scale.grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

        self._built_motors = motors

    # ----------------------------------------------------------------- centro --
    def _build_center(self) -> None:
        tabs = ctk.CTkTabview(self)
        tabs.grid(row=1, column=1, sticky="nsew", padx=6, pady=(0, 6))
        self.tab_teleop = tabs.add("Teleoperación y poses")
        self.tab_routines = tabs.add("Rutinas")
        self.tab_record = tabs.add("Grabación de dataset")
        self.tab_config = tabs.add("Ajustes")
        self._build_tab_teleop()
        self._build_tab_routines()
        self._build_tab_record()
        self._build_tab_config()

    def _build_tab_teleop(self) -> None:
        tab = self.tab_teleop
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        box = ctk.CTkFrame(tab)
        box.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            box, text="Teleoperación (modo espejo)", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(
            box,
            text="El brazo seguidor copia la postura del brazo líder en tiempo real.",
            text_color=COLOR_MUTED,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=14, pady=(2, 8))

        self.btn_teleop = ctk.CTkButton(
            box, text="▶  Iniciar teleoperación", height=42, command=self._toggle_teleop, state="disabled"
        )
        self.btn_teleop.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))

        poses = ctk.CTkFrame(tab)
        poses.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        poses.grid_columnconfigure(0, weight=1)
        poses.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            poses, text="Poses predefinidas", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))

        head = ctk.CTkFrame(poses, fg_color="transparent")
        head.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 4))
        ctk.CTkLabel(head, text="Duración del movimiento (s):").grid(row=0, column=0, padx=(0, 8))
        self.ent_pose_dur = ctk.CTkEntry(head, width=70)
        self.ent_pose_dur.insert(0, f"{self.settings.preset_duration_s:g}")
        self.ent_pose_dur.grid(row=0, column=1)
        ctk.CTkButton(head, text="Capturar pose actual", width=170, command=self._capture_pose).grid(
            row=0, column=2, padx=(16, 0)
        )

        self.frm_poses = ctk.CTkScrollableFrame(poses)
        self.frm_poses.grid(row=2, column=0, sticky="nsew", padx=14, pady=(4, 14))
        self.frm_poses.grid_columnconfigure(0, weight=1)
        self._render_poses()

    def _build_tab_routines(self) -> None:
        tab = self.tab_routines
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        box = ctk.CTkFrame(tab)
        box.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            box, text="Ejecutar un episodio grabado", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(
            box,
            text="El brazo repite las mismas acciones que grabaste. Primero se acerca despacio "
            "a la postura inicial del episodio para no dar un tirón.",
            text_color=COLOR_MUTED,
            justify="left",
            wraplength=560,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 10))

        ctk.CTkLabel(box, text="Dataset").grid(row=2, column=0, sticky="w", padx=14, pady=4)
        self.cmb_dataset = ctk.CTkComboBox(box, values=["(buscando…)"], command=self._on_dataset_change)
        self.cmb_dataset.grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        ctk.CTkButton(box, text="Actualizar", width=100, command=self._reload_datasets).grid(
            row=2, column=2, padx=(0, 14), pady=4
        )

        ctk.CTkLabel(box, text="Episodio").grid(row=3, column=0, sticky="w", padx=14, pady=4)
        self.opt_episode = ctk.CTkOptionMenu(box, values=["0"], width=120)
        self.opt_episode.grid(row=3, column=1, sticky="w", padx=8, pady=4)

        params = ctk.CTkFrame(box, fg_color="transparent")
        params.grid(row=4, column=0, columnspan=3, sticky="ew", padx=14, pady=(6, 4))
        ctk.CTkLabel(params, text="Repeticiones").grid(row=0, column=0, padx=(0, 6))
        self.ent_repeats = ctk.CTkEntry(params, width=60)
        self.ent_repeats.insert(0, "1")
        self.ent_repeats.grid(row=0, column=1, padx=(0, 20))
        ctk.CTkLabel(params, text="Acercamiento (s)").grid(row=0, column=2, padx=(0, 6))
        self.ent_approach = ctk.CTkEntry(params, width=60)
        self.ent_approach.insert(0, "3")
        self.ent_approach.grid(row=0, column=3, padx=(0, 20))
        ctk.CTkLabel(params, text="Velocidad").grid(row=0, column=4, padx=(0, 6))
        self.sld_speed = ctk.CTkSlider(
            params, from_=0.25, to=2.0, number_of_steps=7, width=140, command=self._on_speed
        )
        self.sld_speed.set(1.0)
        self.sld_speed.grid(row=0, column=5)
        self.lbl_speed = ctk.CTkLabel(params, text="1.00×", width=54)
        self.lbl_speed.grid(row=0, column=6, padx=(6, 0))

        acciones = ctk.CTkFrame(box, fg_color="transparent")
        acciones.grid(row=5, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 12))
        acciones.grid_columnconfigure((0, 1), weight=1)
        self.btn_run_routine = ctk.CTkButton(
            acciones, text="▶  Ejecutar", height=42, command=self._run_routine, state="disabled"
        )
        self.btn_run_routine.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_save_routine = ctk.CTkButton(
            acciones,
            text="Guardar como rutina",
            height=42,
            fg_color="transparent",
            border_width=1,
            command=self._save_routine,
        )
        self.btn_save_routine.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        progreso = ctk.CTkFrame(tab)
        progreso.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        progreso.grid_columnconfigure(0, weight=1)
        self.lbl_routine_phase = ctk.CTkLabel(progreso, text="Sin rutina en curso", anchor="w")
        self.lbl_routine_phase.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 2))
        self.bar_routine = ctk.CTkProgressBar(progreso)
        self.bar_routine.set(0)
        self.bar_routine.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        self.lbl_routine_detail = ctk.CTkLabel(progreso, text="", anchor="w", text_color=COLOR_MUTED)
        self.lbl_routine_detail.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 6))
        self.btn_stop_routine = ctk.CTkButton(
            progreso,
            text="■  Detener rutina",
            state="disabled",
            fg_color=COLOR_ERR,
            hover_color="#8c1d18",
            command=self._stop_routine,
        )
        self.btn_stop_routine.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

        guardadas = ctk.CTkFrame(tab)
        guardadas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        guardadas.grid_columnconfigure(0, weight=1)
        guardadas.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            guardadas, text="Rutinas guardadas", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        self.frm_routines = ctk.CTkScrollableFrame(guardadas)
        self.frm_routines.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.frm_routines.grid_columnconfigure(0, weight=1)

        self._render_routines()

    def _build_tab_record(self) -> None:
        tab = self.tab_record
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        form = ctk.CTkScrollableFrame(tab)
        form.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        form.grid_columnconfigure(1, weight=1)
        d = self.settings.dataset

        self.ent_repo = self._field(form, 0, "Repositorio (usuario/dataset)", d.repo_id)
        self.ent_task = self._field(form, 1, "Tarea (single_task)", d.single_task)
        self.ent_eps = self._field(form, 2, "Nº de episodios", str(d.num_episodes), width=110)
        self.ent_ep_time = self._field(form, 3, "Duración por episodio (s)", f"{d.episode_time_s:g}", width=110)
        self.ent_reset_time = self._field(form, 4, "Pausa entre episodios (s)", f"{d.reset_time_s:g}", width=110)
        self.ent_root = self._field(form, 5, "Carpeta destino (opcional)", d.root)

        opts = ctk.CTkFrame(form, fg_color="transparent")
        opts.grid(row=6, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))
        self.var_push = ctk.BooleanVar(value=d.push_to_hub)
        self.var_private = ctk.BooleanVar(value=d.private)
        self.var_resume = ctk.BooleanVar(value=d.resume)
        ctk.CTkCheckBox(opts, text="Subir al Hub", variable=self.var_push).grid(row=0, column=0, padx=6)
        ctk.CTkCheckBox(opts, text="Repositorio privado", variable=self.var_private).grid(row=0, column=1, padx=6)
        ctk.CTkCheckBox(opts, text="Continuar dataset existente", variable=self.var_resume).grid(
            row=0, column=2, padx=6
        )

        ctk.CTkLabel(
            form,
            text=(
                "La grabación usa el mismo bucle que `lerobot-record`: el líder genera las acciones "
                "y cada fotograma se guarda con la observación del seguidor.\n"
                "Se necesita el brazo líder conectado; activa la cámara en Ajustes para incluir vídeo."
            ),
            text_color=COLOR_MUTED,
            justify="left",
            wraplength=560,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 10))

        self.btn_record = ctk.CTkButton(
            form, text="●  Iniciar grabación", height=42, command=self._start_recording, state="disabled"
        )
        self.btn_record.grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        live = ctk.CTkFrame(form)
        live.grid(row=9, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        live.grid_columnconfigure(0, weight=1)
        self.lbl_rec_phase = ctk.CTkLabel(live, text="Sin grabación en curso", anchor="w")
        self.lbl_rec_phase.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 2))
        self.bar_rec = ctk.CTkProgressBar(live)
        self.bar_rec.set(0)
        self.bar_rec.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        self.lbl_rec_detail = ctk.CTkLabel(live, text="", anchor="w", text_color=COLOR_MUTED)
        self.lbl_rec_detail.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 10))

        btns = ctk.CTkFrame(live, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        btns.grid_columnconfigure((0, 1, 2), weight=1)
        self.btn_next = ctk.CTkButton(
            btns, text="Terminar episodio", state="disabled", command=lambda: self._rec_event("exit_early")
        )
        self.btn_next.grid(row=0, column=0, padx=4, sticky="ew")
        self.btn_redo = ctk.CTkButton(
            btns,
            text="Repetir episodio",
            state="disabled",
            fg_color=COLOR_WARN,
            hover_color="#c25a02",
            command=lambda: self._rec_event("rerecord_episode"),
        )
        self.btn_redo.grid(row=0, column=1, padx=4, sticky="ew")
        self.btn_stop_rec = ctk.CTkButton(
            btns,
            text="Detener grabación",
            state="disabled",
            fg_color=COLOR_ERR,
            hover_color="#8c1d18",
            command=lambda: self._rec_event("stop_recording"),
        )
        self.btn_stop_rec.grid(row=0, column=2, padx=4, sticky="ew")

    def _build_tab_config(self) -> None:
        tab = self.tab_config
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        form = ctk.CTkScrollableFrame(tab)
        form.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        form.grid_columnconfigure(1, weight=1)
        s = self.settings

        ctk.CTkLabel(form, text="Puertos", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4)
        )
        self.cmb_fport = self._combo(form, 1, "Puerto seguidor", s.follower_port)
        self.ent_fid = self._field(form, 2, "ID calibración seguidor", s.follower_id)
        self.cmb_lport = self._combo(form, 3, "Puerto líder", s.leader_port)
        self.ent_lid = self._field(form, 4, "ID calibración líder", s.leader_id)
        self.var_leader = ctk.BooleanVar(value=s.use_leader)
        ctk.CTkCheckBox(form, text="Usar brazo líder", variable=self.var_leader).grid(
            row=5, column=1, sticky="w", padx=10, pady=4
        )
        ctk.CTkButton(form, text="Detectar puertos", width=150, command=self._detect_ports).grid(
            row=5, column=0, sticky="w", padx=10, pady=4
        )

        ctk.CTkLabel(form, text="Cámara", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=10, pady=(14, 4)
        )
        self.var_cam = ctk.BooleanVar(value=s.camera.enabled)
        ctk.CTkCheckBox(
            form, text="Incluir la cámara en el robot (necesario para grabar vídeo)", variable=self.var_cam
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=4)
        self.ent_cam_name = self._field(form, 8, "Nombre de la cámara", s.camera.name, width=140)
        ctk.CTkLabel(form, text="Índice o ruta", anchor="w").grid(
            row=9, column=0, sticky="w", padx=10, pady=5
        )
        cam_row = ctk.CTkFrame(form, fg_color="transparent")
        cam_row.grid(row=9, column=1, sticky="w", padx=10, pady=5)
        self.ent_cam_idx = ctk.CTkComboBox(cam_row, values=[s.camera.index_or_path], width=160)
        self.ent_cam_idx.set(s.camera.index_or_path)
        self.ent_cam_idx.grid(row=0, column=0)
        self.btn_detect_cam = ctk.CTkButton(
            cam_row, text="Detectar cámaras", width=150, command=self._detect_cameras
        )
        self.btn_detect_cam.grid(row=0, column=1, padx=(8, 0))
        self.ent_cam_w = self._field(form, 10, "Ancho", str(s.camera.width), width=110)
        self.ent_cam_h = self._field(form, 11, "Alto", str(s.camera.height), width=110)
        ctk.CTkLabel(form, text="FPS cámara / formato", anchor="w").grid(
            row=12, column=0, sticky="w", padx=10, pady=5
        )
        fps_row = ctk.CTkFrame(form, fg_color="transparent")
        fps_row.grid(row=12, column=1, sticky="w", padx=10, pady=5)
        self.ent_cam_fps = ctk.CTkEntry(fps_row, width=80)
        self.ent_cam_fps.insert(0, str(s.camera.fps))
        self.ent_cam_fps.grid(row=0, column=0)
        self.ent_cam_fourcc = ctk.CTkEntry(fps_row, width=90, placeholder_text="MJPG")
        self.ent_cam_fourcc.insert(0, s.camera.fourcc)
        self.ent_cam_fourcc.grid(row=0, column=1, padx=(8, 0))
        ctk.CTkLabel(
            fps_row, text="MJPG si la cámara llega por usbipd", text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=10),
        ).grid(row=0, column=2, padx=(8, 0))

        ctk.CTkLabel(form, text="Control", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=13, column=0, columnspan=2, sticky="w", padx=10, pady=(14, 4)
        )
        self.ent_fps = self._field(form, 14, "FPS del bucle de control", str(s.fps), width=110)
        self.ent_idle_fps = self._field(form, 15, "FPS en reposo", str(s.idle_fps), width=110)
        self.ent_maxrel = self._field(
            form,
            16,
            "Salto máximo por comando (vacío = sin límite)",
            "" if s.max_relative_target is None else f"{s.max_relative_target:g}",
            width=110,
        )
        self.var_degrees = ctk.BooleanVar(value=s.use_degrees)
        ctk.CTkCheckBox(form, text="Usar grados en vez de escala normalizada", variable=self.var_degrees).grid(
            row=17, column=1, sticky="w", padx=10, pady=4
        )

        ctk.CTkLabel(
            form,
            text="Los cambios de puertos, cámara y unidades se aplican en la próxima conexión.",
            text_color=COLOR_MUTED,
            wraplength=560,
            justify="left",
        ).grid(row=18, column=0, columnspan=2, sticky="w", padx=10, pady=(12, 6))

        ctk.CTkButton(form, text="Guardar ajustes", command=self._save_settings).grid(
            row=19, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 12)
        )

    # ---------------------------------------------------------------- derecha --
    def _build_right(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=1, column=2, sticky="nsew", padx=(6, 12), pady=(0, 6))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(panel, text="Cámara", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 4)
        )

        holder = ctk.CTkFrame(panel, fg_color="#101418", height=int(PREVIEW_W * 0.75))
        holder.grid(row=1, column=0, sticky="ew", padx=14)
        holder.grid_propagate(False)
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        self.lbl_video = tk.Label(holder, bg="#101418", fg="#8a8a8a", text="Sin señal de vídeo")
        self.lbl_video.grid(row=0, column=0, sticky="nsew")

        self.btn_preview = ctk.CTkButton(
            panel, text="Vista previa (sin robot)", command=self._toggle_preview
        )
        self.btn_preview.grid(row=2, column=0, sticky="ew", padx=14, pady=10)

        ctk.CTkLabel(panel, text="Registro", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=3, column=0, sticky="nw", padx=14, pady=(6, 4)
        )
        self.txt_log = ctk.CTkTextbox(panel, height=180, wrap="word")
        self.txt_log.grid(row=4, column=0, sticky="nsew", padx=14, pady=(28, 14))
        self.txt_log.configure(state="disabled")
        panel.grid_rowconfigure(4, weight=1)

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, height=32)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(bar, text="Listo", anchor="w", text_color=COLOR_MUTED)
        self.lbl_status.grid(row=0, column=0, sticky="ew", padx=16, pady=6)
        self.lbl_hz = ctk.CTkLabel(bar, text="", anchor="e", text_color=COLOR_MUTED)
        self.lbl_hz.grid(row=0, column=1, sticky="e", padx=16, pady=6)

    # ------------------------------------------------------------- utilidades --
    def _field(self, parent, row: int, label: str, value: str, width: int = 320) -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=10, pady=5)
        entry = ctk.CTkEntry(parent, width=width)
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="w", padx=10, pady=5)
        return entry

    def _combo(self, parent, row: int, label: str, value: str) -> ctk.CTkComboBox:
        ctk.CTkLabel(parent, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=10, pady=5)
        options = find_serial_ports() or [value]
        if value not in options:
            options = [value, *options]
        combo = ctk.CTkComboBox(parent, values=options, width=320)
        combo.set(value)
        combo.grid(row=row, column=1, sticky="w", padx=10, pady=5)
        return combo

    def _cfg(self, widget, **kwargs) -> None:
        """`widget.configure(...)` sólo con lo que realmente cambió."""
        cache = self._cfg_cache.setdefault(widget, {})
        changed = {k: v for k, v in kwargs.items() if k not in cache or cache[k] != v}
        if changed:
            widget.configure(**changed)
            cache.update(changed)

    def _expect(self, key: str, value: object) -> None:
        """Anota que se pidió un cambio: no se sincroniza el widget hasta confirmarlo."""
        self._pending[key] = (value, time.monotonic() + 3.0)

    def _settled(self, key: str, actual: object) -> bool:
        pending = self._pending.get(key)
        if pending is None:
            return True
        value, deadline = pending
        if actual == value or time.monotonic() > deadline:
            del self._pending[key]
            return True
        return False

    def _log(self, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # =============================================================== acciones ==
    def _toggle_connection(self) -> None:
        snap = self.service.snapshot()
        if snap.connected:
            self.service.disconnect()
            self._log("Desconectando…")
            return
        self._collect_settings()
        if self.settings.camera.enabled and self.preview.running:
            self.preview.stop()  # la cámara pasa a ser propiedad de LeRobot
        self.service.connect()
        self._log(
            f"Conectando seguidor en {self.settings.follower_port}"
            + (f" y líder en {self.settings.leader_port}" if self.settings.use_leader else "")
        )

    def _toggle_manual(self) -> None:
        snap = self.service.snapshot()
        if not snap.connected:
            self.var_manual.set(False)
            return
        if self.var_manual.get():
            if snap.mode is Mode.TELEOP:
                self.service.set_mode(Mode.IDLE)
            self._expect("manual", True)
            self._expect("torque", True)
            self.service.set_mode(Mode.MANUAL)
            self._log("Control manual activado (los sliders mandan).")
        else:
            self._expect("manual", False)
            self.service.set_mode(Mode.IDLE)
            self._log("Control manual desactivado.")

    def _toggle_torque(self) -> None:
        self._expect("torque", self.var_torque.get())
        self.service.set_torque(self.var_torque.get())

    def _toggle_teleop(self) -> None:
        snap = self.service.snapshot()
        if snap.mode is Mode.TELEOP:
            self.service.set_mode(Mode.IDLE)
            self._log("Teleoperación detenida.")
        else:
            self._set_manual_off()
            self._expect("torque", True)
            self.service.set_mode(Mode.TELEOP)
            self._log("Teleoperación iniciada: mueve el brazo líder.")

    def _set_manual_off(self) -> None:
        self.var_manual.set(False)
        self._expect("manual", False)

    def _on_slider(self, motor: str, value: float) -> None:
        if self._syncing or not self.var_manual.get():
            return
        self.service.set_goal(motor, value)

    def _estop(self) -> None:
        self.service.emergency_stop()
        self._set_manual_off()
        self.var_torque.set(False)
        self._expect("torque", False)
        self._log("PARO DE EMERGENCIA: bucles detenidos y torque liberado.")

    # --------------------------------------------------------------- poses --
    def _render_poses(self) -> None:
        for widget in self.frm_poses.winfo_children():
            widget.destroy()
        names = self.poses.names()
        if not names:
            ctk.CTkLabel(
                self.frm_poses, text="No hay poses guardadas.", text_color=COLOR_MUTED
            ).grid(row=0, column=0, pady=20)
            return
        for row, name in enumerate(names):
            line = ctk.CTkFrame(self.frm_poses, fg_color="transparent")
            line.grid(row=row, column=0, sticky="ew", pady=3)
            line.grid_columnconfigure(0, weight=1)
            ctk.CTkButton(
                line, text=f"➜  {name}", height=36, anchor="w", command=lambda n=name: self._goto_pose(n)
            ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
            ctk.CTkButton(
                line,
                text="✕",
                width=36,
                height=36,
                fg_color="transparent",
                border_width=1,
                command=lambda n=name: self._delete_pose(n),
            ).grid(row=0, column=1)

    def _goto_pose(self, name: str) -> None:
        snap = self.service.snapshot()
        if not snap.connected:
            messagebox.showwarning("Sin conexión", "Conecta el robot antes de mover a una pose.")
            return
        if snap.mode in (Mode.TELEOP, Mode.RECORD):
            messagebox.showwarning("Ocupado", "Detén la teleoperación o la grabación primero.")
            return
        target = self.poses.get(name)
        if not target:
            return
        try:
            duration = float(self.ent_pose_dur.get())
        except ValueError:
            duration = self.settings.preset_duration_s
        self._set_manual_off()
        self.service.goto_pose(target, duration)
        self._log(f"Moviendo a la pose «{name}» en {duration:g} s.")

    def _capture_pose(self) -> None:
        snap = self.service.snapshot()
        if not snap.present:
            messagebox.showwarning("Sin datos", "Conecta el robot para capturar su postura actual.")
            return
        dialog = ctk.CTkInputDialog(text="Nombre de la pose:", title="Capturar pose")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.poses.put(name, snap.present)
        self._render_poses()
        self._log(f"Pose «{name}» guardada.")

    def _delete_pose(self, name: str) -> None:
        if messagebox.askyesno("Eliminar pose", f"¿Eliminar la pose «{name}»?"):
            self.poses.remove(name)
            self._render_poses()
            self._log(f"Pose «{name}» eliminada.")

    # ------------------------------------------------------------- rutinas --
    def _reload_datasets(self) -> None:
        self._datasets = discover_datasets(self.settings.dataset.root or None)
        if not self._datasets:
            self.cmb_dataset.configure(values=["(no hay datasets grabados)"])
            self.cmb_dataset.set("(no hay datasets grabados)")
            self.opt_episode.configure(values=["0"])
            self._cfg(self.btn_run_routine, state="disabled")
            return
        etiquetas = [d.label for d in self._datasets]
        self.cmb_dataset.configure(values=etiquetas)
        if self.cmb_dataset.get() not in etiquetas:
            self.cmb_dataset.set(etiquetas[0])
        self._on_dataset_change(self.cmb_dataset.get())
        self._log(f"Datasets encontrados: {len(self._datasets)}")

    def _selected_dataset(self) -> DatasetInfo | None:
        etiqueta = self.cmb_dataset.get()
        return next((d for d in self._datasets if d.label == etiqueta), None)

    def _on_dataset_change(self, _value: str = "") -> None:
        info = self._selected_dataset()
        if info is None:
            return
        episodios = [str(i) for i in range(max(1, info.total_episodes))]
        self.opt_episode.configure(values=episodios)
        if self.opt_episode.get() not in episodios:
            self.opt_episode.set(episodios[0])

    def _on_speed(self, value: float) -> None:
        self._cfg(self.lbl_speed, text=f"{value:.2f}×")

    def _routine_request(self) -> RoutineRequest | None:
        info = self._selected_dataset()
        if info is None:
            messagebox.showwarning("Sin dataset", "Graba primero un episodio o pulsa «Actualizar».")
            return None
        return RoutineRequest(
            repo_id=info.repo_id,
            episode=_as_int(self.opt_episode.get(), 0),
            root=str(info.root),
            repeats=max(1, _as_int(self.ent_repeats.get(), 1)),
            speed=float(self.sld_speed.get()),
            approach_s=max(0.5, _as_float(self.ent_approach.get(), 3.0)),
        )

    def _run_routine(self, req: RoutineRequest | None = None, name: str = "") -> None:
        snap = self.service.snapshot()
        if not snap.connected:
            messagebox.showwarning("Sin conexión", "Conecta el robot antes de ejecutar una rutina.")
            return
        if snap.mode in (Mode.TELEOP, Mode.RECORD):
            messagebox.showwarning("Ocupado", "Detén la teleoperación o la grabación primero.")
            return
        if req is None:
            req = self._routine_request()
            if req is None:
                return
        if not messagebox.askyesno(
            "Ejecutar rutina",
            f"El brazo se moverá solo.\n\n"
            f"Episodio {req.episode} de «{req.repo_id}», {req.repeats}× a {req.speed:.2f}×.\n"
            f"Primero irá a la postura inicial en {req.approach_s:g} s.\n\n"
            "Despeja el área de trabajo. ¿Continuar?",
        ):
            return
        self._set_manual_off()
        self._expect("torque", True)
        self.service.start_routine(req, name)
        self._log(f"Rutina lanzada: {name or req.repo_id} · episodio {req.episode}")

    def _stop_routine(self) -> None:
        self.service.stop_routine()
        self._log("Deteniendo la rutina…")

    def _save_routine(self) -> None:
        req = self._routine_request()
        if req is None:
            return
        dialog = ctk.CTkInputDialog(text="Nombre de la rutina:", title="Guardar rutina")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.routines.put(
            name,
            {
                "repo_id": req.repo_id,
                "episode": req.episode,
                "root": req.root,
                "repeats": req.repeats,
                "speed": req.speed,
                "approach_s": req.approach_s,
            },
        )
        self._render_routines()
        self._log(f"Rutina «{name}» guardada.")

    def _render_routines(self) -> None:
        for widget in self.frm_routines.winfo_children():
            widget.destroy()
        names = self.routines.names()
        if not names:
            ctk.CTkLabel(
                self.frm_routines,
                text="Aún no guardaste ninguna rutina.\nElige dataset y episodio arriba y pulsa "
                "«Guardar como rutina».",
                text_color=COLOR_MUTED,
                justify="left",
            ).grid(row=0, column=0, pady=20)
            return
        for row, name in enumerate(names):
            data = self.routines.get(name) or {}
            line = ctk.CTkFrame(self.frm_routines, fg_color="transparent")
            line.grid(row=row, column=0, sticky="ew", pady=3)
            line.grid_columnconfigure(0, weight=1)
            detalle = f"ep {data.get('episode', 0)} · {data.get('repeats', 1)}× · {data.get('speed', 1.0):.2f}×"
            ctk.CTkButton(
                line,
                text=f"▶  {name}      {detalle}",
                height=36,
                anchor="w",
                command=lambda n=name: self._run_saved_routine(n),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
            ctk.CTkButton(
                line,
                text="✕",
                width=36,
                height=36,
                fg_color="transparent",
                border_width=1,
                command=lambda n=name: self._delete_routine(n),
            ).grid(row=0, column=1)

    def _run_saved_routine(self, name: str) -> None:
        data = self.routines.get(name)
        if not data:
            return
        self._run_routine(
            RoutineRequest(
                repo_id=data["repo_id"],
                episode=int(data.get("episode", 0)),
                root=data.get("root") or None,
                repeats=int(data.get("repeats", 1)),
                speed=float(data.get("speed", 1.0)),
                approach_s=float(data.get("approach_s", 3.0)),
            ),
            name,
        )

    def _delete_routine(self, name: str) -> None:
        if messagebox.askyesno("Eliminar rutina", f"¿Eliminar la rutina «{name}»?"):
            self.routines.remove(name)
            self._render_routines()
            self._log(f"Rutina «{name}» eliminada.")

    # ----------------------------------------------------------- grabación --
    def _start_recording(self) -> None:
        snap = self.service.snapshot()
        if not snap.connected:
            messagebox.showwarning("Sin conexión", "Conecta el robot antes de grabar.")
            return
        if not snap.has_leader:
            messagebox.showwarning("Falta el líder", "La grabación necesita el brazo líder conectado.")
            return
        try:
            req = RecordRequest(
                repo_id=self.ent_repo.get().strip(),
                single_task=self.ent_task.get().strip(),
                num_episodes=int(self.ent_eps.get()),
                fps=self.settings.fps,
                episode_time_s=float(self.ent_ep_time.get()),
                reset_time_s=float(self.ent_reset_time.get()),
                root=self.ent_root.get().strip() or None,
                push_to_hub=self.var_push.get(),
                private=self.var_private.get(),
                resume=self.var_resume.get(),
                vcodec=self.settings.dataset.vcodec,
            )
        except ValueError:
            messagebox.showerror("Datos inválidos", "Revisa los campos numéricos de la grabación.")
            return

        problem = validate(req)
        if problem:
            messagebox.showerror("Datos inválidos", problem)
            return
        if not messagebox.askyesno(
            "Iniciar grabación",
            f"Se grabarán {req.num_episodes} episodios de {req.episode_time_s:g} s en «{req.repo_id}».\n\n"
            "Durante la grabación controla el robot con el brazo líder.\n¿Comenzar?",
        ):
            return

        self._collect_settings()
        self._set_manual_off()
        self.service.start_recording(req)
        self._log(f"Grabación solicitada: {req.repo_id} ({req.num_episodes} episodios).")

    def _rec_event(self, name: str) -> None:
        self.service.record_event(name)
        labels = {
            "exit_early": "Episodio terminado por el usuario.",
            "rerecord_episode": "Se repetirá el episodio actual.",
            "stop_recording": "Deteniendo la grabación…",
        }
        self._log(labels.get(name, name))

    # ------------------------------------------------------------- cámara --
    def _toggle_preview(self) -> None:
        if self.preview.running:
            self.preview.stop()
            self._log("Vista previa detenida.")
            return
        snap = self.service.snapshot()
        if snap.connected and self.settings.camera.enabled:
            messagebox.showinfo(
                "Cámara en uso",
                "La cámara ya la gestiona LeRobot mientras el robot está conectado; "
                "el vídeo que ves proviene de ahí.",
            )
            return
        self._collect_settings()
        cam = self.settings.camera
        self.preview.start(cam.index_or_path, cam.width, cam.height, cam.fps, cam.fourcc)
        self._log(f"Vista previa de la cámara '{cam.index_or_path}' iniciada.")

    # ------------------------------------------------------------ ajustes --
    def _detect_ports(self) -> None:
        ports = find_serial_ports()
        if not ports:
            messagebox.showwarning(
                "Sin puertos",
                "No se encontraron /dev/ttyACM* ni /dev/ttyUSB*.\n"
                "Comprueba que usbipd haya adjuntado los dispositivos a WSL.",
            )
            return
        self.cmb_fport.configure(values=ports)
        self.cmb_lport.configure(values=ports)
        self._log("Puertos detectados: " + ", ".join(ports))

    def _detect_cameras(self, silencioso: bool = False) -> None:
        """Escanea /dev/video* en segundo plano (abrir cada cámara puede tardar segundos).

        `silencioso` lo usa el escaneo de arranque: rellena la lista sin diálogos.
        """
        if self._cam_scan_running:
            return
        self._cam_scan_silent = silencioso

        # Un /dev/videoN no se puede abrir dos veces: si LeRobot ya tiene la cámara,
        # el escaneo la vería como inexistente y el aviso sería engañoso.
        if self.service.snapshot().connected and self.settings.camera.enabled:
            if silencioso:
                return
            messagebox.showinfo(
                "Cámara en uso",
                "La cámara ya está abierta por el robot, así que el escaneo no puede verla.\n\n"
                "Si el vídeo se ve en el panel derecho, está funcionando y no hace falta "
                "detectar nada.\n\n"
                "Para cambiar de cámara: desconecta el robot, pulsa «Detectar cámaras» y "
                "vuelve a conectar.",
            )
            return

        self._cam_scan_running = True
        self._cfg(self.btn_detect_cam, text="Buscando…", state="disabled")
        self._log("Buscando cámaras…")

        def work() -> None:
            error = ""
            found: list[dict] = []
            try:
                from lerobot.cameras.opencv.camera_opencv import OpenCVCamera

                found = OpenCVCamera.find_cameras()
            except Exception as exc:  # noqa: BLE001
                log.exception("Fallo al buscar cámaras")
                error = str(exc)

            # El servidor de Windows no aparece en /dev/video*: hay que preguntarle.
            for url in dict.fromkeys(
                u for u in (self.settings.camera.index_or_path, url_por_defecto()) if u
            ):
                if not str(url).lower().startswith("http"):
                    continue
                estado = comprobar_stream(str(url))
                if estado:
                    found.insert(0, {
                        "id": str(url),
                        "default_stream_profile": {
                            "width": estado.get("ancho"),
                            "height": estado.get("alto"),
                            "fps": estado.get("fps"),
                            "fourcc": "MJPG",
                        },
                    })
                    break
            self._cam_result = (found, error)

        threading.Thread(target=work, name="BuscarCamaras", daemon=True).start()

    def _cameras_found(self, found: list[dict], error: str) -> None:
        silencioso = self._cam_scan_silent
        self._cam_scan_running = False
        self._cam_scan_silent = False
        self._cfg(self.btn_detect_cam, text="Detectar cámaras", state="normal")

        if error:
            if not silencioso:
                messagebox.showerror("Error al buscar cámaras", error)
            return

        if not found:
            self.ent_cam_idx.configure(values=[self.settings.camera.index_or_path or "0"])
            self._log("No se encontró ninguna cámara.")
            if not silencioso:
                messagebox.showwarning("Sin cámaras", _AYUDA_SIN_CAMARAS)
            return

        # La etiqueta empieza por la ruta, así que basta partirla para recuperarla
        # y seguir aceptando una ruta escrita a mano.
        etiquetas = [_etiqueta_camara(c) for c in found]
        self.ent_cam_idx.configure(values=etiquetas)
        actual = self._camara_elegida()
        coincide = next((e for e in etiquetas if e.split(" · ")[0] == actual), None)
        self.ent_cam_idx.set(coincide or etiquetas[0])

        for cam in found:
            p = cam.get("default_stream_profile") or {}
            self._log(
                f"Cámara {cam['id']}: {p.get('width')}x{p.get('height')} "
                f"@ {p.get('fps')} fps ({p.get('fourcc')})"
            )

        if silencioso:
            return

        # Solo al pulsar el botón: ajusta al perfil nativo, porque LeRobot aborta
        # la conexión si se le pide una resolución que la cámara no soporta.
        elegida = next((c for c in found if str(c["id"]) == self._camara_elegida()), found[0])
        perfil = elegida.get("default_stream_profile") or {}
        for entry, clave in ((self.ent_cam_w, "width"), (self.ent_cam_h, "height"), (self.ent_cam_fps, "fps")):
            valor = perfil.get(clave)
            if valor:
                entry.delete(0, "end")
                entry.insert(0, str(int(valor)))

        messagebox.showinfo(
            "Cámaras encontradas",
            f"{len(found)} cámara(s) disponibles:\n\n"
            + "\n".join(f"· {e}" for e in etiquetas)
            + "\n\nElige una en el desplegable, marca «Incluir la cámara en el robot» "
            "y reconecta para usarla."
            + ("\n\nSi falta la del portátil, hay que reenviarla con usbipd: "
               "ejecuta iniciar_gui.ps1 sin la cámara USB externa enchufada."),
        )

    def _camara_elegida(self) -> str:
        """Ruta del dispositivo elegido, a partir de la etiqueta del desplegable."""
        return self.ent_cam_idx.get().split(" · ")[0].strip()

    def _collect_settings(self) -> None:
        s = self.settings
        s.follower_port = self.cmb_fport.get().strip()
        s.leader_port = self.cmb_lport.get().strip()
        s.follower_id = self.ent_fid.get().strip()
        s.leader_id = self.ent_lid.get().strip()
        s.use_leader = self.var_leader.get()
        s.use_degrees = self.var_degrees.get()
        s.fps = _as_int(self.ent_fps.get(), s.fps)
        s.idle_fps = _as_int(self.ent_idle_fps.get(), s.idle_fps)
        s.max_relative_target = _as_float_or_none(self.ent_maxrel.get())
        s.preset_duration_s = _as_float(self.ent_pose_dur.get(), s.preset_duration_s)

        s.camera.enabled = self.var_cam.get()
        s.camera.name = self.ent_cam_name.get().strip() or "front"
        s.camera.index_or_path = self._camara_elegida() or "0"
        s.camera.width = _as_int(self.ent_cam_w.get(), s.camera.width)
        s.camera.height = _as_int(self.ent_cam_h.get(), s.camera.height)
        s.camera.fps = _as_int(self.ent_cam_fps.get(), s.camera.fps)
        s.camera.fourcc = self.ent_cam_fourcc.get().strip().upper()

        d = s.dataset
        d.repo_id = self.ent_repo.get().strip()
        d.single_task = self.ent_task.get().strip()
        d.num_episodes = _as_int(self.ent_eps.get(), d.num_episodes)
        d.episode_time_s = _as_float(self.ent_ep_time.get(), d.episode_time_s)
        d.reset_time_s = _as_float(self.ent_reset_time.get(), d.reset_time_s)
        d.root = self.ent_root.get().strip()
        d.push_to_hub = self.var_push.get()
        d.private = self.var_private.get()
        d.resume = self.var_resume.get()

    def _save_settings(self) -> None:
        self._collect_settings()
        self.settings.save()
        self._log("Ajustes guardados en gui_data/ajustes.json.")

    # ============================================================ refresco ==
    def _refresh(self) -> None:
        try:
            self._apply(self.service.snapshot())
        except Exception:  # noqa: BLE001  - la GUI nunca debe morir por un refresco
            log.exception("Error refrescando la interfaz")
        finally:
            self.after(REFRESH_MS, self._refresh)

    def _apply(self, snap: Snapshot) -> None:
        if self._cam_result is not None:
            found, error = self._cam_result
            self._cam_result = None
            self._cameras_found(found, error)
        recording = snap.record.active
        rep = snap.replay
        replaying = rep.active
        busy = recording or replaying
        connected = snap.connected

        self._cfg(
            self.btn_connect,
            text="Desconectar" if connected else "Conectar",
            state="disabled" if (snap.busy or busy) else "normal",
        )
        if snap.busy:
            self._cfg(self.lbl_conn, text="● Conectando…", text_color=COLOR_WARN)
        elif connected:
            self._cfg(self.lbl_conn, text="● Conectado", text_color=COLOR_OK)
        else:
            self._cfg(self.lbl_conn, text="● Desconectado", text_color=COLOR_MUTED)
        self._cfg(self.lbl_mode, text=MODE_LABELS.get(snap.mode, "") if connected else "")

        # --- sliders ---------------------------------------------------------
        if snap.motors != self._built_motors:
            if snap.motors:
                self._build_sliders(snap.motors, snap.limits)
            else:
                self._build_sliders((), {})
                self.lbl_no_motors = ctk.CTkLabel(
                    self.frm_sliders,
                    text="Conecta el brazo seguidor para ver los motores.",
                    text_color=COLOR_MUTED,
                )
                self.lbl_no_motors.grid(row=0, column=0, pady=30)

        manual = self.var_manual.get() and snap.mode is Mode.MANUAL
        self._syncing = True
        for motor, slider in self._sliders.items():
            present = snap.present.get(motor)
            self._cfg(slider, state="normal" if manual else "disabled")
            if not manual and present is not None and abs(slider.get() - present) > 0.05:
                slider.set(present)
            goal = slider.get() if manual else present
            self._cfg(
                self._slider_values[motor],
                text=_fmt_value(goal, present, snap.raw.get(motor), snap.unit),
            )
        self._syncing = False

        self._cfg(self.sw_manual, state="normal" if connected and not busy else "disabled")
        # El torque sigue disponible durante una rutina: apagarlo la aborta y suelta el brazo.
        self._cfg(self.sw_torque, state="normal" if connected and not recording else "disabled")
        if self._settled("torque", snap.torque) and snap.torque != self.var_torque.get():
            self.var_torque.set(snap.torque)
        in_manual = snap.mode is Mode.MANUAL
        if self._settled("manual", in_manual) and in_manual != self.var_manual.get():
            self.var_manual.set(in_manual)

        # --- teleoperación ---------------------------------------------------
        teleop_on = snap.mode is Mode.TELEOP
        teleop_ready = connected and snap.has_leader and not busy
        self._cfg(
            self.btn_teleop,
            state="normal" if teleop_ready else "disabled",
            text="■  Detener teleoperación" if teleop_on else "▶  Iniciar teleoperación",
            fg_color=COLOR_ERR if teleop_on else ["#3a7ebf", "#1f538d"],
            hover_color="#8c1d18" if teleop_on else ["#325882", "#14375e"],
        )

        # --- grabación -------------------------------------------------------
        self._cfg(
            self.btn_record,
            state="normal" if connected and snap.has_leader and not busy else "disabled",
        )
        for button in (self.btn_next, self.btn_redo, self.btn_stop_rec):
            self._cfg(button, state="normal" if recording else "disabled")

        rec = snap.record
        if recording:
            self._cfg(
                self.lbl_rec_phase,
                text=f"{rec.phase} · episodio {rec.episode}/{rec.total}" if rec.episode else rec.phase,
            )
            self.bar_rec.set(min(1.0, rec.elapsed / rec.limit) if rec.limit > 0 else 0.0)
            self._cfg(
                self.lbl_rec_detail,
                text=f"{rec.elapsed:5.1f} s / {rec.limit:g} s   ·   episodios guardados: {rec.saved}"
                if rec.limit > 0
                else f"episodios guardados: {rec.saved}",
            )
        elif rec.result and rec.result != self._last_record_result:
            self._last_record_result = rec.result
            self._cfg(self.lbl_rec_phase, text=rec.result)
            self.bar_rec.set(0)
            self._cfg(self.lbl_rec_detail, text="")
            self._log(rec.result)

        # --- rutinas ---------------------------------------------------------
        self._cfg(
            self.btn_run_routine,
            state="disabled"
            if (not connected or busy or snap.mode is Mode.TELEOP or not self._datasets)
            else "normal",
        )
        self._cfg(self.btn_stop_routine, state="normal" if replaying else "disabled")
        if replaying:
            self._cfg(
                self.lbl_routine_phase,
                text=f"{rep.phase} · «{rep.name}»"
                + (f"  ({rep.repeat}/{rep.repeats})" if rep.repeats > 1 else ""),
            )
            self.bar_routine.set(min(1.0, rep.elapsed / rep.duration) if rep.duration > 0 else 0.0)
            self._cfg(
                self.lbl_routine_detail,
                text=f"fotograma {rep.frame}/{rep.frames}   ·   {rep.elapsed:5.1f} s / {rep.duration:.1f} s"
                if rep.frames
                else "",
            )
        elif rep.result and rep.result != self._last_routine_result:
            self._last_routine_result = rep.result
            self._cfg(self.lbl_routine_phase, text=rep.result)
            self.bar_routine.set(0)
            self._cfg(self.lbl_routine_detail, text="")
            self._log(rep.result)

        # --- cámara ----------------------------------------------------------
        self._update_video(snap)
        self._cfg(
            self.btn_preview,
            text="Detener vista previa" if self.preview.running else "Vista previa (sin robot)",
        )

        # --- estado ----------------------------------------------------------
        if snap.status != self._last_status:
            self._last_status = snap.status
            self._log(snap.status)
        self._cfg(
            self.lbl_status,
            text=snap.error or snap.status,
            text_color=COLOR_ERR if snap.error else COLOR_MUTED,
        )
        if snap.error and snap.error != self._last_error:
            self._last_error = snap.error
            self._log("ERROR · " + snap.error.replace("\n", " "))
        elif not snap.error:
            self._last_error = ""
        self._cfg(self.lbl_hz, text=f"{snap.hz:.0f} Hz" if connected else "")

    def _update_video(self, snap: Snapshot) -> None:
        frame, frame_id = None, -1
        if snap.frame is not None and snap.connected:
            frame, frame_id = snap.frame, snap.frame_id
        elif self.preview.running:
            preview_frame, preview_id, error = self.preview.read()
            if error:
                self.lbl_video.configure(image="", text=error)
                return
            frame, frame_id = preview_frame, 1_000_000 + preview_id

        if frame is None:
            if self._last_frame_id != -1:
                self._last_frame_id = -1
                self.lbl_video.configure(image="", text="Sin señal de vídeo")
                self._photo = None
            return
        if frame_id == self._last_frame_id:
            return
        self._last_frame_id = frame_id

        image = Image.fromarray(frame)
        height = int(image.height * PREVIEW_W / image.width)
        image = image.resize((PREVIEW_W, height), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(image)
        self.lbl_video.configure(image=self._photo, text="")

    # ================================================================ cierre ==
    def _on_close(self) -> None:
        snap = self.service.snapshot()
        if snap.record.active and not messagebox.askyesno(
            "Grabación en curso", "Hay una grabación activa. ¿Cerrar de todas formas?"
        ):
            return
        try:
            self._collect_settings()
            self.settings.save()
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron guardar los ajustes al cerrar")
        self.preview.stop()
        self.service.shutdown()
        self.destroy()


# ------------------------------------------------------------------ helpers --
_AYUDA_SIN_CAMARAS = (
    "No se encontró ninguna cámara utilizable.\n\n"
    "Si otro proceso la tiene abierta (el robot conectado, una vista previa o una "
    "videollamada), ciérralo y reintenta.\n\n"
    "Si dentro de WSL no existe ningún /dev/video*, falta reenviarla con usbipd. "
    "La forma simple es cerrar la GUI y ejecutar iniciar_gui.ps1 desde Windows: "
    "reenvía la cámara USB externa si está enchufada, y si no, la del portátil.\n\n"
    "La primera vez que se usa una cámara nueva hace falta autorizarla una sola vez, "
    "desde PowerShell como ADMINISTRADOR:\n"
    "    usbipd bind --busid <BUSID>"
)


def _etiqueta_camara(cam: dict) -> str:
    """Texto del desplegable. Empieza por la ruta para poder recuperarla luego."""
    perfil = cam.get("default_stream_profile") or {}
    ancho, alto = perfil.get("width"), perfil.get("height")
    fps = perfil.get("fps")
    partes = [str(cam["id"])]
    if ancho and alto:
        resolucion = f"{int(ancho)}x{int(alto)}"
        if fps:
            resolucion += f" @{int(fps)}"
        partes.append(resolucion)
    if perfil.get("fourcc"):
        partes.append(str(perfil["fourcc"]))
    return " · ".join(partes)


def _fmt_value(goal, present, raw, unit: str) -> str:
    if present is None and goal is None:
        return "—"
    parts = []
    if goal is not None:
        parts.append(f"obj {goal:6.1f}{unit}")
    if present is not None:
        parts.append(f"real {present:6.1f}{unit}")
    if raw is not None:
        parts.append(f"{raw} pasos")
    return "   ".join(parts)


def _as_int(text: str, fallback: int) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return fallback


def _as_float(text: str, fallback: float) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return fallback


def _as_float_or_none(text: str) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()
