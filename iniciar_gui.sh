#!/usr/bin/env bash
# Lanzador de la GUI dentro de WSL2 (Ubuntu 22.04 + conda "lerobot").
#
#   bash iniciar_gui.sh            -> pide permisos de puerto y abre la interfaz
#   bash iniciar_gui.sh --no-chmod -> sáltate el chmod (si ya tienes permisos)

set -euo pipefail

ENV_NAME="${LEROBOT_ENV:-lerobot}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Permisos de los puertos serie inyectados por usbipd.
SKIP_CHMOD=0
if [[ "${1:-}" == "--no-chmod" ]]; then
  SKIP_CHMOD=1
  shift
fi
if [[ "$SKIP_CHMOD" -eq 0 ]]; then
  if ! compgen -G "/dev/ttyACM*" > /dev/null; then
    echo "⚠  No hay /dev/ttyACM*. Con ESTA terminal abierta, desde PowerShell en Windows:"
    echo "     usbipd list                       # anota el BUSID de cada CH343"
    echo "     usbipd attach --wsl --busid 2-1"
    echo "     usbipd attach --wsl --busid 2-2"
    echo "   (usbipd falla si WSL no está corriendo: por eso hay que dejar esta terminal abierta)"
  else
    # Solo pedimos sudo si de verdad hace falta: quien esté en el grupo 'dialout'
    # ya tiene acceso y no debería ver una petición de contraseña.
    NEEDS_CHMOD=0
    for dev in /dev/ttyACM*; do
      [[ -r "$dev" && -w "$dev" ]] || NEEDS_CHMOD=1
    done
    if [[ "$NEEDS_CHMOD" -eq 1 ]]; then
      echo "→ Ajustando permisos de /dev/ttyACM* (pedirá tu contraseña de sudo)"
      echo "  Para no repetirlo nunca más:  sudo usermod -aG dialout \$USER  + reiniciar WSL"
      sudo chmod 666 /dev/ttyACM*
    else
      echo "✓ Permisos de puerto correctos"
    fi
  fi
fi

# 2. Entorno conda.
for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
  if [[ -f "$base/etc/profile.d/conda.sh" ]]; then
    # shellcheck disable=SC1091
    source "$base/etc/profile.d/conda.sh"
    break
  fi
done
conda activate "$ENV_NAME"

# 3. Arranque.
cd "$HERE"
exec python -m lerobot_gui "$@"
