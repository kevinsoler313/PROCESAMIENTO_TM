#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   inyectar_metadatos.sh  ─  METADATOS PARA LOS PDF QUE QUEDARON SIN ELLOS
# DESC:     Toma los PDF de OUTPUT/SIN METADATOS (los que main.sh entregó sin
#           entrada en el CSV) y, con el CSV que se indique (puede ser otro):
#             · si el código está → escribe los metadatos (mismos campos que
#               main.sh), mueve el PDF a OUTPUT y actualiza el registro a OK
#               con el SHA-256 y el peso nuevos
#             · si no está → lo deja donde está y lo reporta
#           No necesita los TIFF ni repite el OCR.
#
# USO:
#   bash inyectar_metadatos.sh --csv metadatos.csv
#   bash inyectar_metadatos.sh --csv metadatos.csv --book AR10611 --book XTEST01
#   bash inyectar_metadatos.sh --csv m.csv --output-dir /datos/OUT --registro-dir /datos/REGISTRO
#
# SALIDA: logs/inyeccion_<ts>.txt con una línea ESTADO|libro|motivo por PDF
#         (OK, FALTA = no está en el CSV, ERROR)
# =============================================================================

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
# shellcheck source=metadatos.sh
source "${SCRIPT_DIR}/metadatos.sh"

CSV_META=""
OUTPUT_DIR="/mnt/datos/OUTPUT"
REGISTRO_DIR=""            # vacío = REGISTRO junto a OUTPUT, como en la interfaz
declare -a SOLO=()
LOG_DIR="${SCRIPT_DIR}/logs"
LOG="${LOG_DIR}/inyeccion_${TIMESTAMP}.log"
RESULTADOS="${LOG_DIR}/inyeccion_${TIMESTAMP}.txt"

mkdir -p "$LOG_DIR"
_log() { printf '[%s] [%-5s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" | tee -a "$LOG"; }

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --csv)          CSV_META="$2"; shift 2 ;;
        --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
        --registro-dir) REGISTRO_DIR="$2"; shift 2 ;;
        --book)         SOLO+=("$2"); shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *) _log ERROR "Argumento desconocido: $1"; exit 1 ;;
    esac
done

[[ -n "$REGISTRO_DIR" ]] || REGISTRO_DIR="$(dirname "$OUTPUT_DIR")/REGISTRO"
SIN_META_DIR="${OUTPUT_DIR}/${SIN_META_SUBDIR}"
REGISTRO_CSV="${REGISTRO_DIR}/registro_libros_procesados.csv"

for cmd in exiftool pdfinfo sha256sum; do
    command -v "$cmd" >/dev/null || { _log ERROR "Falta: $cmd"; exit 1; }
done
[[ -f "$CSV_META" ]] || { _log ERROR "Falta el CSV de metadatos (--csv): ${CSV_META:-sin indicar}"; exit 1; }

INDICE="$(mktemp)"
trap 'rm -f "$INDICE"' EXIT
meta_indice "$CSV_META" > "$INDICE"
[[ -s "$INDICE" ]] || { _log ERROR "El CSV no tiene códigos válidos: $CSV_META"; exit 1; }
_log INFO "CSV: $(basename "$CSV_META") ($(cut -d'|' -f1 "$INDICE" | sort -u | wc -l) códigos)"
_log INFO "PDF sin metadatos: $SIN_META_DIR"
_log INFO "Resultados de esta ejecución: $RESULTADOS"
: > "$RESULTADOS"

# Actualiza la línea del libro en el registro: estado OK, SHA-256 y peso nuevos;
# conserva páginas y peso de la carpeta de TIFF (que ya no existe)
actualizar_registro() {
    local libro="$1" pdf="$2"
    local peso sha fecha prev paginas peso_carpeta
    peso=$(du -sb "$pdf" | awk '{printf "%.2f", $1/1048576}' | tr ',' '.')
    sha=$(sha256sum "$pdf" | awk '{print $1}')
    fecha=$(date '+%Y-%m-%d %H:%M:%S')
    prev=$(awk -F'|' -v c="$libro" '$1==c{print; exit}' "$REGISTRO_CSV" 2>/dev/null || true)
    if [[ -n "$prev" ]]; then
        paginas=$(cut -d'|' -f2 <<< "$prev")
        peso_carpeta=$(cut -d'|' -f3 <<< "$prev")
    else
        paginas=$(pdfinfo "$pdf" 2>/dev/null | awk '/^Pages:/{print $2}')
        peso_carpeta="?"
    fi
    mkdir -p "$REGISTRO_DIR"
    if [[ ! -f "$REGISTRO_CSV" ]]; then
        echo "Libro|Paginas|Peso_Carpeta_GB|Peso_PDF_MB|Estado|Fecha_Proceso|SHA256|Detalle_Error" > "$REGISTRO_CSV"
    fi
    # Quitar la línea anterior del libro (comparación exacta, sin regex) y anexar la nueva
    awk -F'|' -v c="$libro" '$1!=c' "$REGISTRO_CSV" > "${REGISTRO_CSV}.tmp" && mv "${REGISTRO_CSV}.tmp" "$REGISTRO_CSV"
    printf '%s|%s|%s|%s|%s|%s|%s|%s\n' "$libro" "${paginas:-?}" "$peso_carpeta" "$peso" "OK" "$fecha" "$sha" \
        "Metadatos inyectados después ($(basename "$CSV_META"))" >> "$REGISTRO_CSV"
}

ok=0; falta=0; error=0
shopt -s nullglob
for pdf in "$SIN_META_DIR"/*.pdf; do
    libro="$(basename "${pdf%.pdf}")"
    if [[ ${#SOLO[@]} -gt 0 ]] && ! printf '%s\n' "${SOLO[@]}" | grep -qxF "$libro"; then
        continue
    fi
    meta=$(meta_buscar "$libro" "$INDICE")
    if [[ -z "$meta" ]]; then
        _log WARN "[${libro}] No está en el CSV — sigue en ${SIN_META_SUBDIR}."
        echo "FALTA|${libro}|No está en $(basename "$CSV_META")" >> "$RESULTADOS"
        falta=$((falta + 1)); continue
    fi
    destino="${OUTPUT_DIR}/${libro}.pdf"
    if [[ -e "$destino" ]]; then
        _log ERROR "[${libro}] Ya existe ${destino}; no se sobrescribe."
        echo "ERROR|${libro}|Ya existe un PDF con ese nombre en OUTPUT" >> "$RESULTADOS"
        error=$((error + 1)); continue
    fi
    if ! meta_escribir "$libro" "$pdf" "$meta" || ! pdfinfo "$pdf" >/dev/null 2>&1; then
        _log ERROR "[${libro}] exiftool no pudo escribir los metadatos."
        echo "ERROR|${libro}|exiftool no pudo escribir los metadatos" >> "$RESULTADOS"
        error=$((error + 1)); continue
    fi
    mv "$pdf" "$destino"
    actualizar_registro "$libro" "$destino"
    _log OK "[${libro}] Metadatos inyectados → OUTPUT/${libro}.pdf"
    echo "OK|${libro}|" >> "$RESULTADOS"
    ok=$((ok + 1))
done

_log INFO "Resumen: ${ok} con metadatos inyectados · ${falta} siguen sin entrada en el CSV · ${error} con error"
[[ $error -eq 0 ]]
