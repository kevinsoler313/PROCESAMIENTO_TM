#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   OCR.sh
# VERSION:  2.0.0
# FECHA:    2026-02-23
# DESC:     Pipeline OCR/ICR sobre imágenes TIFF de alta calidad.
#           Motores: Tesseract | PaddleOCR | Kraken
#           Hardware: i7-14700 · RTX 5060 · 24 GB RAM · Linux Mint
#
# ESTRUCTURA INPUT esperada:
#   input/
#   ├── Libro_01/
#   │   ├── pagina_001.tiff
#   │   └── pagina_002.tiff
#   └── Libro_02/
#       └── pagina_001.tiff
#
# SALIDA FINAL:
#   output/PDF_COMPILADO_<timestamp>.pdf  ← PDF con TODO el contenido
#   output/<libro>/tesseract/<libro>.pdf  ← PDF por libro (Tesseract)
#   output/<libro>/consolidado/           ← texto plano fusionado por página
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# Libros procesados en esta ejecución (para reportar solo los relevantes)
declare -a PROCESSED_BOOKS=()

# Subir límite de archivos abiertos (necesario para pdfunite con libros grandes)
ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# 0. CONFIGURACIÓN GLOBAL
# ─────────────────────────────────────────────────────────────────────────────

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# --- Rutas de trabajo ---
INPUT_DIR="${INPUT_DIR:-${SCRIPT_DIR}/input}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/output}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
LOG_FILE="${LOG_DIR}/ocr_${TIMESTAMP}.log"

# PDF compilado final (todos los libros, todos los motores)
PDF_COMPILADO="${OUTPUT_DIR}/PDF_COMPILADO_${TIMESTAMP}.pdf"

# --- Motores activos (1=activo, 0=desactivado) ---
USE_TESSERACT=1
USE_PADDLEOCR=0 # ${USE_PADDLEOCR:-1}
USE_KRAKEN=0    # ${USE_KRAKEN:-1}

# --- Idioma / Modelos ---
# NOTA: se usaba "spa+spa_old" pero el modelo custom spa_old.traineddata no está
# instalado en este equipo. Se deja "spa" (oficial) para operar. Si consigues
# spa_old.traineddata, cópialo a /usr/share/tesseract-ocr/5/tessdata/ y vuelve a "spa+spa_old".
TESSERACT_LANG="spa"
TESSERACT_OEM=1
TESSERACT_PSM=6
# IMPORTANTE: 'pdf' debe estar siempre para poder generar el PDF compilado
TESSERACT_OUTPUT_FORMATS="txt pdf"

# PaddleOCR
PADDLE_LANG="es"
PADDLE_USE_GPU=true
PADDLE_GPU_ID=0
PADDLE_DET_MODEL="ch_PP-OCRv4_det"
PADDLE_REC_MODEL="latin_PP-OCRv3_rec"

# Kraken
KRAKEN_MODEL="${SCRIPT_DIR}/models/kraken/spanish_hist.mlmodel"
KRAKEN_FALLBACK_MODEL="10.5281/zenodo.12743230"
KRAKEN_DEVICE="cuda:0"

# --- Paralelismo (v2 — optimizado para 40GB RAM) ---
# i7-14700: 20 núcleos físicos (8P + 12E), 28 hilos.
# Usamos 26 hilos (dejando 2 para el SO) para maximizar throughput.
# Cada instancia Tesseract consume ~400MB → 26×400MB = ~10.4GB (de 40GB disponibles).
PARALLEL_JOBS_CPU=26
PARALLEL_JOBS_GPU=1  # No se usa GPU (solo Tesseract)

# --- Prevención de OOM para imágenes grandes (v2 — más agresivo) ---
# Con 40GB de RAM podemos ser más permisivos.
# Imágenes >200MB consumen ~3-5x su tamaño en RAM por instancia Tesseract.
# Con 6 jobs paralelos de 200MB+ → ~3-6GB de RAM → manejable con 40GB.
LARGE_IMAGE_THRESHOLD_MB=200
LARGE_IMAGE_THRESHOLD=$(( LARGE_IMAGE_THRESHOLD_MB * 1024 * 1024 ))  # En bytes
PARALLEL_JOBS_LARGE=6   # Jobs paralelos para imágenes >200MB (antes: 2)

# ─────────────────────────────────────────────────────────────────────────────
# 1. LOGGING
# ─────────────────────────────────────────────────────────────────────────────

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
        *)     color="\033[0m"     ;;
    esac
    local reset="\033[0m"
    printf "${color}[%s] [%-5s] %s${reset}\n" "$ts" "$level" "$msg" | tee -a "$LOG_FILE"
}

log_info()  { _log INFO  "$@"; }
log_ok()    { _log OK    "$@"; }
log_warn()  { _log WARN  "$@"; }
log_error() { _log ERROR "$@"; }

# ─────────────────────────────────────────────────────────────────────────────
# 2. MANEJO DE ERRORES
# ─────────────────────────────────────────────────────────────────────────────

_cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        log_error "Script terminado con error (código $exit_code). Revisa: $LOG_FILE"
    else
        log_ok "Pipeline finalizado correctamente."
    fi
}
trap _cleanup EXIT

_handle_error() {
    log_error "Error inesperado en línea $1. Abortando."
    exit 1
}
trap '_handle_error $LINENO' ERR

# ─────────────────────────────────────────────────────────────────────────────
# 3. VERIFICACIÓN DE DEPENDENCIAS
# ─────────────────────────────────────────────────────────────────────────────

