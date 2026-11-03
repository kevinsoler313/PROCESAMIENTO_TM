#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   instalar.sh  ─  INSTALADOR COMPLETO DE UN EQUIPO
# DESC:     Deja un equipo recién clonado listo para todos los procesos:
#             1 · Paquetes del sistema que falten (apt, pide la contraseña una vez)
#             2 · Entorno de Python e ícono (instalar_interfaz.sh)
#             3 · Verificación: cada herramienta responde
#           No crea carpetas de datos: INPUT, OUTPUT, etc. se eligen en la
#           interfaz. Se puede volver a correr: lo ya instalado queda igual.
# USO:      bash instalar.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAQUETES=(git ocrmypdf libimage-exiftool-perl poppler-utils tesseract-ocr tesseract-ocr-spa
          ghostscript parallel python3-venv libxcb-cursor0)

echo "═══ 1/3 · Paquetes del sistema"
FALTAN=()
for p in "${PAQUETES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || FALTAN+=("$p")
done
if (( ${#FALTAN[@]} )); then
    echo "→ Faltan: ${FALTAN[*]}"
    echo "→ Se pedirá la contraseña del equipo (sudo)."
    sudo apt-get update -q
    sudo apt-get install -y "${FALTAN[@]}"
else
    echo "→ Ya están todos."
fi

echo
echo "═══ 2/3 · Entorno de Python e ícono"
bash "${SCRIPT_DIR}/instalar_interfaz.sh"

echo
echo "═══ 3/3 · Verificación"
FALLOS=0
probar() {    # probar <nombre> <comando…>
    local nombre="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  ✅ ${nombre}"
    else
        echo "  ❌ ${nombre}"
        FALLOS=$((FALLOS + 1))
    fi
}
probar "Tesseract con español"   bash -c "tesseract --list-langs 2>&1 | grep -qx spa"
probar "ocrmypdf"                ocrmypdf --version
probar "exiftool"                exiftool -ver
probar "pdfinfo"                 pdfinfo -v
probar "GNU parallel"            parallel --version
probar "Ghostscript"             gs --version
probar "git"                     git --version
probar "Librerías de Python"     "${SCRIPT_DIR}/ocr_env/bin/python" -c \
    "import PySide6.QtWidgets, PySide6.QtPdf, pypdf, reportlab, pikepdf, PIL, psutil"
probar "CSV de metadatos y logo" test -s "${SCRIPT_DIR}/recursos/TechnoMart Funza - TEMP.csv" -a \
    -s "${SCRIPT_DIR}/recursos/logo.png"
probar "Actualizable (git clone)" test -d "${SCRIPT_DIR}/.git"

echo
if (( FALLOS )); then
    echo "❌ ${FALLOS} verificación(es) fallaron. Revisa los mensajes de arriba y vuelve a correr: bash instalar.sh"
    exit 1
fi
echo "✅ Equipo listo. Abre \"Procesamiento OCR\" desde el menú o el escritorio y elige las carpetas de"
echo "   trabajo en Procesamiento › CARPETAS DE TRABAJO."
