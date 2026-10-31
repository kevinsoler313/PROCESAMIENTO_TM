#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   instalar_interfaz.sh
# DESC:     Instala la interfaz de escritorio "Procesamiento OCR":
#             · librerías de Python en ocr_env (requirements.txt)
#             · lanzador en el menú de aplicaciones y en el escritorio, con ícono
#           No usa sudo. Las librerías del sistema que necesita Qt van en el README.
# USO:      bash instalar_interfaz.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/ocr_env"
APP="${SCRIPT_DIR}/interfaz/app.py"
ICONO="${SCRIPT_DIR}/interfaz/icono.png"
DESKTOP_ID="procesamiento-ocr.desktop"
MENU_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

echo "→ Entorno de Python: ${VENV_DIR}"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"${VENV_DIR}/bin/pip" install -q -r "${SCRIPT_DIR}/requirements.txt"

echo "→ Lanzador en el menú: ${MENU_DIR}/${DESKTOP_ID}"
mkdir -p "$MENU_DIR"
cat > "${MENU_DIR}/${DESKTOP_ID}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Procesamiento OCR
Comment=Digitalización de libros patrimoniales: OCR, marca de agua y PDF/A
Exec="${VENV_DIR}/bin/python" "${APP}"
Path=${SCRIPT_DIR}
Icon=${ICONO}
Terminal=false
Categories=Office;Graphics;
StartupWMClass=procesamiento-ocr
EOF
chmod +x "${MENU_DIR}/${DESKTOP_ID}"
command -v update-desktop-database >/dev/null && update-desktop-database "$MENU_DIR" 2>/dev/null || true

# Escritorio (Linux Mint lo llama "Escritorio"; xdg-user-dir da la ruta real)
ESCRITORIO="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [[ -d "$ESCRITORIO" ]]; then
    cp "${MENU_DIR}/${DESKTOP_ID}" "${ESCRITORIO}/${DESKTOP_ID}"
    chmod +x "${ESCRITORIO}/${DESKTOP_ID}"
    # Marca el lanzador como confiable para que se abra con doble clic
    command -v gio >/dev/null && gio set "${ESCRITORIO}/${DESKTOP_ID}" metadata::trusted true 2>/dev/null || true
    echo "→ Acceso directo en el escritorio: ${ESCRITORIO}/${DESKTOP_ID}"
fi

echo "✅ Listo. Busca \"Procesamiento OCR\" en el menú o en el escritorio."