check_dependencies() {
    log_info "Verificando dependencias del sistema..."
    local missing=0

    for cmd in find parallel pdfunite; do
        if ! command -v "$cmd" &>/dev/null; then
            log_error "Herramienta no encontrada: $cmd"
            missing=$((missing + 1))
        fi
    done

    if [[ $USE_TESSERACT -eq 1 ]]; then
        if ! command -v tesseract &>/dev/null; then
            log_error "Tesseract no encontrado. Instalar: sudo apt install tesseract-ocr tesseract-ocr-spa"
            missing=$((missing + 1))
        else
            local tver; tver="$(tesseract --version 2>&1 | head -1)"
            log_info "Tesseract: $tver"
            if ! tesseract --list-langs 2>/dev/null | grep -q "^spa$"; then
                log_warn "Modelo 'spa' no encontrado."
            fi
            if ! tesseract --list-langs 2>/dev/null | grep -q "^spa_old$"; then
                log_warn "Modelo 'spa_old' no encontrado. Descarga desde: https://github.com/tesseract-ocr/tessdata_best"
            fi
        fi
    fi

    if [[ $USE_PADDLEOCR -eq 1 ]]; then
        if ! python3 -c "import paddleocr" 2>/dev/null; then
            log_error "PaddleOCR no encontrado. Activa el venv: source ocr_env/bin/activate"
            missing=$((missing + 1))
        else
            log_info "PaddleOCR: $(python3 -c 'import paddleocr; print(paddleocr.__version__)' 2>/dev/null)"
        fi
    fi

    if [[ $USE_KRAKEN -eq 1 ]]; then
        if ! command -v kraken &>/dev/null; then
            log_error "Kraken no encontrado. Activa el venv: source ocr_env/bin/activate"
            missing=$((missing + 1))
        else
            log_info "Kraken: $(kraken --version 2>&1 | head -1)"
            if [[ ! -f "$KRAKEN_MODEL" ]]; then
                log_warn "Modelo local Kraken no encontrado: $KRAKEN_MODEL"
                log_warn "Se usará: $KRAKEN_FALLBACK_MODEL (se descargará automáticamente)"
            fi
        fi
    fi

    if nvidia-smi &>/dev/null; then
        log_info "GPU detectada: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)"
    else
        log_warn "nvidia-smi no responde. Los motores usarán CPU."
        PADDLE_USE_GPU=false
        KRAKEN_DEVICE="cpu"
    fi

    if [[ $missing -gt 0 ]]; then
        log_error "$missing dependencia(s) faltante(s). Corrige el entorno y reintenta."
        exit 1
    fi

    log_ok "Todas las dependencias verificadas."
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. PREPARACIÓN DE DIRECTORIOS (por libro)
# ─────────────────────────────────────────────────────────────────────────────

setup_directories() {
    log_info "Preparando estructura de directorios..."
    mkdir -p "$LOG_DIR" "$INPUT_DIR" "$OUTPUT_DIR"
    mkdir -p "${SCRIPT_DIR}/models/kraken"
    log_ok "Directorios base listos."
}

setup_book_dirs() {
    local book_name="$1"
    [[ $USE_TESSERACT -eq 1 ]] && mkdir -p "${OUTPUT_DIR}/${book_name}/tesseract"
    [[ $USE_PADDLEOCR -eq 1 ]] && mkdir -p "${OUTPUT_DIR}/${book_name}/paddleocr"
    [[ $USE_KRAKEN    -eq 1 ]] && mkdir -p "${OUTPUT_DIR}/${book_name}/kraken"
    mkdir -p "${OUTPUT_DIR}/${book_name}/consolidado"
}

# ─────────────────────────────────────────────────────────────────────────────
# 5. MOTOR A: TESSERACT OCR
# ─────────────────────────────────────────────────────────────────────────────

run_tesseract() {
    local tiff_file="$1"
    local book_name="$2"
    local base; base="$(basename "${tiff_file%.*}")"
    local out_dir="${OUTPUT_DIR}/${book_name}/tesseract"
    local out_base="${out_dir}/${base}"

    local book_pdf="${OUTPUT_DIR}/${book_name}/${book_name}_ocr.pdf"

    # Saltar solo si ya tenemos el TXT Y (ya tenemos el PDF intermedio o el PDF final compilado del libro)
    if [[ -f "${out_base}.txt" ]]; then
        if [[ -f "${out_base}.pdf" || -f "$book_pdf" ]]; then
            log_info "[TESSERACT] Ya procesado, omitiendo: ${book_name}/${base}"
            return 0
        fi
    fi

    local tiff_size
    tiff_size=$(stat -c%s "$tiff_file" 2>/dev/null || echo 0)
    if [[ "$tiff_size" -eq 0 ]]; then
        log_warn "[TESSERACT] ⚠ Omitiendo imagen corrupta (0 bytes): ${book_name}/${base}.tif"
        return 0
    fi

    log_info "[TESSERACT] Procesando: ${book_name}/${base}"

    # Ejecutar Tesseract capturando el código de salida real
    local tess_exit=0
    tesseract \
        "$tiff_file" \
        "$out_base" \
        -l "$TESSERACT_LANG" \
        --oem "$TESSERACT_OEM" \
        --psm "$TESSERACT_PSM" \
        -c preserve_interword_spaces=1 \
        -c tessedit_do_invert=0 \
        txt pdf \
        2>>"${LOG_DIR}/tesseract_errors.log" \
    || tess_exit=$?

    if [[ $tess_exit -eq 0 ]]; then
        log_ok "[TESSERACT] ✓ ${book_name}/${base}"
        return 0
    fi

    # ── SEGFAULT u otro crash (exit 139=SIGSEGV, 134=SIGABRT, 136=SIGFPE) ──
    if [[ $tess_exit -ge 128 ]]; then
        local signal_num=$((tess_exit - 128))
        log_warn "[TESSERACT] ⚠ CRASH (señal $signal_num, exit $tess_exit) en: ${book_name}/${base}"

        # Limpiar archivos parciales/corruptos del intento fallido
        rm -f "${out_base}.txt" "${out_base}.pdf" 2>/dev/null

        # ── REINTENTO 1: re-guardar la imagen con PIL para normalizar headers TIFF ──
        log_info "[TESSERACT] Reintento 1/3: imagen normalizada — ${book_name}/${base}"
        local tmp_tiff="${out_dir}/_retry_${base}.tif"

        # Rutas por variables de entorno: una comilla en el nombre no rompe el código
        SRC_TIFF="$tiff_file" DST_TIFF="$tmp_tiff" python3 -c "
import os
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
img = Image.open(os.environ['SRC_TIFF'])
if img.mode == 'RGBA':
    bg = Image.new('RGB', img.size, (255,255,255))
    bg.paste(img, mask=img.split()[3])
    img = bg
elif img.mode not in ('RGB', 'L'):
    img = img.convert('RGB')
img.save(os.environ['DST_TIFF'], format='TIFF', compression='raw', dpi=img.info.get('dpi', (300, 300)))
img.close()
" 2>>"${LOG_DIR}/tesseract_errors.log"

        if [[ -f "$tmp_tiff" ]]; then
            local retry1_exit=0
            tesseract \
                "$tmp_tiff" \
                "$out_base" \
                -l "$TESSERACT_LANG" \
                --oem "$TESSERACT_OEM" \
                --psm "$TESSERACT_PSM" \
                -c preserve_interword_spaces=1 \
                -c tessedit_do_invert=0 \
                txt pdf \
                2>>"${LOG_DIR}/tesseract_errors.log" \
            || retry1_exit=$?

            if [[ $retry1_exit -eq 0 ]]; then
                rm -f "$tmp_tiff" 2>/dev/null
                log_ok "[TESSERACT] ✓ ${book_name}/${base} (OK en reintento 1 — imagen normalizada)"
                return 0
            fi
            rm -f "${out_base}.txt" "${out_base}.pdf" 2>/dev/null
        fi

        # ── REINTENTO 2: motor legacy (OEM 0) + PSM 3 (auto sin OSD) ──
        log_info "[TESSERACT] Reintento 2/3: motor legacy (--oem 0 --psm 3) — ${book_name}/${base}"
        local retry2_src="${tmp_tiff}"
        [[ ! -f "$retry2_src" ]] && retry2_src="$tiff_file"

        local retry2_exit=0
        tesseract \
            "$retry2_src" \
            "$out_base" \
            -l "$TESSERACT_LANG" \
            --oem 0 \
            --psm 3 \
            -c preserve_interword_spaces=1 \
            -c tessedit_do_invert=0 \
            txt pdf \
            2>>"${LOG_DIR}/tesseract_errors.log" \
        || retry2_exit=$?

        rm -f "$tmp_tiff" 2>/dev/null

        if [[ $retry2_exit -eq 0 ]]; then
            log_ok "[TESSERACT] ✓ ${book_name}/${base} (OK en reintento 2 — motor legacy)"
            return 0
        fi
        rm -f "${out_base}.txt" "${out_base}.pdf" 2>/dev/null

        # ── REINTENTO 3 (ÚLTIMO RECURSO): Generar PDF directamente desde la imagen ──
        # No se puede extraer texto OCR, pero la IMAGEN del documento histórico
        # se preserva en el PDF final. Ninguna página puede faltar.
        log_warn "[TESSERACT] Reintento 3/3: FALLBACK — generando PDF de imagen (sin OCR) — ${book_name}/${base}"

        SRC_TIFF="$tiff_file" OUT_BASE="$out_base" python3 -c "
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from io import BytesIO
import os

Image.MAX_IMAGE_PIXELS = None
tiff_path = os.environ['SRC_TIFF']
out_pdf = os.environ['OUT_BASE'] + '.pdf'
out_txt = os.environ['OUT_BASE'] + '.txt'

img = Image.open(tiff_path)
# Convertir a RGB si es necesario
if img.mode == 'RGBA':
    bg = Image.new('RGB', img.size, (255,255,255))
    bg.paste(img, mask=img.split()[3])
    img = bg
elif img.mode not in ('RGB', 'L'):
    img = img.convert('RGB')

dpi = img.info.get('dpi', (300, 300))
dpi_x = dpi[0] if dpi[0] > 0 else 300
dpi_y = dpi[1] if dpi[1] > 0 else 300

# Dimensiones en puntos (72 pts por pulgada)
w_pts = img.width * 72.0 / dpi_x
h_pts = img.height * 72.0 / dpi_y

# Generar PDF con la imagen embebida a tamaño completo
c = canvas.Canvas(out_pdf, pagesize=(w_pts, h_pts))

# Guardar imagen temporal como PNG para embeber (más rápido que TIFF para reportlab)
tmp_img = BytesIO()
img.save(tmp_img, format='PNG')
tmp_img.seek(0)

c.drawImage(ImageReader(tmp_img), 0, 0, width=w_pts, height=h_pts)
c.save()
img.close()

# Generar TXT vacío (sin OCR posible)
with open(out_txt, 'w') as f:
    f.write('[Página generada como imagen — Tesseract no pudo procesar este archivo]\\n')

print(f'PDF de imagen generado: {os.path.getsize(out_pdf)} bytes')
" 2>>"${LOG_DIR}/tesseract_errors.log"

        if [[ -f "${out_base}.pdf" ]]; then
            log_ok "[TESSERACT] ✓ ${book_name}/${base} (FALLBACK: PDF de imagen sin OCR — página preservada)"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] FALLBACK_IMG: ${book_name}/${base} | Tesseract crash → PDF de imagen generado" \
                >> "${LOG_DIR}/tesseract_segfaults.log"
            return 0
        else
            log_error "[TESSERACT] ✗ FALLO TOTAL: no se pudo generar ni siquiera el PDF de imagen — ${book_name}/${base}"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] FALLO_TOTAL: ${book_name}/${base} | exit=$tess_exit" \
                >> "${LOG_DIR}/tesseract_segfaults.log"
            return 0  # No abortar el libro
        fi
    fi

    # ── Error normal de Tesseract (no crash) ──
    log_error "[TESSERACT] ✗ Error (exit $tess_exit) en: ${book_name}/${base}"
    return 0  # No abortar el libro por 1 página
}

