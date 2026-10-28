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

## Carpetas de datos (por defecto en `/mnt/datos`)

| Carpeta | Contenido |
|---|---|
| `INPUT/` | Un subdirectorio de TIFF por libro |
| `OUTPUT/` | Solo PDFs finalizados y verificados |
| `TEMPORALES/` | Intermedios de cada libro; la carpeta del libro es también su candado |
| `REVIEW/` | Libros rechazados (DPI, corruptos, páginas que no coinciden) y `review_log.csv` |
| `REGISTRO/` | `registro_libros_procesados.csv` con páginas, pesos, estado y SHA-256 |
| `PNG/` | PNG por página, solo si se exportan PNG (opcional) |

En la interfaz se eligen INPUT, OUTPUT, REVIEW y PNG. TEMPORALES y REGISTRO se
crean junto a OUTPUT, para que TEMPORALES quede en su mismo disco.

## Instalación

```bash
sudo apt install -y ocrmypdf libimage-exiftool-perl poppler-utils \
    tesseract-ocr tesseract-ocr-spa ghostscript parallel python3-venv \
    libxcb-cursor0
sudo mkdir -p /mnt/datos && sudo chown "$USER" /mnt/datos
bash instalar_interfaz.sh     # crea ocr_env, instala requirements.txt y el lanzador
```

`instalar_interfaz.sh` deja "Procesamiento OCR", con su ícono, en el menú de
aplicaciones y en el escritorio.

## Interfaz de escritorio

`interfaz/app.py` (PySide6) prepara y lanza `main.sh`:

1. **Configuración:** CSV de metadatos, logo y texto de la marca de agua,
   carpetas de trabajo y datos del equipo (RAM, hilos y gráfica; el botón
   "Detectar este equipo" los llena). Con ellos calcula la combinación más
   rápida de procesos de OCR y libros en paralelo, que se puede ajustar a mano.
2. **Libros a procesar:** los libros de INPUT con páginas, tamaño y si están
   en el CSV; se marcan los que se quieren procesar. Exportar PNG es opcional.
3. **En proceso:** avance por libro, CPU, RAM, disco y log en vivo. "Detener"
   equivale a Ctrl+C.
4. **Resultados:** exitosos, sin metadatos, en REVIEW y por reintentar.

Tiene modo claro y oscuro, y recuerda la última configuración.

Los textos de marca de agua permitidos están en `TEXTOS_MARCA`, al inicio de
`interfaz/app.py`; no se editan desde la interfaz.

## Archivos que no están en el repositorio

Son datos del cliente y se eligen en la interfaz (o con `--csv` y `--logo`):

- CSV de metadatos por libro, sin encabezado:
  `codigo,fondo,subfondo,equipo,resolucion`
- Logo de la marca de agua

Sin esas opciones, `main.sh` los busca junto a sí mismo como
`TechnoMart Funza - TEMP.csv` y `logo.png`.

## Uso desde la terminal

```bash
bash main.sh                          # todos los libros de INPUT
bash main.sh --only-book AR10582      # un solo libro
bash main.sh --book AR10582 --book AR10590   # varios libros
bash main.sh --jobs 2 --ocr-jobs 12   # 2 libros en post-proceso, 12 Tesseract
bash main.sh --skip-ocr               # sin OCR (PDF del OCR ya en TEMPORALES)
bash main.sh --only-book AR10582 --png
bash main.sh --csv meta.csv --logo logo.png --input-dir /datos/IN --output-dir /datos/OUT
```

Todas las opciones: `bash main.sh -h`.

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
