#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   main.sh  ─  ORQUESTADOR PRINCIPAL
# VERSION:  1.0.0
# FECHA:    2026-02-23
# DESC:     Coordina el pipeline completo por libro:
#
#   PASO 1 · Quitar " OK" a la carpeta del libro (solo al empezar ese libro)
#   PASO 2 · OCR (run_ocr.sh) → PDF por libro
#   PASO 3 · Marca de agua (pypdf + reportlab + logo.png)
#   PASO 4 · Conversión PDF/A-3b v1.7 (ocrmypdf --skip-text)
#   PASO 5 · Metadatos (pikepdf desde CSV, PDF/A válido)
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

CSV_META="${SCRIPT_DIR}/recursos/TechnoMart Funza - TEMP.csv"
LOGO_PNG="${SCRIPT_DIR}/recursos/logo.png"
VENV_DIR="${SCRIPT_DIR}/ocr_env"
RUN_OCR="${SCRIPT_DIR}/run_ocr.sh"
MARCA_AGUA="${SCRIPT_DIR}/marca_agua.py"

# Lectura del CSV y escritura de metadatos, compartidas con inyectar_metadatos.sh.
# META_CONFIG (--meta-config): qué columnas van en cada campo; vacío = lo de siempre
# shellcheck source=metadatos.sh
source "${SCRIPT_DIR}/metadatos.sh" || { echo "Falta ${SCRIPT_DIR}/metadatos.sh" >&2; exit 1; }

# Registro CSV principal (solo libros exitosos)
REGISTRO_CSV="${REGISTRO_DIR}/registro_libros_procesados.csv"
REGISTRO_OCR_ICR="${SCRIPT_DIR}/registro_ocr_icr.py"
# Libros fragmentados en subcarpetas (--fragmentados): vacío = no se aceptan (van
# a REVIEW) · unico = un PDF con todo el libro · fragmentos = un PDF por subcarpeta
LIBROS_PY="${SCRIPT_DIR}/libros.py"
FRAGMENTADOS=""
# Entrega a la que pertenecen los libros de esta ejecución (--entrega 1-30); va en
# el registro OCR-ICR del día. Vacío = la columna ENTREGA queda en blanco.
ENTREGA=""
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

# Texto de la marca de agua, al pie de cada página (--texto-marca; vacío = sin texto)
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
    if find "${INPUT_DIR}/${book_name}" -maxdepth 2 \( -iname "*.tif" -o -iname "*.tiff" \) -print -quit 2>/dev/null | grep -q .; then
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
    for cmd in ocrmypdf pdfunite pdfinfo; do
        command -v "$cmd" &>/dev/null || { log_error "Falta: $cmd"; missing=$((missing+1)); }
    done
    [[ -f "$VENV_DIR/bin/activate" ]] || { log_error "Falta entorno virtual: $VENV_DIR"; missing=$((missing+1)); }
    # --logo "" = sin logo (la marca de agua es opcional)
    [[ -z "$LOGO_PNG" || -f "$LOGO_PNG" ]] || { log_error "Falta logo: $LOGO_PNG";  missing=$((missing+1)); }
    [[ -f "$CSV_META"              ]] || { log_error "Falta CSV: $CSV_META";   missing=$((missing+1)); }
    [[ -f "$RUN_OCR"               ]] || { log_error "Falta: $RUN_OCR";        missing=$((missing+1)); }
    [[ -f "$MARCA_AGUA"            ]] || { log_error "Falta: $MARCA_AGUA";     missing=$((missing+1)); }
    [[ -f "$META_PY"               ]] || { log_error "Falta: $META_PY";        missing=$((missing+1)); }
    [[ -f "$LIBROS_PY"             ]] || { log_error "Falta: $LIBROS_PY";      missing=$((missing+1)); }
    if [[ -n "$META_CONFIG" && ! -f "$META_CONFIG" ]]; then
        log_error "Falta configuración de metadatos: $META_CONFIG"; missing=$((missing+1))
    fi
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
# (columnas y campos según --meta-config; sin él, código, fondo, subfondo,
#  equipo, resolución)
# ─────────────────────────────────────────────────────────────────────────────

TEMP_META=""
load_csv_index() {
    TEMP_META="$(mktemp)"
    meta_indice "$CSV_META" > "$TEMP_META"

    [[ -s "$TEMP_META" ]] || { log_error "No se pudo leer el CSV de metadatos."; exit 1; }
    local count; count=$(cut -d"$META_SEP" -f1 "$TEMP_META" | sort -u | wc -l)
    log_info "CSV cargado: ${count} códigos únicos."
}
cleanup_temp() { if [[ -n "$TEMP_META" && -f "$TEMP_META" ]]; then rm -f "$TEMP_META"; fi; }

# ─────────────────────────────────────────────────────────────────────────────
# PASO 1 · QUITAR " OK" A LA CARPETA DEL LIBRO, JUSTO ANTES DE PROCESARLO
# ─────────────────────────────────────────────────────────────────────────────