export -f run_tesseract log_info log_ok log_warn log_error _log
export OUTPUT_DIR LOG_DIR LOG_FILE TESSERACT_LANG TESSERACT_OEM TESSERACT_PSM TESSERACT_OUTPUT_FORMATS

# ─────────────────────────────────────────────────────────────────────────────
# 6. MOTOR B: PADDLEOCR
# ─────────────────────────────────────────────────────────────────────────────

_generate_paddle_runner() {
    cat > "${SCRIPT_DIR}/_paddle_runner.py" << 'PYEOF'
#!/usr/bin/env python3
"""
PaddleOCR runner para múltiples archivos TIFF en batch.
Uso: python3 _paddle_runner.py <output_dir> <use_gpu> <gpu_id> <input1.tiff> [<input2.tiff> ...]
"""
import sys
import os
import json
from pathlib import Path

def main():
    if len(sys.argv) < 5:
        print("Uso: paddle_runner.py <output_dir> <use_gpu> <gpu_id> <input1.tiff> ...")
        sys.exit(1)

    output_dir = Path(sys.argv[1])
    use_gpu    = sys.argv[2].lower() == "true"
    gpu_id     = sys.argv[3]
    tiff_paths = sys.argv[4:]

    if use_gpu:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    # Import and initialize model ONLY ONCE
    from paddleocr import PaddleOCR
    import cv2

    print(f"[PADDLEOCR] Inicializando modelo en GPU {gpu_id} para {len(tiff_paths)} imágenes...")
    ocr = PaddleOCR(
        use_textline_orientation=True,
        lang="es",
        text_det_thresh=0.3,
        text_det_box_thresh=0.5,
        text_recognition_batch_size=4,
    )

    for tiff_path in tiff_paths:
        base_name = Path(tiff_path).stem
        out_txt   = output_dir / f"{base_name}.txt"
        out_json  = output_dir / f"{base_name}.json"

        if out_txt.exists():
            print(f"[PADDLEOCR] Ya procesado, omitiendo: {base_name}")
            continue

        img = cv2.imread(str(tiff_path))
        if img is None:
            print(f"[PADDLEOCR] No se pudo leer la imagen: {base_name}", file=sys.stderr)
            continue

        h, w = img.shape[:2]
        max_side = 2000
        scale = 1.0
        if max(h, w) > max_side:
            scale = max_side / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)))

        result = ocr.ocr(img)

        if not result or result[0] is None:
            print(f"[PADDLEOCR] Sin resultados para: {base_name}", file=sys.stderr)
            continue

        lines = []
        structured = []
        for line in result[0]:
            try:
                bbox, (text, confidence) = line
                if scale != 1.0:
                    bbox = [[pt[0] / scale, pt[1] / scale] for pt in bbox]
                lines.append(text)
                structured.append({
                    "bbox": bbox,
                    "text": text,
                    "confidence": round(float(confidence), 4)
                })
            except Exception:
                continue

        out_txt.write_text("\n".join(lines), encoding="utf-8")
        out_json.write_text(
            json.dumps({"file": tiff_path, "pages": structured}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        avg_conf = sum(s["confidence"] for s in structured) / len(structured) if structured else 0
        print(f"[PADDLEOCR] ✓ {base_name} | líneas: {len(lines)} | confianza media: {avg_conf:.2%}")

if __name__ == "__main__":
    main()
PYEOF
    chmod +x "${SCRIPT_DIR}/_paddle_runner.py"
}
# ─────────────────────────────────────────────────────────────────────────────
# 7. MOTOR C: KRAKEN HTR/OCR (Ejecución en lote)
# ─────────────────────────────────────────────────────────────────────────────

_generate_kraken_extractor() {
    cat > "${SCRIPT_DIR}/_kraken_extractor.py" << 'PYEOF'
import sys
import xml.etree.ElementTree as ET

def main():
    if len(sys.argv) < 3:
        sys.exit(1)
        
    in_xml = sys.argv[1]
    out_txt = sys.argv[2]
    
    try:
        tree = ET.parse(in_xml)
        ns = {'alto': 'http://www.loc.gov/standards/alto/ns-v4#'}
        strings = tree.findall('.//alto:String', ns)
        if not strings:
            strings = tree.findall('.//{*}String')
            
        lines_text = [s.get('CONTENT', '') for s in strings if s.get('CONTENT', '')]
        with open(out_txt, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines_text))
    except Exception:
        pass

if __name__ == '__main__':
    main()
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# 8. CONSOLIDACIÓN POR LIBRO: texto plano + PDF por libro
# ─────────────────────────────────────────────────────────────────────────────

consolidate_book() {
    local book_name="$1"

    log_info "Consolidando libro: $book_name"

    # (Bucle de generación de _consolidado.txt eliminado — solo usaba Tesseract,
    #  generaba 1200+ archivos por libro y luego se borraban en main.sh paso 6)

    # ── PDF por libro: unir los PDF por página generados por Tesseract ──
    if [[ $USE_TESSERACT -eq 1 ]]; then
        local tess_dir="${OUTPUT_DIR}/${book_name}/tesseract"
        # main.sh pasa -o TEMPORALES: el PDF del OCR queda en TEMPORALES/<libro>/
        local book_pdf="${OUTPUT_DIR}/${book_name}/${book_name}_ocr.pdf"
        local page_pdfs=()
        local skipped_corrupt=0
        # Recoger PDFs válidos (> 1KB) en orden de página — ignorar corruptos
        # sort -V (orden natural): los nombres no llevan ceros a la izquierda
        # ("Hoja 2" < "Hoja 10"). Con 'sort' lexicográfico el PDF se armaría
        # desordenado: Hoja 1, Hoja 10, Hoja 100, Hoja 11...
        while IFS= read -r pdf_page; do
            # Validar con pdfinfo (detecta stubs de 2KB dejados por OOM kills)
            if ! pdfinfo "$pdf_page" &>/dev/null; then
                local fsize
                fsize=$(stat -c%s "$pdf_page" 2>/dev/null || echo 0)
                log_warn "[${book_name}] PDF corrupto ignorado: $(basename "$pdf_page") (${fsize} bytes) — se re-procesará"
                rm -f "$pdf_page"
                rm -f "${pdf_page%.pdf}.txt"   # Forzar re-procesamiento en próxima ejecución
                skipped_corrupt=$((skipped_corrupt + 1))
            else
                page_pdfs+=("$pdf_page")
            fi
        done < <(find "$tess_dir" -name "*.pdf" | sort -V)

        if [[ $skipped_corrupt -gt 0 ]]; then
            log_warn "[${book_name}] Se ignoraron $skipped_corrupt PDF(s) corruptos. Re-ejecuta el pipeline para completar esas páginas."
        fi

        if [[ ${#page_pdfs[@]} -gt 0 ]]; then
            local total_pages=${#page_pdfs[@]}
            local CHUNK_SIZE=200

            if [[ $total_pages -le $CHUNK_SIZE ]]; then
                # Pocos PDFs: merge directo
                if pdfunite "${page_pdfs[@]}" "$book_pdf"; then
                    log_ok "PDF libro generado: ${book_name}.pdf (${total_pages} páginas válidas)"
                    rm -f "${page_pdfs[@]}"
                else
                    log_warn "No se pudo generar PDF para libro: $book_name"
                fi
            else
                # Muchos PDFs: merge por lotes para evitar 'Too many open files'
                log_info "[${book_name}] Merge por lotes ($total_pages páginas, chunks de $CHUNK_SIZE)..."
                local tmp_dir; tmp_dir=$(mktemp -d)
                local chunk_pdfs=()
                local chunk_num=0
                local merge_ok=true

                for ((i = 0; i < total_pages; i += CHUNK_SIZE)); do
                    local chunk=("${page_pdfs[@]:i:CHUNK_SIZE}")
                    local chunk_out="${tmp_dir}/chunk_$(printf '%04d' $chunk_num).pdf"

                    if [[ ${#chunk[@]} -eq 1 ]]; then
                        # Solo 1 PDF en el chunk, copiarlo directamente
                        cp "${chunk[0]}" "$chunk_out"
                    else
                        if ! pdfunite "${chunk[@]}" "$chunk_out" 2>>"${LOG_DIR}/pdfunite_errors.log"; then
                            log_error "[${book_name}] Error en merge chunk $chunk_num"
                            merge_ok=false
                            break
                        fi
                    fi
                    chunk_pdfs+=("$chunk_out")
                    chunk_num=$((chunk_num + 1))
                done

                if [[ "$merge_ok" == true && ${#chunk_pdfs[@]} -gt 0 ]]; then
                    if [[ ${#chunk_pdfs[@]} -eq 1 ]]; then
                        mv "${chunk_pdfs[0]}" "$book_pdf"
                    else
                        if pdfunite "${chunk_pdfs[@]}" "$book_pdf" 2>>"${LOG_DIR}/pdfunite_errors.log"; then
                            true
                        else
                            log_warn "No se pudo generar PDF final para libro: $book_name"
                            merge_ok=false
                        fi
                    fi
                fi

                # Limpiar temporales
                rm -rf "$tmp_dir"

                if [[ "$merge_ok" == true && -f "$book_pdf" ]]; then
                    log_ok "PDF libro generado: ${book_name}.pdf (${total_pages} páginas, ${chunk_num} chunks)"
                    rm -f "${page_pdfs[@]}"
                fi
            fi
        else
            log_warn "Sin páginas PDF válidas para unir en libro: $book_name"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# (generate_compiled_pdf function removed)

# ─────────────────────────────────────────────────────────────────────────────
# 10. REPORTE FINAL
# ─────────────────────────────────────────────────────────────────────────────

generate_report() {
    local csv_file="${OUTPUT_DIR}/reporte_general.csv"

    # ── 1. Inicializar CSV si no existe ──
    if [[ ! -f "$csv_file" ]]; then
        echo "Libro,Paginas,Peso_TIFF,Peso_PDF" > "$csv_file"
    fi

    # ── 2. Determinar directorio base real de INPUT ──
    # Si INPUT_DIR contiene TIFFs directamente (modo libro individual desde main.sh),
    # usar el padre como base para que la resolución de rutas funcione correctamente.
    local base_input_dir="$INPUT_DIR"
    shopt -s nullglob nocaseglob
    local _check_tiffs=("$INPUT_DIR"/*.tif "$INPUT_DIR"/*.tiff)
    shopt -u nullglob nocaseglob
    if [[ ${#_check_tiffs[@]} -gt 0 ]]; then
        base_input_dir="$(dirname "$INPUT_DIR")"
    fi

    # ── 3. Solo reportar libros procesados en ESTA ejecución ──
    for book_name in "${PROCESSED_BOOKS[@]}"; do
        local pdf_file="${OUTPUT_DIR}/${book_name}.pdf"
        if [[ ! -f "$pdf_file" ]]; then continue; fi

        # Peso PDF en MB (LC_ALL=C fuerza punto decimal, evitando romper el CSV)
        local size_pdf
        size_pdf=$(LC_ALL=C awk "BEGIN {printf \"%.1fM\", $(stat -c%s "$pdf_file" 2>/dev/null || echo 0) / 1048576}")

        # Contar páginas TIFF del libro
        local tiff_count=0
        local book_tiff_dir="${base_input_dir}/${book_name}"
        if [[ -d "$book_tiff_dir" ]]; then
            tiff_count=$(find "$book_tiff_dir" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) 2>/dev/null | wc -l)
        elif [[ "$book_name" == "$(basename "$INPUT_DIR")" ]]; then
            tiff_count=$(find "$INPUT_DIR" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) 2>/dev/null | wc -l)
        fi

        # Peso carpeta TIFF en GB
        local size_tiff="0G"
        if [[ -d "$book_tiff_dir" ]]; then
            size_tiff=$(LC_ALL=C awk "BEGIN {printf \"%.1fG\", $(du -sb "$book_tiff_dir" 2>/dev/null | awk '{print $1}') / 1073741824}")
        elif [[ "$book_name" == "$(basename "$INPUT_DIR")" ]]; then
            size_tiff=$(LC_ALL=C awk "BEGIN {printf \"%.1fG\", $(du -sb "$INPUT_DIR" 2>/dev/null | awk '{print $1}') / 1073741824}")
        fi

        # Actualizar CSV: eliminar entrada previa y anexar nueva
        sed -i "/^${book_name},/d" "$csv_file" 2>/dev/null || true
        echo "${book_name},${tiff_count},${size_tiff},${size_pdf}" >> "$csv_file"
    done

    log_ok "CSV general actualizado en: $csv_file"
}

# ─────────────────────────────────────────────────────────────────────────────
# 11. PIPELINE PRINCIPAL (por libro → por página)
# ─────────────────────────────────────────────────────────────────────────────

run_pipeline() {
    # 1. Obtener lista de carpetas a procesar
    local book_dirs=()
    local input_has_tiffs=0
    
    # Comprobar si $INPUT_DIR ya es el libro mismo (contiene TIFFs directamente)
    shopt -s nullglob nocaseglob
    local check_tiffs=("$INPUT_DIR"/*.tif "$INPUT_DIR"/*.tiff)
    shopt -u nullglob nocaseglob

    if [[ ${#check_tiffs[@]} -gt 0 ]]; then
        input_has_tiffs=1
        book_dirs+=("$INPUT_DIR")
    else
        # Si no tiene TIFFs directos, buscar subcarpetas que los tengan
        while IFS= read -r d; do
            if [[ -d "$d" ]]; then
                book_dirs+=("$d")
            fi
        done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type d | sort)
    fi

    if [[ ${#book_dirs[@]} -eq 0 && $input_has_tiffs -eq 0 ]]; then
        log_warn "No se encontraron subcarpetas de libros ni TIFFs en: $INPUT_DIR"
        return 0
    fi

    log_info "Libros detectados: ${#book_dirs[@]}"

    for book_dir in "${book_dirs[@]}"; do
        local book_name
        book_name=$(basename "$book_dir")

        # Buscar TIFF dentro de la carpeta del libro
        local tiff_list=()
        while IFS= read -r f; do
            tiff_list+=("$f")
        done < <(find "$book_dir" -maxdepth 1 \( -iname "*.tif" -o -iname "*.tiff" \) -not -name '_*' | sort -V)

        if [[ ${#tiff_list[@]} -eq 0 ]]; then
            log_warn "Sin imágenes TIFF en: $book_dir — omitiendo libro."
            continue
        fi

        log_info "════════════════════════════════════════════════════"
        log_info "LIBRO: $book_name  (${#tiff_list[@]} páginas) en $book_dir"
        log_info "════════════════════════════════════════════════════"

        # ── PRE-PROCESAMIENTO: Validar DPI, suavizar >300MB ──
        # v2: Si hay alguna imagen con DPI≠300 o corrupta, el libro COMPLETO se rechaza.
        #     No se excluyen imágenes individuales — integridad del libro es prioridad.
        log_info "--- Pre-procesamiento: Validando imágenes de $book_name ---"
        # Las imágenes >300MB se reducen como COPIA en <salida>/<libro>/reducidas/
        local reduced_dir="${OUTPUT_DIR}/${book_name}/reducidas"
        local preprocess_exit=0
        python3 "${SCRIPT_DIR}/preprocess_validate.py" "$book_dir" --work-dir "$reduced_dir" || preprocess_exit=$?

        if [[ $preprocess_exit -eq 1 ]]; then
            log_error "Pre-procesamiento: Error crítico en $book_name (carpeta vacía / sin TIFFs). Omitiendo libro."
            continue
        fi

        if [[ $preprocess_exit -eq 2 ]]; then
            log_error "══════════════════════════════════════════════════════════"
            log_error "🚫 LIBRO RECHAZADO: $book_name"
            log_error "   Tiene imágenes con DPI≠300 o corruptas."
            log_error "   El libro queda INTACTO en INPUT para corrección."
            log_error "   Ver detalle: ${book_dir}/_book_rejected.json"
            log_error "══════════════════════════════════════════════════════════"
            continue
        fi

        # v2: Si llegamos aquí, TODAS las imágenes son válidas (DPI=300, no corruptas)
        # No hay filtrado de imágenes individuales — el libro está completo
        log_ok "[${book_name}] Todas las imágenes validadas correctamente (${#tiff_list[@]} TIFFs)"

        # Usar la copia reducida en lugar del original cuando exista
        local n_reduced=0
        for i in "${!tiff_list[@]}"; do
            local reduced="${reduced_dir}/$(basename "${tiff_list[$i]}")"
            if [[ -f "$reduced" ]]; then
                tiff_list[$i]="$reduced"
                n_reduced=$((n_reduced + 1))
            fi
        done
        if [[ $n_reduced -gt 0 ]]; then
            log_info "[${book_name}] ${n_reduced} imagen(es) grandes se procesan desde su copia reducida."
        fi

        setup_book_dirs "$book_name"

        # ── Tesseract (Paralelo con protección OOM) ──
        if [[ $USE_TESSERACT -eq 1 ]]; then
            touch "$LOG_FILE"

            # Hilos OpenMP por instancia de Tesseract:
            # - Con 20 jobs paralelos: 1 hilo cada uno (GNU parallel distribuye la carga)
            # - Con 2 jobs paralelos (imágenes grandes): más hilos para mayor velocidad
            export CURRENT_BOOK="$book_name"

            # ── Clasificar imágenes por tamaño para prevenir OOM ──
            local normal_tiffs=()
            local large_tiffs=()
            for tiff in "${tiff_list[@]}"; do
                local fsize
                fsize=$(stat -c%s "$tiff" 2>/dev/null || echo 0)
                if [[ "$fsize" -gt "$LARGE_IMAGE_THRESHOLD" ]]; then
                    large_tiffs+=("$tiff")
                else
                    normal_tiffs+=("$tiff")
                fi
            done

            # Pase 1: Imágenes normales con paralelismo completo (1 hilo por instancia)
            if [[ ${#normal_tiffs[@]} -gt 0 ]]; then
                export OMP_THREAD_LIMIT=1
                log_info "--- Tesseract: $book_name — ${#normal_tiffs[@]} imágenes normales ($PARALLEL_JOBS_CPU en paralelo) ---"
                printf '%s\n' "${normal_tiffs[@]}" | parallel --jobs "$PARALLEL_JOBS_CPU" 'run_tesseract {} "$CURRENT_BOOK"'
            fi

            # Pase 2: Imágenes grandes con paralelismo reducido + más hilos por instancia
            if [[ ${#large_tiffs[@]} -gt 0 ]]; then
                local threads_per_large=$(( $(nproc) / PARALLEL_JOBS_LARGE ))
                export OMP_THREAD_LIMIT="$threads_per_large"
                log_warn "--- Tesseract: $book_name — ${#large_tiffs[@]} imágenes GRANDES (>${LARGE_IMAGE_THRESHOLD_MB}MB) procesando con $PARALLEL_JOBS_LARGE jobs × $threads_per_large hilos ---"
                for lt in "${large_tiffs[@]}"; do
                    log_info "[TESSERACT] Imagen grande: $(basename "$lt") ($(LC_ALL=C awk "BEGIN{printf \"%.0f\", $(stat -c%s "$lt")/1048576}")MB)"
                done
                printf '%s\n' "${large_tiffs[@]}" | parallel --jobs "$PARALLEL_JOBS_LARGE" 'run_tesseract {} "$CURRENT_BOOK"'
            fi

            log_ok "Tesseract completado: $book_name"
        fi

        # ── PaddleOCR (paralelo GPU Batching) ──
        # COMENTADO: Solo usamos Tesseract
        # if [[ $USE_PADDLEOCR -eq 1 ]]; then
        #     log_info "--- PaddleOCR: $book_name ---"
        #     touch "$LOG_FILE"
        #     local out_dir="${OUTPUT_DIR}/${book_name}/paddleocr"
        #     # Calcular ancho de cada partición
        #     local total_tiffs=${#tiff_list[@]}
        #     local chunk_size=$(( (total_tiffs + PARALLEL_JOBS_GPU - 1) / PARALLEL_JOBS_GPU ))
        # 
        #     for ((i = 0; i < PARALLEL_JOBS_GPU; i++)); do
        #         local start_idx=$(( i * chunk_size ))
        #         local end_idx=$(( start_idx + chunk_size ))
        #         if [[ $start_idx -ge $total_tiffs ]]; then continue; fi
        # 
        #         local chunk=("${tiff_list[@]:start_idx:chunk_size}")
        #         log_info "[PADDLEOCR] Lanzando Worker $((i+1))/$PARALLEL_JOBS_GPU con ${#chunk[@]} imágenes..."
        #         
        #         python3 "${SCRIPT_DIR}/_paddle_runner.py" \
        #             "$out_dir" \
        #             "$PADDLE_USE_GPU" \
        #             "$PADDLE_GPU_ID" \
        #             "${chunk[@]}" \
        #             >> "$LOG_FILE" 2>&1 &
        #     done
        #     wait
        #     log_ok "PaddleOCR completado: $book_name"
        # fi

        # ── Kraken (paralelo GPU Batching) ──
        # COMENTADO: Solo usamos Tesseract
        # if [[ $USE_KRAKEN -eq 1 ]]; then
        #     log_info "--- Kraken: $book_name ---"
        #     touch "$LOG_FILE"
        #     local out_dir="${OUTPUT_DIR}/${book_name}/kraken"
        #     mkdir -p "$out_dir"
        #     
        #     # Preparar modelo kraken
        #     local model_arg=()
        #     if [[ -f "$KRAKEN_MODEL" ]]; then
        #         model_arg=("-m" "$KRAKEN_MODEL")
        #     else
        #         if [[ ! -d "$HOME/.local/share/htrmopo" || -z "$(find "$HOME/.local/share/htrmopo" -name "*.mlmodel" 2>/dev/null)" ]]; then
        #             kraken get "$KRAKEN_FALLBACK_MODEL" || true
        #         fi
        #         local downloaded_model
        #         downloaded_model=$(find "$HOME/.local/share/htrmopo" -name "*.mlmodel" | head -n 1)
        #         if [[ -n "$downloaded_model" ]]; then
        #             model_arg=("-m" "$downloaded_model")
        #         else
        #             model_arg=("-m" "$KRAKEN_FALLBACK_MODEL")
        #         fi
        #     fi
        # 
        #     local total_tiffs=${#tiff_list[@]}
        #     local chunk_size=$(( (total_tiffs + PARALLEL_JOBS_GPU - 1) / PARALLEL_JOBS_GPU ))
        # 
        #     for ((i = 0; i < PARALLEL_JOBS_GPU; i++)); do
        #         local start_idx=$(( i * chunk_size ))
        #         if [[ $start_idx -ge $total_tiffs ]]; then continue; fi
        #         
        #         local chunk=("${tiff_list[@]:start_idx:chunk_size}")
        #         local kraken_args=()
        #         
        #         for tiff in "${chunk[@]}"; do
        #             local base; base="$(basename "${tiff%.*}")"
        #             local out_alto="${out_dir}/${base}.xml"
        #             local out_txt="${out_dir}/${base}.txt"
        #             if [[ ! -f "$out_txt" ]]; then
        #                 kraken_args+=("-i" "$tiff" "$out_alto")
        #             fi
        #         done
        #         
        #         if [[ ${#kraken_args[@]} -gt 0 ]]; then
        #             log_info "[KRAKEN] Lanzando Worker $((i+1))/$PARALLEL_JOBS_GPU con $((${#kraken_args[@]} / 2)) imágenes..."
        #             echo "[DEBUG KRAKEN CMD] kraken -a --device \"$KRAKEN_DEVICE\" \"${kraken_args[@]}\" segment -bl ocr \"${model_arg[@]}\"" >>"${LOG_DIR}/kraken_debug.log"
        #             kraken -a --device "$KRAKEN_DEVICE" "${kraken_args[@]}" segment -bl ocr "${model_arg[@]}" >>"${LOG_DIR}/kraken_errors.log" 2>&1 &
        #         fi
        #     done
        #     wait
        # 
        #     # Extraer texto de XMLs en paralelo rápido usando GNU parallel
        #     export out_dir
        #     export SCRIPT_DIR
        #     printf '%s\n' "${tiff_list[@]}" | \
        #         parallel --jobs "$PARALLEL_JOBS_CPU" "if [[ -f \"$out_dir/{/.}.xml\" && ! -f \"$out_dir/{/.}.txt\" ]]; then python3 \"\${SCRIPT_DIR}/_kraken_extractor.py\" \"$out_dir/{/.}.xml\" \"$out_dir/{/.}.txt\"; fi" || true
        # 
        #     log_ok "Kraken completado: $book_name"
        # fi

        # Consolidar resultados y generar PDF por libro (solo si hubo al menos un tiff)
        if [[ ${#tiff_list[@]} -gt 0 ]]; then
            consolidate_book "$book_name"
        fi
        PROCESSED_BOOKS+=("$book_name")
        log_ok "Libro finalizado: $book_name ✓"
    done
}

# (extract_kraken_text function removed)

# ─────────────────────────────────────────────────────────────────────────────
# 12. AYUDA / USO
# ─────────────────────────────────────────────────────────────────────────────

usage() {
    cat << EOF

  USO:
    $0 [opciones]

  ESTRUCTURA DE INPUT ESPERADA:
    input/
    ├── Libro_01/
    │   ├── pag_001.tiff
    │   └── pag_002.tiff
    └── Libro_02/
        └── pag_001.tiff

  OPCIONES:
    -i <dir>    Directorio de entrada  (default: ./input)
    -o <dir>    Directorio de salida   (default: ./output)
    -j <n>      Jobs paralelos CPU     (default: $PARALLEL_JOBS_CPU)
    -g <n>      Jobs paralelos GPU     (default: $PARALLEL_JOBS_GPU)
    --no-tesseract   Desactivar Tesseract
    --no-paddle      Desactivar PaddleOCR
    --no-kraken      Desactivar Kraken
    --cpu            Forzar CPU (sin CUDA)
    -h               Mostrar esta ayuda

  SALIDA GENERADA:
    output/<libro>/<libro>.pdf          → PDF por libro
    output/PDF_COMPILADO_<ts>.pdf       → PDF con TODOS los libros
    output/<libro>/consolidado/         → texto plano fusionado
    output/<libro>/tesseract/           → resultados Tesseract (txt,pdf,hocr)
    output/<libro>/paddleocr/           → texto + JSON con coordenadas
    output/<libro>/kraken/              → texto + ALTO XML

  EJEMPLOS:
    $0
    $0 -i /datos/libros -o /resultados
    $0 --no-kraken -j 4
    $0 --cpu --no-paddle

EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# 13. PARSEO DE ARGUMENTOS
# ─────────────────────────────────────────────────────────────────────────────

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -i) INPUT_DIR="$2";  shift 2 ;;
            -o) OUTPUT_DIR="$2"; shift 2 ;;
            -j) PARALLEL_JOBS_CPU="$2"; shift 2 ;;
            -g) PARALLEL_JOBS_GPU="$2"; shift 2 ;;
            --no-tesseract) USE_TESSERACT=0; shift ;;
            --no-paddle)    USE_PADDLEOCR=0; shift ;;
            --no-kraken)    USE_KRAKEN=0;    shift ;;
            --cpu)
                PADDLE_USE_GPU=false
                KRAKEN_DEVICE="cpu"
                shift ;;
            -h|--help) usage; exit 0 ;;
            *) log_error "Argumento desconocido: $1"; usage; exit 1 ;;
        esac
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

main() {
    parse_args "$@"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║   OCR/ICR · Libros Patrimoniales Colombia · v2.0.0          ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    setup_directories
    check_dependencies
    # _generate_paddle_runner
    # _generate_kraken_extractor

    local start_ts; start_ts=$(date +%s)

    run_pipeline
    # generate_report  # Desactivado: main.sh genera registro_libros_procesados.csv con datos completos

    local end_ts; end_ts=$(date +%s)
    local elapsed=$(( end_ts - start_ts ))
    log_ok "Tiempo total: $(( elapsed / 60 )) min $(( elapsed % 60 )) seg"
}

main "$@"
