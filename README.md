# GUI_Lerobot

Interfaz gráfica para controlar dos brazos **SO-101** (Seeed Studio) con
[LeRobot](https://github.com/huggingface/lerobot), sin usar la terminal.

Permite teleoperar en modo espejo, mover cada servo con deslizadores, guardar y
reproducir poses, ejecutar rutinas a partir de episodios grabados, grabar datasets
en formato LeRobot y ver la cámara del espacio de trabajo.

Desarrollado y verificado sobre **lerobot 0.4.4**, Python 3.10, CustomTkinter 6.0,
en WSL2 Ubuntu 22.04 bajo Windows 11.

---

## 1. Qué hace

| Módulo | Función | Equivalente en terminal |
|---|---|---|
| Teleoperación | Bucle espejo a 30 Hz: el seguidor copia al líder | `lerobot-teleoperate` |
| Articulaciones | Un deslizador por servo, en su rango calibrado | — |
| Poses | Posturas guardadas | — |
| Rutinas | Reproduce un episodio grabado, con repeticiones y velocidad | `lerobot-replay` |
| Grabación | Dataset con el bucle oficial de LeRobot | `lerobot-record` |
| Cámara | Vista del entorno, incluida en el dataset | `lerobot-find-cameras` |

Tres medidas de seguridad condicionan el diseño:

- **Paro de emergencia** siempre visible: corta cualquier bucle y libera el torque.
- **Sin saltos bruscos**: los deslizadores arrancan en la posición actual; poses y
  rutinas se alcanzan interpolando, nunca de golpe.
- **Los brazos mandan**: un fallo de cámara degrada el vídeo, jamás desconecta el robot.

---

## 2. Requisitos

### Hardware

- Kit **Seeed Studio SO-101**, dos brazos (líder y seguidor).
- Servos **Feetech STS3215**, seis por brazo, IDs 1 a 6.
- Alimentación: 12 V para el seguidor, 5 V para el líder.
- Opcional: una webcam USB.

### Software

- Linux, o **Windows 11 + WSL2** con WSLg (es el entorno probado).
- Python 3.10.
- [LeRobot](https://github.com/huggingface/lerobot) 0.4.4 o compatible.
- En Windows, [usbipd-win](https://github.com/dorssel/usbipd-win) para reenviar
  los puertos USB a WSL.

> **Por qué WSL y no solo Windows:** LeRobot espera dispositivos tipo
> `/dev/ttyACM*`. En Windows los brazos son puertos COM, que la librería no
> entiende. Todo el código Python corre dentro de WSL; Windows solo reenvía USB
> y, opcionalmente, sirve el vídeo de la cámara.

---

## 3. Instalación

### 3.1. LeRobot

Dentro de WSL (o tu Linux), siguiendo la guía oficial. Resumen de lo usado aquí:

```bash
conda create -n lerobot python=3.10 && conda activate lerobot
```

```bash
git clone https://github.com/huggingface/lerobot.git && cd lerobot && pip install -e ".[feetech]"
```

El extra `[feetech]` es imprescindible: instala `feetech-servo-sdk`, sin el cual
los servos STS3215 no se pueden abrir.

### 3.2. Esta aplicación

```bash
git clone <url-de-este-repositorio> && cd GUI_Lerobot
```

```bash
conda activate lerobot && pip install -r requirements.txt
```

Si `import tkinter` falla:

```bash
sudo apt install python3-tk
```

### 3.3. Permisos de los puertos serie (una sola vez)

Los `/dev/ttyACM*` pertenecen al grupo `dialout` con permisos `660`. Añade tu
usuario a ese grupo, y de paso al de vídeo si vas a usar cámara en Linux:

```bash
sudo usermod -aG dialout,video $USER
```

Cierra la sesión y vuelve a entrar (en WSL: `wsl --shutdown` desde PowerShell).
Sin esto hay que hacer `sudo chmod 666 /dev/ttyACM*` en cada arranque.

### 3.4. Calibración de los brazos

La aplicación **no calibra**: usa los archivos que genera LeRobot. Si aún no lo
hiciste, una vez por brazo:

```bash
lerobot-calibrate --robot.type=so101_follower --robot.port=/dev/ttyACM1 --robot.id=my_awesome_follower_arm
```

```bash
lerobot-calibrate --teleop.type=so101_leader --teleop.port=/dev/ttyACM0 --teleop.id=my_awesome_leader_arm
```

Los archivos quedan en `~/.cache/huggingface/lerobot/calibration/`. Los `id` que
uses aquí son los que hay que poner en la pestaña **Ajustes**.

> Durante la calibración, el motor 5 (`wrist_roll`) registra el rango `0–4095`
> sin medirlo. Es el comportamiento normal de LeRobot, que lo trata como
> articulación de giro completo; no es una avería.

---

## 4. Uso

### 4.1. En Windows (con WSL)

Desde PowerShell, en la carpeta del proyecto:

```bash
.\iniciar_gui.ps1
```

Ese lanzador hace los tres pasos en el orden que exige usbipd: levanta WSL,
reenvía los brazos (y la cámara, si procede) y abre la ventana. **Deja esa
consola abierta**: es la que mantiene WSL vivo.

La primera vez, cada dispositivo necesita autorizarse **una sola vez** desde
PowerShell **como administrador**:

```bash
usbipd bind --busid 2-1
```

El BUSID sale de `usbipd list`. Después queda recordado y el lanzador lo adjunta solo.

### 4.2. En Linux nativo

```bash
bash iniciar_gui.sh
```

o directamente:

```bash
conda activate lerobot && python -m lerobot_gui
```

### 4.3. Qué brazo está en cada puerto

Linux numera `/dev/ttyACM*` por **orden de llegada**, no por puerto físico, así
que líder y seguidor pueden intercambiarse entre sesiones. Para saberlo sin adivinar:

```bash
python identificar_brazos.py
```

Compara la calibración grabada en los servos con tus archivos guardados y dice
cuál es cuál. Es de solo lectura: no toca el torque ni escribe en los motores.

---

## 5. Cámara

Hay dos formas de darle vídeo a la aplicación. **La segunda es la recomendada.**

### 5.1. Cámara USB directa (Linux nativo, o WSL con reservas)

En Linux basta con poner `/dev/video0` en Ajustes. En WSL hay que reenviar la
cámara con usbipd, y entonces aparece un problema medido:

Una webcam UVC transmite por endpoints **isócronos**, y el `vhci_hcd` de WSL no
implementa `vhci_get_frame_number`, que es justo lo que ese modo necesita. El
resultado es que el vídeo llega a trompicones **y además tira a los brazos del
bus**, porque comparten el mismo túnel USB/IP:

```
[111s] usb 1-3: new high-speed USB device   ← la cámara
        353 × urb->status -104
        vhci_get_frame_number: 1718 callbacks suppressed
[338s] usb 1-1: USB disconnect              ← se cae un brazo
[341s] usb 1-2: USB disconnect              ← y el otro
```

Si aun así la usas por este camino, pon el formato en **MJPG**: en YUYV el túnel
no entrega un solo fotograma útil (los 1.720 medidos en 60 s salieron vacíos, que
es lo que se ve como una imagen verde uniforme).

### 5.2. Vídeo por TCP (recomendado en WSL)

Windows abre la cámara de forma nativa y envía los fotogramas a WSL por TCP. El
túnel USB queda libre para los servos. Medido sobre la misma cámara:

| | Por usbipd | Por TCP |
|---|---|---|
| Fotogramas reales | 15,5 fps | **30,1 fps** |
| Intervalo mediano | 64 ms | **32 ms** |
| Percentil 90 | 89 ms | **38 ms** |
| Fallos de lectura | 1 de 313 | **0 de 453** |
| Interfiere con los brazos | Sí | **No** |

En una ventana de PowerShell aparte, con la cámara **no** adjuntada a WSL:

```bash
python servidor_camara.py
```

Imprime la URL que hay que poner en Ajustes, del estilo
`http://172.23.16.1:8181/stream`. El botón **Detectar cámaras** también la
encuentra sola si el servidor está corriendo.

El servidor escucha **solo en la interfaz virtual de WSL**, nunca en `0.0.0.0`:
el vídeo no sale a la red local. Deduce esa dirección preguntándole a la propia
WSL cuál es su puerta de enlace, así que no hay nada que configurar.

Cuando la cámara configurada es una URL, `iniciar_gui.ps1` deja de adjuntar
cámaras a WSL para no recrear el conflicto.

### 5.3. Diagnóstico

```bash
python probar_camara.py --segundos 60
```

Mide el porcentaje de fotogramas útiles, cuenta los vacíos y dice cuánto aguanta
antes del primer fallo. Con `--listar` enumera las cámaras visibles.

### 5.4. Si la imagen va lenta

Con poca luz, una webcam alarga la exposición y baja sola el ritmo a la mitad o
menos. No es una avería del transporte: ilumina mejor la escena. El servidor lo
avisa por consola y solo reabre la cámara ante una parada real.

---

## 6. Unidades

Por defecto se trabaja en la escala normalizada de LeRobot:

| Motor | Rango |
|---|---|
| `shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`, `wrist_roll` | −100 … +100 |
| `gripper` | 0 … 100 |

Los extremos corresponden al `range_min` y `range_max` del archivo de
calibración, así que un deslizador **no puede** ordenar una posición fuera del
rango físico medido. La pantalla muestra además el conteo bruto del encoder
(0–4095), calculado sin ocupar el bus.

Marcando *Usar grados* se cambia a grados. Un dataset grabado en una unidad no es
compatible con una política entrenada en la otra.

---

## 7. Arquitectura

```
lerobot_gui/
├── app.py            Interfaz CustomTkinter (1.400 líneas)
├── service.py        ArmService: hilo único dueño de los puertos serie
├── settings.py       Ajustes, poses y rutinas persistentes en gui_data/
├── recording.py      Sesión de grabación sobre record_loop() de LeRobot
├── routines.py       Descubrir datasets y cargar las acciones de un episodio
├── camera.py         Vista previa con OpenCV, cuando el robot está desconectado
├── stream_camera.py  Cámara por TCP + envoltorio tolerante a fallos
└── __main__.py       Punto de entrada: python -m lerobot_gui
```

Cuatro decisiones explican el resto del código:

**1. Un solo hilo toca el bus serie.** El bus Feetech no admite acceso
concurrente: dos hilos escribiendo en el mismo puerto corrompen paquetes. Por eso
`ArmService` es el único que lee y escribe, y la interfaz solo encola comandos y
lee instantáneas inmutables (`Snapshot`) cada 50 ms.

**2. No se llama a `SOFollower.connect()` tal cual.** Ese método usa `input()`
cuando la calibración de los motores no coincide con el archivo, lo que colgaría
una ventana gráfica. `service._connect_device()` repite sus mismos pasos
escribiendo el archivo existente con el torque liberado, porque el torque bloquea
la EEPROM mediante el registro `Lock`.

**3. La grabación reutiliza `record_loop()` de LeRobot**, no una
reimplementación, para que el dataset sea idéntico al de `lerobot-record`. Los
botones mueven el mismo diccionario de flags que movería el listener de teclado.

**4. La cámara nunca está en el camino crítico.** `async_read()` devuelve el
último fotograma sin esperar a que llegue otro, y `CamaraTolerante` absorbe
cualquier fallo. Esperar un fotograma nuevo dejaba la teleoperación en 5 Hz en
vez de 30; dejar propagar el error desconectaba los brazos.

---

## 8. Archivos

| Archivo | Para qué |
|---|---|
| `lerobot_gui/` | El paquete de la aplicación |
| `iniciar_gui.ps1` | Lanzador desde Windows: WSL + usbipd + GUI |
| `iniciar_gui.sh` | Lanzador desde Linux/WSL |
| `servidor_camara.py` | Servidor MJPEG que corre **en Windows** |
| `identificar_brazos.py` | Dice qué brazo está en cada puerto |
| `probar_camara.py` | Mide la estabilidad del vídeo |
| `requirements.txt` | Dependencias de la GUI |
| `gui_data/ajustes.ejemplo.json` | Plantilla de configuración |

`gui_data/ajustes.json`, `poses.json` y `rutinas.json` los crea la aplicación en
el primer arranque y no se versionan: contienen puertos e IDs de cada equipo.

---

## 9. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `Permission denied` en el puerto | Falta el grupo `dialout`. Ver 3.3. |
| No existe ningún `/dev/ttyACM*` | Los brazos no están adjuntados a WSL. `usbipd list` y `usbipd attach --wsl --busid <BUSID>`. Ojo: `Shared` **no** es lo mismo que `Attached`. |
| `usbipd: error: There is no WSL 2 distribution running` | `attach` exige que WSL ya esté corriendo. Abre una terminal de Ubuntu primero, o usa `iniciar_gui.ps1`, que lo hace en orden. |
| `Required argument missing for option: '--busid'` | El dispositivo se recuerda pero no está enchufado. Conéctalo. |
| Seguidor y líder intercambiados | `python identificar_brazos.py` y corrige los puertos en Ajustes. |
| `Missing motor IDs` o un modelo imposible (`829`) | Paquete corrompido en el túnel USB/IP, no un motor averiado. La GUI reintenta tres veces sola. |
| `There is no status packet` | El dispositivo USB se cayó y volvió renumerado. Pulsa Desconectar y Conectar. |
| Imagen verde uniforme | La cámara no entrega nada. Ver 5.1: usa MJPG, o mejor el modo TCP. |
| Vídeo lento o a tirones | Falta de luz, o la cámara va por usbipd. Ver 5.2 y 5.4. |
| La ventana no abre | Falta WSLg o `python3-tk`. Comprueba que `echo $DISPLAY` devuelve algo. |

---

## 10. Limitaciones conocidas

- **Los datasets grabados sin cámara no sirven para entrenar una política.** Solo
  contienen `observation.state` y `action`; sin imágenes, un modelo no puede saber
  dónde está el objeto. Para entrenar hay que regrabar con la cámara activada, y
  no se pueden mezclar episodios con y sin cámara en el mismo dataset.
- **El FPS del dataset va atado al del bucle de control.** Si la cámara entrega
  menos, los fotogramas se duplican en el dataset. Conviene igualar ambos valores.
- **Las poses que vienen por defecto** dejan cada articulación en el punto medio
  de su rango calibrado, que es lo único seguro sin conocer el montaje. Captura
  las tuyas con el botón correspondiente.

---

## Licencia y créditos

Construido sobre [LeRobot](https://github.com/huggingface/lerobot) (Apache 2.0),
de Hugging Face. Trabajo de práctica de estudios, Universidad de La Frontera.
