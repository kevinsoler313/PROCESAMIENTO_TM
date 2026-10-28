#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   marca_agua.py  ─  MARCA DE AGUA (paso 3 de main.sh)
# DESC:     Estampa en cada página tres elementos, cada uno con su posición,
#           transparencia y tamaño:
#             · logo           (por defecto arriba-izquierda)
#             · texto al pie   (por defecto abajo-izquierda)
#             · marca diagonal (opcional; ascendente de izquierda a derecha)
#           Los valores predeterminados son los que usaba main.sh antes de que
#           la marca de agua fuera configurable. La interfaz usa vista_previa()
#           para mostrar el resultado con los mismos cálculos.
#
# USO:
#   python3 marca_agua.py --entrada ocr.pdf --salida marca.pdf --logo logo.png \
#       [--config marca.json] [--texto "…"] [--diagonal "…"]
#
#   marca.json (todas las claves son opcionales):
#   {"logo":     {"posicion": "arriba-izquierda", "opacidad": 0.25, "tamano": 0.20},
#    "texto":    {"posicion": "abajo-izquierda",  "opacidad": 0.20, "tamano": 12},
#    "diagonal": {"posicion": "centro",           "opacidad": 0.12, "tamano": 0.80}}
#   tamano: logo = fracción del ancho de página · texto = puntos (se reduce si
#           no cabe) · diagonal = fracción de la diagonal de la página
# =============================================================================

import argparse
import copy
import json
import math
import sys
from io import BytesIO

POSICIONES = [
    "arriba-izquierda", "arriba-centro", "arriba-derecha",
    "centro-izquierda", "centro", "centro-derecha",
    "abajo-izquierda", "abajo-centro", "abajo-derecha",
]

PREDETERMINADA = {
    "logo": {"posicion": "arriba-izquierda", "opacidad": 0.25, "tamano": 0.20},
    "texto": {"texto": "CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES",
              "posicion": "abajo-izquierda", "opacidad": 0.20, "tamano": 12},
    "diagonal": {"texto": "", "posicion": "centro", "opacidad": 0.12, "tamano": 0.80},
}

CM_TO_POINTS = 72 / 2.54
LOGO_MIN_W = 45                 # pts — el logo nunca más pequeño que esto
LOGO_MAX_W = 180                # pts con el tamaño predeterminado (20%); escala con él
LOGO_MAX_H_FRAC = 0.18          # alto máximo con el tamaño predeterminado; escala con él
MARGIN_FRAC = 0.04              # margen = 4% del ancho/alto de la página…
MARGIN_CAP = 1.5 * CM_TO_POINTS  # …con tope de 1.5 cm
MARGIN_FLOOR = 6                # …y piso de 6 pts
DIAGONAL_GRIS = 0.35            # gris medio: se nota sin tapar el documento


def combinar(config=None):
    """Valores predeterminados completados con los del usuario."""
    cfg = copy.deepcopy(PREDETERMINADA)
    for elemento, valores in (config or {}).items():
        if elemento in cfg and isinstance(valores, dict):
            cfg[elemento].update({k: v for k, v in valores.items() if v is not None})
    return cfg


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _partes(posicion):
    """'abajo-izquierda' → ('abajo', 'izquierda'); 'centro' → ('centro', 'centro')."""
    if posicion not in POSICIONES:
        posicion = "centro"
    if posicion == "centro":
        return "centro", "centro"
    return tuple(posicion.split("-"))


def _x(horizontal, pw, ancho, margen):
    return {"izquierda": margen, "centro": (pw - ancho) / 2, "derecha": pw - margen - ancho}[horizontal]


def _y(vertical, ph, alto, margen_arriba, margen_abajo):
    return {"arriba": ph - alto - margen_arriba, "centro": (ph - alto) / 2, "abajo": margen_abajo}[vertical]


class Logo:
    """El logo se lee una sola vez y se reutiliza en todas las páginas."""

    def __init__(self, ruta):
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        with Image.open(ruta) as img:
            self.aspecto = img.height / img.width
        self.reader = ImageReader(ruta)


