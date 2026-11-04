#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   contenedores_gdoc.py  ─  XLSX PARA IMPORTAR CONTENEDORES EN GDOC
# DESC:     Una fila por cada PDF terminado en OUTPUT (no incluye OUTPUT/SIN
#           METADATOS), con las columnas de la plantilla
#           layout_importar_contenedores.xls, en un Excel (.xlsx, hoja "Hoja1"):
#
#   A contenedor = código del libro      H nivel3  = ENTREGA <n> (1 a 30)
#   B ubicacion  = vacío                 I Campo_1 = sede (Configuración)
#   C posicion   = vacío                 J Campo_2 = código del libro
#   D resguardo  = ARCHIVO               K Campo_3 = número de páginas
#   E estatus    = DENTRO                L Campo_4 = vacío
#   F nivel1     = CONSORCIO D3LF1N      M Campo_5 = vacío
#   G nivel2     = CAMARA DE REPRESENTANTES
#
# USO:
#   python3 contenedores_gdoc.py --output-dir /mnt/datos/OUTPUT --entrega 10 \
#       --sede FUNZA --salida contenedores.xlsx [--book AR10582 --book …]
#
#   El .xlsx se escribe solo con la biblioteca estándar de Python (es un ZIP
#   con unos XML), para no sumar dependencias.
# =============================================================================

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ENCABEZADOS = ["contenedor", "ubicacion", "posicion", "resguardo", "estatus", "nivel1",
               "nivel2", "nivel3", "Campo_1", "Campo_2", "Campo_3", "Campo_4", "Campo_5"]
RESGUARDO = "ARCHIVO"
ESTATUS = "DENTRO"
NIVEL1 = "CONSORCIO D3LF1N"
NIVEL2 = "CAMARA DE REPRESENTANTES"
ENTREGAS = range(1, 31)
SEDES = ["AGN", "BIBLIOTECA", "FUNZA", "SANTA CLARA"]


def libros_output(output_dir):
    """PDF terminados en OUTPUT (solo la raíz: SIN METADATOS queda fuera)."""
    carpeta = Path(output_dir)
    if not carpeta.is_dir():
        return []
    return sorted(p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def paginas_pdf(ruta):
    """Número de páginas según pdfinfo; None si no se puede leer."""
    try:
        salida = subprocess.run(["pdfinfo", str(ruta)], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for linea in salida.splitlines():
        if linea.startswith("Pages:"):
            return int(linea.split(":", 1)[1])
    return None


def fila(codigo, paginas, entrega, sede):
    return [codigo, "", "", RESGUARDO, ESTATUS, NIVEL1, NIVEL2, f"ENTREGA {entrega}",
            sede, codigo, paginas, "", ""]


def _columna(n):
    """0 → A, 25 → Z, 26 → AA."""
    letras = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        letras = chr(65 + r) + letras
    return letras


def _celda(ref, valor):
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return f'<c r="{ref}"><v>{valor}</v></c>'
    if valor in (None, ""):
        return ""
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(valor))}</t></is></c>'


def escribir_xlsx(destino, filas, hoja="Hoja1"):
    """Encabezados + filas en un .xlsx de una hoja. Números como número, el resto como texto."""
    xml_filas = []
    for i, fila in enumerate([ENCABEZADOS, *filas], 1):
        celdas = "".join(_celda(f"{_columna(j)}{i}", v) for j, v in enumerate(fila))
        xml_filas.append(f'<row r="{i}">{celdas}</row>')
    anchos = "".join(f'<col min="{j}" max="{j}" width="{w}" customWidth="1"/>'
                     for j, w in enumerate([16, 12, 10, 12, 10, 22, 30, 14, 14, 16, 10, 10, 10], 1))
    hoja_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<cols>{anchos}</cols><sheetData>{"".join(xml_filas)}</sheetData></worksheet>')
    archivos = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="xl/workbook.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(hoja)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>',
        "xl/worksheets/sheet1.xml": hoja_xml,
    }
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, contenido in archivos.items():
            z.writestr(nombre, contenido)


def main():
    p = argparse.ArgumentParser(description="XLSX de contenedores GDOC a partir de los PDF de OUTPUT")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--entrega", type=int, required=True, choices=list(ENTREGAS))
    p.add_argument("--sede", required=True, choices=SEDES)
    p.add_argument("--salida", required=True)
    p.add_argument("--book", action="append", help="Solo estos libros (repetible)")
    a = p.parse_args()

    pdfs = libros_output(a.output_dir)
    if a.book:
        pdfs = [x for x in pdfs if x.stem in set(a.book)]
    filas, sin_paginas = [], []
    for pdf in pdfs:
        n = paginas_pdf(pdf)
        if n is None:
            sin_paginas.append(pdf.stem)
            continue
        filas.append(fila(pdf.stem, n, a.entrega, a.sede))
    escribir_xlsx(a.salida, filas)
    print(f"{len(filas)} libro(s) en {a.salida}", file=sys.stderr)
    if sin_paginas:
        print(f"No se pudo leer el número de páginas de: {', '.join(sin_paginas)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
