#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   preprocess_validate.py
# VERSION:  2.0.0
# DESC:     Pre-procesamiento y validación de imágenes TIFF antes del OCR.
#
#   REGLAS (v2 — integridad del libro es prioridad):
#     1. Todas las imágenes DEBEN ser TIFF (.tif/.tiff)
#     2. DPI DEBE ser 300x300 → si alguna NO cumple, RECHAZAR LIBRO COMPLETO
#     3. Imágenes corruptas → RECHAZAR LIBRO COMPLETO
#     4. Peso > 300 MB → crear COPIA reducida en --work-dir (el original no se
#        toca; el DPI de la copia se ajusta para conservar el tamaño de página)
#        SOLO si el libro pasa validación de DPI y corrupción
#     5. Conteo TIFF ≠ páginas PDF → PDF inválido (post-verificación)
#
#   FILOSOFÍA v2:
#     - Un libro histórico no puede quedar incompleto NUNCA
#     - Si hay 1 sola imagen con DPI≠300 o corrupta, el libro entero se salta
#     - El libro queda intacto en INPUT para corrección manual
#     - Se genera _book_rejected.json con diagnóstico detallado
#
# USO:
#     python3 preprocess_validate.py <carpeta_libro>
#     python3 preprocess_validate.py <carpeta_libro> --dry-run
#     python3 preprocess_validate.py <carpeta_libro> --work-dir <carpeta_copias>
#
# SALIDA:
#     Código de salida 0 = libro válido para procesar (puede incluir suavizado)
#     Código de salida 1 = error crítico (carpeta vacía, sin TIFFs)
#     Código de salida 2 = libro RECHAZADO (DPI≠300 o corruptas) → SALTAR COMPLETO
#
#     Archivo <carpeta>/_book_rejected.json = diagnóstico detallado (si rechazado)
#     Archivo <carpeta>/_preprocess_report.txt = reporte completo
# =============================================================================

import os
import sys
import json
import math
import argparse
import time
from pathlib import Path
from datetime import datetime

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # Sin límite para imágenes enormes
except ImportError:
    print("ERROR: PIL/Pillow no disponible. Activar venv: source ocr_env/bin/activate")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

DPI_REQUIRED = 300
DPI_TOLERANCE = 5        # Margen de tolerancia para DPI (295-305 = ok)
SIZE_SMOOTH_MB = 300     # Suavizar imágenes > 300 MB
SIZE_SKIP_MB = 100       # No tocar imágenes < 100 MB
TARGET_MAX_MB = 150      # Objetivo después de suavizar: ~150 MB

COMPRESSION_NAMES = {
    1: 'None', 2: 'CCITT', 3: 'T4/G3', 4: 'T6/G4',
    5: 'LZW', 6: 'JPEG_old', 7: 'JPEG', 8: 'Deflate',
    32773: 'PackBits', 34712: 'JPEG2000',
}

# Colores ANSI
C_RED = '\033[0;31m'
C_GREEN = '\033[0;32m'
C_YELLOW = '\033[0;33m'
C_CYAN = '\033[0;36m'
C_MAGENTA = '\033[1;35m'
C_RESET = '\033[0m'
C_BOLD = '\033[1m'


# ─────────────────────────────────────────────────────────────────────────────
# FUNCIONES DE LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def log_ok(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{C_GREEN}[{ts}] [OK]    {msg}{C_RESET}")

