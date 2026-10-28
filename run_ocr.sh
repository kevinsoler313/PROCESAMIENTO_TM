#!/usr/bin/env bash
# =============================================================================
# LANZADOR: run_ocr.sh
# Activa el entorno virtual ocr_env y ejecuta OCR.sh con todos los argumentos.
# USO: bash run_ocr.sh [opciones de OCR.sh]
#   Ej: bash run_ocr.sh -i input/ -o output/
#   Ej: bash run_ocr.sh --no-kraken -j 4
#   Ej: bash run_ocr.sh --cpu --no-paddle
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/ocr_env"

if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
    echo "❌ Entorno virtual no encontrado en: ${VENV_DIR}"
    echo "   Ejecuta primero: python3 -m venv ocr_env && source ocr_env/bin/activate && pip install paddlepaddle-gpu paddleocr kraken"
    exit 1
fi

# Activar entorno virtual
source "${VENV_DIR}/bin/activate"
echo "✅ Entorno virtual activado: $(python3 --version)"

# Pasar todos los argumentos al script principal
exec bash "${SCRIPT_DIR}/OCR.sh" "$@"
