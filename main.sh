#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   main.sh  ─  ORQUESTADOR PRINCIPAL
# VERSION:  1.0.0
# FECHA:    2026-02-23
# DESC:     Coordina el pipeline completo por libro:
#
#   PASO 1 · Renombrar carpetas "NNN OK" → "NNN" en input/
#   PASO 2 · OCR (run_ocr.sh) → PDF por libro
#   PASO 3 · Marca de agua (pypdf + reportlab + logo.png)
#   PASO 4 · Conversión PDF/A-3b v1.7 (ocrmypdf --skip-text)
#   PASO 5 · Metadatos (exiftool desde CSV)
#   PASO 6 · Verificación TIFF vs páginas PDF → limpiar TIFFs si coincide
#   PASO 7 · Registro SHA-256 (CSV final)
#
# USO:
#   bash main.sh                      # pipeline completo
#   bash main.sh --skip-ocr           # saltar paso OCR (libros ya procesados)
#   bash main.sh --only-book 003106   # procesar únicamente ese libro
#   bash main.sh --jobs 4             # post-proceso de 4 libros en paralelo
#   bash main.sh -h                   # ayuda
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# Subir límite de archivos abiertos (necesario para pdfunite con libros grandes)
ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# 0. CONFIGURACIÓN — ajusta estas rutas si es necesario
# ─────────────────────────────────────────────────────────────────────────────

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

INPUT_DIR="/mnt/datos/INPUT"
# OUTPUT contiene ÚNICAMENTE PDFs finalizados y verificados.
OUTPUT_DIR="/mnt/datos/OUTPUT"
# TEMPORALES/<libro>/ guarda todo lo intermedio (OCR, _ocr, _marca, _final).
# La carpeta del libro es también su candado entre terminales.
TEMP_DIR="/mnt/datos/TEMPORALES"
REVIEW_DIR="/mnt/datos/REVIEW"
REGISTRO_DIR="/mnt/datos/REGISTRO"
PNG_DIR="/mnt/datos/PNG"
LOG_DIR="${SCRIPT_DIR}/logs"
MAIN_LOG="${LOG_DIR}/main_${TIMESTAMP}.log"

CSV_META="${SCRIPT_DIR}/TechnoMart Funza - TEMP.csv"
LOGO_PNG="${SCRIPT_DIR}/logo.png"
VENV_DIR="${SCRIPT_DIR}/ocr_env"
RUN_OCR="${SCRIPT_DIR}/run_ocr.sh"
MARCA_AGUA="${SCRIPT_DIR}/marca_agua.py"

# Registro CSV principal (solo libros exitosos)
REGISTRO_CSV="${REGISTRO_DIR}/registro_libros_procesados.csv"
# Registro de libros enviados a REVIEW
REVIEW_LOG="${REVIEW_DIR}/review_log.csv"
SIN_META_TXT="${LOG_DIR}/sin_metadatos_${TIMESTAMP}.txt"
# Resultado de cada libro en esta ejecución (ESTADO|libro|motivo). Lo escriben
# también las subshells (--jobs, paso 6), por eso el resumen se arma desde aquí.
RESULTS_FILE="${LOG_DIR}/resultados_${TIMESTAMP}.txt"

# Flags de control
SKIP_OCR=false
ONLY_BOOK=""
PARALLEL_JOBS=1   # Libros en post-proceso simultáneo (pasos 3-7). Usar con --jobs N

# Flag opcional --png: además del PDF, exporta cada página a PNG (300 DPI) desde
# los TIFF originales (limpios, sin marca de agua). Solo actúa si se pasa --png,
# así no interfiere con el procesamiento normal. Pensado para libros puntuales.
EXPORT_PNG=false
PNG_DPI=300

# Libros elegidos con --book (repetible; lo usa la interfaz) y procesos de
# Tesseract en paralelo (--ocr-jobs; vacío = valor por defecto de OCR.sh)
declare -a BOOKS=()
OCR_JOBS=""

# Texto de la marca de agua, al pie de cada página junto al logo (--texto-marca;
# vacío = solo el logo)
WATERMARK_TEXT="CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES"

# Marca diagonal opcional, ascendente de izquierda a derecha (--marca-diagonal;
# vacío = sin marca diagonal, que es lo predeterminado)
DIAGONAL_TEXT=""

# JSON con posición, transparencia y tamaño de logo, texto y diagonal
# (--marca-config; vacío = valores predeterminados de marca_agua.py)
MARCA_CONFIG=""

# Libro actualmente en proceso — usado por el trap para liberarlo si se interrumpe
CURRENT_BOOK=""

# ─── Listas para el resumen final (se llenan desde RESULTS_FILE al terminar) ───
declare -a BOOKS_OK=()          # Libros procesados exitosamente
declare -a BOOKS_NO_META=()     # Libros sin metadatos en CSV (pero PDF OK)
declare -a BOOKS_REVIEW=()      # Libros enviados a REVIEW (cualquier fallo)
declare -a BOOKS_SKIPPED=()     # Libros omitidos (ya procesados previamente)
declare -a BOOKS_RETRY=()       # Libros que siguen en INPUT por fallo del OCR

# Anota el resultado de un libro. Una línea corta con >> es atómica, así que
# varias subshells pueden escribir a la vez sin mezclarse.
record_result() {
    local estado="$1" book_name="$2" motivo="${3:-}"
    printf '%s|%s|%s\n' "$estado" "$book_name" "$motivo" >> "$RESULTS_FILE"
}

# Reconstruye las listas del resumen a partir de RESULTS_FILE.
load_results() {
    BOOKS_OK=(); BOOKS_NO_META=(); BOOKS_REVIEW=(); BOOKS_SKIPPED=(); BOOKS_RETRY=()
    [[ -f "$RESULTS_FILE" ]] || return 0
    local estado book_name motivo
    while IFS='|' read -r estado book_name motivo; do
        case "$estado" in
            OK)       BOOKS_OK+=("$book_name") ;;
            SIN_META) BOOKS_OK+=("$book_name"); BOOKS_NO_META+=("$book_name") ;;
            REVIEW)   BOOKS_REVIEW+=("${book_name} → ${motivo}") ;;
            ERROR)    BOOKS_REVIEW+=("${book_name} → ⚠ ${motivo}") ;;
            OMITIDO)  BOOKS_SKIPPED+=("$book_name") ;;
            REINTENTAR) BOOKS_RETRY+=("${book_name} → ${motivo}") ;;
        esac
    done < "$RESULTS_FILE"
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. LOGGING
# ─────────────────────────────────────────────────────────────────────────────

# Las carpetas de datos se crean en main(), cuando ya se leyeron --input-dir y demás
mkdir -p "$LOG_DIR"
touch "$MAIN_LOG"

_log() {
    local level="$1"; shift
    local msg="$*"
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local color=""
    case "$level" in
        INFO)  color="\033[0;36m"  ;;
        OK)    color="\033[0;32m"  ;;
        WARN)  color="\033[0;33m"  ;;
        ERROR) color="\033[0;31m"  ;;
        STEP)  color="\033[1;35m"  ;;
        *)     color="\033[0m"     ;;
    esac
    local reset="\033[0m"
    printf "${color}[%s] [%-5s] %s${reset}\n" "$ts" "$level" "$msg" | tee -a "$MAIN_LOG" >&2
}
log_info()  { _log INFO  "$@"; }
log_ok()    { _log OK    "$@"; }
log_warn()  { _log WARN  "$@"; }
log_error() { _log ERROR "$@"; }
log_step()  { _log STEP  "$@"; }

