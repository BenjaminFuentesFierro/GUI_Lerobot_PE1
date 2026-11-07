# Lanzador de la GUI del SO-101 desde Windows.
#
#   Clic derecho sobre este archivo -> "Ejecutar con PowerShell"
#   o desde PowerShell:  .\iniciar_gui.ps1
#
# Hace, en el orden que exige usbipd:
#   1. Levanta WSL (usbipd falla si la distro no esta corriendo).
#   2. Adjunta los brazos CH343 y la camara C270 si no lo estan ya.
#   3. Abre la GUI dentro de WSL.

param(
    [string]$Distro = "Ubuntu-22.04",
    # VID:PID de lo que se reenvia siempre que este enchufado:
    #   1A86:55D3 -> placa CH343 de los brazos SO-101
    #   046D:0825 -> webcam Logitech C270 (camara USB externa)
    [string[]]$DevicePatterns = @("VID_1A86&PID_55D3", "VID_046D&PID_0825"),
    [string]$CamaraExterna = "VID_046D&PID_0825",
    # Camara integrada del portatil. Solo se reenvia cuando NO hay camara externa:
    # mientras este adjuntada a WSL, Windows no puede usarla.
    [string]$CamaraIntegrada = "VID_3277&PID_0029",
    [switch]$SinCamaraIntegrada,
    # Puerto donde escucha servidor_camara.py en Windows.
    [int]$PuertoCamara = 8181,
    # Carpeta del proyecto. Por defecto, la del propio script.
    [string]$ProjectDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Test-Usbipd {
    if (-not (Get-Command usbipd -ErrorAction SilentlyContinue)) {
        Write-Host "usbipd no esta instalado. Instalalo con:" -ForegroundColor Red
        Write-Host "    winget install usbipd" -ForegroundColor Yellow
        exit 1
    }
}

function Test-ServidorCamara([int]$puerto) {
    # Red de seguridad: si servidor_camara.py ya esta sirviendo, adjuntar la camara
    # a WSL se la quitaria y romperia el video. Mejor no tocarla.
    # Se pregunta a Windows que hay escuchando: el servidor NO escucha en 127.0.0.1,
    # sino solo en la interfaz virtual de WSL, asi que conectarse no sirve de sonda.
    try {
        $escuchando = Get-NetTCPConnection -LocalPort $puerto -State Listen -ErrorAction SilentlyContinue
        return ($null -ne $escuchando)
    } catch {
        return $false
    }
}

function Get-Devices {
    # Todos los dispositivos que usbipd conoce, enchufados o solo recordados.
    $state = usbipd state | ConvertFrom-Json
    return $state.Devices
}

function Get-Known {
    # 'usbipd state' devuelve JSON, pero incluye tambien los dispositivos RECORDADOS
    # (seccion Persisted) que no estan enchufados: esos traen BusId vacio y no se
    # pueden adjuntar. Hay que separarlos de los realmente presentes.
    #   BusId vacio          -> conocido pero sin enchufar
    #   ClientIPAddress nulo -> enchufado pero no adjuntado a WSL
    return Get-Devices | Where-Object {
        $id = $_.InstanceId
        ($DevicePatterns | Where-Object { $id -like "*$_*" }).Count -gt 0
    }
}

function Start-WslKeepAlive {
    # usbipd necesita la distro viva. Un proceso corto basta para levantarla;
    # despues la mantiene abierta la propia GUI.
    Start-Process -WindowStyle Hidden -FilePath "wsl.exe" `
        -ArgumentList "-d", $Distro, "--exec", "sleep", "90" | Out-Null

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        $running = (wsl.exe -l --running -q) -replace "`0", ""
        if ($running -match [regex]::Escape($Distro)) { return $true }
    }
    return $false
}

Test-Usbipd

$conocidos = @(Get-Known)
$presentes = @($conocidos | Where-Object { $_.BusId })
$ausentes = @($conocidos | Where-Object { -not $_.BusId })

if ($presentes.Count -eq 0) {
    Write-Host "No hay ningun dispositivo enchufado al equipo." -ForegroundColor Red
    if ($ausentes.Count -gt 0) {
        Write-Host "Conocidos de otras sesiones, pero ahora ausentes:" -ForegroundColor Yellow
        foreach ($d in $ausentes) { Write-Host "    $($d.Description)" }
    }
    Write-Host ""
    Write-Host "Enchufa los brazos y la camara, comprueba su alimentacion (el seguidor"
    Write-Host "necesita los 12V) y vuelve a ejecutar este script."
    exit 1
}