# "AR10582 OK" → "AR10582" (sin distinguir mayúsculas, espacios finales fuera)
sin_ok() {
    local n="$1"
    if [[ "$n" =~ [[:space:]][Oo][Kk]$ ]]; then
        n="${n%[[:space:]]*[Oo][Kk]}"
    fi
    printf '%s' "${n%"${n##*[![:space:]]}"}"
}

# Solo se renombra el libro que se va a procesar, con su candado ya tomado:
# si la ejecución se cancela, los libros que no se alcanzaron conservan " OK".
step1_rename_book() {
    local book_name="$1"
    [[ -d "${INPUT_DIR}/${book_name}" ]] && return 0
    local dir name
    while IFS= read -r dir; do
        name="$(basename "$dir")"
        [[ "$name" != "$book_name" && "$(sin_ok "$name")" == "$book_name" ]] || continue
        if mv "$dir" "${INPUT_DIR}/${book_name}" 2>/dev/null; then
            log_ok "[${book_name}] Carpeta renombrada: '$name' → '$book_name'"
            return 0
        fi
    done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type d | sort)
    log_error "[${book_name}] No se encontró su carpeta en INPUT."
    return 1
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

# ─────────────────────────────────────────────────────────────────────────────
# PLAN DEL LIBRO (libros.py): qué PDF salen y con qué carpetas de TIFF
# TEMPORALES/<libro>/_plan.tsv, una línea por carpeta de TIFF, en orden:
#   nombre_pdf <TAB> carpeta <TAB> archivo_de_orden <TAB> n_tiff
# Libro normal: una línea (el PDF se llama como el libro, carpeta = INPUT/<libro>).
# Fragmentado: una línea por subcarpeta; con «un solo PDF» todas comparten el
# nombre del libro, con «un PDF por fragmento» cada una lleva el suyo.
# ─────────────────────────────────────────────────────────────────────────────

plan_file() { printf '%s' "${TEMP_DIR}/$1/_plan.tsv"; }

# PDF del libro, en orden y sin repetir
plan_pdfs() { awk -F'\t' '!visto[$1]++ { print $1 }' "$(plan_file "$1")"; }

# Carpetas de TIFF de un PDF, en orden
plan_carpetas() { awk -F'\t' -v u="$2" '$1 == u { print $2 }' "$(plan_file "$1")"; }

# Carpeta de trabajo de un PDF: TEMPORALES/<libro> si se llama como el libro,
# TEMPORALES/<libro>/pdf/<nombre> si es un fragmento con su propio PDF
unidad_dir() {
    if [[ "$1" == "$2" ]]; then
        printf '%s' "${TEMP_DIR}/$1"
    else
        printf '%s' "${TEMP_DIR}/$1/pdf/$2"
    fi
}

# Etiqueta del log: "[libro]" o "[libro] ‹fragmento›" (la interfaz busca "[libro]")
etiqueta() {
    if [[ "$1" == "$2" ]]; then
        printf '[%s]' "$1"
    else
        printf '[%s] ‹%s›' "$1" "$2"
    fi
}

# TIFF de una carpeta (sin archivos de control _*)
contar_tiff() {
    find "$1" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) -not -name '_*' 2>/dev/null | wc -l
}

step3_watermark() {
    local book_name="$1" unidad="$2"
    local dir t; dir=$(unidad_dir "$book_name" "$unidad"); t=$(etiqueta "$book_name" "$unidad")
    local book_pdf="${dir}/${unidad}_ocr.pdf"
    local out_pdf="${dir}/${unidad}_marca.pdf"
    local tmp_pdf="${out_pdf%.pdf}.tmp.pdf"

    if [[ ! -f "$book_pdf" ]]; then
        log_warn "${t} PDF fuente no encontrado: ${book_pdf} — omitiendo marca de agua."
        return 1
    fi

    if reuse_pdf "$out_pdf"; then
        log_info "${t} Marca de agua ya existe y es válida, omitiendo."
        echo "$out_pdf"
        return 0
    fi

    log_info "${t} Aplicando marca de agua (optimizada)..."

    # Logo, texto al pie y marca diagonal: posición, transparencia y tamaño de cada
    # uno vienen de --marca-config (JSON); sin él, marca_agua.py usa sus valores
    # predeterminados. Las rutas van como argumentos: ninguna se mete en código.
    local -a marca_args=(--entrada "$book_pdf" --salida "$tmp_pdf" --logo "$LOGO_PNG"
                         --texto "$WATERMARK_TEXT" --diagonal "$DIAGONAL_TEXT")
    if [[ -n "$MARCA_CONFIG" ]]; then marca_args+=(--config "$MARCA_CONFIG"); fi
    python3 "$MARCA_AGUA" "${marca_args[@]}" || rm -f "$tmp_pdf"

    if commit_pdf "$tmp_pdf" "$out_pdf"; then
        log_ok "${t} Marca de agua aplicada → $(basename "$out_pdf")"
        echo "$out_pdf"
    else
        log_error "${t} Error al generar PDF con marca de agua."
        return 1
    fi
}


# ─────────────────────────────────────────────────────────────────────────────
# PASO 4 · PDF/A-3b VERSIÓN 1.7 (ocrmypdf --skip-text)
# ─────────────────────────────────────────────────────────────────────────────

