# Procesamiento OCR de libros patrimoniales

Convierte libros escaneados (una carpeta de TIFF por libro) en un PDF/A-3B
(PDF 1.7) por libro, con texto OCR, marca de agua y metadatos, y deja un
registro con SHA-256 de cada PDF entregado.

Está pensado para Linux (probado en Ubuntu 26.04 y en WSL).

## Flujo

```
INPUT/<libro>/*.tif
  └─ main.sh
       1. Quita " OK" del nombre de la carpeta (solo al empezar ese libro)
       2. OCR.sh (vía run_ocr.sh)
            · preprocess_validate.py: DPI = 300 y sin imágenes corruptas,
              copia reducida de imágenes > 300 MB (el original no se toca)
            · Tesseract por página, en paralelo
       3. Marca de agua (logo + texto)
       4. PDF/A-3B con ocrmypdf, cabecera PDF 1.7
       5. Metadatos desde el CSV (pikepdf: Info y XMP iguales, PDF/A válido)
       6. Verifica: páginas del PDF == número de TIFF
       7. Mueve el PDF a OUTPUT (o a OUTPUT/SIN METADATOS si el libro no
          está en el CSV), lo registra y recién entonces borra los TIFF
```

## Libros fragmentados (subcarpetas)

Algunas sedes entregan el libro partido en subcarpetas, con las TIFF un nivel
más abajo:

```
K-1394/
├── ps21_gacetadecund_bog_nov_2013 OK/   …-0000.tif … -0238.tif
└── ps21_gacetadecund_bog_dic_2013 OK/   …-0000.tif … -0450.tif
```

En PROCESAR se marca **Libros fragmentados en subcarpetas** y aparece la
segunda opción:

- **Un solo PDF por libro:** todas las partes, en orden, en `K-1394.pdf`.
- **Un PDF por cada fragmento:** un PDF por subcarpeta, con su nombre tal cual
  (sin « OK»): `ps21_gacetadecund_bog_nov_2013.pdf`. Cada uno cuenta como un
  libro: su fila en los registros, en el registro OCR-ICR y en GDOC.

Sin la casilla, un libro con subcarpetas no se puede seleccionar en la
interfaz (desde la terminal va a REVIEW). `--fragmentados unico|fragmentos` en
`main.sh`.

**Orden** (`libros.py`). Se decide una sola vez, antes del OCR, y la unión de
páginas lo sigue; procesar en paralelo no lo cambia. En la lista de libros se
ve el orden detectado (y el detalle al pasar el mouse).

1. Partes con mes y año en el nombre (`nov_2013`, `dic 2013`, `2013-nov`):
   por fecha. Por nombre quedaría `dic` antes que `nov`.
2. Partes que solo difieren en un número (`01`, `02`, `10`; `Tomo 1`,
   `Tomo 2`): por ese número.
3. Cualquier otra cosa no se adivina y el libro no se procesa: mes sin año
   (`jul`, `ago`: no se sabe si enero va antes o después de diciembre),
   mezclas, dos partes con la misma fecha, nombres sin fecha ni número.

Dentro de cada parte, las TIFF van en orden natural por nombre (`Hoja 2` antes
que `Hoja 10`). Da igual si cada parte vuelve a empezar en `0000` o sigue el
consecutivo de la anterior.

**Páginas faltantes.** Si los nombres terminan en número, no puede faltar ni
repetirse ninguno, y cada parte debe empezar en 0000/0001 o en el número
siguiente al de la parte anterior. Si no, el libro va a REVIEW con el número
que falta: el conteo PDF = TIFF no lo detectaría.

**Todo o nada.** Si una parte falla (DPI, páginas sin OCR, conteo), el libro
entero va a REVIEW y ningún PDF suyo llega a OUTPUT. Los TIFF se borran solo
cuando todos sus PDF están en OUTPUT y registrados.

**Metadatos.** El PDF de un fragmento busca su nombre en el CSV y, si no está,
usa la fila del libro. En las plantillas de METADATOS, `{nombre}` es el nombre
del PDF tal cual (el del fragmento o el del libro); p. ej., Título = `{nombre}`.

## Carpetas de datos

Cada equipo las tiene donde quiera y las elige en la interfaz. `/mnt/datos` es
solo la sugerencia del botón "Usar /mnt/datos" y el valor por defecto de
`main.sh` desde la terminal.

