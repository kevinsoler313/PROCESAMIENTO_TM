#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   registro_ocr_icr.py  ─  CSV DE REGISTRO OCR-ICR POR ENTREGA
# DESC:     Una fila por cada PDF de la carpeta de PDF (solo la raíz), con los
#           datos de su carpeta de TIFF. Mismas columnas que la hoja de la sede:
#
#   CODIFICACION          código del libro (nombre del PDF)
#   LIBRO                 parte numérica final del código, con sus ceros (AHFDAP001 → 001)
#   # IMAGENES            número de TIFF en <carpeta TIFF>/<código>
#   PESO DE PDF           MB (1 MB = 1024² bytes), coma decimal, 1 decimal
#   PESO DE CARPETA TIFF  GB (1 GB = 1024³ bytes), coma decimal, 1 decimal
#   FECHA                 día en que se genera el CSV (d/m/aaaa)
#   ENTREGA               ENTREGA 06 (dos dígitos)
#   HASH                  SHA-256 del PDF
#
#           La carpeta de TIFF de un libro puede llamarse como el código o con
#           " OK" al final (como llegan a INPUT).
# USO:
#   python3 registro_ocr_icr.py --pdf-dir /mnt/datos/OUTPUT --tiff-dir /datos/TIFF \
#       --entrega 6 --salida registro.csv [--book AR10582 --book …]
# =============================================================================

import argparse
import csv
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

ENCABEZADOS = ["CODIFICACION", "LIBRO ", "# IMAGENES", "PESO DE PDF", "PESO DE CARPETA TIFF",
               "FECHA", "ENTREGA  ", "HASH", ""]
ENTREGAS = range(1, 31)
EXT_TIFF = {".tif", ".tiff"}


def libros_pdf(pdf_dir):
    carpeta = Path(pdf_dir)
    if not carpeta.is_dir():
        return []
    return sorted(p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def carpeta_tiff(tiff_dir, codigo):
    """Carpeta de TIFF del libro, o None si no está."""
    base = Path(tiff_dir)
    for nombre in (codigo, f"{codigo} OK"):
        if (base / nombre).is_dir():
            return base / nombre
    if base.is_dir():
        buscado = codigo.upper()
        for p in base.iterdir():
            if p.is_dir() and p.name.upper().removesuffix(" OK") == buscado:
                return p
    return None


def datos_tiff(carpeta):
    """(número de TIFF, bytes) de la carpeta del libro."""
    n, total = 0, 0
    for p in carpeta.iterdir():
        if p.is_file() and p.suffix.lower() in EXT_TIFF:
            n += 1
            total += p.stat().st_size
    return n, total


def numero_libro(codigo):
    m = re.search(r"\d+$", codigo)
    return m.group() if m else codigo


def decimal_coma(valor):
    """185.54 → '185,5'; 351.0 → '351' (como en la hoja de la sede)."""
    texto = f"{valor:.1f}".removesuffix(".0")
    return texto.replace(".", ",")


def sha256(ruta, bloque=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while trozo := f.read(bloque):
            h.update(trozo)
    return h.hexdigest()


def fila(codigo, imagenes, bytes_pdf, bytes_tiff, entrega, hash_pdf, fecha=None):
    fecha = fecha or date.today()
    return [codigo, numero_libro(codigo), imagenes, decimal_coma(bytes_pdf / 1024 ** 2),
            decimal_coma(bytes_tiff / 1024 ** 3), f"{fecha.day}/{fecha.month}/{fecha.year}",
            f"ENTREGA {entrega:02d}", hash_pdf, ""]


def escribir_csv(destino, filas):
    # UTF-8 con BOM, como el CSV de contenedores: Excel lo abre bien
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(ENCABEZADOS)
        w.writerows(filas)


def main():
    p = argparse.ArgumentParser(description="CSV de registro OCR-ICR de una entrega")
    p.add_argument("--pdf-dir", required=True)
    p.add_argument("--tiff-dir", required=True)
    p.add_argument("--entrega", type=int, required=True, choices=list(ENTREGAS))
    p.add_argument("--salida", required=True)
    p.add_argument("--book", action="append", help="Solo estos libros (repetible)")
    a = p.parse_args()

    pdfs = libros_pdf(a.pdf_dir)
    if a.book:
        pdfs = [x for x in pdfs if x.stem in set(a.book)]
    filas, sin_tiff = [], []
    for pdf in pdfs:
        carpeta = carpeta_tiff(a.tiff_dir, pdf.stem)
        if carpeta is None:
            sin_tiff.append(pdf.stem)
            continue
        n, peso_tiff = datos_tiff(carpeta)
        filas.append(fila(pdf.stem, n, pdf.stat().st_size, peso_tiff, a.entrega, sha256(pdf)))
    escribir_csv(a.salida, filas)
    print(f"{len(filas)} libro(s) en {a.salida}", file=sys.stderr)
    if sin_tiff:
        print(f"Sin carpeta de TIFF: {', '.join(sin_tiff)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
