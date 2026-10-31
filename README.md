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
       7. Mueve el PDF a OUTPUT (o a OUTPUT/SIN METADATOS si el libro no
          está en el CSV), lo registra y recién entonces borra los TIFF
```

## Carpetas de datos (por defecto en `/mnt/datos`)

| Carpeta | Contenido |
|---|---|
| `INPUT/` | Un subdirectorio de TIFF por libro |
| `OUTPUT/` | Solo PDFs finalizados, verificados y con metadatos |
| `OUTPUT/SIN METADATOS/` | PDFs de libros que no estaban en el CSV; se completan con `inyectar_metadatos.sh` |
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

`interfaz/app.py` (PySide6) prepara y lanza `main.sh`. Las secciones están en
una barra lateral:

- **Procesamiento**, con pestañas:
  - **PROCESAR**
    1. CSV de metadatos y resumen del rendimiento que se usará.
    2. Libros de INPUT con páginas, tamaño y si están en el CSV; se marcan los
       que se quieren procesar. Un buscador (empieza con, contiene, termina
       con) filtra la lista, p. ej. "AHFCPC", y "Solo estos" marca solo los que
       coinciden. Exportar PNG es opcional.
    3. En proceso: avance por libro, CPU, RAM, disco y log en vivo. "Detener"
       equivale a Ctrl+C.
    4. Resultados: exitosos, sin metadatos, en REVIEW y por reintentar.
  - **MARCAS DE AGUA:** logo, texto al pie y marca diagonal opcional
    («CÁMARA DE REPRESENTANTES»), cada uno con posición, transparencia y tamaño
    propios, y una vista previa que usa el mismo código que estampa los PDF.
  - **CARPETAS DE TRABAJO:** INPUT, OUTPUT, REVIEW y PNG.
  - **COMPONENTES:** RAM, hilos y gráfica del equipo ("Detectar este equipo"
    los llena). Con ellos calcula la combinación más rápida de procesos de OCR
    y libros en paralelo, que se puede ajustar a mano.
- **Inyectar metadatos:** lista los PDF de `OUTPUT/SIN METADATOS` y, con un CSV
  que puede ser distinto al del procesamiento, les escribe los metadatos a los
  que estén, los pasa a OUTPUT y actualiza el registro a `OK`; avisa cuáles
  siguen sin aparecer. Usa las carpetas de Procesamiento › CARPETAS DE TRABAJO.
- **Crear contenedores GDOC:** en construcción.

Tiene modo claro y oscuro, y recuerda la última sección, pestaña y configuración.

Los textos de marca de agua permitidos están en `TEXTOS_MARCA` y
`MARCA_DIAGONAL`, al inicio de `interfaz/app.py`; no se editan desde la
interfaz.

## Marca de agua

`marca_agua.py` estampa la marca (lo llama `main.sh` en el paso 3). Sin
`--marca-config` usa los valores de siempre:

| Elemento | Posición | Opacidad | Tamaño |
|---|---|---|---|
| Logo | arriba-izquierda | 25 % | 20 % del ancho de página |
| Texto al pie | abajo-izquierda | 20 % | 12 pt (se reduce si no cabe) |
| Diagonal (opcional) | centro | 12 % | 80 % de la diagonal |

El formato del JSON de `--marca-config` está en la cabecera de `marca_agua.py`.

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
bash main.sh --marca-diagonal "CÁMARA DE REPRESENTANTES" --marca-config marca.json
```

Todas las opciones: `bash main.sh -h`.

Para los PDF que quedaron en `OUTPUT/SIN METADATOS`:

```bash
bash inyectar_metadatos.sh --csv otro.csv                      # los que estén en otro.csv
bash inyectar_metadatos.sh --csv otro.csv --book AR10611       # solo ese libro
```

`metadatos.sh` reúne la lectura del CSV y la escritura de metadatos; lo usan
`main.sh` e `inyectar_metadatos.sh`, así que ambos escriben los mismos campos.

Al final se muestra un resumen: exitosos, sin metadatos, en REVIEW, por
reintentar y omitidos. El detalle queda en `logs/`.

## Qué pasa si algo falla

- **Ctrl+C, kill o caída:** se borran los temporales del libro y este queda en
  INPUT; se reprocesa en la siguiente ejecución.
- **El OCR no genera el PDF:** se reintenta una vez; si vuelve a fallar, el
  libro queda en INPUT como "por reintentar".
- **Imágenes con DPI ≠ 300 o corruptas, o páginas que no coinciden:** el libro
  va a `REVIEW/` con el motivo en `review_log.csv`.
- **Libro sin entrada en el CSV:** el PDF se entrega igual en
  `OUTPUT/SIN METADATOS` y se registra como `SIN_META`, hasta que se le
  inyecten los metadatos.