def dibujar(c, pw, ph, cfg, logo):
    """Dibuja logo, texto y diagonal sobre el canvas c de una página pw × ph."""
    margin_x = _clamp(MARGIN_FRAC * pw, MARGIN_FLOOR, MARGIN_CAP)
    margin_top = _clamp(MARGIN_FRAC * ph, MARGIN_FLOOR, MARGIN_CAP)
    text_y = _clamp(0.5 * MARGIN_FRAC * ph, 4, 0.5 * CM_TO_POINTS)

    # ── Texto al pie (o donde se ubique): la fuente se encoge para que SIEMPRE quepa
    t = cfg["texto"]
    texto = (t.get("texto") or "").strip()
    if texto and t["opacidad"] > 0:
        avail = pw - 2 * margin_x
        tw = c.stringWidth(texto, "Helvetica", t["tamano"])
        font_size = t["tamano"] if tw <= avail else (t["tamano"] * avail / tw)
        ancho = c.stringWidth(texto, "Helvetica", font_size)
        vert, horiz = _partes(t["posicion"])
        x = _x(horiz, pw, ancho, margin_x)
        y = _y(vert, ph, font_size, text_y, text_y)
        c.saveState()
        c.setFillAlpha(t["opacidad"])
        c.setFont("Helvetica", font_size)
        c.drawString(x, y, texto)
        c.restoreState()

    # ── Marca diagonal: ascendente de izquierda a derecha, con el ángulo de la
    # diagonal de la página; tamano = fracción de esa diagonal
    d = cfg["diagonal"]
    diagonal = (d.get("texto") or "").strip()
    if diagonal and d["opacidad"] > 0:
        vert, horiz = _partes(d["posicion"])
        cx = {"izquierda": 0.25, "centro": 0.5, "derecha": 0.75}[horiz] * pw
        cy = {"abajo": 0.25, "centro": 0.5, "arriba": 0.75}[vert] * ph
        ancho_1pt = c.stringWidth(diagonal, "Helvetica-Bold", 1)
        font_size = d["tamano"] * math.hypot(pw, ph) / ancho_1pt
        c.saveState()
        c.setFillGray(DIAGONAL_GRIS)
        c.setFillAlpha(d["opacidad"])
        c.translate(cx, cy)
        c.rotate(math.degrees(math.atan2(ph, pw)))
        c.setFont("Helvetica-Bold", font_size)
        c.drawCentredString(0, -0.35 * font_size, diagonal)
        c.restoreState()

    # ── Logo: ancho relativo a la página, con límites, sin desbordar
    g = cfg["logo"]
    if logo is not None and g["opacidad"] > 0:
        escala = g["tamano"] / PREDETERMINADA["logo"]["tamano"]
        logo_w = _clamp(g["tamano"] * pw, LOGO_MIN_W, min(LOGO_MAX_W * escala, pw - 2 * margin_x))
        logo_h = logo_w * logo.aspecto
        if logo_h > LOGO_MAX_H_FRAC * escala * ph:      # páginas muy anchas y bajas
            logo_h = LOGO_MAX_H_FRAC * escala * ph
            logo_w = logo_h / logo.aspecto
        vert, horiz = _partes(g["posicion"])
        x = _x(horiz, pw, logo_w, margin_x)
        y = _y(vert, ph, logo_h, margin_top, margin_top)
        c.saveState()
        c.setFillAlpha(g["opacidad"])
        c.setStrokeAlpha(g["opacidad"])
        c.drawImage(logo.reader, x=x, y=y, width=logo_w, height=logo_h,
                    preserveAspectRatio=True, mask="auto")
        c.restoreState()


def overlay(pw, ph, cfg, logo):
    from reportlab.pdfgen import canvas
    pkt = BytesIO()
    c = canvas.Canvas(pkt, pagesize=(pw, ph))
    dibujar(c, pw, ph, cfg, logo)
    c.save()
    return pkt.getvalue()


def aplicar(entrada, salida, logo_ruta, cfg):
    from pypdf import PdfReader, PdfWriter

    logo = Logo(logo_ruta) if logo_ruta else None
    reader = PdfReader(entrada)
    writer = PdfWriter()
    if reader.metadata:
        writer.add_metadata(reader.metadata)

    # Cache de overlays por tamaño de página: los libros escaneados suelen tener
    # un solo tamaño, así que el overlay se genera una vez y se reutiliza.
    cache = {}
    for page in reader.pages:
        # /Rotate se pasa al contenido para que la marca quede derecha tal como se ve
        if page.rotation:
            page.transfer_rotation_to_content()
        # Área visible (cropbox, por defecto = mediabox); su origen puede no ser (0,0)
        box = page.cropbox
        pw, ph = float(box.width), float(box.height)
        key = (round(pw, 2), round(ph, 2))
        if key not in cache:
            cache[key] = overlay(pw, ph, cfg, logo)
        wm = PdfReader(BytesIO(cache[key]))
        page.merge_translated_page(wm.pages[0], tx=float(box.left), ty=float(box.bottom))
        writer.add_page(page)

    with open(salida, "wb") as f:
        writer.write(f)
    n = len(reader.pages)
    print(f"[WATERMARK] {n} págs | {len(cache)} tamaño(s) | {n - len(cache)} overlays reutilizados del caché",
          file=sys.stderr)


def vista_previa(cfg, logo_ruta, pw=595.28, ph=841.89):
    """PDF de una página de ejemplo (líneas grises de texto) con la marca de agua."""
    from reportlab.pdfgen import canvas
    pkt = BytesIO()
    c = canvas.Canvas(pkt, pagesize=(pw, ph))
    c.setFillColorRGB(0.82, 0.84, 0.87)
    margen = 0.12 * pw
    y = ph - 0.14 * ph
    i = 0
    while y > 0.1 * ph:
        largo = (pw - 2 * margen) * (0.55 if i % 7 == 6 else 1.0)
        c.rect(margen, y, largo, 0.006 * ph, stroke=0, fill=1)
        y -= 0.022 * ph
        i += 1
    dibujar(c, pw, ph, cfg, Logo(logo_ruta) if logo_ruta else None)
    c.save()
    return pkt.getvalue()


def main():
    p = argparse.ArgumentParser(description="Estampa la marca de agua en un PDF")
    p.add_argument("--entrada", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--logo", default="")
    p.add_argument("--config", help="JSON con posición, opacidad y tamaño de cada elemento")
    p.add_argument("--texto", help="Texto al pie (sobrescribe el del JSON)")
    p.add_argument("--diagonal", help="Texto diagonal; vacío = sin marca diagonal")
    a = p.parse_args()

    config = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            config = json.load(f)
    cfg = combinar(config)
    if a.texto is not None:
        cfg["texto"]["texto"] = a.texto
    if a.diagonal is not None:
        cfg["diagonal"]["texto"] = a.diagonal
    aplicar(a.entrada, a.salida, a.logo, cfg)


if __name__ == "__main__":
    main()