# ─────────────────────────────────────────────────────────────────────────────
# MANEJO DE ERRORES
# ─────────────────────────────────────────────────────────────────────────────

# Libro interrumpido (Ctrl+C, kill, caída): si aún tiene sus TIFF, se borra
# TEMPORALES/<libro> (parciales + candado) para que la próxima ejecución lo
# reprocese desde cero. Si los TIFF ya se borraron, no se toca nada.
release_book() {
    local book_name="$1"
    if find "${INPUT_DIR}/${book_name}" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) -print -quit 2>/dev/null | grep -q .; then
        rm -rf "${TEMP_DIR}/${book_name}" 2>/dev/null || true
        log_warn "[${book_name}] ⚡ Interrumpido. Temporales y candado eliminados; sigue en INPUT y se reprocesará en la próxima ejecución."
    else
        log_error "[${book_name}] ⚡ Interrumpido después de borrar los TIFF. TEMPORALES/${book_name}/ se conserva para recuperación manual."
    fi
}

# Trap de cada libro en paralelo (--jobs > 1): solo libera su libro.
_on_book_exit() {
    if [[ -n "$CURRENT_BOOK" ]]; then
        release_book "$CURRENT_BOOK"
        CURRENT_BOOK=""
    fi
}

# Único trap EXIT del proceso principal: libera el libro en curso y borra temporales.
_cleanup() {
    local code=$?
    _on_book_exit
    cleanup_temp
    if [[ $code -ne 0 ]]; then
        log_error "main.sh terminó con error (código $code). Log: $MAIN_LOG"
    else
        log_ok "Pipeline principal finalizado correctamente."
    fi
}
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'log_error "Error inesperado en línea $LINENO"; exit 1' ERR

# ─────────────────────────────────────────────────────────────────────────────
# CUARENTENA — quarantine_book()
# Mueve el libro fallido a REVIEW, limpia OUTPUT y registra el motivo.
# GARANTÍA: después de esta función, OUTPUT queda 100% limpio del libro.
# ─────────────────────────────────────────────────────────────────────────────

quarantine_book() {
    local book_name="$1"
    local motivo="$2"
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"

    log_error "[${book_name}] 🔍 Enviando a REVIEW: ${motivo}"

    # ── 1. Mover carpeta del libro desde INPUT → REVIEW ──
    local src_dir="${INPUT_DIR}/${book_name}"
    local dst_dir="${REVIEW_DIR}/${book_name}"

    if [[ -d "$src_dir" ]]; then
        # Si ya existe una entrada en REVIEW (re-ejecución), agregar timestamp
        if [[ -d "$dst_dir" ]]; then
            dst_dir="${REVIEW_DIR}/${book_name}_${TIMESTAMP}"
            log_warn "[${book_name}] Ya existe en REVIEW — guardando como: $(basename "$dst_dir")"
        fi
        mv "$src_dir" "$dst_dir" 2>/dev/null \
            && log_ok  "[${book_name}] Carpeta INPUT movida → REVIEW/$(basename "$dst_dir")" \
            || log_warn "[${book_name}] No se pudo mover la carpeta INPUT (puede que ya no exista)."
    else
        log_warn "[${book_name}] Carpeta INPUT no encontrada (ya movida o nunca existió)."
    fi

    # ── 2. Limpiar TODO rastro del libro en TEMPORALES (libera el candado) ──
    rm -rf "${TEMP_DIR}/${book_name}" 2>/dev/null || true
    log_ok "[${book_name}] TEMPORALES limpio (sin rastros del libro fallido)."

    # ── 3. Inicializar REVIEW_LOG si no existe ──
    if [[ ! -f "$REVIEW_LOG" ]]; then
        printf '%s\n' "Libro|Fecha|Motivo|Ruta_REVIEW" > "$REVIEW_LOG"
    fi

    # ── 4. Registrar en review_log.csv ──
    printf '%s|%s|%s|%s\n' \
        "$book_name" "$ts" "$motivo" "$(basename "$dst_dir" 2>/dev/null || echo "$book_name")" \
        >> "$REVIEW_LOG"

    # ── 5. Agregar al array de resumen ──
    record_result REVIEW "$book_name" "$motivo"

    # ── 6. Limpiar CURRENT_BOOK para evitar doble cuarentena en el trap ──
    [[ "$CURRENT_BOOK" == "$book_name" ]] && CURRENT_BOOK=""

    log_warn "[${book_name}] Libro en REVIEW. Corrígelo y vuelve a colocarlo en INPUT para reprocesar."
}

# ─────────────────────────────────────────────────────────────────────────────
# VERIFICACIÓN DE DEPENDENCIAS
# ─────────────────────────────────────────────────────────────────────────────

check_deps() {
    log_info "Verificando dependencias..."
    local missing=0
    for cmd in exiftool ocrmypdf pdfunite; do
        command -v "$cmd" &>/dev/null || { log_error "Falta: $cmd"; missing=$((missing+1)); }
    done
    [[ -f "$VENV_DIR/bin/activate" ]] || { log_error "Falta entorno virtual: $VENV_DIR"; missing=$((missing+1)); }
    [[ -f "$LOGO_PNG"              ]] || { log_error "Falta logo: $LOGO_PNG";  missing=$((missing+1)); }
    [[ -f "$CSV_META"              ]] || { log_error "Falta CSV: $CSV_META";   missing=$((missing+1)); }
    [[ -f "$RUN_OCR"               ]] || { log_error "Falta: $RUN_OCR";        missing=$((missing+1)); }
    [[ -f "$MARCA_AGUA"            ]] || { log_error "Falta: $MARCA_AGUA";     missing=$((missing+1)); }
    if [[ -n "$MARCA_CONFIG" && ! -f "$MARCA_CONFIG" ]]; then
        log_error "Falta configuración de marca de agua: $MARCA_CONFIG"; missing=$((missing+1))
    fi
    if [[ $missing -gt 0 ]]; then
        log_error "$missing dependencia(s) faltante(s). Abortando."
        exit 1
    fi
    # Verificar pypdf y reportlab en el venv
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    python3 -c "import pypdf, reportlab, pikepdf, PIL" 2>/dev/null \
        || { log_error "Faltan librerías en ocr_env. Ejecuta: pip install pypdf reportlab pikepdf pillow"; exit 1; }
    log_ok "Dependencias OK."
}

# ─────────────────────────────────────────────────────────────────────────────
# CARGAR ÍNDICE DEL CSV DE METADATOS
# (Columnas: código, fondo, subfondo, equipo, resolución)
# ─────────────────────────────────────────────────────────────────────────────

TEMP_META=""
load_csv_index() {
    TEMP_META="$(mktemp)"
    awk -F',' '
    {
        # Limpiar espacios en todos los campos relevantes
        gsub(/\r/, "")
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $3)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $4)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $5)
        # Filas con código alfanumérico (p. ej. AHFCP0001) o numérico (legado)
        if ($1 ~ /^[A-Za-z0-9._-]+$/) {
            # Guardar en formato: CODIGO|fondo|subfondo|equipo|resolucion
            printf "%s|%s|%s|%s|%s\n", $1, $2, $3, $4, $5
            # Códigos puramente numéricos (legado): añadir variante con padding
            # de 6 dígitos por si las carpetas vienen sin ceros a la izquierda.
            if ($1 ~ /^[0-9]+$/) {
                printf "%06d|%s|%s|%s|%s\n", $1, $2, $3, $4, $5
            }
        }
    }' "$CSV_META" | sort -u > "$TEMP_META"

    [[ -s "$TEMP_META" ]] || { log_error "No se pudo leer el CSV de metadatos."; exit 1; }
    local count; count=$(cut -d'|' -f1 "$TEMP_META" | sort -u | wc -l)
    log_info "CSV cargado: ${count} códigos únicos."
}
cleanup_temp() { if [[ -n "$TEMP_META" && -f "$TEMP_META" ]]; then rm -f "$TEMP_META"; fi; }

