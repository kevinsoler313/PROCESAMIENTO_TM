# Procesamiento OCR de libros patrimoniales

Convierte libros escaneados (una carpeta de TIFF por libro) en un PDF/A-3B
(PDF 1.7) por libro, con texto OCR, marca de agua y metadatos, y deja un
registro con SHA-256 de cada PDF entregado.

Está pensado para Linux (probado en Ubuntu 26.04 y en WSL).

## Flujo

```
INPUT/<libro>/*.tif
  └─ main.sh
       1. Quita " OK" del nombre de la carpeta
       2. OCR.sh (vía run_ocr.sh)
            · preprocess_validate.py: DPI = 300 y sin imágenes corruptas,
              copia reducida de imágenes > 300 MB (el original no se toca)
            · Tesseract por página, en paralelo
       3. Marca de agua (logo + texto)
       4. PDF/A-3B con ocrmypdf, cabecera PDF 1.7
       5. Metadatos con exiftool desde el CSV
       6. Verifica: páginas del PDF == número de TIFF
       7. Mueve el PDF a OUTPUT, lo registra y recién entonces borra los TIFF
```

## Carpetas de datos (`/mnt/datos`)

| Carpeta | Contenido |
|---|---|
| `INPUT/` | Un subdirectorio de TIFF por libro |
| `OUTPUT/` | Solo PDFs finalizados y verificados |
| `TEMPORALES/` | Intermedios de cada libro; la carpeta del libro es también su candado |
| `REVIEW/` | Libros rechazados (DPI, corruptos, páginas que no coinciden) y `review_log.csv` |
| `REGISTRO/` | `registro_libros_procesados.csv` con páginas, pesos, estado y SHA-256 |
| `PNG/` | PNG por página, solo con `--png` |

## Instalación

```bash
sudo apt install -y ocrmypdf libimage-exiftool-perl poppler-utils \
    tesseract-ocr tesseract-ocr-spa ghostscript parallel python3-venv
python3 -m venv ocr_env
ocr_env/bin/pip install -r requirements.txt
sudo mkdir -p /mnt/datos && sudo chown "$USER" /mnt/datos
```

## Archivos que no están en el repositorio

Son datos del cliente y se seleccionan antes de procesar. Por ahora deben
estar junto a `main.sh` con estos nombres:

- `TechnoMart Funza - TEMP.csv`: metadatos por libro, sin encabezado:
  `codigo,fondo,subfondo,equipo,resolucion`
- `logo.png`: logo de la marca de agua

## Uso

```bash
bash main.sh                          # todos los libros de INPUT
bash main.sh --only-book AR10582      # un solo libro
bash main.sh --jobs 2                 # post-proceso de 2 libros en paralelo
bash main.sh --skip-ocr               # sin OCR (PDF del OCR ya en TEMPORALES)
bash main.sh --only-book AR10582 --png
```

Al final se muestra un resumen: exitosos, sin metadatos, en REVIEW, por
reintentar y omitidos. El detalle queda en `logs/`.

## Qué pasa si algo falla

- **Ctrl+C, kill o caída:** se borran los temporales del libro y este queda en
  INPUT; se reprocesa en la siguiente ejecución.
- **El OCR no genera el PDF:** se reintenta una vez; si vuelve a fallar, el
  libro queda en INPUT como "por reintentar".
- **Imágenes con DPI ≠ 300 o corruptas, o páginas que no coinciden:** el libro
  va a `REVIEW/` con el motivo en `review_log.csv`.
- **Libro sin entrada en el CSV:** el PDF se entrega igual y se registra como
  `SIN_META`.