| Carpeta | Contenido |
|---|---|
| `INPUT/` | Un subdirectorio de TIFF por libro |
| `OUTPUT/` | Solo PDFs finalizados, verificados y con metadatos |
| `OUTPUT/SIN METADATOS/` | PDFs de libros que no estaban en el CSV; se completan con `inyectar_metadatos.sh` |
| `TEMPORALES/` | Intermedios de cada libro; la carpeta del libro es también su candado |
| `REVIEW/` | Libros rechazados (DPI, corruptos, páginas que no coinciden) y `review_log.csv` |
| `REGISTRO/` | `registro_libros_procesados.csv` con páginas, pesos, estado y SHA-256, y el registro OCR-ICR de cada día |
| `PNG/` | PNG por página, solo si se exportan PNG (opcional) |

En la interfaz se eligen INPUT, OUTPUT, REVIEW y PNG. TEMPORALES y REGISTRO se
crean junto a OUTPUT, para que TEMPORALES quede en su mismo disco.

## Instalación

En un equipo nuevo (Ubuntu, Linux Mint o WSL):

```bash
git clone https://github.com/kevinsoler313/PROCESAMIENTO_TM.git
cd PROCESAMIENTO_TM && bash instalar.sh
```

`instalar.sh` hace tres cosas:

1. Instala los paquetes del sistema que falten (pide la contraseña una vez):
   git, ocrmypdf, exiftool, poppler-utils, Tesseract con español,
   Ghostscript, GNU parallel, python3-venv y libxcb-cursor0.
2. Corre `instalar_interfaz.sh`, que crea `ocr_env` con `requirements.txt` y
   deja "Procesamiento OCR", con su ícono, en el menú de aplicaciones y en el
   escritorio.
3. Verifica que cada herramienta responda y muestra ✅ o ❌.

Se puede volver a correr sin problema. No crea carpetas de datos: INPUT,
OUTPUT, REVIEW y PNG se eligen en la interfaz.

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
  - **METADATOS:** muestra las columnas del CSV elegido y sus primeras filas.
    Se indica si la primera fila es encabezado y qué columna es el código del
    libro. Cada campo del PDF (Título, Asunto, Autor, Palabras clave, Creador,
    Productor y Descripción) es texto libre con columnas `{N}`; «+ Columna» las
    inserta, y debajo se ve el resultado con la primera fila. Vale también para
    Inyectar metadatos.
  - **MARCAS DE AGUA:** logo, texto al pie y marca diagonal
    («CÁMARA DE REPRESENTANTES»). Los tres son opcionales, cada uno con su
    casilla, y tienen posición, transparencia y tamaño propios. La vista previa
    usa el mismo código que estampa los PDF.
  - **CARPETAS DE TRABAJO:** INPUT, OUTPUT, REVIEW y PNG.
  - **COMPONENTES:** RAM, hilos y gráfica del equipo ("Detectar este equipo"
    los llena). Con ellos calcula la combinación más rápida de procesos de OCR
    y libros en paralelo, que se puede ajustar a mano.
- **Inyectar metadatos:** lista los PDF de `OUTPUT/SIN METADATOS` y, con un CSV
  que puede ser distinto al del procesamiento, les escribe los metadatos a los
  que estén, los pasa a OUTPUT y actualiza el registro a `OK`; avisa cuáles
  siguen sin aparecer. Usa las carpetas de Procesamiento › CARPETAS DE TRABAJO.
- **Crear contenedores GDOC:** genera el Excel (.xls) para importar contenedores en GDOC
  (ver abajo). Se eligen la carpeta de PDF (por defecto OUTPUT), la entrega
  (ENTREGA 1 a 30) y los libros.
- **Crear registro OCR-ICR:** genera el CSV de registro de una entrega (ver
  abajo). Se eligen la carpeta de PDF, la carpeta de TIFF, la entrega y los
  libros.
- **Configuración** (al pie de la barra): la sede donde se usa el equipo
  (AGN, BIBLIOTECA, FUNZA o SANTA CLARA) y la versión del programa, con el
  botón **Buscar actualización** (ver abajo).

La versión se ve en el título de la ventana y al pie de la barra, junto a la
sede.

Tiene modo oscuro (predeterminado) y claro, con el sistema visual de
`DESIGN.md` adaptado a los colores de TechnoMart. Cada selección se guarda en
cuanto se cambia y se recupera al abrir el programa. Para probar sin tocar esos
ajustes, `PROCESAMIENTO_PERFIL=Prueba` usa un perfil aparte.

