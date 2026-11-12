#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   libros.py  ─  QUÉ PDF SALEN DE UN LIBRO Y CON QUÉ TIFF, EN QUÉ ORDEN
# DESC:     Un libro puede venir de dos formas:
#
#             normal       LIBRO/*.tif
#             fragmentado  LIBRO/<fragmento>/*.tif   (n subcarpetas, un nivel)
#
#           Este módulo decide el orden UNA SOLA VEZ y lo deja escrito en un plan
#           (TSV) y en un archivo de orden por carpeta. OCR.sh une las páginas
#           siguiendo ese archivo, no volviendo a ordenar nombres: así el OCR en
#           paralelo nunca cambia el orden.
#
#   ORDEN DE LOS FRAGMENTOS (nombre de la subcarpeta, sin " OK"):
#     1. Si todos tienen mes y año (nov_2013, dic 2013, noviembre-2013, 2013_nov)
#        → por fecha. Ordenar por nombre pondría dic antes que nov.
#     2. Si no, si todos son iguales salvo sus números (01, 02, 10 · Tomo 1, Tomo 2)
#        → por esos números (2 antes que 10).
#     3. Cualquier otra cosa (mezclas, un mes sin año, fechas repetidas) → REVIEW.
#        No se adivina.
#
#   ORDEN DE LAS TIFF dentro de cada carpeta: por nombre, en orden natural
#   (Hoja 2 antes que Hoja 10), sin distinguir mayúsculas.
#
#   NUMERACIÓN (solo libros fragmentados): si los nombres de las TIFF terminan
#   en un número (…-0000.tif), no puede faltar ninguno. Cada fragmento puede
#   volver a empezar (0000 o 0001) o seguir el consecutivo del anterior.
#   Un hueco o un número repetido → REVIEW («posible página faltante»).
#
# USO:
#   python3 libros.py plan <carpeta_libro> [--modo unico|fragmentos] --salida plan.tsv
#     --modo vacío = no se aceptan libros fragmentados (van a REVIEW)
#     Salida: una línea por carpeta de TIFF, en orden:
#       nombre_pdf <TAB> carpeta <TAB> archivo_de_orden <TAB> n_tiff
#     (en modo «unico» todas las carpetas comparten el nombre del PDF)
#     Código de salida 0 = plan listo · 2 = el libro va a REVIEW (motivo en stdout)
# =============================================================================

import argparse
import re
import sys
from pathlib import Path

EXT_TIFF = (".tif", ".tiff")

MESES = {
    "ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4, "may": 5, "mayo": 5, "jun": 6, "junio": 6,
    "jul": 7, "julio": 7, "ago": 8, "agosto": 8, "sep": 9, "sept": 9, "set": 9,
    "septiembre": 9, "setiembre": 9, "oct": 10, "octubre": 10,
    "nov": 11, "noviembre": 11, "dic": 12, "diciembre": 12,
}
_MES = "|".join(sorted(MESES, key=len, reverse=True))
_SEP = r"[\s_.\-]*"
# mes y año (nov_2013) o año y mes (2013_nov); el mes no puede estar pegado a otras letras
FECHA = re.compile(rf"(?<![a-z])(?:(?P<m1>{_MES}){_SEP}(?P<a1>(?:19|20)\d\d)|(?P<a2>(?:19|20)\d\d){_SEP}(?P<m2>{_MES}))(?![a-z\d])")


MES_SUELTO = re.compile(rf"(?<![a-z])(?:{_MES})(?![a-z])")


class ErrorLibro(Exception):
    """El libro no se puede procesar tal como viene: va a REVIEW con este motivo."""


def sin_ok(nombre):
    """'AR10582 OK' → 'AR10582' (como el paso 1 de main.sh)."""
    return re.sub(r"\s+[Oo][Kk]$", "", nombre).rstrip()