# ─────────────────────────────────────────────────────────────────────────────
# PASO 1 · RENOMBRAR CARPETAS "NNN OK" → "NNN"
# ─────────────────────────────────────────────────────────────────────────────

step1_rename_input() {
    log_step "══ PASO 1: Renombrar carpetas (eliminar ' OK') ══"
    local renamed=0 skipped=0

    while IFS= read -r dir; do
        local name; name="$(basename "$dir")"
        # Detectar si termina en " OK" o " ok" (case insensitive)
        if [[ "$name" =~ [[:space:]][Oo][Kk]$ ]]; then
            local new_name="${name%[[:space:]]*[Oo][Kk]}"
            new_name="${new_name%"${new_name##*[![:space:]]}"}"  # trim trailing spaces
            local new_path="${INPUT_DIR}/${new_name}"
            if [[ -d "$new_path" ]]; then
                log_warn "Ya existe destino '$new_name', omitiendo renombrado de: $name"
                skipped=$((skipped+1))
            elif [[ -d "$dir" ]]; then
                # Verificación doble para evitar race conditions en ejecuciones simultáneas
                mv "$dir" "$new_path" 2>/dev/null && {
                    log_ok "Renombrado: '$name' → '$new_name'"
                    renamed=$((renamed+1))
                } || skipped=$((skipped+1))
            fi
        fi
    done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type d | sort)

    log_ok "Paso 1 completado: $renamed renombradas, $skipped omitidas."
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 2 · OCR (run_ocr.sh)
# ─────────────────────────────────────────────────────────────────────────────

# El OCR se ejecuta por libro dentro del bucle de main() y deja su PDF en
# TEMPORALES/<libro>/<libro>_ocr.pdf.

# ─────────────────────────────────────────────────────────────────────────────
# PASO 3 · MARCA DE AGUA (pypdf + reportlab + logo.png)
# Optimizaciones: cache de overlay por dimensión + ImageReader pre-cargado
# ─────────────────────────────────────────────────────────────────────────────

# Los PDF de los pasos 3 y 4 se escriben primero como <nombre>.tmp.pdf y solo
# se renombran al terminar bien. Así un PDF a medio escribir nunca se reutiliza.
pdf_valido() { pdfinfo "$1" &>/dev/null; }

# reuse_pdf <pdf>: 0 si ya existe y es válido; si existe roto lo borra.
reuse_pdf() {
    local pdf="$1"
    rm -f "${pdf%.pdf}.tmp.pdf" 2>/dev/null || true
    [[ -f "$pdf" ]] || return 1
    if pdf_valido "$pdf"; then
        return 0
    fi
    log_warn "PDF existente dañado, se regenera: $(basename "$pdf")"
    rm -f "$pdf"
    return 1
}

# commit_pdf <tmp> <final>: valida el .tmp.pdf y lo renombra al nombre final.
commit_pdf() {
    local tmp="$1" final="$2"
    if [[ -f "$tmp" ]] && pdf_valido "$tmp" && mv "$tmp" "$final"; then
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 1
}

# ocrmypdf entrega PDF/A-3 con cabecera 1.6; PDF/A-3 se basa en PDF 1.7, así
# que se sube la cabecera a 1.7 conservando el resto (XMP, pdfaid, metadatos).
set_pdf_version_17() {
    PDF_PATH="$1" python3 -c "
import os, pikepdf
path = os.environ['PDF_PATH']
with pikepdf.open(path, allow_overwriting_input=True) as pdf:
    pdf.save(path, min_version='1.7')
" 2>>"${LOG_DIR}/ocrmypdf_${TIMESTAMP}.log"
}

step3_watermark() {
    local book_name="$1"
    local book_pdf="${TEMP_DIR}/${book_name}/${book_name}_ocr.pdf"
    local out_pdf="${TEMP_DIR}/${book_name}/${book_name}_marca.pdf"
    local tmp_pdf="${out_pdf%.pdf}.tmp.pdf"

    if [[ ! -f "$book_pdf" ]]; then
        log_warn "[${book_name}] PDF fuente no encontrado: ${book_pdf} — omitiendo marca de agua."
        return 1
    fi

    if reuse_pdf "$out_pdf"; then
        log_info "[${book_name}] Marca de agua ya existe y es válida, omitiendo."
        echo "$out_pdf"
        return 0
    fi

    log_info "[${book_name}] Aplicando marca de agua (optimizada)..."

    # Logo, texto al pie y marca diagonal: posición, transparencia y tamaño de cada
    # uno vienen de --marca-config (JSON); sin él, marca_agua.py usa sus valores
    # predeterminados. Las rutas van como argumentos: ninguna se mete en código.
    local -a marca_args=(--entrada "$book_pdf" --salida "$tmp_pdf" --logo "$LOGO_PNG"
                         --texto "$WATERMARK_TEXT" --diagonal "$DIAGONAL_TEXT")
    if [[ -n "$MARCA_CONFIG" ]]; then marca_args+=(--config "$MARCA_CONFIG"); fi
    python3 "$MARCA_AGUA" "${marca_args[@]}" || rm -f "$tmp_pdf"

    if commit_pdf "$tmp_pdf" "$out_pdf"; then
        log_ok "[${book_name}] Marca de agua aplicada → $(basename "$out_pdf")"
        echo "$out_pdf"
    else
        log_error "[${book_name}] Error al generar PDF con marca de agua."
        return 1
    fi
}


# ─────────────────────────────────────────────────────────────────────────────
# PASO 4 · PDF/A-3b VERSIÓN 1.7 (ocrmypdf --skip-text)
# ─────────────────────────────────────────────────────────────────────────────

step4_pdfa() {
    local book_name="$1"
    local in_pdf="$2"         # PDF con marca de agua
    local out_pdf="${TEMP_DIR}/${book_name}/${book_name}_final.pdf"
    local tmp_pdf="${out_pdf%.pdf}.tmp.pdf"

    if reuse_pdf "$out_pdf"; then
        log_info "[${book_name}] PDF/A ya existe y es válido, omitiendo conversión."
        echo "$out_pdf"
        return 0
    fi

    log_info "[${book_name}] Convirtiendo a PDF/A-3b v1.7..."

    local avail_jobs=$(( $(nproc) - 1 ))
    [[ $avail_jobs -lt 1 ]] && avail_jobs=1

    ocrmypdf \
        --skip-text \
        --output-type pdfa-3 \
        --optimize 0 \
        --jobs "$avail_jobs" \
        --quiet \
        "$in_pdf" "$tmp_pdf" \
        2>>"${LOG_DIR}/ocrmypdf_${TIMESTAMP}.log" \
    && set_pdf_version_17 "$tmp_pdf" \
    && commit_pdf "$tmp_pdf" "$out_pdf" \
    && log_ok "[${book_name}] PDF/A-3b generado → $(basename "$out_pdf")" \
    || { rm -f "$tmp_pdf"; log_error "[${book_name}] Error en ocrmypdf. Ver: ${LOG_DIR}/ocrmypdf_${TIMESTAMP}.log"; return 1; }

    echo "$out_pdf"
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 5 · METADATOS (exiftool desde CSV)
# ─────────────────────────────────────────────────────────────────────────────

step5_metadata() {
    local book_name="$1"
    local pdf_path="$2"

    # Buscar metadatos por el código exacto de la carpeta (p. ej. AHFCP0001).
    # Comparación de campo con awk: evita que el código se interprete como regex.
    local meta
    meta=$(awk -F'|' -v code="$book_name" '$1 == code { print; exit }' "$TEMP_META" 2>/dev/null || true)

    # Compatibilidad con códigos numéricos antiguos sin ceros (3106 → 003106)
    if [[ -z "$meta" && "$book_name" =~ ^[0-9]+$ ]]; then
        local padded; padded=$(printf '%06d' "$book_name")
        meta=$(awk -F'|' -v code="$padded" '$1 == code { print; exit }' "$TEMP_META" 2>/dev/null || true)
    fi

    if [[ -z "$meta" ]]; then
        log_warn "[${book_name}] ⚠ Sin metadatos en CSV — no se modificará el PDF."
        echo "$book_name" >> "$SIN_META_TXT"
        BOOKS_NO_META+=("$book_name")
        return 0
    fi

    IFS='|' read -r _ fondo subfondo equipo resolucion <<< "$meta"

    # Limpiar espacios residuales
    subfondo="${subfondo//  / }"
    equipo="${equipo//  / }"

    log_info "[${book_name}] Insertando metadatos: Subfondo=$subfondo | Equipo=$equipo"

    exiftool -q -overwrite_original \
        -Title="$book_name" \
        -Subject="Congreso de la República - ${subfondo}" \
        -Keywords="Fondo: ${fondo}, Subfondo: ${subfondo}, Código: ${book_name}, Resolución: ${resolucion}, Equipo: ${equipo}" \
        -Author="Congreso de la República" \
        -Creator="$equipo" \
        -Producer="Sistema de Digitalización - Congreso de la República" \
        -Description="Documento digitalizado - Código: ${book_name}" \
        "$pdf_path" 2>/dev/null \
    && log_ok "[${book_name}] Metadatos insertados." \
    || log_warn "[${book_name}] Advertencia: exiftool no pudo escribir algunos metadatos."
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 6 · VERIFICACIÓN TIFF vs PÁGINAS PDF → limpiar intermedios
# Los TIFF NO se borran aquí: solo al final de process_book, cuando el PDF ya
# está en OUTPUT y registrado.
# ─────────────────────────────────────────────────────────────────────────────

step6_verify_and_cleanup() {
    local book_name="$1"
    local final_pdf="$2"
    local tiff_dir="${INPUT_DIR}/${book_name}"

    # Contar TIFFs originales (excluyendo archivos de control _*)
    local n_tiff
    n_tiff=$(find "$tiff_dir" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) -not -name '_*' 2>/dev/null | wc -l)

    # Sin TIFF no hay contra qué comparar: el PDF no se puede dar por verificado
    if [[ $n_tiff -eq 0 ]]; then
        log_error "[${book_name}] ❌ No hay TIFF en INPUT para verificar el PDF. Enviando libro a REVIEW."
        # El PDF podría ser la única copia: se guarda en REVIEW antes de limpiar TEMPORALES
        local pdf_sin_verificar="${REVIEW_DIR}/${book_name}_${TIMESTAMP}_sin_verificar.pdf"
        mv "$final_pdf" "$pdf_sin_verificar" 2>/dev/null \
            && log_warn "[${book_name}] PDF sin verificar guardado en REVIEW/$(basename "$pdf_sin_verificar")"
        quarantine_book "$book_name" "Sin TIFF en INPUT para verificar el PDF"
        echo "REVIEW"
        return 0
    fi

    # Contar páginas del PDF final
    local n_pdf_pages
    n_pdf_pages=$(pdfinfo "$final_pdf" 2>/dev/null | grep -i '^Pages:' | awk '{print $2}')
    [[ -z "$n_pdf_pages" ]] && n_pdf_pages=0

    log_info "[${book_name}] TIFFs totales: $n_tiff | Páginas PDF: $n_pdf_pages"

    if [[ "$n_tiff" -eq "$n_pdf_pages" && "$n_tiff" -gt 0 ]]; then
        # ✅ ÉXITO ESTRICTO: conteo exacto TIFFs == páginas PDF
        local peso_tiff
        peso_tiff=$(du -sb "$tiff_dir" 2>/dev/null | awk '{printf "%.3f", $1/1073741824}' | tr ',' '.')
        log_ok "[${book_name}] ✅ Conteo EXACTO ($n_tiff TIFFs == $n_pdf_pages páginas)."

        # Limpiar intermedios del OCR en TEMPORALES (queda solo <libro>_final.pdf)
        local book_tmp_dir="${TEMP_DIR}/${book_name}"
        log_info "[${book_name}] Limpiando archivos intermedios de OCR en TEMPORALES..."
        rm -rf "${book_tmp_dir}/tesseract" "${book_tmp_dir}/paddleocr" "${book_tmp_dir}/kraken" "${book_tmp_dir}/consolidado"
        rm -f "${book_tmp_dir}/${book_name}_ocr.pdf" "${book_tmp_dir}/${book_name}_marca.pdf" 2>/dev/null || true
        # NO se elimina aquí TEMPORALES/<libro>: es el LOCK del libro.
        # Se conserva hasta que el PDF final esté en OUTPUT con su nombre
        # definitivo (ver final de process_book). Así se evita la ventana de
        # carrera que borró AR10622 cuando dos terminales procesaban el mismo libro.

        log_ok "[${book_name}] Archivos intermedios eliminados."
        echo "${peso_tiff}"
    else
        # ❌ REGLA ESTRICTA: cualquier diferencia = libro inválido → REVIEW
        local n_diff=$(( n_tiff - n_pdf_pages ))
        log_error "[${book_name}] ❌ INCONSISTENCIA ESTRICTA: $n_tiff TIFFs ≠ $n_pdf_pages páginas PDF (diferencia: ${n_diff} pág.)."
        log_error "[${book_name}] Ninguna excepción permitida. Enviando libro a REVIEW."
        quarantine_book "$book_name" \
            "Inconsistencia: ${n_tiff} TIFFs ≠ ${n_pdf_pages} páginas PDF (diferencia: ${n_diff} pág.)"
        echo "REVIEW"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 7 · REGISTRO CSV DETALLADO (con metadatos, estado, timestamp)
# ─────────────────────────────────────────────────────────────────────────────

step7_register() {
    local book_name="$1"
    local final_pdf="$2"
    local n_pages="$3"
    local peso_carpeta_gb="$4"
    local status="$5"         # OK | SIN_META | ERROR
    local error_detail="$6"   # Detalle del error (si aplica)

    # Peso del PDF final en MB con decimales en punto
    local peso_pdf_mb="0.00"
    if [[ -f "$final_pdf" ]]; then
        peso_pdf_mb=$(du -sb "$final_pdf" 2>/dev/null | awk '{printf "%.2f", $1/1048576}' | tr ',' '.')
    fi

    # SHA-256 del PDF final
    local sha256="N/A"
    if [[ -f "$final_pdf" ]]; then
        sha256=$(sha256sum "$final_pdf" 2>/dev/null | awk '{print $1}')
        [[ -z "$sha256" ]] && sha256="ERROR"
    fi

    # Timestamp de procesamiento
    local proc_timestamp
    proc_timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # ── Inicializar CSV si no existe (pipe-separated para claridad) ──
    if [[ ! -f "$REGISTRO_CSV" ]]; then
        printf '%s\n' \
            "Libro|Paginas|Peso_Carpeta_GB|Peso_PDF_MB|Estado|Fecha_Proceso|SHA256|Detalle_Error" \
            > "$REGISTRO_CSV"
    fi

    # Eliminar entrada previa del mismo libro (si se re-procesa)
    sed -i "/^${book_name}|/d" "$REGISTRO_CSV" 2>/dev/null || true

    printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
        "$book_name" "$n_pages" "$peso_carpeta_gb" "$peso_pdf_mb" \
        "$status" "$proc_timestamp" "$sha256" "$error_detail" \
        >> "$REGISTRO_CSV"

    log_ok "[${book_name}] Registrado → ${status} | ${n_pages} pág | PDF: ${peso_pdf_mb} MB"
}

# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE POR LIBRO
# ─────────────────────────────────────────────────────────────────────────────

export_png_from_tiff() {
    # Exporta cada TIFF del libro a PNG (a PNG_DPI) en PNG/<libro>/.
    # Conversión sin re-muestreo: el TIFF ya está a 300 DPI, solo se re-codifica
    # a PNG conservando los píxeles originales (sin marca de agua ni OCR).
    local book_name="$1"
    local src_dir="${INPUT_DIR}/${book_name}"
    local dst_dir="${PNG_DIR}/${book_name}"

    local n_tiff
    n_tiff=$(find "$src_dir" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) -not -name '_*' 2>/dev/null | wc -l)
    if [[ "$n_tiff" -eq 0 ]]; then
        log_warn "[${book_name}] Export PNG: no hay TIFF en ${src_dir} (¿ya procesado/borrado?)."
        return 1
    fi

    mkdir -p "$dst_dir"
    log_info "[${book_name}] Export PNG: convirtiendo ${n_tiff} páginas a ${dst_dir} (${PNG_DPI} DPI)..."

    SRC_DIR="$src_dir" DST_DIR="$dst_dir" PNG_DPI="$PNG_DPI" BOOK="$book_name" python3 - <<'PYEOF'
import os, sys, re
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # sin límite para imágenes patrimoniales enormes

src = os.environ["SRC_DIR"]
dst = os.environ["DST_DIR"]
dpi = int(os.environ["PNG_DPI"])

def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

tiffs = [f for f in os.listdir(src)
         if f.lower().endswith((".tif", ".tiff")) and not f.startswith("_")]
tiffs.sort(key=natkey)

ok = 0
fail = 0
def convert(name):
    inp = os.path.join(src, name)
    out = os.path.join(dst, os.path.splitext(name)[0] + ".png")
    if os.path.exists(out):
        return ("skip", name)
    try:
        img = Image.open(inp)
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Sin re-muestreo: se conservan los píxeles nativos (300 DPI del escaneo)
        img.save(out, format="PNG", dpi=(dpi, dpi))
        img.close()
        return ("ok", name)
    except Exception as e:
        sys.stderr.write(f"[PNG] ERROR {name}: {e}\n")
        return ("fail", name)

workers = min(8, (os.cpu_count() or 4))
with ThreadPoolExecutor(max_workers=workers) as ex:
    for i, (status, name) in enumerate(ex.map(convert, tiffs), 1):
        if status in ("ok", "skip"):
            ok += 1
        else:
            fail += 1
        if i % 25 == 0 or i == len(tiffs):
            sys.stderr.write(f"[PNG] {i}/{len(tiffs)} páginas ({fail} con error)\n")

sys.stderr.write(f"[PNG] Convertidas {ok}/{len(tiffs)} (errores: {fail})\n")
sys.exit(1 if fail > 0 and ok == 0 else 0)
PYEOF

    local rc=$?
    local n_png; n_png=$(find "$dst_dir" -maxdepth 1 -iname "*.png" 2>/dev/null | wc -l)
    local peso; peso=$(du -sh "$dst_dir" 2>/dev/null | awk '{print $1}')
    if [[ "$n_png" -gt 0 ]]; then
        log_ok "[${book_name}] Export PNG: ${n_png} PNG en $(basename "$dst_dir")/ (${peso})."
        return 0
    fi
    log_error "[${book_name}] Export PNG: no se generó ningún PNG (rc=$rc)."
    return 1
}

process_book() {
    local book_name="$1"
    local start; start=$(date +%s)

    echo ""
    log_step "╔══════════════════════════════════════════════════════╗"
    log_step "║  LIBRO: ${book_name}"
    log_step "╚══════════════════════════════════════════════════════╝"

    # ── PASO 3: Marca de agua ──
    log_step "  [3/7] Marca de agua"
    local marked_pdf
    marked_pdf=$(step3_watermark "$book_name") || {
        log_error "[${book_name}] Fallo en marca de agua. Enviando a REVIEW."
        quarantine_book "$book_name" "Fallo en generación de marca de agua (paso 3)"
        return 1
    }

    # ── PASO 4: PDF/A-3b ──
    log_step "  [4/7] PDF/A-3b v1.7"
    local final_pdf
    final_pdf=$(step4_pdfa "$book_name" "$marked_pdf") || {
        log_error "[${book_name}] Fallo en conversión PDF/A. Enviando a REVIEW."
        quarantine_book "$book_name" "Fallo en conversión PDF/A-3b (paso 4)"
        return 1
    }

    # ── PASO 5: Metadatos ──
    log_step "  [5/7] Insertar metadatos"
    step5_metadata "$book_name" "$final_pdf"

    # ── EXTRA (--png): exportar PNG desde los TIFF antes de que se borren al final ──
    if [[ "$EXPORT_PNG" == "true" ]]; then
        log_step "  [PNG] Exportando páginas a PNG (${PNG_DPI} DPI) desde TIFF"
        export_png_from_tiff "$book_name" || log_warn "[${book_name}] Export PNG falló (el pipeline continúa)."
    fi

    # ── PASO 6: Verificar conteo TIFF == páginas PDF (regla ESTRICTA) ──
    log_step "  [6/7] Verificar TIFF vs PDF → limpiar"
    local peso_tiff
    peso_tiff=$(step6_verify_and_cleanup "$book_name" "$final_pdf")

    # Si step6 devuelve "REVIEW", el libro ya fue enviado a cuarentena → salir
    if [[ "$peso_tiff" == "REVIEW" ]]; then
        return 1
    fi

    # ── Mover PDF final de TEMPORALES a OUTPUT ──
    # FIX RACE CONDITION: primero se coloca el PDF final con su nombre definitivo
    # y SOLO DESPUÉS se libera el lock (carpeta TEMPORALES/<libro>). Invariante: en
    # todo instante existe el lock O el PDF final — nunca un hueco por el que otra
    # terminal pueda re-procesar un libro ya terminado.
    local out_pdf="${OUTPUT_DIR}/${book_name}.pdf"
    if ! mv "$final_pdf" "$out_pdf"; then
        log_error "[${book_name}] No se pudo mover el PDF final a OUTPUT. Queda en TEMPORALES/${book_name}/; los TIFF siguen en INPUT."
        record_result ERROR "$book_name" "PDF final no se pudo mover a OUTPUT; quedó en TEMPORALES/${book_name}/"
        return 1
    fi
    log_ok "[${book_name}] PDF final disponible en OUTPUT/${book_name}.pdf"

    # ── PASO 7: Registro CSV (solo libros exitosos) ──
    log_step "  [7/7] Registro CSV"
    local book_status="OK"
    for nm in "${BOOKS_NO_META[@]}"; do
        if [[ "$nm" == "$book_name" ]]; then
            book_status="SIN_META"
            break
        fi
    done
    # Páginas del PDF entregado (el paso 6 ya verificó que coinciden con los TIFF)
    local n_pages
    n_pages=$(pdfinfo "$out_pdf" 2>/dev/null | awk '/^Pages:/{print $2}')
    [[ -n "$n_pages" ]] || n_pages="?"
    step7_register "$book_name" "$out_pdf" "$n_pages" "$peso_tiff" "$book_status" ""

    # ── Recién ahora se borran los TIFF: el PDF ya está en OUTPUT y registrado ──
    log_info "[${book_name}] Eliminando TIFFs originales (${peso_tiff} GB)..."
    rm -rf "${INPUT_DIR}/${book_name}"

    # ── Liberar el lock: eliminar TEMPORALES/<libro> ──
    rm -rf "${TEMP_DIR}/${book_name}" 2>/dev/null || true

    # ── Libro completado exitosamente ──
    record_result "$book_status" "$book_name"
    CURRENT_BOOK=""   # Limpiar tracker para que el trap no lo libere

    local end; end=$(date +%s)
    log_ok "[${book_name}] Completado en $(( (end-start)/60 )) min $(( (end-start)%60 )) seg. ✓"
}

# ─────────────────────────────────────────────────────────────────────────────
# AYUDA
# ─────────────────────────────────────────────────────────────────────────────

usage() {
    cat << EOF

  ORQUESTADOR: main.sh — Pipeline Post-OCR
  =========================================

  USO:
    $0 [opciones]

  OPCIONES:
    --skip-ocr          Omite el paso OCR (run_ocr.sh). Útil si los PDFs
                        ya están generados y solo quieres postprocesar.
    --only-book <id>    Procesa únicamente el libro con ese identificador
                        (debe coincidir con el nombre de la subcarpeta).
    --jobs <N>          Ejecuta el post-proceso (pasos 3-7) de N libros en
                        paralelo. Default: 1 (secuencial).
                        El OCR siempre es secuencial (usa todos los cores).
                        Recomendado: --jobs 2 o --jobs 3 según RAM disponible.
    --png               Además del PDF, exporta cada página a PNG (300 DPI) desde
                        los TIFF originales (limpios, sin marca de agua) en
                        /mnt/datos/PNG/<libro>/. Solo para libros puntuales; no
                        interfiere con el procesamiento normal. Requiere los TIFF
                        en INPUT. Úsalo junto con --only-book.
    --book <id>         Procesa ese libro; se puede repetir (lo usa la interfaz).
    --csv <ruta>        CSV de metadatos (default: junto a main.sh).
    --logo <ruta>       Logo de la marca de agua (default: logo.png junto a main.sh).
    --ocr-jobs <N>      Procesos de Tesseract en paralelo (default: el de OCR.sh).
    --texto-marca <txt> Texto de la marca de agua al pie de página ("" = solo logo).
                        Default: CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES
    --marca-diagonal <txt>  Marca diagonal opcional (de abajo-izquierda a arriba-derecha),
                        p. ej. "CÁMARA DE REPRESENTANTES". Default: sin marca diagonal.
    --marca-config <json>  Posición, transparencia y tamaño de logo, texto y diagonal
                        (formato en la cabecera de marca_agua.py). Default: los de siempre.
    --input-dir <ruta>     Carpeta INPUT       (default: /mnt/datos/INPUT)
    --output-dir <ruta>    Carpeta OUTPUT      (default: /mnt/datos/OUTPUT)
    --temp-dir <ruta>      Carpeta TEMPORALES  (default: /mnt/datos/TEMPORALES)
    --review-dir <ruta>    Carpeta REVIEW      (default: /mnt/datos/REVIEW)
    --registro-dir <ruta>  Carpeta REGISTRO    (default: /mnt/datos/REGISTRO)
    --png-dir <ruta>       Carpeta de PNG      (default: /mnt/datos/PNG)
                        Conviene que TEMPORALES y OUTPUT (e INPUT y REVIEW) estén
                        en el mismo disco: así mover el PDF final es instantáneo.
    -h, --help          Muestra esta ayuda.

  FLUJO COMPLETO:
    1 · Renombrar carpetas input ("NNN OK" → "NNN")
    2 · OCR          run_ocr.sh → PDF por libro
    3 · Marca agua   logo.png + texto en cada página
    4 · PDF/A-3b     ocrmypdf --skip-text --pdf-version 1.7
    5 · Metadatos    exiftool desde TechnoMart Funza - TEMP.csv
    6 · Verificar    TIFF == páginas PDF → eliminar TIFFs
    7 · Registro     REGISTRO/registro_libros_procesados.csv (con SHA-256)

  SALIDA:
    OUTPUT/<libro>.pdf                          Solo PDFs finalizados y verificados
    TEMPORALES/<libro>/                         Intermedios (se borran al terminar)
    REGISTRO/registro_libros_procesados.csv     Registro con SHA-256
    REVIEW/<libro>/                             Libros con error + review_log.csv
    PNG/<libro>/                                PNGs (solo con --png)
    logs/sin_metadatos_<ts>.txt                 Libros sin entrada en CSV

  EJEMPLOS:
    $0
    $0 --skip-ocr
    $0 --skip-ocr --only-book 003106
    $0 --only-book AR10650 --png     # PDF normal + PNGs 300 DPI de ese libro

EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# PARSEO DE ARGUMENTOS
# ─────────────────────────────────────────────────────────────────────────────

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --skip-ocr)         SKIP_OCR=true; shift ;;
            --only-book)        ONLY_BOOK="$2"; shift 2 ;;
            --jobs)             PARALLEL_JOBS="$2"; shift 2 ;;
            --png)              EXPORT_PNG=true; shift ;;
            --book)             BOOKS+=("$2"); shift 2 ;;
            --csv)              CSV_META="$2"; shift 2 ;;
            --logo)             LOGO_PNG="$2"; shift 2 ;;
            --ocr-jobs)         OCR_JOBS="$2"; shift 2 ;;
            --texto-marca)      WATERMARK_TEXT="$2"; shift 2 ;;
            --marca-diagonal)   DIAGONAL_TEXT="$2"; shift 2 ;;
            --marca-config)     MARCA_CONFIG="$2"; shift 2 ;;
            --input-dir)        INPUT_DIR="$2"; shift 2 ;;
            --output-dir)       OUTPUT_DIR="$2"; shift 2 ;;
            --temp-dir)         TEMP_DIR="$2"; shift 2 ;;
            --review-dir)       REVIEW_DIR="$2"; shift 2 ;;
            --registro-dir)     REGISTRO_DIR="$2"; shift 2 ;;
            --png-dir)          PNG_DIR="$2"; shift 2 ;;
            -h|--help)          usage; exit 0 ;;
            *) log_error "Argumento desconocido: $1"; usage; exit 1 ;;
        esac
    done
    # Validar --jobs y --ocr-jobs
    if ! [[ "$PARALLEL_JOBS" =~ ^[1-9][0-9]*$ ]]; then
        log_error "--jobs requiere un número entero positivo (recibido: '$PARALLEL_JOBS')"
        exit 1
    fi
    if [[ -n "$OCR_JOBS" ]] && ! [[ "$OCR_JOBS" =~ ^[1-9][0-9]*$ ]]; then
        log_error "--ocr-jobs requiere un número entero positivo (recibido: '$OCR_JOBS')"
        exit 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# ENTRADA PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