def log_info(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{C_CYAN}[{ts}] [INFO]  {msg}{C_RESET}")

def log_warn(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{C_YELLOW}[{ts}] [WARN]  {msg}{C_RESET}")

def log_error(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{C_RED}[{ts}] [ERROR] {msg}{C_RESET}")

def log_step(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{C_MAGENTA}[{ts}] [STEP]  {msg}{C_RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE IMAGEN
# ─────────────────────────────────────────────────────────────────────────────

def analyze_image(filepath):
    """Analiza una imagen y retorna su información."""
    result = {
        'path': str(filepath),
        'name': filepath.name,
        'size_bytes': os.path.getsize(filepath),
        'size_mb': os.path.getsize(filepath) / (1024 * 1024),
        'is_tiff': filepath.suffix.lower() in ('.tif', '.tiff'),
        'width': 0,
        'height': 0,
        'dpi_x': 0,
        'dpi_y': 0,
        'dpi_ok': False,
        'mode': 'unknown',
        'bits_per_sample': 0,
        'compression': 'unknown',
        'can_open': False,
        'needs_smooth': False,
        'error': None,
    }

    if not result['is_tiff']:
        result['error'] = f"No es TIFF: extensión {filepath.suffix}"
        return result

    try:
        with Image.open(filepath) as img:
            result['can_open'] = True
            result['width'] = img.width
            result['height'] = img.height
            result['mode'] = img.mode

            # DPI
            dpi = img.info.get('dpi', (0, 0))
            if dpi and isinstance(dpi, tuple):
                result['dpi_x'] = int(round(dpi[0]))
                result['dpi_y'] = int(round(dpi[1]))

            # Verificar DPI con tolerancia
            dpi_ok_x = abs(result['dpi_x'] - DPI_REQUIRED) <= DPI_TOLERANCE
            dpi_ok_y = abs(result['dpi_y'] - DPI_REQUIRED) <= DPI_TOLERANCE
            result['dpi_ok'] = dpi_ok_x and dpi_ok_y

            # BYPASS MANUAL: si SKIP_DPI_CHECK=1, se ignora la validación de DPI
            # (se sigue validando corrupción y formato). Uso puntual para forzar
            # el procesamiento de un libro cuyo DPI está mal pero se necesita el PDF.
            if os.environ.get('SKIP_DPI_CHECK') == '1':
                result['dpi_ok'] = True

            # Compression
            tag_info = img.tag_v2 if hasattr(img, 'tag_v2') else {}
            if 259 in tag_info:
                comp_val = tag_info[259]
                if isinstance(comp_val, tuple):
                    comp_val = comp_val[0]
                result['compression'] = COMPRESSION_NAMES.get(comp_val, f'code_{comp_val}')

            # BitsPerSample
            if 258 in tag_info:
                bps = tag_info[258]
                if isinstance(bps, tuple):
                    result['bits_per_sample'] = bps[0]
                else:
                    result['bits_per_sample'] = bps

            # ¿Necesita suavizado?
            result['needs_smooth'] = result['size_mb'] > SIZE_SMOOTH_MB

    except Exception as e:
        result['error'] = f"No se puede abrir: {str(e)}"
        result['can_open'] = False

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SUAVIZADO DE IMAGEN (>300MB)
# ─────────────────────────────────────────────────────────────────────────────

def smooth_image(filepath, info, work_dir, dry_run=False):
    """
    Crea una COPIA reducida (~150MB) de una imagen >300MB en work_dir.
    El TIFF original NUNCA se modifica.

    Estrategia:
    - Calcular las dimensiones objetivo para que el archivo no
      supere TARGET_MAX_MB como TIFF sin comprimir (W×H×3 bytes para RGB).
    - Redimensionar proporcionalmente.
    - Bajar el DPI en la misma proporción (DPI × escala), para que la página
      conserve su tamaño físico real en el PDF.
    - Guardar como TIFF sin compresión en work_dir con el mismo nombre.
    """
    current_mb = info['size_mb']
    width = info['width']
    height = info['height']
    mode = info['mode']

    if width == 0 or height == 0:
        log_error(f"  No se puede suavizar {info['name']}: dimensiones {width}x{height}")
        return False

    # Canales de color
    channels = 3  # RGB
    if mode == 'RGBA':
        channels = 3  # Convertiremos a RGB
    elif mode == 'L':
        channels = 1
    elif mode == 'CMYK':
        channels = 3  # Convertiremos a RGB

    # Tamaño en bytes como TIFF sin comprimir = W × H × channels
    current_raw_bytes = width * height * channels
    target_bytes = TARGET_MAX_MB * 1024 * 1024

    # Factor de escala necesario
    scale = math.sqrt(target_bytes / current_raw_bytes)
    
    if scale >= 1.0:
        scale = 1.0

    new_width = int(width * scale)
    new_height = int(height * scale)
    new_size_est_mb = (new_width * new_height * channels) / (1024 * 1024)

    # DPI proporcional: mismo tamaño físico de página con menos píxeles
    new_dpi_x = (info['dpi_x'] or DPI_REQUIRED) * scale
    new_dpi_y = (info['dpi_y'] or DPI_REQUIRED) * scale

    out_path = Path(work_dir) / filepath.name

    log_info(f"  Reduciendo copia: {info['name']}")
    log_info(f"    Original:  {width}x{height} | {current_mb:.1f} MB | {mode} (no se modifica)")
    log_info(f"    Copia:     {new_width}x{new_height} | ~{new_size_est_mb:.1f} MB | RGB | {new_dpi_x:.0f} DPI")
    log_info(f"    Escala:    {scale:.3f}")

    if dry_run:
        log_info(f"    [DRY-RUN] No se creará la copia.")
        return True

    if out_path.exists():
        log_ok(f"    Copia reducida ya existe, se reutiliza: {out_path}")
        return True

    try:
        with Image.open(filepath) as img:
            # Convertir RGBA → RGB (flatten sobre fondo blanco)
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            elif img.mode == 'CMYK':
                img = img.convert('RGB')
            elif img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')

            # Redimensionar si es necesario
            if scale < 1.0:
                img = img.resize((new_width, new_height), Image.LANCZOS)

            # Guardar la copia en work_dir: primero como .tmp y luego renombrar,
            # para que una copia a medias nunca se confunda con una terminada
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_name(out_path.name + '.tmp')
            img.save(
                str(tmp_path),
                format='TIFF',
                compression='raw',  # Sin compresión
                dpi=(new_dpi_x, new_dpi_y),
            )
            os.replace(tmp_path, out_path)

        # Verificar resultado
        new_size_mb = os.path.getsize(out_path) / (1024 * 1024)
        log_ok(f"    Copia reducida creada: {new_size_mb:.1f} MB (original: {current_mb:.1f} MB intacto)")
        return True

    except Exception as e:
        log_error(f"    Error al reducir {info['name']}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# GENERAR REPORTE DE RECHAZO (JSON)
# ─────────────────────────────────────────────────────────────────────────────

def generate_rejection_report(book_path, book_name, results, problems_dpi, problems_corrupt):
    """
    Genera _book_rejected.json con diagnóstico detallado de las imágenes
    problemáticas para que el operador sepa exactamente qué corregir.
    """
    problems = []

    for r in problems_dpi:
        problems.append({
            "file": r['name'],
            "type": "DPI_INCORRECTO",
            "detail": f"DPI={r['dpi_x']}x{r['dpi_y']} (requiere {DPI_REQUIRED}x{DPI_REQUIRED}, tolerancia ±{DPI_TOLERANCE})",
            "size_mb": round(r['size_mb'], 1),
            "dimensions": f"{r['width']}x{r['height']}",
            "compression": r['compression'],
            "mode": r['mode'],
        })

    for r in problems_corrupt:
        problems.append({
            "file": r['name'],
            "type": "CORRUPTA",
            "detail": r.get('error', 'No se puede abrir'),
            "size_mb": round(r['size_mb'], 1),
        })

    report = {
        "book": book_name,
        "status": "REJECTED",
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "total_images": len(results),
        "images_dpi_incorrecto": len(problems_dpi),
        "images_corruptas": len(problems_corrupt),
        "images_validas": len(results) - len(problems_dpi) - len(problems_corrupt),
        "problems": problems,
        "action": "Libro saltado completamente. Corregir las imágenes listadas y re-procesar el libro.",
    }

    rejected_file = book_path / "_book_rejected.json"
    with open(rejected_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    log_error(f"  📋 Reporte de rechazo generado: {rejected_file.name}")
    return report


# ─────────────────────────────────────────────────────────────────────────────
# VALIDACIÓN DE LIBRO COMPLETO
# ─────────────────────────────────────────────────────────────────────────────

def validate_book(book_dir, dry_run=False, fix_mode=False, work_dir=None):
    """
    Valida y pre-procesa todas las imágenes de un libro.
    
    Filosofía v2: Si CUALQUIER imagen tiene DPI≠300 o está corrupta,
    el libro COMPLETO se rechaza. No se procesa parcialmente.
    
    Retorna:
        (exit_code, stats_dict)
        exit_code: 0=OK (libro válido), 1=error crítico, 2=RECHAZADO (problemas en imágenes)
    """
    book_path = Path(book_dir)
    book_name = book_path.name

    print()
    log_step(f"╔═══════════════════════════════════════════════════════════╗")
    log_step(f"║  PRE-PROCESAMIENTO: {book_name:<37} ║")
    log_step(f"╚═══════════════════════════════════════════════════════════╝")

    # Buscar TODOS los archivos (no solo TIFF, para detectar errores)
    all_files = sorted([
        f for f in book_path.iterdir()
        if f.is_file() and not f.name.startswith('_')
    ])

    if not all_files:
        log_error(f"Carpeta vacía: {book_dir}")
        return 1, {}

    # ── PASO 1: Verificar que todos sean TIFF ──
    log_step("  [1/4] Verificando formato de archivos...")
    non_tiff = []
    tiff_files = []
    for f in all_files:
        if f.suffix.lower() in ('.tif', '.tiff'):
            tiff_files.append(f)
        else:
            non_tiff.append(f)

    if non_tiff:
        log_error(f"  ❌ {len(non_tiff)} archivo(s) NO son TIFF:")
        for f in non_tiff:
            log_error(f"     → {f.name} ({f.suffix}) — {f.stat().st_size / 1048576:.1f} MB")
    
    if not tiff_files:
        log_error(f"  No hay archivos TIFF en {book_name}")
        return 1, {}

    log_ok(f"  {len(tiff_files)} archivos TIFF encontrados" + 
           (f" ({len(non_tiff)} no-TIFF detectados)" if non_tiff else ""))

    # ── PASO 2: Analizar cada imagen ──
    log_step("  [2/4] Analizando imágenes...")
    
    results = []
    for tiff in tiff_files:
        info = analyze_image(tiff)
        results.append(info)

    # Clasificar
    ok_images = []           # DPI ok, tamaño ok
    excluded_dpi = []        # DPI incorrecto → RECHAZAR LIBRO
    needs_smooth = []        # >300MB → suavizar (si el libro es válido)
    corrupt_images = []      # No se pueden abrir → RECHAZAR LIBRO

    for r in results:
        if not r['can_open']:
            corrupt_images.append(r)
        elif not r['dpi_ok']:
            excluded_dpi.append(r)
        elif r['needs_smooth']:
            needs_smooth.append(r)
        else:
            ok_images.append(r)

    # Reporte de análisis
    log_info(f"  ─── Resultados del análisis ───")
    log_ok(f"  ✅ Imágenes válidas       : {len(ok_images)}")
    
    if needs_smooth:
        log_info(f"  🔧 Requieren suavizado (>{SIZE_SMOOTH_MB}MB): {len(needs_smooth)}")
        for r in needs_smooth:
            log_info(f"     → {r['name']}: {r['size_mb']:.1f} MB | {r['width']}x{r['height']}")

    if excluded_dpi:
        log_error(f"  ⛔ DPI INCORRECTO (≠300): {len(excluded_dpi)} imagen(es)")
        for r in excluded_dpi:
            log_error(f"     → {r['name']}: DPI={r['dpi_x']}x{r['dpi_y']} | {r['size_mb']:.1f} MB")

    if corrupt_images:
        log_error(f"  💀 CORRUPTAS (no se abren) : {len(corrupt_images)}")
        for r in corrupt_images:
            log_error(f"     → {r['name']}: {r['size_mb']:.1f} MB | {r['error']}")

    # ══════════════════════════════════════════════════════════════════════════
    # DECISIÓN: ¿El libro tiene problemas? → RECHAZAR COMPLETO
    # ══════════════════════════════════════════════════════════════════════════
    
    has_problems = len(excluded_dpi) > 0 or len(corrupt_images) > 0

    if has_problems:
        print()
        log_error(f"  ╔═══════════════════════════════════════════════════════╗")
        log_error(f"  ║  🚫 LIBRO RECHAZADO: {book_name:<35} ║")
        log_error(f"  ╚═══════════════════════════════════════════════════════╝")
        log_error(f"  Razón: {len(excluded_dpi)} imagen(es) con DPI≠300, "
                  f"{len(corrupt_images)} imagen(es) corrupta(s)")
        log_error(f"  El libro queda INTACTO en INPUT para corrección manual.")
        log_error(f"  NO se procesará ninguna imagen de este libro.")
        
        # Generar reporte de rechazo detallado
        report = generate_rejection_report(
            book_path, book_name, results, excluded_dpi, corrupt_images
        )

        # Mostrar resumen de imágenes a corregir
        print()
        log_error(f"  ── Imágenes que requieren corrección: ──")
        for p in report['problems']:
            emoji = "📐" if p['type'] == 'DPI_INCORRECTO' else "💀"
            log_error(f"     {emoji} {p['file']}: {p['type']} — {p['detail']}")

        # Limpiar archivos de control antiguos si existen
        for old_file in ['_excluded_dpi.txt', '_corrupt_images.txt']:
            old_path = book_path / old_file
            if old_path.exists():
                old_path.unlink()

        # Estadísticas
        stats = {
            'book_name': book_name,
            'total_tiff': len(tiff_files),
            'ok': len(ok_images),
            'excluded_dpi': len(excluded_dpi),
            'smoothed': 0,
            'smooth_failed': 0,
            'corrupt': len(corrupt_images),
            'non_tiff': len(non_tiff),
            'processable': 0,  # CERO — libro rechazado
            'rejected': True,
            'rejection_report': report,
        }

        return 2, stats

    # ══════════════════════════════════════════════════════════════════════════
    # LIBRO VÁLIDO — Proceder con suavizado si es necesario
    # ══════════════════════════════════════════════════════════════════════════

    # Limpiar archivo de rechazo anterior si el libro ahora es válido
    rejected_file = book_path / "_book_rejected.json"
    if rejected_file.exists():
        rejected_file.unlink()
        log_info(f"  Reporte de rechazo anterior eliminado (libro ahora válido).")

    # Limpiar archivos de control antiguos
    for old_file in ['_excluded_dpi.txt', '_corrupt_images.txt']:
        old_path = book_path / old_file
        if old_path.exists():
            old_path.unlink()

    # ── PASO 3: Copias reducidas de imágenes >300MB (originales intactos) ──
    smoothed_count = 0
    smooth_failed = 0
    if needs_smooth and work_dir is None:
        log_warn(f"  [3/4] {len(needs_smooth)} imagen(es) >{SIZE_SMOOTH_MB}MB, pero sin --work-dir no se reducen (se usará el original).")
    elif needs_smooth:
        log_step(f"  [3/4] Creando copia reducida de {len(needs_smooth)} imagen(es) grandes en {work_dir}...")
        for r in needs_smooth:
            filepath = Path(r['path'])
            if smooth_image(filepath, r, work_dir, dry_run=dry_run):
                smoothed_count += 1
            else:
                smooth_failed += 1
    else:
        log_ok("  [3/4] Sin imágenes que suavizar.")

    # ── PASO 4: Generar reporte ──
    log_step("  [4/4] Generando reportes...")

    # Reporte completo
    report_file = book_path / "_preprocess_report.txt"
    with open(report_file, 'w') as f:
        f.write(f"PREPROCESS REPORT — {book_name}\n")
        f.write(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*60}\n\n")
        f.write(f"Estado            : VÁLIDO ✅\n")
        f.write(f"Total archivos    : {len(all_files)}\n")
        f.write(f"Archivos TIFF     : {len(tiff_files)}\n")
        f.write(f"No-TIFF           : {len(non_tiff)}\n")
        f.write(f"Válidas (OK)      : {len(ok_images)}\n")
        f.write(f"Suavizadas        : {smoothed_count}\n")
        f.write(f"Fallo suavizado   : {smooth_failed}\n\n")

        if needs_smooth:
            f.write("SUAVIZADAS (>300MB):\n")
            for r in needs_smooth:
                f.write(f"  {r['name']}: {r['size_mb']:.1f}MB | {r['width']}x{r['height']}\n")
            f.write("\n")

        if non_tiff:
            f.write("ARCHIVOS NO-TIFF:\n")
            for ft in non_tiff:
                f.write(f"  {ft.name}\n")

    # ── Resumen final ──
    total_processable = len(ok_images) + smoothed_count

    print()
    log_step(f"  ──── RESUMEN: {book_name} ────")
    log_ok(f"  📋 Total TIFF            : {len(tiff_files)}")
    log_ok(f"  ✅ Procesables           : {total_processable}")
    if smoothed_count > 0:
        log_ok(f"  🔧 Suavizadas exitosamente: {smoothed_count}")
    if smooth_failed > 0:
        log_warn(f"  ⚠ Fallo suavizado        : {smooth_failed}")
    if non_tiff:
        log_warn(f"  ❌ No-TIFF              : {len(non_tiff)}")

    # Estadísticas
    stats = {
        'book_name': book_name,
        'total_tiff': len(tiff_files),
        'ok': len(ok_images),
        'excluded_dpi': 0,
        'smoothed': smoothed_count,
        'smooth_failed': smooth_failed,
        'corrupt': 0,
        'non_tiff': len(non_tiff),
        'processable': total_processable,
        'rejected': False,
    }

    return 0, stats


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICACIÓN PDF vs TIFF COUNT (post-OCR)
# ─────────────────────────────────────────────────────────────────────────────

def verify_pdf_page_count(pdf_path, tiff_dir):
    """
    Verifica que el número de páginas del PDF coincida con el número de TIFFs.
    v2: Ya no descuenta imágenes excluidas — TODAS las imágenes deben estar en el PDF.
    
    Retorna: (is_valid, n_tiff, n_pdf_pages, message)
    """
    from pathlib import Path
    import subprocess

    tiff_path = Path(tiff_dir)
    tiff_count = len([
        f for f in tiff_path.iterdir()
        if f.suffix.lower() in ('.tif', '.tiff') and not f.name.startswith('_')
    ])

    # v2: Ya NO descontamos excluidas — todas deben estar procesadas
    processable_tiff = tiff_count

    # Contar páginas del PDF
    try:
        result = subprocess.run(
            ['pdfinfo', str(pdf_path)],
            capture_output=True, text=True, timeout=30
        )
        pdf_pages = 0
        for line in result.stdout.splitlines():
            if line.lower().startswith('pages:'):
                pdf_pages = int(line.split(':')[1].strip())
                break
    except Exception as e:
        return False, processable_tiff, 0, f"Error leyendo PDF: {e}"

    if processable_tiff == pdf_pages:
        return True, processable_tiff, pdf_pages, "OK"
    else:
        return False, processable_tiff, pdf_pages, \
            f"INCONSISTENCIA: {processable_tiff} TIFFs ≠ {pdf_pages} páginas PDF"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Pre-procesamiento y validación de imágenes TIFF para OCR (v2 — integridad del libro)'
    )
    parser.add_argument(
        'book_dir',
        help='Carpeta del libro a validar (contiene TIFFs)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Solo analizar, no modificar archivos'
    )
    parser.add_argument(
        '--fix',
        action='store_true',
        help='Aplicar correcciones (suavizado de imágenes >300MB)'
    )
    parser.add_argument(
        '--verify-pdf',
        help='Verificar conteo de páginas PDF vs TIFFs (ruta al PDF)'
    )
    parser.add_argument(
        '--work-dir',
        help='Carpeta donde guardar las copias reducidas de imágenes >300MB (los originales no se tocan)'
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Salida mínima (solo errores)'
    )

    args = parser.parse_args()

    book_dir = Path(args.book_dir)
    if not book_dir.is_dir():
        log_error(f"Directorio no encontrado: {args.book_dir}")
        sys.exit(1)

    # Modo por defecto: fix (suavizar si es necesario)
    dry_run = args.dry_run

    start_time = time.time()

    # Validar y pre-procesar
    exit_code, stats = validate_book(str(book_dir), dry_run=dry_run, work_dir=args.work_dir)

    elapsed = time.time() - start_time
    log_info(f"  ⏱ Tiempo: {elapsed:.1f} seg")

    # Verificación PDF si se solicitó
    if args.verify_pdf:
        pdf_path = Path(args.verify_pdf)
        if pdf_path.exists():
            is_valid, n_tiff, n_pdf, msg = verify_pdf_page_count(str(pdf_path), str(book_dir))
            if is_valid:
                log_ok(f"  ✅ PDF válido: {n_tiff} TIFFs = {n_pdf} páginas PDF")
            else:
                log_error(f"  ❌ PDF INVÁLIDO: {msg}")
                log_error(f"  El PDF debe ser regenerado.")
                sys.exit(3)
        else:
            log_warn(f"  PDF no encontrado: {args.verify_pdf}")

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
