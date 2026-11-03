#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   metadatos.py  ─  CSV DE METADATOS → CAMPOS DEL PDF
# DESC:     Cada campo del PDF (Title, Subject, Author…) se arma con una
#           plantilla: texto libre con columnas del CSV, escritas {1}, {2}…
#           Ej.: "Congreso de la República - {3}". Una plantilla vacía deja el
#           campo sin escribir. También se elige qué columna es el código del
#           libro y si la primera fila es encabezado.
#
#           Sin --config se usa PREDETERMINADA, que reproduce los metadatos de
#           siempre (CSV: código, fondo, subfondo, equipo, resolución).
#           La interfaz guarda la configuración en sus ajustes y la pasa a
#           main.sh e inyectar_metadatos.sh con --meta-config.
#
# USO:
#   python3 metadatos.py indice --csv m.csv [--config meta.json]
#     → una línea por código: código y los valores de CAMPOS, separados por
#       el carácter 0x1F (lo lee metadatos.sh)
#
#   meta.json: {"codigo": 1, "encabezado": false,
#               "campos": {"Title": "{1}", "Subject": "…", …}}
#   encabezado: true / false, o null para detectarlo
# =============================================================================

import argparse
import csv
import json
import re
import sys

# Campos del PDF, en el orden en que viajan a metadatos.sh: (etiqueta exiftool, nombre)
CAMPOS = [
    ("Title", "Título"),
    ("Subject", "Asunto"),
    ("Author", "Autor"),
    ("Keywords", "Palabras clave"),
    ("Creator", "Creador"),
    ("Producer", "Productor"),
    ("Description", "Descripción"),
]

PREDETERMINADA = {
    "codigo": 1,
    "encabezado": None,
    "campos": {
        "Title": "{1}",
        "Subject": "Congreso de la República - {3}",
        "Author": "Congreso de la República",
        "Keywords": "Fondo: {2}, Subfondo: {3}, Código: {1}, Resolución: {5}, Equipo: {4}",
        "Creator": "{4}",
        "Producer": "Sistema de Digitalización - Congreso de la República",
        "Description": "Documento digitalizado - Código: {1}",
    },
}

CODIGO_OK = re.compile(r"^[A-Za-z0-9._-]+$")
COLUMNA = re.compile(r"\{(\d+)\}")
SEPARADOR = "\x1f"


def combinar(config=None):
    """Valores predeterminados completados con los guardados."""
    cfg = json.loads(json.dumps(PREDETERMINADA))
    config = config or {}
    for clave in ("codigo", "encabezado"):
        if clave in config:
            cfg[clave] = config[clave]
    cfg["campos"].update({k: v for k, v in (config.get("campos") or {}).items()
                          if k in cfg["campos"] and isinstance(v, str)})
    return cfg


def leer_filas(ruta):
    """Filas del CSV (listas de celdas sin espacios sobrantes). Acepta coma o punto y coma."""
    for codificacion in ("utf-8-sig", "latin-1"):
        try:
            with open(ruta, encoding=codificacion, newline="") as f:
                texto = f.read()
            break
        except UnicodeDecodeError:
            continue
    primera = texto.split("\n", 1)[0]
    delimitador = ";" if primera.count(";") > primera.count(",") else ","
    return [[c.strip() for c in fila] for fila in csv.reader(texto.splitlines(), delimiter=delimitador)]


def detectar_encabezado(filas):
    """La primera fila es encabezado si no tiene dígitos y la siguiente sí."""
    con_datos = [f for f in filas if any(f)]
    if len(con_datos) < 2:
        return False
    tiene_digito = lambda fila: any(ch.isdigit() for celda in fila for ch in celda)  # noqa: E731
    return not tiene_digito(con_datos[0]) and tiene_digito(con_datos[1])


def con_encabezado(filas, cfg):
    return detectar_encabezado(filas) if cfg.get("encabezado") is None else bool(cfg["encabezado"])


def separar(filas, cfg):
    """(encabezado o None, filas de datos sin vacías)."""
    con_datos = [f for f in filas if any(f)]
    if con_datos and con_encabezado(filas, cfg):
        return con_datos[0], con_datos[1:]
    return None, con_datos


def num_columnas(filas):
    return max((len(f) for f in filas), default=0)


def celda(fila, n):
    return fila[n - 1] if 0 < n <= len(fila) else ""


def armar(plantilla, fila):
    """Reemplaza {N} por la columna N; espacios dobles y de los bordes se limpian."""
    texto = COLUMNA.sub(lambda m: celda(fila, int(m.group(1))), plantilla or "")
    return re.sub(r"\s+", " ", texto).strip()


def columnas_usadas(cfg):
    usadas = {int(cfg["codigo"])}
    for plantilla in cfg["campos"].values():
        usadas.update(int(n) for n in COLUMNA.findall(plantilla or ""))
    return usadas


def indice(filas, cfg):
    """{código: {campo: valor}} en el orden del CSV; los repetidos conservan la primera fila.
    Los códigos solo numéricos también se registran con 6 dígitos (3106 → 003106)."""
    _, datos = separar(filas, cfg)
    resultado = {}
    for fila in datos:
        codigo = celda(fila, int(cfg["codigo"]))
        if not CODIGO_OK.match(codigo):
            continue
        valores = {campo: armar(cfg["campos"].get(campo, ""), fila) for campo, _ in CAMPOS}
        resultado.setdefault(codigo, valores)
        if codigo.isdigit():
            resultado.setdefault(f"{int(codigo):06d}", valores)
    return resultado


def revisar(filas, cfg):
    """(códigos, nº de códigos distintos, avisos) para mostrar en la interfaz."""
    _, datos = separar(filas, cfg)
    vistos, avisos, incompletas = {}, [], 0
    usadas = columnas_usadas(cfg)
    for n, fila in enumerate(datos, 1):
        codigo = celda(fila, int(cfg["codigo"]))
        if not CODIGO_OK.match(codigo):
            continue
        if codigo in vistos:
            avisos.append(f"{codigo} repetido (filas {vistos[codigo]} y {n})")
        vistos.setdefault(codigo, n)
        if any(not celda(fila, c) for c in usadas):
            incompletas += 1
    if incompletas:
        avisos.append(f"{incompletas} fila(s) con vacías las columnas que se usan en los metadatos")
    codigos = set(vistos) | {f"{int(c):06d}" for c in vistos if c.isdigit()}
    return codigos, len(vistos), avisos


def main():
    p = argparse.ArgumentParser(description="Índice de metadatos por código a partir del CSV")
    p.add_argument("accion", choices=["indice"])
    p.add_argument("--csv", required=True)
    p.add_argument("--config", help="JSON con columna del código, encabezado y plantillas")
    a = p.parse_args()

    config = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            config = json.load(f)
    cfg = combinar(config)
    sys.stdout.reconfigure(encoding="utf-8")
    limpio =lambda v: v.replace(SEPARADOR, " ").replace("\n", " ").replace("\r", " ")  # noqa: E731
    for codigo, valores in indice(leer_filas(a.csv), cfg).items():
        print(SEPARADOR.join([codigo, *(limpio(valores[c]) for c, _ in CAMPOS)]))


if __name__ == "__main__":
    sys.exit(main())
