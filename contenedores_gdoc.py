#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   contenedores_gdoc.py  ─  CSV PARA IMPORTAR CONTENEDORES EN GDOC
# DESC:     Una fila por cada PDF terminado en OUTPUT (no incluye OUTPUT/SIN
#           METADATOS), con las columnas de la plantilla
#           layout_importar_contenedores.xls:
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
#       --sede FUNZA --salida contenedores.csv [--book AR10582 --book …]
# =============================================================================

import argparse
import csv
import subprocess
import sys
from pathlib import Path

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


def escribir_csv(destino, filas):
    # UTF-8 con BOM: Excel abre bien las tildes; coma como separador
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(ENCABEZADOS)
        w.writerows(filas)


def main():
    p = argparse.ArgumentParser(description="CSV de contenedores GDOC a partir de los PDF de OUTPUT")
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
    escribir_csv(a.salida, filas)
    print(f"{len(filas)} libro(s) en {a.salida}", file=sys.stderr)
    if sin_paginas:
        print(f"No se pudo leer el número de páginas de: {', '.join(sin_paginas)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