Los textos de marca de agua permitidos están en `TEXTOS_MARCA` y
`MARCA_DIAGONAL`, al inicio de `interfaz/app.py`; no se editan desde la
interfaz.

## Versiones y actualización

La versión instalada está en `VERSION` (formato `X.Y.Z`). Cada versión
publicada es una etiqueta `vX.Y.Z` en GitHub.

**Buscar actualización** (Configuración) consulta las etiquetas de GitHub. Si
hay una más nueva, el botón **Actualizar** hace lo siguiente:

1. Comprueba que no haya un procesamiento en curso ni archivos del programa
   modificados a mano.
2. Avanza el código hasta esa etiqueta, solo si es un avance directo
   (fast-forward).
3. Corre `instalar_interfaz.sh` si cambió `requirements.txt` o el instalador.
4. Reinicia el programa.

No se tocan los ajustes de la interfaz (`~/.config/TechnoMart/`),
`ocr_env` ni las carpetas de datos, porque no están en el repositorio.
Se necesita que el equipo se haya instalado con `git clone`.

Desde la terminal:

```bash
python3 actualizacion.py              # informa si hay versión nueva
python3 actualizacion.py --instalar   # la instala
```

**Para publicar una versión** (en el equipo de desarrollo):

1. Cambia `VERSION`.
2. Haz commit y luego:

```bash
git tag v1.1.0 && git push origin main --tags
```

## Contenedores GDOC

`contenedores_gdoc.py` crea un Excel (.xls, hoja "Hoja1") con los encabezados de la plantilla
`layout_importar_contenedores.xls` y una fila por cada PDF de la carpeta elegida, por defecto OUTPUT (los de
`SIN METADATOS` no entran):

| Columna | Valor |
|---|---|
| contenedor, Campo_2 | Código del libro |
| resguardo · estatus | ARCHIVO · DENTRO |
| nivel1 · nivel2 | CONSORCIO D3LF1N · CAMARA DE REPRESENTANTES |
| nivel3 | ENTREGA 1 a ENTREGA 30 |
| Campo_1 | Sede |
| Campo_3 | Número de páginas |
| ubicacion, posicion, Campo_4, Campo_5 | Vacíos |

```bash
python3 contenedores_gdoc.py --output-dir /mnt/datos/OUTPUT --entrega 10 --sede FUNZA --salida contenedores.xls
```

## Registro OCR-ICR

**Automático, al procesar:** cada PDF que se entrega, con o sin metadatos, se
agrega a `REGISTRO/registro OCR-ICR <d> <Mes> <aaaa>.csv`, por ejemplo
`registro OCR-ICR 4 Nov 2026.csv`. Es un archivo por día: si un libro se
reprocesa ese día, su fila se reemplaza. Los libros que van a REVIEW o quedan
por reintentar no entran. La entrega se elige en PROCESAR, junto a «Iniciar
procesamiento» (`--entrega` en `main.sh`).

**Desde la sección Crear registro OCR-ICR:**
`registro_ocr_icr.py` crea un CSV con las columnas de la hoja de la sede y una
fila por cada PDF de la carpeta de PDF (solo la raíz).

| Columna | Valor |
|---|---|
| CODIFICACION | Código del libro |
| LIBRO | Parte numérica final del código, con sus ceros (`AHFDAP001` → `001`) |
| # IMAGENES | TIFF en la carpeta del libro (`<código>` o `<código> OK`) |
| PESO DE PDF | KB, como la columna Tamaño del Explorador de Windows (`190.003 KB`) |
| PESO DE CARPETA TIFF | GB, con coma decimal |
| FECHA | Día en que se genera (d/m/aaaa) |
| ENTREGA | `ENTREGA 06` |
| HASH | SHA-256 del PDF |

La interfaz avisa de los libros sin carpeta de TIFF y de los que tienen más o
menos páginas en el PDF que imágenes TIFF.

La casilla **Calcular HASH** (marcada por defecto) decide si se calcula el
SHA-256 de cada PDF. Sin marcar, el CSV sale mucho más rápido pero con la
columna HASH vacía; sirve para revisar páginas, pesos e imágenes, no para
entregar. Desde la terminal: `--sin-hash`.

**Si se inyectan metadatos después** (`inyectar_metadatos.sh`), el PDF cambia,
y con él su peso y su hash: se corrigen en `registro_libros_procesados.csv` y
en el registro OCR-ICR del día en que se procesó el libro.