step4_pdfa() {
    local book_name="$1" unidad="$2" in_pdf="$3"     # in_pdf: PDF con marca de agua
    local dir t; dir=$(unidad_dir "$book_name" "$unidad"); t=$(etiqueta "$book_name" "$unidad")
    local out_pdf="${dir}/${unidad}_final.pdf"
    local tmp_pdf="${out_pdf%.pdf}.tmp.pdf"

    if reuse_pdf "$out_pdf"; then
        log_info "${t} PDF/A ya existe y es válido, omitiendo conversión."
        echo "$out_pdf"
        return 0
    fi

    log_info "${t} Convirtiendo a PDF/A-3b v1.7..."

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
    && log_ok "${t} PDF/A-3b generado → $(basename "$out_pdf")" \
    || { rm -f "$tmp_pdf"; log_error "${t} Error en ocrmypdf. Ver: ${LOG_DIR}/ocrmypdf_${TIMESTAMP}.log"; return 1; }

    echo "$out_pdf"
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 5 · METADATOS (pikepdf desde CSV: Info y XMP iguales, PDF/A válido)
# ─────────────────────────────────────────────────────────────────────────────

step5_metadata() {
    local book_name="$1" unidad="$2" pdf_path="$3"
    local dir t; dir=$(unidad_dir "$book_name" "$unidad"); t=$(etiqueta "$book_name" "$unidad")

    # Metadatos por el nombre exacto del PDF (p. ej. AHFCP0001). Un fragmento con
    # su propio PDF que no está en el CSV toma la fila del libro.
    local meta
    meta=$(meta_buscar "$unidad" "$TEMP_META")
    if [[ -z "$meta" && "$unidad" != "$book_name" ]]; then
        meta=$(meta_buscar "$book_name" "$TEMP_META")
    fi

    if [[ -z "$meta" ]]; then
        log_warn "${t} ⚠ Sin metadatos en CSV — no se modificará el PDF (irá a OUTPUT/${SIN_META_SUBDIR})."
        echo "$unidad" >> "$SIN_META_TXT"
        : > "${dir}/_sin_metadatos"
        return 0
    fi

    # {nombre} en una plantilla = nombre del PDF (código del libro o del fragmento)
    local titulo; titulo=$(meta_titulo "$meta")
    log_info "${t} Insertando metadatos: Title=${titulo//\{nombre\}/$unidad}"
    meta_escribir "$unidad" "$pdf_path" "$meta" \
        && log_ok "${t} Metadatos insertados." \
        || log_warn "${t} Advertencia: no se pudieron escribir los metadatos (ver log)."
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 6 · VERIFICACIÓN TIFF vs PÁGINAS PDF → limpiar intermedios
# Los TIFF NO se borran aquí: solo al final de process_book, cuando TODOS los
# PDF del libro ya están en OUTPUT y registrados.
# ─────────────────────────────────────────────────────────────────────────────

step6_verify_and_cleanup() {
    local book_name="$1" unidad="$2" final_pdf="$3"
    local dir t; dir=$(unidad_dir "$book_name" "$unidad"); t=$(etiqueta "$book_name" "$unidad")

    # TIFF originales de las carpetas de este PDF (excluyendo archivos de control _*)
    local -a carpetas; mapfile -t carpetas < <(plan_carpetas "$book_name" "$unidad")
    local n_tiff=0 c
    for c in "${carpetas[@]}"; do
        n_tiff=$(( n_tiff + $(contar_tiff "$c") ))
    done

    # Sin TIFF no hay contra qué comparar: el PDF no se puede dar por verificado
    if [[ $n_tiff -eq 0 ]]; then
        log_error "${t} ❌ No hay TIFF en INPUT para verificar el PDF. Enviando libro a REVIEW."
        # El PDF podría ser la única copia: se guarda en REVIEW antes de limpiar TEMPORALES
        local pdf_sin_verificar="${REVIEW_DIR}/${unidad}_${TIMESTAMP}_sin_verificar.pdf"
        mv "$final_pdf" "$pdf_sin_verificar" 2>/dev/null \
            && log_warn "${t} PDF sin verificar guardado en REVIEW/$(basename "$pdf_sin_verificar")"
        quarantine_book "$book_name" "Sin TIFF en INPUT para verificar el PDF ${unidad}"
        echo "REVIEW"
        return 0
    fi

    # Contar páginas del PDF final
    local n_pdf_pages
    n_pdf_pages=$(pdfinfo "$final_pdf" 2>/dev/null | grep -i '^Pages:' | awk '{print $2}')
    [[ -z "$n_pdf_pages" ]] && n_pdf_pages=0

    log_info "${t} TIFFs totales: $n_tiff | Páginas PDF: $n_pdf_pages"

    if [[ "$n_tiff" -eq "$n_pdf_pages" && "$n_tiff" -gt 0 ]]; then
        # ✅ ÉXITO ESTRICTO: conteo exacto TIFFs == páginas PDF
        local peso_tiff
        peso_tiff=$(du -sbc "${carpetas[@]}" 2>/dev/null | awk 'END {printf "%.3f", $1/1073741824}' | tr ',' '.')
        log_ok "${t} ✅ Conteo EXACTO ($n_tiff TIFFs == $n_pdf_pages páginas)."

        # Limpiar intermedios en TEMPORALES (queda solo <nombre>_final.pdf)
        log_info "${t} Limpiando archivos intermedios de OCR en TEMPORALES..."
        local book_tmp_dir="${TEMP_DIR}/${book_name}"
        rm -rf "${book_tmp_dir}/tesseract" "${book_tmp_dir}/paddleocr" "${book_tmp_dir}/kraken" \
               "${book_tmp_dir}/consolidado" "${book_tmp_dir}/ocr"
        rm -f "${dir}/${unidad}_ocr.pdf" "${dir}/${unidad}_marca.pdf" 2>/dev/null || true
        # NO se elimina aquí TEMPORALES/<libro>: es el LOCK del libro.
        # Se conserva hasta que los PDF finales estén en OUTPUT con su nombre
        # definitivo (ver final de process_book). Así se evita la ventana de
        # carrera que borró AR10622 cuando dos terminales procesaban el mismo libro.

        log_ok "${t} Archivos intermedios eliminados."
        echo "${peso_tiff}"
    else
        # ❌ REGLA ESTRICTA: cualquier diferencia = libro inválido → REVIEW
        local n_diff=$(( n_tiff - n_pdf_pages ))
        log_error "${t} ❌ INCONSISTENCIA ESTRICTA: $n_tiff TIFFs ≠ $n_pdf_pages páginas PDF (diferencia: ${n_diff} pág.)."
        log_error "${t} Ninguna excepción permitida. Enviando libro a REVIEW."
        local de=""; [[ "$unidad" != "$book_name" ]] && de=" en ${unidad}"
        quarantine_book "$book_name" \
            "Inconsistencia${de}: ${n_tiff} TIFFs ≠ ${n_pdf_pages} páginas PDF (diferencia: ${n_diff} pág.)"
        echo "REVIEW"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# PASO 7 · REGISTRO CSV DETALLADO (con metadatos, estado, timestamp)
# Una fila por PDF entregado: un fragmento con su propio PDF es una fila más,
# como un libro común.
# ─────────────────────────────────────────────────────────────────────────────

step7_register() {
    local book_name="$1"      # nombre del PDF (libro o fragmento)
    local final_pdf="$2"
    local n_pages="$3"
    local peso_carpeta_gb="$4"
    local status="$5"         # OK | SIN_META | ERROR
    local error_detail="$6"   # Detalle del error (si aplica)
    shift 6
    local -a carpetas_tiff=("$@")   # carpetas de TIFF de este PDF (para el registro OCR-ICR)

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

    # Eliminar entrada previa del mismo PDF (si se re-procesa). Con awk y no sed:
    # un nombre con puntos o paréntesis no se interpreta como expresión regular.
    awk -F'|' -v c="$book_name" '$1 != c' "$REGISTRO_CSV" > "${REGISTRO_CSV}.tmp.$$" \
        && mv "${REGISTRO_CSV}.tmp.$$" "$REGISTRO_CSV"

    printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
        "$book_name" "$n_pages" "$peso_carpeta_gb" "$peso_pdf_mb" \
        "$status" "$proc_timestamp" "$sha256" "$error_detail" \
        >> "$REGISTRO_CSV"

    log_ok "[${book_name}] Registrado → ${status} | ${n_pages} pág | PDF: ${peso_pdf_mb} MB"

    # Registro OCR-ICR del día (REGISTRO/registro OCR-ICR <d> <Mes> <aaaa>.csv): todo
    # PDF entregado, con o sin metadatos. Los TIFF todavía están en INPUT.
    local -a ro_args=(agregar --registro-dir "$REGISTRO_DIR" --pdf "$final_pdf" --hash "$sha256")
    local c
    for c in "${carpetas_tiff[@]}"; do ro_args+=(--tiff-dir "$c"); done
    [[ -n "$ENTREGA" ]] && ro_args+=(--entrega "$ENTREGA")
    local ro_csv
    if ro_csv=$(python3 "$REGISTRO_OCR_ICR" "${ro_args[@]}" 2>>"$MAIN_LOG"); then
        log_ok "[${book_name}] Agregado al registro OCR-ICR → $(basename "$ro_csv")"
    else
        log_warn "[${book_name}] No se pudo agregar al registro OCR-ICR del día (ver log)."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE POR LIBRO
# ─────────────────────────────────────────────────────────────────────────────

# exportar_png <carpeta TIFF> <carpeta PNG> <etiqueta>: cada TIFF a PNG (PNG_DPI)
exportar_png() {
    local src_dir="$1" dst_dir="$2" t="$3"

    local n_tiff; n_tiff=$(contar_tiff "$src_dir")
    if [[ "$n_tiff" -eq 0 ]]; then
        log_warn "${t} Export PNG: no hay TIFF en ${src_dir} (¿ya procesado/borrado?)."
        return 1
    fi

    mkdir -p "$dst_dir"
    log_info "${t} Export PNG: convirtiendo ${n_tiff} páginas a ${dst_dir} (${PNG_DPI} DPI)..."

    SRC_DIR="$src_dir" DST_DIR="$dst_dir" PNG_DPI="$PNG_DPI" python3 - <<'PYEOF'
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
        log_ok "${t} Export PNG: ${n_png} PNG en $(basename "$dst_dir")/ (${peso})."
        return 0
    fi
    log_error "${t} Export PNG: no se generó ningún PNG (rc=$rc)."
    return 1
}

# Exporta los PNG del libro en PNG/<libro>/ (normal) o PNG/<libro>/<fragmento>/
# (fragmentado: cada fragmento aparte, porque su numeración puede repetirse).
# Conversión sin re-muestreo: el TIFF ya está a 300 DPI, solo se re-codifica a
# PNG conservando los píxeles originales (sin marca de agua ni OCR).
export_png_from_tiff() {
    local book_name="$1" c fallos=0
    while IFS=$'\t' read -r _ c _ _; do
        if [[ "$c" == "${INPUT_DIR}/${book_name}" ]]; then
            exportar_png "$c" "${PNG_DIR}/${book_name}" "[${book_name}]" || fallos=$((fallos + 1))
        else
            local frag; frag=$(sin_ok "$(basename "$c")")
            exportar_png "$c" "${PNG_DIR}/${book_name}/${frag}" "$(etiqueta "$book_name" "$frag")" \
                || fallos=$((fallos + 1))
        fi
    done < "$(plan_file "$book_name")"
    [[ $fallos -eq 0 ]]
}

# "3 página(s) sin texto OCR (Tesseract falló): Hoja 2, Hoja 7, Hoja 9"
paginas_sin_ocr() {
    local archivo="$1" n lista
    n=$(sort -u "$archivo" | wc -l)
    lista=$(sort -uV "$archivo" | head -5 | paste -sd ',' - | sed 's/,/, /g')
    (( n > 5 )) && lista+=" (+$((n - 5)) más)"
    printf '%s página(s) sin texto OCR (Tesseract falló): %s' "$n" "$lista"
}

process_book() {
    local book_name="$1"
    local start; start=$(date +%s)

    echo ""
    log_step "╔══════════════════════════════════════════════════════╗"
    log_step "║  LIBRO: ${book_name}"
    log_step "╚══════════════════════════════════════════════════════╝"

    local -a unidades; mapfile -t unidades < <(plan_pdfs "$book_name")
    if [[ ${#unidades[@]} -gt 1 ]]; then
        log_info "[${book_name}] ${#unidades[@]} PDF (uno por fragmento): $(printf '%s, ' "${unidades[@]}" | sed 's/, $//')"
    fi

    # ── Fase A: pasos 3 a 6 de TODOS los PDF del libro. Si uno falla, el libro
    # entero va a REVIEW y ningún PDF suyo llega a OUTPUT (todo o nada).
    local -A finales=() pesos=()
    local u t de
    for u in "${unidades[@]}"; do
        t=$(etiqueta "$book_name" "$u")
        de=""; [[ "$u" != "$book_name" ]] && de=" en ${u}"

        # ── PASO 3: Marca de agua ──
        log_step "  [3/7] Marca de agua ${t}"
        local marked_pdf
        marked_pdf=$(step3_watermark "$book_name" "$u") || {
            log_error "${t} Fallo en marca de agua. Enviando a REVIEW."
            quarantine_book "$book_name" "Fallo en generación de marca de agua (paso 3)${de}"
            return 1
        }

        # ── PASO 4: PDF/A-3b ──
        log_step "  [4/7] PDF/A-3b v1.7 ${t}"
        local final_pdf
        final_pdf=$(step4_pdfa "$book_name" "$u" "$marked_pdf") || {
            log_error "${t} Fallo en conversión PDF/A. Enviando a REVIEW."
            quarantine_book "$book_name" "Fallo en conversión PDF/A-3b (paso 4)${de}"
            return 1
        }

        # ── PASO 5: Metadatos ──
        log_step "  [5/7] Insertar metadatos ${t}"
        step5_metadata "$book_name" "$u" "$final_pdf"

        # ── PASO 6: Verificar conteo TIFF == páginas PDF (regla ESTRICTA) ──
        log_step "  [6/7] Verificar TIFF vs PDF → limpiar ${t}"
        local peso_tiff
        peso_tiff=$(step6_verify_and_cleanup "$book_name" "$u" "$final_pdf")
        # Si step6 devuelve "REVIEW", el libro ya fue enviado a cuarentena → salir
        if [[ "$peso_tiff" == "REVIEW" ]]; then
            return 1
        fi
        finales[$u]="$final_pdf"
        pesos[$u]="$peso_tiff"
    done

    # ── EXTRA (--png): exportar PNG desde los TIFF antes de que se borren al final ──
    if [[ "$EXPORT_PNG" == "true" ]]; then
        log_step "  [PNG] Exportando páginas a PNG (${PNG_DPI} DPI) desde TIFF"
        export_png_from_tiff "$book_name" || log_warn "[${book_name}] Export PNG falló (el pipeline continúa)."
    fi

    # ── Fase B: mover cada PDF final de TEMPORALES a OUTPUT y registrarlo ──
    # FIX RACE CONDITION: primero se colocan los PDF finales con su nombre definitivo
    # y SOLO DESPUÉS se libera el lock (carpeta TEMPORALES/<libro>). Invariante: en
    # todo instante existe el lock O los PDF finales — nunca un hueco por el que otra
    # terminal pueda re-procesar un libro ya terminado.
    # Los PDF sin metadatos no se mezclan con los completos: van a
    # OUTPUT/SIN METADATOS, de donde los toma inyectar_metadatos.sh
    local -a estados=()
    for u in "${unidades[@]}"; do
        local dir; dir=$(unidad_dir "$book_name" "$u")
        local book_status="OK" out_dir="$OUTPUT_DIR"
        if [[ -f "${dir}/_sin_metadatos" ]]; then
            book_status="SIN_META"
            out_dir="${OUTPUT_DIR}/${SIN_META_SUBDIR}"
        fi
        mkdir -p "$out_dir"
        local out_pdf="${out_dir}/${u}.pdf"
        if ! mv "${finales[$u]}" "$out_pdf"; then
            log_error "[${book_name}] No se pudo mover ${u} a OUTPUT. Queda en TEMPORALES/${book_name}/; los TIFF siguen en INPUT."
            record_result ERROR "$u" "PDF final no se pudo mover a OUTPUT; quedó en TEMPORALES/${book_name}/"
            return 1
        fi
        log_ok "$(etiqueta "$book_name" "$u") PDF final disponible en ${out_pdf#"${OUTPUT_DIR}/"}"

        # ── PASO 7: Registro CSV (solo PDF entregados) ──
        log_step "  [7/7] Registro CSV $(etiqueta "$book_name" "$u")"
        # Páginas del PDF entregado (el paso 6 ya verificó que coinciden con los TIFF)
        local n_pages
        n_pages=$(pdfinfo "$out_pdf" 2>/dev/null | awk '/^Pages:/{print $2}')
        [[ -n "$n_pages" ]] || n_pages="?"
        # Solo llega aquí con páginas sin OCR si se forzó con ACEPTAR_PAGINAS_SIN_OCR=1
        local detalle=""
        if [[ -s "${dir}/_paginas_sin_ocr.txt" ]]; then
            detalle=$(paginas_sin_ocr "${dir}/_paginas_sin_ocr.txt")
        fi
        local -a carpetas_u; mapfile -t carpetas_u < <(plan_carpetas "$book_name" "$u")
        step7_register "$u" "$out_pdf" "$n_pages" "${pesos[$u]}" "$book_status" "$detalle" "${carpetas_u[@]}"
        estados+=("${book_status}|${u}")
    done

    # ── Recién ahora se borran los TIFF: todos los PDF ya están en OUTPUT y registrados ──
    log_info "[${book_name}] Eliminando TIFFs originales de INPUT/${book_name}..."
    rm -rf "${INPUT_DIR:?}/${book_name}"

    # ── Liberar el lock: eliminar TEMPORALES/<libro> ──
    rm -rf "${TEMP_DIR:?}/${book_name}" 2>/dev/null || true

    # ── Libro completado exitosamente ──
    local e
    for e in "${estados[@]}"; do
        record_result "${e%%|*}" "${e#*|}"
    done
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
    --logo <ruta>       Logo de la marca de agua (default: recursos/logo.png; "" = sin logo).
    --ocr-jobs <N>      Procesos de Tesseract en paralelo (default: el de OCR.sh).
    --texto-marca <txt> Texto de la marca de agua al pie de página ("" = solo logo).
                        Default: CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES
    --marca-diagonal <txt>  Marca diagonal opcional (de abajo-izquierda a arriba-derecha),
                        p. ej. "CÁMARA DE REPRESENTANTES". Default: sin marca diagonal.
    --fragmentados <unico|fragmentos>
                        Acepta libros que vienen en subcarpetas (LIBRO/<parte>/*.tif).
                        unico = un PDF con todo el libro (nombre del libro) ·
                        fragmentos = un PDF por subcarpeta (nombre de la subcarpeta).
                        Sin esta opción, un libro con subcarpetas va a REVIEW.
                        Orden de las partes: por mes y año, o por número (ver libros.py).
    --entrega <1-30>    Entrega de estos libros; va en el registro OCR-ICR del día
                        (REGISTRO/registro OCR-ICR <d> <Mes> <aaaa>.csv).
    --meta-config <json>   Qué columna del CSV es el código y cómo se arma cada campo
                        del PDF (formato en la cabecera de metadatos.py). Default: los de siempre.
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
    1 · Quitar " OK" a la carpeta (solo del libro que empieza)
    2 · OCR          run_ocr.sh → PDF por libro
    3 · Marca agua   logo.png + texto en cada página
    4 · PDF/A-3b     ocrmypdf --skip-text --pdf-version 1.7
    5 · Metadatos    pikepdf desde el CSV (Info y XMP iguales, PDF/A válido)
    6 · Verificar    TIFF == páginas PDF → eliminar TIFFs
    7 · Registro     REGISTRO/registro_libros_procesados.csv (con SHA-256)

  SALIDA:
    OUTPUT/<libro>.pdf                          Solo PDFs finalizados y verificados
    OUTPUT/SIN METADATOS/<libro>.pdf            Libros sin entrada en el CSV
                                                (se completan con inyectar_metadatos.sh)
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
            --meta-config)      META_CONFIG="$2"; shift 2 ;;
            --entrega)          ENTREGA="$2"; shift 2 ;;
            --fragmentados)     FRAGMENTADOS="$2"; shift 2 ;;
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
    if [[ -n "$FRAGMENTADOS" && "$FRAGMENTADOS" != "unico" && "$FRAGMENTADOS" != "fragmentos" ]]; then
        log_error "--fragmentados acepta: unico | fragmentos (recibido: '$FRAGMENTADOS')"
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

    log_info "Resultados de esta ejecución: ${RESULTS_FILE}"

    # ── Descubrir libros a procesar (desde INPUT_DIR) ──
    local book_list=()
    # Los nombres van sin " OK"; la carpeta se renombra al empezar cada libro
    if [[ -n "$ONLY_BOOK" ]]; then
        book_list=("$(sin_ok "$ONLY_BOOK")")
    elif [[ ${#BOOKS[@]} -gt 0 ]]; then
        local b
        for b in "${BOOKS[@]}"; do
            book_list+=("$(sin_ok "$b")")
        done
    else
        while IFS= read -r d; do
            book_list+=("$d")
        done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
                 | while IFS= read -r n; do sin_ok "$n"; echo; done | sort -u)
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
        if [[ -f "${OUTPUT_DIR}/${book}.pdf" || -f "${OUTPUT_DIR}/${SIN_META_SUBDIR}/${book}.pdf" ]]; then
            log_info "[${book}] PDF final ya existe en OUTPUT. Omitiendo (ya completado)."
            record_result OMITIDO "$book"
            continue
        fi

        # ─── MECANISMO DE LOCK (Multi-terminal - Basado en Carpeta TEMPORALES) ───
        if ! mkdir "${TEMP_DIR}/${book}" 2>/dev/null; then
            log_info "[${book}] Carpeta en TEMPORALES detectada. Saltando (ya en proceso por otra terminal)."
            continue
        fi

        # ── PASO 1: quitar " OK" solo a este libro (con el candado ya tomado) ──
        if ! step1_rename_book "$book"; then
            rm -rf "${TEMP_DIR}/${book}" 2>/dev/null || true
            record_result REINTENTAR "$book" "Carpeta no encontrada en INPUT"
            continue
        fi

        # ─── OCR SIEMPRE SECUENCIAL (usa todos los cores internamente) ───
        CURRENT_BOOK="$book"
        log_step "=========================================================="
        log_step " PROCESANDO LIBRO: $book"
        log_step "=========================================================="

        # ── Plan del libro (libros.py): qué PDF salen, con qué TIFF y en qué orden.
        # El orden se decide aquí UNA sola vez; el OCR y la unión lo siguen.
        local plan_tsv; plan_tsv=$(plan_file "$book")
        local plan_msg plan_rc=0
        plan_msg=$(python3 "$LIBROS_PY" plan "${INPUT_DIR}/${book}" --modo "$FRAGMENTADOS" \
                   --salida "$plan_tsv" 2>&1) || plan_rc=$?
        if [[ $plan_rc -eq 2 ]]; then
            log_error "[${book}] 🚫 ${plan_msg}. Enviando a REVIEW."
            quarantine_book "$book" "$plan_msg"
            CURRENT_BOOK=""
            continue
        elif [[ $plan_rc -ne 0 ]]; then
            log_error "[${book}] No se pudo leer la carpeta del libro: ${plan_msg##*$'\n'}"
            rm -rf "${TEMP_DIR:?}/${book}" 2>/dev/null || true
            record_result REINTENTAR "$book" "No se pudo leer la carpeta del libro"
            CURRENT_BOOK=""
            continue
        fi

        local -a _pdfs=() _carpetas=() _ordenes=()
        mapfile -t _pdfs < <(plan_pdfs "$book")
        local _u _c _o _n
        while IFS=$'\t' read -r _u _c _o _n; do
            _carpetas+=("$_c"); _ordenes+=("$_o")
        done < "$plan_tsv"
        local fragmentado=false
        [[ "${_carpetas[0]}" != "${INPUT_DIR}/${book}" ]] && fragmentado=true
        if [[ "$fragmentado" == true ]]; then
            local _orden_txt=""
            for _c in "${_carpetas[@]}"; do
                _orden_txt+="${_orden_txt:+ → }$(sin_ok "$(basename "$_c")") ($(contar_tiff "$_c"))"
            done
            log_info "[${book}] Libro fragmentado en ${#_carpetas[@]} partes, en este orden: ${_orden_txt}"
            log_info "[${book}] Sale(n) ${#_pdfs[@]} PDF: $(printf '%s, ' "${_pdfs[@]}" | sed 's/, $//')"

            # Un PDF de fragmento con el mismo nombre que uno ya entregado: no se pisa
            local -a _existen=()
            for _u in "${_pdfs[@]}"; do
                [[ -f "${OUTPUT_DIR}/${_u}.pdf" || -f "${OUTPUT_DIR}/${SIN_META_SUBDIR}/${_u}.pdf" ]] && _existen+=("$_u")
            done
            if [[ ${#_existen[@]} -gt 0 && ${#_existen[@]} -eq ${#_pdfs[@]} ]]; then
                log_info "[${book}] Todos sus PDF ya existen en OUTPUT. Omitiendo (ya completado)."
                rm -rf "${TEMP_DIR:?}/${book}" 2>/dev/null || true
                record_result OMITIDO "$book"
                CURRENT_BOOK=""
                continue
            elif [[ ${#_existen[@]} -gt 0 ]]; then
                quarantine_book "$book" "Ya existe(n) en OUTPUT: $(IFS=', '; echo "${_existen[*]}")"
                CURRENT_BOOK=""
                continue
            fi
        fi

        # OCR de cada carpeta de TIFF. Libro normal: como siempre, en
        # TEMPORALES/<libro>/. Fragmentado: TEMPORALES/<libro>/ocr/<subcarpeta>/.
        local ocr_out="$TEMP_DIR"
        [[ "$fragmentado" == true ]] && ocr_out="${TEMP_DIR}/${book}/ocr"
        local -a _ocr_pdfs=()
        local i
        for i in "${!_carpetas[@]}"; do
            _c="${_carpetas[$i]}"
            local _base; _base=$(basename "$_c")
            local _pdf_ocr="${ocr_out}/${_base}/${_base}_ocr.pdf"
            _ocr_pdfs+=("$_pdf_ocr")
            local -a ocr_args=(-i "$_c" -o "$ocr_out" --orden "${_ordenes[$i]}")
            if [[ -n "$OCR_JOBS" ]]; then ocr_args+=(-j "$OCR_JOBS"); fi
            if [[ "$SKIP_OCR" == "true" ]]; then
                continue
            fi
            [[ "$fragmentado" == true ]] && log_info "[${book}] OCR de la parte $((i + 1)) de ${#_carpetas[@]}: ${_base}"
            log_step "  -> [1/2] Ejecutando Fase OCR..."
            bash "$RUN_OCR" "${ocr_args[@]}" 2>&1 | tee -a "$MAIN_LOG" || true

            # Un fallo del OCR suele ser pasajero (memoria, un proceso caído):
            # se reintenta una vez. Las páginas ya hechas no se repiten.
            if [[ ! -f "$_pdf_ocr" && ! -f "${_c}/_book_rejected.json" ]]; then
                if [[ "$fragmentado" == true ]]; then
                    log_warn "[${book}] El OCR no generó el PDF de ${_base}. Reintentando una vez..."
                else
                    log_warn "[${book}] El OCR no generó el PDF. Reintentando una vez..."
                fi
                bash "$RUN_OCR" "${ocr_args[@]}" 2>&1 | tee -a "$MAIN_LOG" || true
            fi
            # Una parte rechazada (DPI, corruptas) ya decide el libro: no se sigue con las demás
            if [[ -f "${_c}/_book_rejected.json" ]]; then break; fi
        done
        [[ "$SKIP_OCR" == "true" ]] && log_info "  -> [1/2] Fase OCR omitida (--skip-ocr activo)."

        # ── Detectar si el libro (o una de sus partes) fue RECHAZADO por preprocess_validate.py ──
        local _rejected_file=""
        for _c in "${_carpetas[@]}"; do
            if [[ -f "${_c}/_book_rejected.json" ]]; then _rejected_file="${_c}/_book_rejected.json"; break; fi
        done
        if [[ -n "$_rejected_file" ]]; then
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
            local _parte=""
            [[ "$fragmentado" == true ]] && _parte=" en $(basename "$(dirname "$_rejected_file")")"
            log_error "[${book}] 🚫 LIBRO RECHAZADO (DPI/Corrupta)${_parte}. Enviando a REVIEW."
            quarantine_book "$book" "Imágenes inválidas${_parte}: ${_reject_summary}"
            CURRENT_BOOK=""
            continue
        fi

        # ── Sin PDF del OCR (tras el reintento): el libro se queda en INPUT ──
        # Las imágenes ya se validaron (DPI, corruptas), así que es un fallo de
        # ejecución, no del libro: se libera el candado y se reprocesa en la
        # próxima ejecución. Nunca va a REVIEW por esto.
        local _falta_ocr=""
        for _o in "${_ocr_pdfs[@]}"; do
            if [[ ! -f "$_o" ]]; then _falta_ocr="$_o"; break; fi
        done
        if [[ -n "$_falta_ocr" ]]; then
            local ocr_motivo="OCR no generó el PDF (tras 1 reintento)"
            [[ "$fragmentado" == true ]] && ocr_motivo+=" de $(basename "$(dirname "$_falta_ocr")")"
            local tess_err_log="${LOG_DIR}/tesseract_errors.log"
            local last_err=""
            if [[ -f "$tess_err_log" ]]; then
                last_err=$(grep -i "error\|failed" "$tess_err_log" 2>/dev/null | grep -F "$book" | tail -1 || true)
            fi
            [[ -n "$last_err" ]] && ocr_motivo="${ocr_motivo}: ${last_err}"
            log_error "[${book}] ${ocr_motivo}. Queda en INPUT para reintentar en la próxima ejecución."
            rm -rf "${TEMP_DIR:?}/${book}" 2>/dev/null || true
            record_result REINTENTAR "$book" "$ocr_motivo"
            CURRENT_BOOK=""
            continue
        fi

        # ── Libro fragmentado: armar el PDF del OCR de cada PDF de salida, uniendo
        # sus partes en el orden del plan (nunca ordenando nombres otra vez) ──
        if [[ "$fragmentado" == true ]]; then
            local _armado_ok=true
            for _u in "${_pdfs[@]}"; do
                local _dir; _dir=$(unidad_dir "$book" "$_u")
                mkdir -p "$_dir"
                local -a _partes=()
                for i in "${!_carpetas[@]}"; do
                    [[ "$(awk -F'\t' -v n=$((i + 1)) 'NR == n { print $1 }' "$plan_tsv")" == "$_u" ]] \
                        && _partes+=("${_ocr_pdfs[$i]}")
                done
                # Páginas sin OCR de sus partes, con el nombre de la parte delante
                for _o in "${_partes[@]}"; do
                    local _marca_sin_ocr; _marca_sin_ocr="$(dirname "$_o")/_paginas_sin_ocr.txt"
                    if [[ -s "$_marca_sin_ocr" ]]; then
                        local _parte_nombre; _parte_nombre=$(sin_ok "$(basename "$(dirname "$_o")")")
                        awk -v p="${_parte_nombre}/" '{ print p $0 }' "$_marca_sin_ocr" | tee -a "${TEMP_DIR}/${book}/_paginas_sin_ocr.txt" \
                            >> "${_dir}/_paginas_sin_ocr.txt.tmp"
                    fi
                done
                if [[ -f "${_dir}/_paginas_sin_ocr.txt.tmp" && "$_dir" != "${TEMP_DIR}/${book}" ]]; then
                    mv "${_dir}/_paginas_sin_ocr.txt.tmp" "${_dir}/_paginas_sin_ocr.txt"
                fi
                rm -f "${_dir}/_paginas_sin_ocr.txt.tmp"
                if [[ ${#_partes[@]} -eq 1 ]]; then
                    mv "${_partes[0]}" "${_dir}/${_u}_ocr.pdf" || _armado_ok=false
                elif ! pdfunite "${_partes[@]}" "${_dir}/${_u}_ocr.pdf" 2>>"${LOG_DIR}/pdfunite_errors.log"; then
                    _armado_ok=false
                fi
                [[ "$_armado_ok" == true ]] || break
                log_ok "[${book}] PDF de ${_u} armado con ${#_partes[@]} parte(s), en orden."
            done
            if [[ "$_armado_ok" != true ]]; then
                log_error "[${book}] No se pudieron unir las partes del libro. Queda en INPUT para reintentar."
                rm -rf "${TEMP_DIR:?}/${book}" 2>/dev/null || true
                record_result REINTENTAR "$book" "No se pudieron unir las partes del libro"
                CURRENT_BOOK=""
                continue
            fi
        fi

        # ── Páginas que Tesseract no pudo leer: OCR.sh las metió como imagen sin
        # texto. Como los TIFF se borran al entregar, el libro no se entrega así:
        # va a REVIEW con sus TIFF. Para entregarlo de todos modos (p. ej. páginas
        # en blanco o dañadas que nunca tendrán texto): ACEPTAR_PAGINAS_SIN_OCR=1
        local _sin_ocr_file="${TEMP_DIR}/${book}/_paginas_sin_ocr.txt"
        if [[ -s "$_sin_ocr_file" ]]; then
            local _sin_ocr_txt
            _sin_ocr_txt=$(paginas_sin_ocr "$_sin_ocr_file")
            if [[ "${ACEPTAR_PAGINAS_SIN_OCR:-0}" != "1" ]]; then
                log_error "[${book}] 🚫 ${_sin_ocr_txt}. Enviando a REVIEW (los TIFF se conservan)."
                quarantine_book "$book" "$_sin_ocr_txt"
                CURRENT_BOOK=""
                continue
            fi
            log_warn "[${book}] ⚠ ${_sin_ocr_txt}. Se entrega igual (ACEPTAR_PAGINAS_SIN_OCR=1); queda anotado en el registro."
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