# Si la GUI esta configurada para recibir el video por TCP, la camara NO debe
# entrar en el tunel USB/IP: ahi es donde su trafico isocrono tira a los brazos.
$modoStream = $false
$ajustes = Join-Path (Join-Path $ProjectDir "gui_data") "ajustes.json"
if (Test-Path $ajustes) {
    try {
        $fuente = (Get-Content $ajustes -Raw | ConvertFrom-Json).camera.index_or_path
        if ($fuente -and $fuente.ToString().StartsWith("http")) { $modoStream = $true }
    } catch {
        # Un ajustes.json ilegible no debe impedir arrancar.
    }
}
if (-not $modoStream -and (Test-ServidorCamara $PuertoCamara)) {
    Write-Host "Hay un servidor de camara escuchando en el puerto $PuertoCamara." -ForegroundColor Cyan
    $modoStream = $true
}
if ($modoStream) {
    Write-Host "Camara por stream TCP: no se adjunta ninguna camara a WSL." -ForegroundColor Cyan
    Write-Host "  Arranca el servidor en otra ventana de PowerShell:" -ForegroundColor Cyan
    Write-Host "      python servidor_camara.py" -ForegroundColor Cyan
    $presentes = @($presentes | Where-Object { $_.InstanceId -notlike "*$CamaraExterna*" -and $_.InstanceId -notlike "*$CamaraIntegrada*" })
}

# Sin camara USB externa, se recurre a la del portatil para no quedarse sin video.
$hayExterna = @($presentes | Where-Object { $_.InstanceId -like "*$CamaraExterna*" }).Count -gt 0
if (-not $modoStream -and -not $hayExterna -and -not $SinCamaraIntegrada) {
    $integrada = @(Get-Devices | Where-Object { $_.InstanceId -like "*$CamaraIntegrada*" -and $_.BusId })
    if ($integrada.Count -gt 0) {
        Write-Host "No hay camara USB externa: se usara la integrada del portatil." -ForegroundColor Yellow
        Write-Host "  Windows no podra usarla mientras este adjuntada a WSL." -ForegroundColor Yellow
        Write-Host "  Para dejarla en Windows:  .\iniciar_gui.ps1 -SinCamaraIntegrada" -ForegroundColor Yellow
        $presentes = @($presentes + $integrada)
    }
}

Write-Host "Enchufados: $($presentes.Count)" -ForegroundColor Green
foreach ($d in $presentes) { Write-Host "  $($d.BusId)  $($d.Description)" }
if ($ausentes.Count -gt 0) {
    Write-Host "Sin enchufar (se recuerdan, pero no estan):" -ForegroundColor Yellow
    foreach ($d in $ausentes) { Write-Host "    $($d.Description)" }
}

$pendientes = @($presentes | Where-Object { -not $_.ClientIPAddress })
if ($pendientes.Count -gt 0) {
    Write-Host "Levantando WSL ($Distro)..."
    if (-not (Start-WslKeepAlive)) {
        Write-Host "WSL no arranco a tiempo. Abre una terminal de Ubuntu y reintenta." -ForegroundColor Red
        exit 1
    }

    # Orden fijo por BusId: Linux numera /dev/ttyACM* segun el orden de llegada, asi que
    # adjuntar siempre en la misma secuencia mantiene estable que brazo es ACM0 y cual ACM1.
    foreach ($arm in ($pendientes | Sort-Object BusId)) {
        Write-Host "Adjuntando $($arm.BusId) ($($arm.Description))..."
        # 'bind' requiere admin y solo hace falta la primera vez por dispositivo.
        usbipd attach --wsl --busid $arm.BusId
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Fallo al adjuntar $($arm.BusId)." -ForegroundColor Red
            Write-Host "Si dice 'not shared', ejecuta UNA VEZ como administrador:" -ForegroundColor Yellow
            Write-Host "    usbipd bind --busid $($arm.BusId)" -ForegroundColor Yellow
            exit 1
        }
    }
} else {
    Write-Host "Todo estaba ya adjuntado a WSL." -ForegroundColor Green
}

Write-Host ""
Write-Host "Abriendo la GUI. NO cierres esta ventana: mantiene WSL vivo." -ForegroundColor Cyan
Write-Host ""
wsl.exe -d $Distro --cd "$ProjectDir" -- bash iniciar_gui.sh