main() {
    parse_args "$@"

    # Rutas que dependen de las carpetas (pueden venir de --review-dir, --registro-dir…)
    REGISTRO_CSV="${REGISTRO_DIR}/registro_libros_procesados.csv"
    REVIEW_LOG="${REVIEW_DIR}/review_log.csv"
    if [[ ! -d "$INPUT_DIR" ]]; then
        log_error "No existe la carpeta INPUT: $INPUT_DIR"
        exit 1
    fi
    mkdir -p "$OUTPUT_DIR" "$TEMP_DIR" "$REVIEW_DIR" "$REGISTRO_DIR"
    log_info "Carpetas: INPUT=$INPUT_DIR | OUTPUT=$OUTPUT_DIR | TEMPORALES=$TEMP_DIR | REVIEW=$REVIEW_DIR | REGISTRO=$REGISTRO_DIR"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║   ORQUESTADOR OCR · Libros Patrimoniales Colombia · v1.0.0  ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    # Activar entorno virtual para todo el proceso
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    check_deps
    load_csv_index

    # ── PASO 1: Renombrar ──
    step1_rename_input

    log_info "Resultados de esta ejecución: ${RESULTS_FILE}"

    # ── Descubrir libros a procesar (desde INPUT_DIR) ──
    local book_list=()
    if [[ -n "$ONLY_BOOK" ]]; then
        book_list=("$ONLY_BOOK")
    elif [[ ${#BOOKS[@]} -gt 0 ]]; then
        # --book (interfaz): mismo recorte de " OK" que hizo el paso 1
        local b
        for b in "${BOOKS[@]}"; do
            b="${b%[[:space:]][Oo][Kk]}"
            book_list+=("${b%"${b##*[![:space:]]}"}")
        done
    else
        while IFS= read -r d; do
            if [[ -d "$d" ]]; then
                book_list+=("$(basename "$d")")
            fi
        done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type d | sort)
    fi

    if [[ ${#book_list[@]} -eq 0 ]]; then
        # Si INPUT_DIR directamente tiene los .tif, añadimos la raíz misma como "el libro"
        if find "$INPUT_DIR" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) | read -r line; then
            book_list+=("$(basename "$INPUT_DIR")")
        else
            log_warn "No se encontraron libros en $INPUT_DIR."
            exit 0
        fi
    fi

    log_info "Libros a procesar: ${#book_list[@]} | Post-proceso paralelo: ${PARALLEL_JOBS} job(s)"

    local total_start; total_start=$(date +%s)

    # ── Semáforo de jobs paralelos (post-proceso pasos 3-7) ──
    # El OCR (paso 2) siempre es secuencial (ya usa todos los cores internamente).
    # El post-proceso (marca de agua, PDF/A, metadatos, verificación) puede
    # ejecutarse en paralelo para N libros cuando se usa --jobs N.
    local running_jobs=0
    local -a job_pids=()

    _wait_for_slot() {
        while [[ ${#job_pids[@]} -ge $PARALLEL_JOBS ]]; do
            local new_pids=()
            for pid in "${job_pids[@]}"; do
                if kill -0 "$pid" 2>/dev/null; then
                    new_pids+=("$pid")
                fi
            done
            job_pids=("${new_pids[@]+"${new_pids[@]}"}")
            if [[ ${#job_pids[@]} -ge $PARALLEL_JOBS ]]; then sleep 0.5; fi
        done
        # Sin esto la función devolvía 1 tras esperar, y con set -e main.sh se cerraba
        return 0
    }

    _wait_all_jobs() {
        for pid in "${job_pids[@]}"; do
            wait "$pid" 2>/dev/null || true
        done
        job_pids=()
    }

    for book in "${book_list[@]}"; do
        # Si el PDF final ya existe en OUTPUT, el libro ya fue procesado completamente
        local final_pdf_check="${OUTPUT_DIR}/${book}.pdf"
        if [[ -f "$final_pdf_check" ]]; then
            log_info "[${book}] PDF final ya existe en OUTPUT. Omitiendo (ya completado)."
            record_result OMITIDO "$book"
            continue
        fi

        # ─── MECANISMO DE LOCK (Multi-terminal - Basado en Carpeta TEMPORALES) ───
        if ! mkdir "${TEMP_DIR}/${book}" 2>/dev/null; then
            log_info "[${book}] Carpeta en TEMPORALES detectada. Saltando (ya en proceso por otra terminal)."
            continue
        fi

        # ─── OCR SIEMPRE SECUENCIAL (usa todos los cores internamente) ───
        CURRENT_BOOK="$book"
        log_step "=========================================================="
        log_step " PROCESANDO LIBRO: $book"
        log_step "=========================================================="

        local book_pdf="${TEMP_DIR}/${book}/${book}_ocr.pdf"
        local -a ocr_args=(-i "${INPUT_DIR}/${book}" -o "$TEMP_DIR")
        if [[ -n "$OCR_JOBS" ]]; then ocr_args+=(-j "$OCR_JOBS"); fi
        if [[ "$SKIP_OCR" != "true" ]]; then
            log_step "  -> [1/2] Ejecutando Fase OCR..."
            bash "$RUN_OCR" "${ocr_args[@]}" 2>&1 | tee -a "$MAIN_LOG" || true

            # Un fallo del OCR suele ser pasajero (memoria, un proceso caído):
            # se reintenta una vez. Las páginas ya hechas no se repiten.
            if [[ ! -f "$book_pdf" && ! -f "${INPUT_DIR}/${book}/_book_rejected.json" ]]; then
                log_warn "[${book}] El OCR no generó el PDF. Reintentando una vez..."
                bash "$RUN_OCR" "${ocr_args[@]}" 2>&1 | tee -a "$MAIN_LOG" || true
            fi
        else
            log_info "  -> [1/2] Fase OCR omitida (--skip-ocr activo)."
        fi

        # ── Detectar si el libro fue RECHAZADO por preprocess_validate.py ──
        local _rejected_file="${INPUT_DIR}/${book}/_book_rejected.json"
        if [[ -f "$_rejected_file" ]]; then
            local _reject_summary
            _reject_summary=$(REJ_FILE="$_rejected_file" python3 -c "
import json, os, sys
with open(os.environ['REJ_FILE']) as f:
    r = json.load(f)
probs = r.get('problems', [])
dpi_count = sum(1 for p in probs if p['type'] == 'DPI_INCORRECTO')
corrupt_count = sum(1 for p in probs if p['type'] == 'CORRUPTA')
files = ', '.join(p['file'] for p in probs[:5])
extra = f' (+{len(probs)-5} más)' if len(probs) > 5 else ''
print(f'{dpi_count} DPI≠300, {corrupt_count} corruptas → {files}{extra}')
" 2>/dev/null || echo "ver _book_rejected.json")
            log_error "[${book}] 🚫 LIBRO RECHAZADO (DPI/Corrupta). Enviando a REVIEW."
            quarantine_book "$book" "Imágenes inválidas: ${_reject_summary}"
            CURRENT_BOOK=""
            continue
        fi

        # ── Sin PDF del OCR (tras el reintento): el libro se queda en INPUT ──
        # Las imágenes ya se validaron (DPI, corruptas), así que es un fallo de
        # ejecución, no del libro: se libera el candado y se reprocesa en la
        # próxima ejecución. Nunca va a REVIEW por esto.
        if [[ ! -f "$book_pdf" ]]; then
            local ocr_motivo="OCR no generó el PDF (tras 1 reintento)"
            local tess_err_log="${LOG_DIR}/tesseract_errors.log"
            local last_err=""
            if [[ -f "$tess_err_log" ]]; then
                last_err=$(grep -i "error\|failed" "$tess_err_log" 2>/dev/null | grep -F "$book" | tail -1 || true)
            fi
            [[ -n "$last_err" ]] && ocr_motivo="${ocr_motivo}: ${last_err}"
            log_error "[${book}] ${ocr_motivo}. Queda en INPUT para reintentar en la próxima ejecución."
            rm -rf "${TEMP_DIR}/${book}" 2>/dev/null || true
            record_result REINTENTAR "$book" "$ocr_motivo"
            CURRENT_BOOK=""
            continue
        fi

        # ── POST-PROCESO: ejecutar en paralelo si --jobs > 1 ──
        log_step "  -> [2/2] Post-Procesamiento (PDF/A, Metadatos, Limpieza)..."
        if [[ "$PARALLEL_JOBS" -gt 1 ]]; then
            _wait_for_slot
            (
                # Las subshells no heredan los traps: cada una libera su libro
                trap _on_book_exit EXIT
                trap 'exit 130' INT
                trap 'exit 143' TERM
                if ! process_book "$book"; then
                    true
                fi
                CURRENT_BOOK=""
            ) &
            job_pids+=("$!")
        else
            if ! process_book "$book"; then
                true
            fi
        fi
        CURRENT_BOOK=""
    done

    # Esperar todos los jobs paralelos antes del resumen
    _wait_all_jobs


    local total_end; total_end=$(date +%s)
    local elapsed=$(( total_end - total_start ))

    # ═══════════════════════════════════════════════════════════════════════════
    # RESUMEN FINAL EN TERMINAL
    # ═══════════════════════════════════════════════════════════════════════════

    load_results
    local n_ok=${#BOOKS_OK[@]}
    local n_no_meta=${#BOOKS_NO_META[@]}
    local n_review=${#BOOKS_REVIEW[@]}
    local n_skip=${#BOOKS_SKIPPED[@]}
    local n_retry=${#BOOKS_RETRY[@]}
    local n_total=$(( n_ok + n_review + n_retry ))

    echo ""
    echo ""
    printf "\033[1;36m%s\033[0m\n" "╔════════════════════════════════════════════════════════════════════╗"
    printf "\033[1;36m%s\033[0m\n" "║                    RESUMEN FINAL DE PROCESAMIENTO                 ║"
    printf "\033[1;36m%s\033[0m\n" "╠════════════════════════════════════════════════════════════════════╣"
    printf "\033[1;36m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "📚 Total gestionados  : ${n_total} libro(s)"
    printf "\033[1;32m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "✅ Exitosos            : ${n_ok}  (PDF en OUTPUT verificado)"
    printf "\033[1;33m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "⚠️  Sin metadatos       : ${n_no_meta}  (PDF OK, sin entrada en CSV)"
    printf "\033[1;31m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "🔍 En REVIEW           : ${n_review}  (movidos a REVIEW/)"
    printf "\033[1;33m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "🔁 Por reintentar      : ${n_retry}  (siguen en INPUT)"
    printf "\033[1;36m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "⏭️  Omitidos (previos)  : ${n_skip}"
    printf "\033[1;36m║\033[0m  %-64s  \033[1;36m║\033[0m\n" "⏱️  Tiempo total        : $(( elapsed/60 )) min $(( elapsed%60 )) seg"
    printf "\033[1;36m%s\033[0m\n" "╚════════════════════════════════════════════════════════════════════╝"

    # ── Libros exitosos ──
    if [[ $n_ok -gt 0 ]]; then
        echo ""
        printf "\033[1;32m  ✅ LIBROS PROCESADOS EXITOSAMENTE (%d):\033[0m\n" "$n_ok"
        for b in "${BOOKS_OK[@]}"; do
            printf "\033[0;32m     ✓ %s\033[0m\n" "$b"
        done
    fi

    # ── Libros sin metadatos (PDF sí generado) ──
    if [[ $n_no_meta -gt 0 ]]; then
        echo ""
        printf "\033[1;33m  ⚠️  LIBROS SIN METADATOS EN CSV (%d):\033[0m\n" "$n_no_meta"
        for b in "${BOOKS_NO_META[@]}"; do
            printf "\033[0;33m     ⚠ %s  (PDF generado, sin metadata)\033[0m\n" "$b"
        done
    fi

    # ── Libros en REVIEW (cualquier fallo — con motivo) ──
    if [[ $n_review -gt 0 ]]; then
        echo ""
        printf "\033[1;31m  ╔══════════════════════════════════════════════════════════════╗\033[0m\n"
        printf "\033[1;31m  ║  🔍 LIBROS ENVIADOS A REVIEW — REQUIEREN CORRECCIÓN (%d)      ║\033[0m\n" "$n_review"
        printf "\033[1;31m  ╚══════════════════════════════════════════════════════════════╝\033[0m\n"
        printf "\033[0;31m  Carpetas movidas desde INPUT → REVIEW/\033[0m\n"
        printf "\033[0;31m  OUTPUT queda 100%% limpio (sin PDFs inválidos ni archivos parciales)\033[0m\n"
        printf "\033[0;31m  Corrígelos y vuelve a colocarlos en INPUT para reprocesar.\033[0m\n"
        echo ""
        for b in "${BOOKS_REVIEW[@]}"; do
            printf "\033[0;31m     🔍 %s\033[0m\n" "$b"
        done
        echo ""
        printf "\033[1;31m  📋 Registro REVIEW : %s\033[0m\n" "$REVIEW_LOG"
    fi

    # ── Libros por reintentar (fallo del OCR, siguen en INPUT) ──
    if [[ $n_retry -gt 0 ]]; then
        echo ""
        printf "\033[1;33m  🔁 POR REINTENTAR — siguen en INPUT, se procesan en la próxima ejecución (%d):\033[0m\n" "$n_retry"
        for b in "${BOOKS_RETRY[@]}"; do
            printf "\033[0;33m     🔁 %s\033[0m\n" "$b"
        done
    fi

    # ── Libros omitidos ──
    if [[ $n_skip -gt 0 ]]; then
        echo ""
        printf "\033[0;90m  ⏭️  Omitidos (ya procesados): %s\033[0m\n" "$(IFS=', '; echo "${BOOKS_SKIPPED[*]}")"
    fi

    # ── Archivos de salida ──
    echo ""
    printf "\033[1;36m  📁 PDFs finales (verificados) : %s\033[0m\n" "${OUTPUT_DIR}"
    printf "\033[1;36m  📄 Registro exitosos CSV      : %s\033[0m\n" "${REGISTRO_CSV}"
    printf "\033[1;31m  🔍 Registro REVIEW CSV        : %s\033[0m\n" "${REVIEW_LOG}"
    if [[ -f "$SIN_META_TXT" && -s "$SIN_META_TXT" ]]; then
        printf "\033[1;33m  📋 Sin metadata               : %s\033[0m\n" "$(basename "$SIN_META_TXT")"
    fi
    echo ""
}

main "$@"
