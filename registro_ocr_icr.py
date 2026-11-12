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
#
#           Registro del día: main.sh agrega cada PDF que entrega (en OUTPUT o en
#           OUTPUT/SIN METADATOS) a REGISTRO/registro OCR-ICR <d> <Mes> <aaaa>.csv
#           (p. ej. "registro OCR-ICR 4 Nov 2026.csv"). Un archivo por día: si un
#           libro se reprocesa ese día, su fila se reemplaza.
# USO:
#   python3 registro_ocr_icr.py --pdf-dir /mnt/datos/OUTPUT --tiff-dir /datos/TIFF \
#       --entrega 6 --salida registro.csv [--book AR10582 --book …]
#   python3 registro_ocr_icr.py agregar --registro-dir /mnt/datos/REGISTRO \
#       --pdf OUTPUT/AR10582.pdf --tiff-dir INPUT/AR10582 [--entrega 6] [--hash …]
#   python3 registro_ocr_icr.py actualizar --registro-dir /mnt/datos/REGISTRO \
#       --pdf OUTPUT/AR10582.pdf [--hash …]
#     → el PDF cambió (inyectar_metadatos.sh): en los registros del día donde
#       esté el libro se corrigen PESO DE PDF y HASH; lo demás no se toca
#   Con --sin-hash (CSV de entrega) la columna HASH queda vacía: es mucho más
#   rápido, porque no hay que leer cada PDF completo.
# =============================================================================

import argparse
import csv
import hashlib
import io
import os
import re
import sys
from datetime import date
from pathlib import Path

ENCABEZADOS = ["CODIFICACION", "LIBRO ", "# IMAGENES", "PESO DE PDF", "PESO DE CARPETA TIFF",
               "FECHA", "ENTREGA  ", "HASH", ""]
ENTREGAS = range(1, 31)
EXT_TIFF = {".tif", ".tiff"}
MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


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
            f"ENTREGA {entrega:02d}" if entrega else "", hash_pdf, ""]


def escribir_csv(destino, filas):
    # UTF-8 con BOM, como el CSV de contenedores: Excel lo abre bien
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(ENCABEZADOS)
        w.writerows(filas)


def nombre_diario(fecha=None):
    fecha = fecha or date.today()
    return f"registro OCR-ICR {fecha.day} {MESES[fecha.month - 1]} {fecha.year}.csv"


def agregar(registro_dir, nueva):
    """Agrega una fila al registro del día; si el libro ya estaba, la reemplaza.
    Con varios libros en paralelo, el archivo se bloquea mientras se reescribe."""
    import fcntl
    Path(registro_dir).mkdir(parents=True, exist_ok=True)
    ruta = Path(registro_dir) / nombre_diario()
    fd = os.open(ruta, os.O_RDWR | os.O_CREAT, 0o664)
    with os.fdopen(fd, "r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        filas = list(csv.reader(io.StringIO(f.read().decode("utf-8-sig"))))
        datos = [r for r in filas[1:] if r and r[0] != nueva[0]]
        salida = io.StringIO()
        w = csv.writer(salida, lineterminator="\r\n")
        w.writerow(ENCABEZADOS)
        w.writerows([*datos, nueva])
        f.seek(0)
        f.truncate()
        f.write(salida.getvalue().encode("utf-8-sig"))
    return ruta


def actualizar(registro_dir, codigo, bytes_pdf, hash_pdf):
    """Corrige PESO DE PDF y HASH del libro en todos los registros del día donde
    esté. Devuelve las rutas modificadas (vacía si el libro no estaba en ninguno)."""
    import fcntl
    col_peso, col_hash = ENCABEZADOS.index("PESO DE PDF"), ENCABEZADOS.index("HASH")
    cambiados = []
    for ruta in sorted(Path(registro_dir).glob("registro OCR-ICR *.csv")):
        with open(ruta, "r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            filas = list(csv.reader(io.StringIO(f.read().decode("utf-8-sig"))))
            tocado = False
            for r in filas[1:]:
                if r and r[0] == codigo and len(r) > col_hash:
                    r[col_peso] = decimal_coma(bytes_pdf / 1024 ** 2)
                    r[col_hash] = hash_pdf
                    tocado = True
            if not tocado:
                continue
            salida = io.StringIO()
            csv.writer(salida, lineterminator="\r\n").writerows(filas)
            f.seek(0)
            f.truncate()
            f.write(salida.getvalue().encode("utf-8-sig"))
        cambiados.append(ruta)
    return cambiados


def main_actualizar(argv):
    p = argparse.ArgumentParser(description="Corrige peso y hash de un PDF en los registros del día")
    p.add_argument("--registro-dir", required=True)
    p.add_argument("--pdf", required=True)
    p.add_argument("--hash", help="SHA-256 del PDF, si ya se calculó")
    a = p.parse_args(argv)
    pdf = Path(a.pdf)
    rutas = actualizar(a.registro_dir, pdf.stem, pdf.stat().st_size, a.hash or sha256(pdf))
    for r in rutas:
        print(r)
    if not rutas:
        print(f"{pdf.stem} no está en ningún registro OCR-ICR de {a.registro_dir}", file=sys.stderr)
        return 3
    return 0


def main_agregar(argv):
    p = argparse.ArgumentParser(description="Agrega un PDF al registro OCR-ICR del día")
    p.add_argument("--registro-dir", required=True)
    p.add_argument("--pdf", required=True)
    p.add_argument("--tiff-dir", required=True, help="Carpeta de TIFF del libro")
    p.add_argument("--entrega", type=int, choices=list(ENTREGAS))
    p.add_argument("--hash", help="SHA-256 del PDF, si ya se calculó")
    a = p.parse_args(argv)
    pdf = Path(a.pdf)
    n, peso_tiff = datos_tiff(Path(a.tiff_dir))
    nueva = fila(pdf.stem, n, pdf.stat().st_size, peso_tiff, a.entrega, a.hash or sha256(pdf))
    print(agregar(a.registro_dir, nueva))


def main():
    if sys.argv[1:2] == ["agregar"]:
        return main_agregar(sys.argv[2:])
    if sys.argv[1:2] == ["actualizar"]:
        return main_actualizar(sys.argv[2:])
    p = argparse.ArgumentParser(description="CSV de registro OCR-ICR de una entrega")
    p.add_argument("--pdf-dir", required=True)
    p.add_argument("--tiff-dir", required=True)
    p.add_argument("--entrega", type=int, required=True, choices=list(ENTREGAS))
    p.add_argument("--salida", required=True)
    p.add_argument("--book", action="append", help="Solo estos libros (repetible)")
    p.add_argument("--sin-hash", action="store_true",
                   help="No calcular el SHA-256 (columna HASH vacía); mucho más rápido")
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
        filas.append(fila(pdf.stem, n, pdf.stat().st_size, peso_tiff, a.entrega,
                          "" if a.sin_hash else sha256(pdf)))
    escribir_csv(a.salida, filas)
    print(f"{len(filas)} libro(s) en {a.salida}", file=sys.stderr)
    if sin_tiff:
        print(f"Sin carpeta de TIFF: {', '.join(sin_tiff)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