def clave_natural(nombre):
    """Orden natural sin distinguir mayúsculas: 'Hoja 2' < 'Hoja 10'."""
    return [(0, int(t), t) if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", nombre.lower()) if t]


def es_tiff(nombre):
    return nombre.lower().endswith(EXT_TIFF) and not nombre.startswith("_")


def visible(p):
    """Carpetas de control del programa o del sistema no son fragmentos."""
    return not p.name.startswith(("_", "."))


def tiffs(carpeta):
    """Nombres de las TIFF de la carpeta, en orden natural."""
    return sorted((p.name for p in Path(carpeta).iterdir() if p.is_file() and es_tiff(p.name)),
                  key=clave_natural)


def subcarpetas(carpeta):
    return [p for p in Path(carpeta).iterdir() if p.is_dir() and visible(p)]


def fecha(nombre):
    """(año, mes) si el nombre trae exactamente un mes con su año; si no, None."""
    hallados = list(FECHA.finditer(nombre.lower()))
    if len(hallados) != 1:
        return None
    m = hallados[0]
    mes = m.group("m1") or m.group("m2")
    año = m.group("a1") or m.group("a2")
    return int(año), MESES[mes]


def ordenar_fragmentos(nombres):
    """Nombres (sin " OK") en orden; ErrorLibro si no hay una regla segura."""
    fechas = {n: fecha(n) for n in nombres}
    if all(fechas.values()):
        repetidas = sorted({f for f in fechas.values() if list(fechas.values()).count(f) > 1})
        if repetidas:
            dup = [n for n in nombres if fechas[n] in repetidas]
            raise ErrorLibro("Fragmentos con el mismo mes y año: " + ", ".join(dup))
        return sorted(nombres, key=lambda n: fechas[n])

    esqueletos = {re.sub(r"\d+", "#", n.lower()) for n in nombres}
    if len(esqueletos) == 1 and "#" in next(iter(esqueletos)):
        numeros = {n: tuple(int(x) for x in re.findall(r"\d+", n)) for n in nombres}
        if len(set(numeros.values())) != len(nombres):
            raise ErrorLibro("Fragmentos con el mismo número: " + ", ".join(nombres))
        return sorted(nombres, key=lambda n: numeros[n])

    solo_mes = [n for n in nombres if not fechas[n] and MES_SUELTO.search(n.lower())]
    if solo_mes:
        raise ErrorLibro("No se puede ordenar los fragmentos: tienen mes pero no año ("
                         + ", ".join(solo_mes[:5]) + "). Sin el año no se sabe, p. ej., si "
                         "enero va antes o después de diciembre.")
    con_mes = [n for n in nombres if fechas[n]]
    if con_mes:
        sin = [n for n in nombres if not fechas[n]]
        raise ErrorLibro("No se puede ordenar los fragmentos: unos tienen mes y año y otros no ("
                         + ", ".join(sin[:5]) + "). Revisa los nombres de las subcarpetas.")
    raise ErrorLibro("No se puede ordenar los fragmentos por fecha (mes y año) ni por número: "
                     + ", ".join(nombres[:5]) + ". Revisa los nombres de las subcarpetas.")


def numeracion(nombres_tiff):
    """Números finales de las TIFF si TODAS terminan en número con el mismo prefijo; si no, None."""
    prefijos, numeros = set(), []
    for n in nombres_tiff:
        m = re.match(r"^(.*?)(\d+)$", Path(n).stem)
        if not m:
            return None
        prefijos.add(m.group(1).lower())
        numeros.append(int(m.group(2)))
    return numeros if len(prefijos) == 1 else None


def revisar_numeracion(fragmentos):
    """fragmentos: [(nombre, [tiffs en orden])]. ErrorLibro si falta o se repite un número."""
    fin_anterior = None
    for nombre, lista in fragmentos:
        numeros = numeracion(lista)
        if numeros is None:                 # nombres sin número final: no se puede revisar
            fin_anterior = None
            continue
        repetidos = sorted({x for x in numeros if numeros.count(x) > 1})
        if repetidos:
            raise ErrorLibro(f"Número de página repetido en {nombre}: "
                             + ", ".join(str(x) for x in repetidos[:5]))
        ini, fin = min(numeros), max(numeros)
        faltan = sorted(set(range(ini, fin + 1)) - set(numeros))
        if faltan:
            extra = f" (+{len(faltan) - 5} más)" if len(faltan) > 5 else ""
            raise ErrorLibro(f"Posible página faltante en {nombre}: falta(n) el(los) número(s) "
                             + ", ".join(str(x) for x in faltan[:5]) + extra)
        validos = {0, 1} | ({fin_anterior + 1} if fin_anterior is not None else set())
        if ini not in validos:
            esperado = "0000 o 0001" + (f" o {fin_anterior + 1}" if fin_anterior is not None else "")
            raise ErrorLibro(f"Posible página faltante al inicio de {nombre}: empieza en {ini} "
                             f"(se esperaba {esperado})")
        fin_anterior = fin


def plan(carpeta_libro, modo=""):
    """[(nombre_pdf, carpeta, [tiffs en orden])] en orden. ErrorLibro → REVIEW.
    modo: '' = solo libros normales · 'unico' = un PDF con todo · 'fragmentos' = un PDF por fragmento."""
    libro = Path(carpeta_libro)
    nombre_libro = sin_ok(libro.name)
    sueltas = tiffs(libro)
    subs = subcarpetas(libro)

    if not subs:
        if not sueltas:
            raise ErrorLibro("La carpeta del libro no tiene TIFF")
        return [(nombre_libro, libro, sueltas)]

    if sueltas:
        raise ErrorLibro(f"El libro tiene {len(sueltas)} TIFF sueltas y además {len(subs)} subcarpeta(s): "
                         "no se sabe dónde van esas páginas. Deja solo TIFF o solo subcarpetas.")
    if not modo:
        raise ErrorLibro(f"Libro fragmentado en {len(subs)} subcarpeta(s): marca «Libros fragmentados» "
                         "para procesarlo")

    por_nombre = {}
    for s in subs:
        n = sin_ok(s.name)
        if n in por_nombre:
            raise ErrorLibro(f"Dos subcarpetas se llaman igual sin « OK»: {por_nombre[n].name} y {s.name}")
        if subcarpetas(s):
            raise ErrorLibro(f"La subcarpeta {s.name} tiene más subcarpetas adentro; "
                             "las TIFF deben estar en el primer nivel")
        por_nombre[n] = s
    orden = ordenar_fragmentos(list(por_nombre))

    fragmentos = []
    for n in orden:
        lista = tiffs(por_nombre[n])
        if not lista:
            raise ErrorLibro(f"La subcarpeta {por_nombre[n].name} no tiene TIFF")
        fragmentos.append((n, lista))
    revisar_numeracion(fragmentos)

    return [(nombre_libro if modo == "unico" else n, por_nombre[n], lista) for n, lista in fragmentos]


def resumen(carpeta_libro, modo="fragmentos"):
    """Para la interfaz: (fragmentos [(nombre, n_tiff)] en orden, motivo de error o None).
    Libro normal → ([], None)."""
    libro = Path(carpeta_libro)
    try:
        if not subcarpetas(libro):
            return [], None
        return [(sin_ok(Path(c).name), len(l)) for _, c, l in plan(libro, modo)], None
    except ErrorLibro as e:
        try:
            subs = sorted(subcarpetas(libro), key=lambda p: clave_natural(p.name))
            return [(sin_ok(s.name), len(tiffs(s))) for s in subs], str(e)
        except OSError:
            return [], str(e)
    except OSError as e:
        return [], f"No se pudo leer la carpeta: {e}"


def main():
    p = argparse.ArgumentParser(description="Plan de un libro: qué PDF salen y con qué TIFF, en orden")
    p.add_argument("accion", choices=["plan"])
    p.add_argument("carpeta")
    p.add_argument("--modo", default="", choices=["", "unico", "fragmentos"])
    p.add_argument("--salida", required=True, help="plan.tsv; los archivos de orden van a su lado")
    a = p.parse_args()

    try:
        unidades = plan(a.carpeta, a.modo)
    except ErrorLibro as e:
        print(e)
        return 2
    salida = Path(a.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    lineas = []
    for i, (pdf, carpeta, lista) in enumerate(unidades):
        orden = salida.parent / f"_orden_{i:03d}.txt"
        orden.write_text("".join(f"{n}\n" for n in lista), encoding="utf-8")
        lineas.append(f"{pdf}\t{carpeta}\t{orden}\t{len(lista)}\n")
    salida.write_text("".join(lineas), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