```bash
python3 registro_ocr_icr.py --pdf-dir /mnt/datos/OUTPUT --tiff-dir /datos/TIFF --entrega 6 --salida registro.csv
```

## Marca de agua

`marca_agua.py` estampa la marca (lo llama `main.sh` en el paso 3). Sin
`--marca-config` usa los valores de siempre:

| Elemento | Posición | Opacidad | Tamaño |
|---|---|---|---|
| Logo | arriba-izquierda | 25 % | 20 % del ancho de página |
| Texto al pie | abajo-izquierda | 20 % | 12 pt (se reduce si no cabe) |
| Diagonal | centro | 12 % | 80 % de la diagonal |

Los tres son opcionales: sin logo (`--logo ""`), sin texto
(`--texto-marca ""`) y sin diagonal (si no se da `--marca-diagonal`). Por
defecto se estampan logo y texto, y la diagonal no.

El formato del JSON de `--marca-config` está en la cabecera de `marca_agua.py`.

## Archivos de datos incluidos

Todos los equipos usan los mismos:

| Archivo | Uso |
|---|---|
| `recursos/TechnoMart Funza - TEMP.csv` | CSV de metadatos por libro, sin encabezado: `codigo,fondo,subfondo,equipo,resolucion` |
| `recursos/logo.png` | Logo de la marca de agua |
| `plantillas/layout_importar_contenedores.xls` | Plantilla oficial de GDOC para importar contenedores |
| `plantillas/registro_OCR-ICR.csv` | Encabezados del CSV de Crear registro OCR-ICR |

La interfaz usa el CSV y el logo incluidos, salvo que se elija otro. Lo mismo
hace `main.sh` sin `--csv` ni `--logo`. Al actualizar, un CSV o logo nuevo
llega a todos los equipos con Buscar actualización.

**El repositorio es público:** estos archivos son visibles para cualquiera.

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
Qué columna va en cada campo lo decide `metadatos.py`, con el JSON de
`--meta-config` (formato en su cabecera). Sin él se escribe lo de siempre:

| Campo | Plantilla predeterminada |
|---|---|
| Title | `{1}` |
| Subject | `Congreso de la República - {3}` |
| Author | `Congreso de la República` |
| Keywords | `Fondo: {2}, Subfondo: {3}, Código: {1}, Resolución: {5}, Equipo: {4}` |
| Creator | `{4}` |
| Producer | `Sistema de Digitalización - Congreso de la República` |
| Description | `Documento digitalizado - Código: {1}` |

Un campo con la plantilla vacía no se escribe y conserva lo que ya traiga el
PDF; por ejemplo, el Productor que pone ocrmypdf.

Los metadatos se escriben con pikepdf (`metadatos.py escribir`), que deja
iguales el diccionario Info y el XMP, como exige PDF/A. Hasta la 1.0.10 se
escribían con exiftool, que los dejaba distintos: esos PDF no pasan la
validación PDF/A-3B (veraPDF, cláusula 6.6.2.3.1). En PDF/A, Asunto y
Descripción ocupan el mismo lugar del XMP (`dc:description`): si Asunto tiene
texto, Descripción no se escribe.

Al final se muestra un resumen: exitosos, sin metadatos, en REVIEW, por
reintentar y omitidos. El detalle queda en `logs/`.

## Qué pasa si algo falla

- **Ctrl+C, kill o caída:** se borran los temporales del libro y este queda en
  INPUT; se reprocesa en la siguiente ejecución.
- **El OCR no genera el PDF:** se reintenta una vez; si vuelve a fallar, el
  libro queda en INPUT como "por reintentar".
- **Tesseract no pudo leer una página** (se cayó en los tres intentos): esa
  página entraría al PDF como imagen sin texto, así que el libro va a `REVIEW/`
  con sus TIFF y el motivo dice qué páginas. Si esas páginas nunca van a tener
  texto (en blanco, dañadas) y hay que entregarlo igual, desde la terminal:
  `ACEPTAR_PAGINAS_SIN_OCR=1 bash main.sh --only-book <código>`. El libro sale
  OK y el registro anota las páginas en `Detalle_Error`.
- **Imágenes con DPI ≠ 300 o corruptas, o páginas que no coinciden:** el libro
  va a `REVIEW/` con el motivo en `review_log.csv`.
- **Libro sin entrada en el CSV:** el PDF se entrega igual en
  `OUTPUT/SIN METADATOS` y se registra como `SIN_META`, hasta que se le
  inyecten los metadatos.
