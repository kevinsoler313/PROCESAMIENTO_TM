#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   interfaz/app.py  ─  INTERFAZ DE ESCRITORIO (PySide6)
# DESC:     Ventana para preparar y lanzar main.sh:
#             1 · Preparar: CSV de metadatos, logo, libros de INPUT y opciones
#             2 · En proceso: avance por libro, recursos del equipo y log
#             3 · Resultados: lo que main.sh anota en logs/resultados_<ts>.txt
#
#           main.sh sigue siendo quien procesa; la interfaz solo lo ejecuta con
#           --csv, --logo, --book, --jobs, --ocr-jobs y --png, y lo detiene con
#           Ctrl+C (SIGINT al grupo de procesos), igual que en la terminal.
# =============================================================================

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import psutil
from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QObject, QSettings, QSize, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPalette, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QMenu, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN (mismas rutas que main.sh)
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent.parent
MAIN_SH = SCRIPT_DIR / "main.sh"
INYECTAR_SH = SCRIPT_DIR / "inyectar_metadatos.sh"
SIN_META_SUBDIR = "SIN METADATOS"     # dentro de OUTPUT; igual que en metadatos.sh
ICONO = Path(__file__).resolve().parent / "icono.png"
# Ajustes guardados (QSettings). PROCESAMIENTO_PERFIL permite probar sin tocar los del usuario.
PERFIL = os.environ.get("PROCESAMIENTO_PERFIL", "ProcesamientoOCR")
MARCA_JSON = SCRIPT_DIR / "logs" / "marca_interfaz.json"   # lo que se pasa a main.sh
META_JSON = SCRIPT_DIR / "logs" / "meta_interfaz.json"     # columnas → campos del PDF
META_JSON_INY = SCRIPT_DIR / "logs" / "meta_inyectar.json"  # lo mismo, para Inyectar
# CSV de metadatos y logo que vienen con el programa; se usan si no se eligió otro
CSV_INCLUIDO = SCRIPT_DIR / "recursos" / "TechnoMart Funza - TEMP.csv"
LOGO_INCLUIDO = SCRIPT_DIR / "recursos" / "logo.png"

# La vista previa usa el mismo código que estampa la marca en los PDF
sys.path.insert(0, str(SCRIPT_DIR))
import marca_agua as MA  # noqa: E402
import contenedores_gdoc as CG  # noqa: E402
import metadatos as MD  # noqa: E402
import registro_ocr_icr as RO  # noqa: E402
import actualizacion as ACT  # noqa: E402

VERSION = ACT.version_local()
TITULO = f"Procesamiento OCR {VERSION}"

PREVIEW_W, PREVIEW_H = 440, 622    # proporción A4; va debajo de los controles

DATOS_DIR = Path(os.environ.get("DATOS_DIR", "/mnt/datos"))

# Carpetas que se eligen en Configuración: (clave, nombre, opción de main.sh, para
# qué sirve). Por defecto cuelgan de /mnt/datos, como en main.sh.
CARPETAS = [
    ("input", "INPUT", "--input-dir", "Libros escaneados (una carpeta de TIFF por libro)"),
    ("output", "OUTPUT", "--output-dir", "Solo PDF finalizados y verificados"),
    ("review", "REVIEW", "--review-dir", "Libros con error y review_log.csv"),
    ("png", "PNG", "--png-dir", "PNG por página (solo si se exportan PNG)"),
]
# Carpetas internas: no se eligen, se crean junto a OUTPUT. TEMPORALES debe estar
# en el mismo disco que OUTPUT para que mover cada PDF final sea instantáneo.
CARPETAS_INTERNAS = [
    ("temp", "TEMPORALES", "--temp-dir"),
    ("registro", "REGISTRO", "--registro-dir"),
]


def con_internas(carpetas):
    """Completa TEMPORALES y REGISTRO al lado de OUTPUT."""
    base = Path(carpetas["output"]).parent
    return {**carpetas, **{clave: base / nombre for clave, nombre, _ in CARPETAS_INTERNAS}}


def carpetas_por_defecto():
    return con_internas({clave: DATOS_DIR / nombre for clave, nombre, _, _ in CARPETAS})


def mismo_disco(a, b):
    """True si las dos rutas (o su primer padre existente) están en el mismo disco."""
    def dispositivo(p):
        p = Path(p)
        while not p.exists() and p != p.parent:
            p = p.parent
        return os.stat(p).st_dev
    try:
        return dispositivo(a) == dispositivo(b)
    except OSError:
        return True

RAM_POR_PROCESO_GB = 0.5      # ~400-500 MB por instancia de Tesseract (OCR.sh)
RAM_POR_LIBRO_GB = 3          # post-proceso: pypdf con el PDF completo + ocrmypdf/Ghostscript
RAM_SISTEMA_GB = 2            # reserva para el sistema y la interfaz
HILOS_RESERVADOS = 2          # para el sistema y el post-proceso que corre junto al OCR
MAX_OCR_JOBS = 64
SIN_GPU = "Sin tarjeta gráfica dedicada"
# Textos de marca de agua permitidos. No se editan desde la interfaz: uno nuevo
# se agrega aquí.
# Sedes donde se usa el programa (Configuración); de la sede dependen otros datos
SEDES = CG.SEDES

TEXTOS_MARCA = [
    "CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES",
    "DIGITALIZADO CAMARA DE REPRESENTANTES_BIBLIOTECA NACIONAL DE COLOMBIA",
]
# Textos que cambiaron: el guardado en los ajustes pasa al nuevo al abrir
TEXTOS_MARCA_ANTERIORES = {
    "DIGITALIZADO CAMARA DE REPRESENTANTES_BIBLIOTECA NACIONAL":
        "DIGITALIZADO CAMARA DE REPRESENTANTES_BIBLIOTECA NACIONAL DE COLOMBIA",
}
# Marca diagonal opcional (ascendente de izquierda a derecha), también fija
MARCA_DIAGONAL = "CÁMARA DE REPRESENTANTES"


def recomendar(ram_gb, hilos, gpu):
    """Configuración más rápida que aguanta el equipo, con su explicación.

    · Libros en paralelo: el post-proceso de un libro corre mientras el OCR del
      siguiente avanza, así que cada libro extra suma ~3 GB. Con poca RAM o pocos
      hilos se deja en 1; más de 3 no acelera (todos comparten disco y CPU).
    · Procesos de OCR: cada Tesseract usa 1 hilo y ~0,5 GB. Se toma el menor entre
      los hilos libres y lo que cabe en la RAM que queda tras el post-proceso.
    · Gráfica: Tesseract no usa GPU; solo contaría si se activa PaddleOCR o Kraken.
    """
    if ram_gb >= 32 and hilos >= 16:
        libros = 3
    elif ram_gb >= 16 and hilos >= 8:
        libros = 2
    else:
        libros = 1
    ram_ocr = ram_gb - RAM_SISTEMA_GB - libros * RAM_POR_LIBRO_GB
    if ram_ocr < 2 and libros > 1:
        libros, ram_ocr = 1, ram_gb - RAM_SISTEMA_GB - RAM_POR_LIBRO_GB
    por_ram = int(max(ram_ocr, 0) / RAM_POR_PROCESO_GB)
    por_cpu = max(1, hilos - HILOS_RESERVADOS)
    ocr = max(1, min(por_ram, por_cpu, MAX_OCR_JOBS))
    limita = "los hilos" if por_cpu <= por_ram else "la RAM"

    partes = [
        f"OCR: {ocr} procesos (limita {limita}: {por_cpu} hilos libres, RAM para {por_ram}).",
        f"Libros en paralelo: {libros} (~{libros * RAM_POR_LIBRO_GB} GB reservados para el post-proceso).",
    ]
    if gpu and gpu != SIN_GPU:
        partes.append(f"La gráfica ({gpu}) no acelera Tesseract; se usaría solo con PaddleOCR o Kraken.")
    return ocr, libros, " ".join(partes)


def detectar_hardware():
    """RAM total (GB), hilos y gráficas encontradas en este equipo."""
    ram = round(psutil.virtual_memory().total / 1024 ** 3)
    hilos = os.cpu_count() or 1
    gpus = []
    try:
        salida = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=10).stdout
        for linea in salida.strip().splitlines():
            nombre, _, memoria = linea.partition(",")
            gpus.append(f"{nombre.strip()} ({memoria.strip()})")
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not gpus:
        try:
            salida = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10).stdout
            gpus = [l.split(": ", 1)[1] for l in salida.splitlines()
                    if ("VGA" in l or "3D controller" in l) and ": " in l]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ram, hilos, gpus

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Fases que se reconocen en el log de main.sh, en orden. (texto, paso, % del libro)
FASES = [
    ("Aplicando marca de agua", 3, 72), ("Marca de agua ya existe", 3, 72),
    ("Convirtiendo a PDF/A", 4, 80), ("PDF/A ya existe", 4, 80),
    ("Insertando metadatos", 5, 88), ("Sin metadatos en CSV", 5, 88),
    ("TIFFs totales", 6, 92), ("Registrado", 7, 97),
]
NOMBRE_PASO = {2: "OCR", 3: "Marca de agua", 4: "PDF/A", 5: "Metadatos",
               6: "Verificación", 7: "Registro"}

LOGO_TM = Path(__file__).resolve().parent / "logo_technomart.jpg"

# Sistema visual (DESIGN.md): planos azul-negro planos, separados por aros de 1 px,
# radios de 6 px en controles y 8 px en tarjetas, sin sombras ni degradados. La
# "señal" de acento es el rojo de TechnoMart (acciones principales y estado
# activo) y el azul de marca va en foco, avance y enlaces. El modo claro es el
# mismo sistema invertido sobre planos blancos.
TEMAS = {
    "oscuro": {
        "fondo": "#020618",        # Void Canvas: lienzo, barra superior, controles oscuros
        "card": "#080e21",         # Midnight Panel: tarjetas, barra lateral, tablas
        "panel": "#0f172b",        # Deep Slate: pestaña/ítem activo, paneles internos
        "barra": "#020618",
        "borde": "#1d293d",        # Steel Outline: aros de tarjetas y separadores
        "borde_fuerte": "#314158",  # Blue Steel: aros de controles
        "texto": "#e2e8f0",        # Pale Frost
        "titulo": "#ffffff",       # Signal White
        "texto_sub": "#90a1b9",    # Muted Fog
        "acento": "#4c9fe6", "acento_suave": "#0f2340",
        "rojo": "#e1283a", "rojo_hover": "#c81d2f", "rojo_off": "#4a1d26",
        "off": "#4a5a73", "log_fondo": "#020618", "log_texto": "#e2e8f0",
        "scroll": "#314158", "hover": "#0f172b",
        "nav_fondo": "#080e21", "nav_borde": "#1d293d", "nav_texto": "#90a1b9", "nav_hover": "#0f172b",
        "nav_activo": "#0f172b", "nav_texto_activo": "#ffffff", "nav_titulo": "#5d6d87", "nav_pie": "#5d6d87",
        "nav_icono": "#90a1b9", "nav_icono_activo": "#ffffff",
        "ok": ("#062b1f", "#4ade80"), "warn": ("#2e2108", "#fbbf24"), "error": ("#33101a", "#ff8a95"),
        "gris": ("#0f172b", "#90a1b9"), "info": ("#0d2238", "#7cc0f5"),
    },
    "claro": {
        "fondo": "#f8fafc", "card": "#ffffff", "panel": "#f1f5f9", "barra": "#ffffff",
        "borde": "#e2e8f0", "borde_fuerte": "#cbd5e1",
        "texto": "#1e293b", "titulo": "#0f172b", "texto_sub": "#64748b",
        "acento": "#0c5aa6", "acento_suave": "#e8f1fb",
        "rojo": "#c01120", "rojo_hover": "#a00e1b", "rojo_off": "#ecb8bd",
        "off": "#a3b1c2", "log_fondo": "#020618", "log_texto": "#e2e8f0",
        "scroll": "#cbd5e1", "hover": "#f1f5f9",
        "nav_fondo": "#ffffff", "nav_borde": "#e2e8f0", "nav_texto": "#475569", "nav_hover": "#f1f5f9",
        "nav_activo": "#f1f5f9", "nav_texto_activo": "#0f172b", "nav_titulo": "#94a3b8", "nav_pie": "#94a3b8",
        "nav_icono": "#64748b", "nav_icono_activo": "#c01120",
        "ok": ("#e7f6ec", "#15803d"), "warn": ("#fdf3dc", "#a16207"), "error": ("#fde8ea", "#b3121f"),
        "gris": ("#f1f5f9", "#475569"), "info": ("#e8f1fb", "#0c5aa6"),
    },
}

FUENTE_TEXTO = ["Public Sans", "Inter", "Segoe UI", "Noto Sans", "DejaVu Sans", "sans-serif"]
FUENTE_TITULOS = '"Public Sans", "Inter", "Segoe UI", "DejaVu Sans", sans-serif'
# Flechas de desplegables y campos numéricos (gris del sistema, sirven en ambos temas)
FLECHA_ABAJO = (Path(__file__).resolve().parent / "flecha_abajo.svg").as_posix()
FLECHA_ARRIBA = (Path(__file__).resolve().parent / "flecha_arriba.svg").as_posix()
CHECK = (Path(__file__).resolve().parent / "check.svg").as_posix()
FUENTE_MONO = '"SFMono-Regular", "Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono", monospace'


def estilo(nombre_tema):
    """Hoja de estilos completa del tema. Los colores de estado van por propiedad
    'tipo' (pill, métricas), así un cambio de tema los repinta todos."""
    t = TEMAS[nombre_tema]
    pills = "".join(
        f'QLabel#pill[tipo="{k}"] {{ background: {t[k][0]}; color: {t[k][1]}; }}\n'
        for k in ("ok", "warn", "error", "gris", "info"))
    return f"""
QMainWindow, QScrollArea, #contenido {{ background: {t['fondo']}; }}
QWidget {{ color: {t['texto']}; font-size: 13px; }}
QToolTip {{ background: {t['card']}; color: {t['texto']}; border: 1px solid {t['borde_fuerte']};
            border-radius: 6px; padding: 6px 8px; }}

/* Barra lateral: plano contenido (como el árbol de archivos de un editor) */
QFrame#nav {{ background: {t['nav_fondo']}; border-right: 1px solid {t['nav_borde']}; }}
QFrame#logoChip {{ background: #ffffff; border-radius: 6px; }}
QLabel#navApp {{ color: {t['titulo']}; font-family: {FUENTE_TITULOS}; font-size: 15px; font-weight: 700;
                 letter-spacing: -0.3px; }}
QLabel#navAppSub {{ color: {t['nav_titulo']}; font-size: 12px; }}
QLabel#navTitulo {{ color: {t['nav_titulo']}; font-size: 10px; font-weight: 600; letter-spacing: 1.2px;
                    padding: 22px 22px 8px 22px; }}
QPushButton#navBoton {{ text-align: left; padding: 8px 10px; margin: 1px 12px; border: none;
    border-radius: 6px; background: transparent; color: {t['nav_texto']};
    font-weight: 500; font-size: 13px; }}
QPushButton#navBoton:hover {{ background: {t['nav_hover']}; color: {t['nav_texto_activo']}; border: none; }}
QPushButton#navBoton:checked {{ background: {t['nav_activo']}; color: {t['nav_texto_activo']}; font-weight: 600; }}
QFrame#navSeparador {{ background: {t['nav_borde']}; margin: 0 16px; }}
QLabel#navPie {{ color: {t['nav_pie']}; font-size: 11px; padding: 0 22px; }}

/* Barra superior de la sección: 64 px, línea de 1 px abajo */
QFrame#topbar {{ background: {t['barra']}; border-bottom: 1px solid {t['borde']}; }}
QLabel#pageTitulo {{ color: {t['titulo']}; font-family: {FUENTE_TITULOS}; font-size: 20px; font-weight: 700;
                     letter-spacing: -0.5px; }}
QLabel#pageSub {{ color: {t['texto_sub']}; font-size: 12px; }}

/* Pestañas tipo editor: la activa sobre Deep Slate, las demás transparentes */
QTabWidget#pestanas::pane {{ border: none; }}
QTabWidget#pestanas QTabBar {{ background: {t['barra']}; border-bottom: 1px solid {t['borde']}; }}
QTabWidget#pestanas QTabBar::tab {{ background: transparent; color: {t['texto_sub']}; border: none;
    border-radius: 6px; padding: 6px 12px; margin: 10px 2px 10px 2px; font-weight: 500; font-size: 13px; }}
QTabWidget#pestanas QTabBar::tab:first {{ margin-left: 24px; }}
QTabWidget#pestanas QTabBar::tab:hover {{ color: {t['texto']}; }}
QTabWidget#pestanas QTabBar::tab:selected {{ background: {t['panel']}; color: {t['titulo']};
    border: 1px solid {t['borde_fuerte']}; }}

/* Tarjetas: Midnight Panel con aro de 1 px, sin sombra */
QFrame#card {{ background: {t['card']}; border: 1px solid {t['borde']}; border-radius: 8px; }}
QFrame#lineaRoja {{ background: {t['rojo']}; border-radius: 1px; }}
QFrame#selector {{ background: {t['fondo']}; border: 1px solid {t['borde']}; border-radius: 8px; }}
QFrame#metrica {{ background: {t['fondo']}; border: 1px solid {t['borde']}; border-radius: 8px; }}
QLabel#antetitulo {{ color: {t['rojo']}; font-size: 10px; font-weight: 600; letter-spacing: 1.2px; }}
QLabel#titulo {{ color: {t['titulo']}; font-family: {FUENTE_TITULOS}; font-size: 18px; font-weight: 700;
                 letter-spacing: -0.4px; }}
QLabel#sub {{ color: {t['texto_sub']}; font-size: 12px; }}
QLabel#subtitulo {{ color: {t['titulo']}; font-weight: 600; font-size: 13px; }}
QLabel#aviso {{ color: {t['rojo']}; font-size: 12px; }}
QLabel#miniatura {{ background: #ffffff; border: 1px solid {t['borde']}; border-radius: 6px;
                    font-size: 11px; color: #64748b; }}
QLabel#pill {{ border-radius: 11px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}
{pills}
QLabel#metricaValor {{ color: {t['titulo']}; font-family: {FUENTE_TITULOS}; font-size: 28px; font-weight: 700;
                       letter-spacing: -0.7px; }}
QLabel#metricaValor[tipo="error"] {{ color: {t['error'][1]}; }}
QLabel#metricaValor[tipo="warn"] {{ color: {t['warn'][1]}; }}

/* Botones: contorno oscuro por defecto; la señal roja solo en la acción principal */
QPushButton {{ padding: 6px 12px; border: 1px solid {t['borde_fuerte']}; border-radius: 6px;
               background: transparent; color: {t['texto']}; font-weight: 500; font-size: 13px; }}
QPushButton:hover {{ background: {t['hover']}; color: {t['titulo']}; border-color: {t['borde_fuerte']}; }}
QPushButton:pressed {{ background: {t['panel']}; }}
QPushButton:disabled {{ color: {t['off']}; border-color: {t['borde']}; background: transparent; }}
QPushButton#primario {{ background: {t['rojo']}; color: #ffffff; border: none; padding: 8px 16px;
                        font-size: 14px; font-weight: 600; }}
QPushButton#primario:hover {{ background: {t['rojo_hover']}; color: #ffffff; }}
QPushButton#primario:disabled {{ background: {t['rojo_off']}; color: {t['off']}; }}
QPushButton#tema {{ padding: 6px 10px; }}

/* Campos: plano oscuro con aro Blue Steel; foco en azul de marca */
QLineEdit, QSpinBox, QComboBox {{ background: {t['fondo']}; border: 1px solid {t['borde_fuerte']};
    border-radius: 6px; padding: 5px 8px; min-height: 20px; color: {t['texto']};
    selection-background-color: {t['acento']}; selection-color: #ffffff; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {t['acento']}; }}
QLineEdit[readOnly="true"] {{ background: {t['panel']}; border: 1px solid {t['borde']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t['off']}; background: {t['card']};
    border-color: {t['borde']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px;
    border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url({FLECHA_ABAJO}); width: 10px; height: 10px; }}
QSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 20px; border: none;
    background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 3px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 3px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {t['panel']}; border-radius: 4px; }}
QSpinBox::up-arrow {{ image: url({FLECHA_ARRIBA}); width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: url({FLECHA_ABAJO}); width: 9px; height: 9px; }}
QComboBox {{ combobox-popup: 0; }}
QComboBox QAbstractItemView {{ background: {t['card']}; border: 1px solid {t['borde_fuerte']};
    selection-background-color: {t['panel']}; selection-color: {t['titulo']}; outline: none; }}

/* Casillas: aro Blue Steel; marcadas, en rojo de marca con ✓ blanca */
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QTableWidget::indicator {{ width: 14px; height: 14px; border-radius: 4px;
    border: 1px solid {t['borde_fuerte']}; background: {t['fondo']}; }}
QCheckBox::indicator:hover, QTableWidget::indicator:hover {{ border-color: {t['acento']}; }}
QCheckBox::indicator:checked, QTableWidget::indicator:checked {{ background: {t['rojo']}; border-color: {t['rojo']};
    image: url({CHECK}); }}
QCheckBox::indicator:disabled, QTableWidget::indicator:disabled {{ background: {t['card']}; border-color: {t['borde']}; }}

/* Barras de avance y deslizador */
QProgressBar {{ border: none; background: {t['borde']}; border-radius: 3px; }}
QProgressBar::chunk {{ background: {t['acento']}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: {t['borde']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['acento']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    background: {t['titulo']}; border: 2px solid {t['acento']}; }}

/* Tablas: plano contenido con aro de 1 px */
QTableWidget {{ background: {t['card']}; alternate-background-color: {t['fondo']}; border: 1px solid {t['borde']};
    border-radius: 8px; gridline-color: transparent; }}
QTableWidget::item {{ padding: 0 6px; border-bottom: 1px solid {t['borde']}; }}
QTableWidget::item:disabled {{ color: {t['off']}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {t['card']}; border: none; border-bottom: 1px solid {t['borde']};
    padding: 8px 6px; color: {t['texto_sub']}; font-weight: 600; font-size: 12px; }}
QTableCornerButton::section {{ background: {t['card']}; border: none; }}

/* Log: como un fragmento de terminal */
QPlainTextEdit {{ background: {t['log_fondo']}; color: {t['log_texto']}; border: 1px solid {t['borde']};
    border-radius: 6px; font-family: {FUENTE_MONO}; font-size: 12px; padding: 8px 12px; }}

/* Barras de desplazamiento finas */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['scroll']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; height: 0; width: 0; }}
QMessageBox, QFileDialog {{ background: {t['card']}; color: {t['texto']}; }}
"""


def paleta(nombre_tema):
    """Paleta de Qt del tema: con ella Fusion dibuja casillas, flechas y diálogos
    con los colores correctos (la hoja de estilos no los cubre)."""
    t = TEMAS[nombre_tema]
    p = QPalette()
    for rol, color in [
        (QPalette.Window, t["fondo"]), (QPalette.WindowText, t["texto"]),
        (QPalette.Base, t["card"]), (QPalette.AlternateBase, t["panel"]),
        (QPalette.Text, t["texto"]), (QPalette.Button, t["card"]),
        (QPalette.ButtonText, t["texto"]), (QPalette.Highlight, t["acento"]),
        (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, t["card"]), (QPalette.ToolTipText, t["texto"]),
        (QPalette.PlaceholderText, t["texto_sub"]), (QPalette.Link, t["acento"]),
    ]:
        p.setColor(rol, QColor(color))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(t["off"]))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t["off"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["off"]))
    return p


ICONOS_NAV = {
    "procesar": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M3 13h18"/>',
    "metadatos": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
    "contenedor": '<path d="M3 4h18v4H3z"/><path d="M5 8h14v12H5z"/><path d="M10 12h4"/>',
    "registro": '<path d="M5 3h14v18H5z"/><path d="M9 8h6M9 12h6M9 16h3"/>',
    "configuracion": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3'
                     'M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
}


def icono_nav(nombre, tema="oscuro"):
    """Ícono de trazo: gris en reposo; en la sección activa, blanco (oscuro) o rojo (claro)."""
    t = TEMAS[tema]
    icono = QIcon()
    for color, estado in ((t["nav_icono"], QIcon.Off), (t["nav_icono_activo"], QIcon.On)):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" '
               f'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
               f'stroke-linejoin="round">{ICONOS_NAV[nombre]}</svg>')
        pix = QPixmap()
        pix.loadFromData(svg.encode(), "SVG")
        icono.addPixmap(pix, QIcon.Normal, estado)
    return icono


def pill(texto, tipo):
    lbl = QLabel(texto, objectName="pill")
    lbl.setProperty("tipo", tipo)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def tipo_pill(lbl, texto, tipo):
    """Cambia texto y color de una pill ya creada (el color lo pone la hoja de estilos)."""
    lbl.setText(texto)
    lbl.setProperty("tipo", tipo)
    lbl.style().unpolish(lbl)
    lbl.style().polish(lbl)


def nombre_libro(carpeta):
    """Mismo recorte que el paso 1 de main.sh: 'AR10582 OK' → 'AR10582'."""
    return re.sub(r"\s+[Oo][Kk]$", "", carpeta).rstrip()


def es_tiff(nombre):
    return nombre.lower().endswith((".tif", ".tiff")) and not nombre.startswith("_")


def formato_gb(nbytes):
    gb = nbytes / 1024 ** 3
    return f"{gb:,.1f} GB".replace(",", "X").replace(".", ",").replace("X", ".") if gb >= 0.1 \
        else f"{nbytes / 1024 ** 2:,.0f} MB".replace(",", ".")


# ─────────────────────────────────────────────────────────────────────────────
# CSV DE METADATOS → CAMPOS DEL PDF (Procesamiento › METADATOS e Inyectar)
# ─────────────────────────────────────────────────────────────────────────────

class ComboCompleto(QComboBox):
    """Combo angosto cuya lista desplegable se abre al ancho del texto más largo."""

    def showPopup(self):
        vista = self.view()
        vista.setTextElideMode(Qt.ElideNone)
        vista.setMinimumWidth(vista.sizeHintForColumn(0) + 2 * vista.frameWidth() + 24)
        super().showPopup()


class PanelMetadatos(QObject):
    """CSV de metadatos, qué columna es el código del libro y cómo se arma cada campo
    del PDF. Procesamiento e Inyectar tienen uno cada uno, con su CSV y su
    configuración guardados aparte en los ajustes."""
    cambiado = Signal()       # otro CSV o cambió la configuración: hay que recontar

    def __init__(self, ventana, antetitulo, titulo_csv, clave_csv, clave_cfg, cfg_inicial=None):
        super().__init__(ventana)
        self.v, self.ajustes = ventana, ventana.ajustes
        self.clave_csv, self.clave_cfg = clave_csv, clave_cfg
        self.titulo_csv = titulo_csv
        self.cfg = self._leer_cfg(cfg_inicial)
        self.filas, self.codigos, self.n_codigos, self.avisos = [], None, 0, []
        self._pintando = False

        # ── CSV ──
        lay, _ = ventana._card(antetitulo, titulo_csv)
        grid = QGridLayout()
        caja, _, self.nombre, self.detalle, b = ventana._selector("CSV")
        b.clicked.connect(self.elegir)
        self.detalle.linkActivated.connect(
            lambda _: QMessageBox.information(ventana, "Avisos del CSV", "\n".join(self.avisos)))
        grid.addWidget(caja, 0, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.lay_csv = lay
        self.widgets = [b]

        # ── Columnas del CSV ──
        lay, _ = ventana._card(antetitulo, "Columnas del CSV")
        self.info = QLabel(objectName="sub")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        fila = QHBoxLayout()
        fila.setSpacing(12)
        self.encabezado = QCheckBox("La primera fila es encabezado (nombres de columna)")
        self.encabezado.toggled.connect(self._encabezado_cambiado)
        fila.addWidget(self.encabezado)
        fila.addSpacing(24)
        fila.addWidget(QLabel("Código del libro", objectName="subtitulo"))
        self.codigo = QComboBox()
        self.codigo.setMinimumWidth(260)
        self.codigo.setToolTip("Columna que se compara con el nombre del libro")
        self.codigo.currentIndexChanged.connect(self._codigo_cambiado)
        fila.addWidget(self.codigo)
        fila.addStretch()
        lay.addLayout(fila)
        self.tabla = QTableWidget(0, 0)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setSelectionMode(QTableWidget.NoSelection)
        self.tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tabla.setFocusPolicy(Qt.NoFocus)
        self.tabla.setAlternatingRowColors(True)
        self.tabla.verticalHeader().setDefaultSectionSize(30)
        self.tabla.setFixedHeight(30 * 6 + 8)
        lay.addWidget(self.tabla)

        # ── Campos del PDF ──
        lay, fila = ventana._card(antetitulo, "Campos del PDF")
        b = QPushButton("Restablecer predeterminados")
        b.clicked.connect(self._predeterminado)
        fila.addWidget(b)
        self.widgets += [self.encabezado, self.codigo, b]
        nota = QLabel("Cada campo es texto libre con columnas del CSV: «+ Columna» agrega {N} donde está "
                      "el cursor. Ej.: Congreso de la República - {3}. Un campo vacío no se escribe en el "
                      "PDF. En PDF/A, Asunto y Descripción ocupan el mismo lugar: si Asunto tiene texto, "
                      "Descripción no se escribe.", objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        self.plantilla, self.ejemplo = {}, {}
        for n, (campo, nombre) in enumerate(MD.CAMPOS):
            etiqueta = QLabel(nombre, objectName="subtitulo")
            etiqueta.setToolTip(f"Campo {campo} del PDF")
            grid.addWidget(etiqueta, 2 * n, 0)
            linea = QLineEdit()
            linea.setPlaceholderText("(no se escribe)")
            linea.textChanged.connect(self._plantilla_cambiada)
            grid.addWidget(linea, 2 * n, 1)
            boton = QPushButton("+ Columna")
            boton.clicked.connect(lambda _=False, l=linea, b=boton: self._menu_columnas(l, b))
            grid.addWidget(boton, 2 * n, 2)
            ejemplo = QLabel(objectName="sub")
            ejemplo.setWordWrap(True)          # un ejemplo largo no ensancha la página
            ejemplo.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(ejemplo, 2 * n + 1, 1, 1, 2)
            self.plantilla[campo], self.ejemplo[campo] = linea, ejemplo
            self.widgets += [linea, boton]
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.timer = QTimer(self, singleShot=True, interval=400, timeout=self._aplicar)
        self.pintar()

    # ── Configuración guardada ──

    def _leer_cfg(self, cfg_inicial):
        guardada = self.ajustes.value(self.clave_cfg, "")
        try:
            return MD.combinar(json.loads(guardada) if guardada else (cfg_inicial or {}))
        except ValueError:
            return MD.combinar(cfg_inicial or {})

    def _guardar(self, repintar=False):
        self.ajustes.setValue(self.clave_cfg, json.dumps(self.cfg, ensure_ascii=False))
        if repintar:
            self.pintar()
        self._aplicar()

    def escribir_json(self, ruta):
        """La configuración en un archivo, para pasarla a main.sh o inyectar_metadatos.sh."""
        ruta = Path(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(ruta)

    @property
    def ruta(self):
        return self.ajustes.value(self.clave_csv, "")

    # ── CSV ──

    def elegir(self):
        inicio = self.ruta or self.ajustes.value("csv", "") or str(Path.home())
        ruta, _ = QFileDialog.getOpenFileName(self.v, self.titulo_csv, inicio, "CSV (*.csv);;Todos (*)")
        if ruta:
            self.cargar(ruta)

    def cargar(self, ruta, avisar=True):
        try:
            filas = MD.leer_filas(ruta)
        except OSError as e:
            QMessageBox.warning(self.v, self.titulo_csv, f"No se pudo leer el CSV.\n{e}")
            return
        if not filas:
            QMessageBox.warning(self.v, self.titulo_csv, "El archivo está vacío.")
            return
        self.filas = filas
        self.ajustes.setValue(self.clave_csv, ruta)
        self.nombre.setText(Path(ruta).name)
        self.pintar()
        self._revisar()
        if avisar and not self.codigos:
            QMessageBox.information(
                self.v, self.titulo_csv,
                "No se encontraron códigos de libro en la columna elegida. Revisa en «Columnas del "
                "CSV» qué columna es el código del libro.")

    def _revisar(self):
        if not self.filas:
            return
        self.codigos, self.n_codigos, self.avisos = MD.revisar(self.filas, self.cfg)
        self.pintar_detalle()
        self.cambiado.emit()

    def pintar_detalle(self):
        # El enlace va en HTML, así que su color se pone a mano según el tema
        if self.codigos is None:
            return
        detalle = f"{self.n_codigos:,} códigos".replace(",", ".")
        if self.avisos:
            color = TEMAS[self.v.tema]["warn"][1]
            detalle += f' · <a href="#" style="color:{color};">{len(self.avisos)} aviso(s)</a>'
        self.detalle.setText(detalle)

    # ── Columnas y plantillas ──

    def _columnas(self):
        """[(n, nombre, ejemplo)] de las columnas del CSV cargado."""
        encabezado, datos = MD.separar(self.filas, self.cfg)
        ejemplo = datos[0] if datos else []
        return [(n, (MD.celda(encabezado, n) if encabezado else "") or f"Columna {n}", MD.celda(ejemplo, n))
                for n in range(1, MD.num_columnas(self.filas) + 1)]

    def pintar(self):
        """Refleja self.cfg y el CSV cargado en los controles, sin volver a guardar."""
        self._pintando = True
        hay_csv = bool(self.filas)
        self.encabezado.setChecked(MD.con_encabezado(self.filas, self.cfg) if hay_csv
                                   else bool(self.cfg.get("encabezado")))
        columnas = self._columnas()
        self.codigo.clear()
        n_guardada = int(self.cfg["codigo"])
        for n, nombre, ejemplo in columnas or [(n_guardada, f"Columna {n_guardada}", "")]:
            self.codigo.addItem(f"{{{n}}} · {nombre}" + (f"  (ej. {ejemplo})" if ejemplo else ""), n)
        self.codigo.setCurrentIndex(max(self.codigo.findData(n_guardada), 0))
        for campo, linea in self.plantilla.items():
            if linea.text() != self.cfg["campos"].get(campo, ""):
                linea.setText(self.cfg["campos"].get(campo, ""))

        _, datos = MD.separar(self.filas, self.cfg)
        self.tabla.clear()
        self.tabla.setColumnCount(len(columnas))
        self.tabla.setHorizontalHeaderLabels([f"{{{n}}} · {nombre}" for n, nombre, _ in columnas])
        self.tabla.setRowCount(min(5, len(datos)))
        for i, fila in enumerate(datos[:5]):
            for n in range(1, len(columnas) + 1):
                self.tabla.setItem(i, n - 1, QTableWidgetItem(MD.celda(fila, n)))
        self.tabla.resizeColumnsToContents()
        self.tabla.setVisible(hay_csv)
        if hay_csv:
            self.info.setText(f"CSV: {Path(self.ruta).name} · {len(columnas)} columnas · "
                              f"{len(datos):,} filas de datos. Vista de las primeras 5:".replace(",", "."))
        else:
            self.info.setText("Elige el CSV arriba para ver sus columnas.")
        self._ejemplos()
        self._pintando = False

    def _ejemplos(self):
        _, datos = MD.separar(self.filas, self.cfg)
        fila = datos[0] if datos else None
        codigo = MD.celda(fila, int(self.cfg["codigo"])) if fila else ""
        for campo, etiqueta in self.ejemplo.items():
            plantilla = self.plantilla[campo].text()
            if fila is None or not plantilla.strip():
                etiqueta.setText("")
                continue
            fuera = [n for n in MD.COLUMNA.findall(plantilla) if int(n) > MD.num_columnas(self.filas)]
            etiqueta.setText(f"Ej. {codigo}: {MD.armar(plantilla, fila)}"
                             + (f"   ⚠ el CSV no tiene la columna {{{fuera[0]}}}" if fuera else ""))

    def _menu_columnas(self, linea, boton):
        menu = QMenu(self.v)
        columnas = self._columnas()
        if not columnas:
            menu.addAction("Primero elige el CSV").setEnabled(False)
        for n, nombre, ejemplo in columnas:
            accion = menu.addAction(f"{{{n}}} · {nombre}" + (f"  — {ejemplo[:40]}" if ejemplo else ""))
            accion.triggered.connect(lambda _=False, n=n: (linea.insert(f"{{{n}}}"), linea.setFocus()))
        menu.exec(boton.mapToGlobal(boton.rect().bottomLeft()))

    def _encabezado_cambiado(self, valor):
        if not self._pintando:
            self.cfg["encabezado"] = bool(valor)
            self._guardar(repintar=True)

    def _codigo_cambiado(self, *_):
        if self._pintando or self.codigo.currentData() is None:
            return
        self.cfg["codigo"] = int(self.codigo.currentData())
        self._guardar()
        self._ejemplos()

    def _plantilla_cambiada(self, *_):
        if self._pintando:
            return
        for campo, linea in self.plantilla.items():
            self.cfg["campos"][campo] = linea.text()
        self._ejemplos()
        self.timer.start()            # guarda y revisa el CSV al dejar de escribir

    def _predeterminado(self):
        self.cfg = MD.combinar()
        self._guardar(repintar=True)

    def _aplicar(self):
        self.ajustes.setValue(self.clave_cfg, json.dumps(self.cfg, ensure_ascii=False))
        self._revisar()


# ─────────────────────────────────────────────────────────────────────────────
# PROCESO main.sh EN SEGUNDO PLANO
# ─────────────────────────────────────────────────────────────────────────────

class Ejecucion(QObject):
    linea = Signal(str)
    terminado = Signal(int)

    def __init__(self, args, script=None):
        super().__init__()
        self.proc = subprocess.Popen(
            ["bash", str(script or MAIN_SH), *args], cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True,   # grupo propio: Detener = Ctrl+C a todo el grupo
        )
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self):
        for linea in self.proc.stdout:
            self.linea.emit(ANSI.sub("", linea.rstrip("\n")))
        self.terminado.emit(self.proc.wait())

    def detener(self):
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass


class Escaner(QObject):
    """Cuenta páginas y peso de cada libro de INPUT sin congelar la ventana."""
    listo = Signal(list)

    def escanear(self, input_dir):
        threading.Thread(target=self._trabajo, args=(Path(input_dir),), daemon=True).start()

    def _trabajo(self, input_dir):
        libros = []
        if input_dir.is_dir():
            for d in sorted(input_dir.iterdir()):
                if not d.is_dir():
                    continue
                paginas, peso = 0, 0
                try:
                    for e in os.scandir(d):
                        if e.is_file():
                            peso += e.stat().st_size
                            if es_tiff(e.name):
                                paginas += 1
                except OSError:
                    pass
                libros.append({"carpeta": d.name, "libro": nombre_libro(d.name),
                               "paginas": paginas, "peso": peso})
        self.listo.emit(libros)


# ─────────────────────────────────────────────────────────────────────────────
# VENTANA PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class Ventana(QMainWindow):
    paginas_listas = Signal(int, int, int)    # turno, fila, páginas (-1 = no se pudo leer)
    actualizacion_lista = Signal(str, object)  # paso ('buscar' | 'instalar'), resultado o ErrorActualizacion
    ocricr_evento = Signal(str, int, object)   # 'fila' | 'hash' | 'fin', turno o avance, datos

    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITULO)
        self.setWindowIcon(QIcon(str(ICONO)))
        self.resize(1320, 940)
        self.ajustes = QSettings("TechnoMart", PERFIL)

        self.logo_ruta = None
        self.tema = "claro"
        self._aplicando = False   # True mientras la app cambia valores (no es el usuario)
        self.libros = []
        self.carpetas = carpetas_por_defecto()
        self._vistos = set()
        self.ejecucion = None
        self.activos = {}          # libro -> {"paso", "pct", "barra", "texto"}
        self.paginas = {}          # libro -> páginas (para el avance del OCR)
        self.resultados_txt = None

        # Barra lateral (logo y secciones) a la izquierda; a la derecha, la barra
        # superior de la sección y sus páginas. PROCESAMIENTO tiene además pestañas.
        raiz = QWidget(objectName="contenido")
        cuerpo = QHBoxLayout(raiz)
        cuerpo.setContentsMargins(0, 0, 0, 0)
        cuerpo.setSpacing(0)
        cuerpo.addWidget(self._barra_lateral())
        derecha = QVBoxLayout()
        derecha.setContentsMargins(0, 0, 0, 0)
        derecha.setSpacing(0)
        derecha.addWidget(self._barra_superior())
        self.secciones = QStackedWidget()
        derecha.addWidget(self.secciones, 1)
        cuerpo.addLayout(derecha, 1)
        self.setCentralWidget(raiz)

        # ── Sección PROCESAMIENTO (con pestañas) ──
        self.tabs = QTabWidget(objectName="pestanas")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)   # sin la línea clara que Qt dibuja bajo las pestañas
        self._seccion_nav("PROCESAMIENTO", self.tabs, "procesar",
                          "Libros escaneados → PDF/A con OCR, marca de agua y metadatos")

        self.col = self._pestana("PROCESAR")
        self._seccion_libros()
        self._seccion_proceso()
        self._seccion_resultados()
        self.col.addStretch()

        self.col = self._pestana("METADATOS")
        self.meta = PanelMetadatos(self, "METADATOS", "CSV de metadatos", "csv", "meta_config")
        self.meta.cambiado.connect(lambda: self._mostrar_libros(self.libros))
        self.col.addStretch()

        self.col = self._pestana("MARCAS DE AGUA")
        self._seccion_marcas()
        self.col.addStretch()

        self.col = self._pestana("CARPETAS DE TRABAJO")
        self._seccion_carpetas()
        self.col.addStretch()

        self.col = self._pestana("COMPONENTES")
        self._seccion_componentes()
        self.col.addStretch()

        # ── Sección INYECTAR METADATOS ──
        self.col = self._pagina("INYECTAR METADATOS", "metadatos",
                                "Completa los PDF que quedaron sin metadatos")
        self._seccion_metadatos()
        self.col.addStretch()

        # ── Sección CREAR CONTENEDORES GDOC ──
        self.col = self._pagina("CREAR CONTENEDORES GDOC", "contenedor",
                                "Excel para importar en GDOC los libros terminados")
        self._seccion_gdoc()
        self.col.addStretch()

        # ── Sección CREAR REGISTRO OCR-ICR ──
        self.col = self._pagina("CREAR REGISTRO OCR-ICR", "registro",
                                "CSV de registro de una entrega: imágenes, pesos y SHA-256 de cada libro")
        self._seccion_ocricr()
        self.col.addStretch()

        # ── Sección CONFIGURACIÓN: al pie de la barra, aparte de las de trabajo ──
        self.nav_lay.addStretch()
        separador = QFrame(objectName="navSeparador")
        separador.setFixedHeight(1)
        self.nav_lay.addWidget(separador)
        self.nav_lay.addSpacing(8)
        self.col = self._pagina("CONFIGURACIÓN", "configuracion", "Sede del equipo y versión del programa")
        self._seccion_configuracion_general()
        self._seccion_version()
        self.col.addStretch()
        self.nav_lay.addSpacing(10)
        self.nav_pie = QLabel(objectName="navPie")
        self.nav_lay.addWidget(self.nav_pie)
        self._pintar_sede()
        self.paginas_listas.connect(self._gdoc_pagina_lista)
        self.ocricr_evento.connect(self._ocricr_evento)
        self.actualizacion_lista.connect(self._actualizacion_lista)
        for combo in self.findChildren(QComboBox):
            combo.activated.connect(combo.hidePopup)
        self._ir_a(int(self.ajustes.value("seccion", 0)))
        self.tabs.setCurrentIndex(int(self.ajustes.value("pestana", 0)))
        self.tabs.currentChanged.connect(lambda i: self.ajustes.setValue("pestana", i))

        self.escaner = Escaner()
        self.escaner.listo.connect(self._mostrar_libros)

        self.reloj = QTimer(self, interval=1000, timeout=self._tic)
        self.reloj.start()
        psutil.cpu_percent(None)

        self.aplicar_tema(self.ajustes.value("tema", "oscuro"))
        self._restaurar_ajustes()
        self.refrescar_libros()

    # ── Construcción ────────────────────────────────────────────────────────

    @staticmethod
    def _desplazable():
        """Área desplazable para apilar tarjetas: (widget, layout)."""
        cont = QWidget(objectName="contenido")
        col = QVBoxLayout(cont)
        col.setContentsMargins(22, 18, 22, 22)
        col.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(cont)
        return scroll, col

    def _pestana(self, nombre):
        """Pestaña de PROCESAMIENTO; devuelve el layout donde se apilan sus tarjetas."""
        scroll, col = self._desplazable()
        self.tabs.addTab(scroll, nombre)
        return col

    def _pagina(self, nombre, icono, descripcion):
        """Sección propia en la barra lateral, sin pestañas."""
        scroll, col = self._desplazable()
        self._seccion_nav(nombre, scroll, icono, descripcion)
        return col

    def _seccion_nav(self, nombre, widget, icono, descripcion):
        indice = self.secciones.addWidget(widget)
        titulo = nombre.capitalize().replace("gdoc", "GDOC").replace("ocr-icr", "OCR-ICR")
        b = QPushButton(f"  {titulo}", objectName="navBoton")
        b.setIcon(icono_nav(icono, self.tema))
        b.setProperty("icono", icono)
        b.setIconSize(QSize(18, 18))
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _=False, i=indice: self._ir_a(i))
        self.nav_lay.addWidget(b)
        self.botones_nav.append((b, titulo, descripcion))

    def _ir_a(self, indice):
        indice = indice if 0 <= indice < self.secciones.count() else 0
        self.secciones.setCurrentIndex(indice)
        for i, (b, titulo, descripcion) in enumerate(self.botones_nav):
            b.setChecked(i == indice)
            if i == indice:
                self.page_titulo.setText(titulo)
                self.page_sub.setText(descripcion)
        self.ajustes.setValue("seccion", indice)

    def _card(self, paso, titulo):
        """Tarjeta con el encabezado de la web: línea roja + antetítulo + título.
        paso: número de paso ('PASO 2') o un antetítulo libre."""
        card = QFrame(objectName="card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(10)
        fila = QHBoxLayout()
        textos = QVBoxLayout()
        textos.setSpacing(2)
        ante = QHBoxLayout()
        ante.setSpacing(8)
        linea = QFrame(objectName="lineaRoja")
        linea.setFixedSize(22, 3)
        ante.addWidget(linea, 0, Qt.AlignVCenter)
        ante.addWidget(QLabel(f"PASO {paso}" if isinstance(paso, int) else paso, objectName="antetitulo"))
        ante.addStretch()
        textos.addLayout(ante)
        textos.addWidget(QLabel(titulo, objectName="titulo"))
        fila.addLayout(textos)
        fila.addStretch()
        lay.addLayout(fila)
        self.col.addWidget(card)
        return lay, fila

    def _barra_lateral(self):
        nav = QFrame(objectName="nav")
        nav.setFixedWidth(272)
        self.nav_lay = QVBoxLayout(nav)
        self.nav_lay.setContentsMargins(0, 18, 0, 16)
        self.nav_lay.setSpacing(0)

        # El logo tiene fondo blanco: va en una ficha blanca para que luzca en la barra oscura
        marca = QHBoxLayout()
        marca.setContentsMargins(18, 0, 18, 0)
        chip = QFrame(objectName="logoChip")
        hc = QHBoxLayout(chip)
        hc.setContentsMargins(10, 6, 10, 6)
        logo = QLabel()
        pix = QPixmap(str(LOGO_TM))
        if not pix.isNull():
            logo.setPixmap(pix.scaledToHeight(40, Qt.SmoothTransformation))
        hc.addWidget(logo)
        marca.addWidget(chip)
        marca.addStretch()
        self.nav_lay.addLayout(marca)
        textos = QVBoxLayout()
        textos.setContentsMargins(22, 12, 18, 0)
        textos.setSpacing(1)
        textos.addWidget(QLabel("Procesamiento OCR", objectName="navApp"))
        textos.addWidget(QLabel("Digitalización de libros", objectName="navAppSub"))
        self.nav_lay.addLayout(textos)

        self.nav_lay.addWidget(QLabel("SECCIONES", objectName="navTitulo"))
        self.botones_nav = []
        return nav

    def _barra_superior(self):
        barra = QFrame(objectName="topbar")
        barra.setFixedHeight(64)
        fila = QHBoxLayout(barra)
        fila.setContentsMargins(24, 8, 20, 8)
        fila.setSpacing(14)
        textos = QVBoxLayout()
        textos.setSpacing(1)
        self.page_titulo = QLabel(objectName="pageTitulo")
        self.page_sub = QLabel(objectName="pageSub")
        textos.addWidget(self.page_titulo)
        textos.addWidget(self.page_sub)
        fila.addLayout(textos, 1)
        self.estado_general = pill("Listo para procesar", "ok")
        fila.addWidget(self.estado_general, 0, Qt.AlignVCenter)
        self.btn_tema = QPushButton(objectName="tema")
        self.btn_tema.setCursor(Qt.PointingHandCursor)
        self.btn_tema.setMinimumWidth(140)
        self.btn_tema.clicked.connect(self.cambiar_tema)
        fila.addWidget(self.btn_tema, 0, Qt.AlignVCenter)
        return barra

    def _selector(self, icono_txt):
        caja = QFrame(objectName="selector")
        lay = QHBoxLayout(caja)
        lay.setContentsMargins(10, 8, 10, 8)
        img = QLabel(icono_txt, objectName="miniatura")
        img.setFixedSize(56, 36)
        img.setAlignment(Qt.AlignCenter)
        lay.addWidget(img)
        textos = QVBoxLayout()
        textos.setSpacing(0)
        nombre = QLabel("Sin seleccionar")
        detalle = QLabel("", objectName="sub")
        detalle.setWordWrap(True)
        textos.addWidget(nombre)
        textos.addWidget(detalle)
        lay.addLayout(textos, 1)
        boton = QPushButton("Elegir…")
        lay.addWidget(boton)
        return caja, img, nombre, detalle, boton

    def _seccion_componentes(self):
        lay, _ = self._card("COMPONENTES", "Equipo donde se procesa")
        # ── Equipo: con estos datos se calcula la configuración más rápida ──
        equipo = QFrame(objectName="selector")
        ge = QGridLayout(equipo)
        ge.setContentsMargins(14, 10, 14, 12)
        ge.setHorizontalSpacing(18)
        ge.setVerticalSpacing(4)
        cab_eq = QHBoxLayout()
        cab_eq.addWidget(QLabel("Equipo donde se procesa", objectName="subtitulo"))
        cab_eq.addStretch()
        b = QPushButton("Detectar este equipo")
        b.clicked.connect(self.detectar_equipo)
        cab_eq.addWidget(b)
        ge.addLayout(cab_eq, 0, 0, 1, 3)
        ge.addWidget(QLabel("Memoria RAM", objectName="sub"), 1, 0)
        ge.addWidget(QLabel("Hilos del procesador", objectName="sub"), 1, 1)
        ge.addWidget(QLabel("Tarjeta gráfica", objectName="sub"), 1, 2)
        self.ram = QSpinBox(minimum=2, maximum=1024, suffix=" GB")
        self.hilos = QSpinBox(minimum=1, maximum=512, suffix=" hilos")
        self.gpu = QComboBox(editable=True)
        self.gpu.addItem(SIN_GPU)
        self.gpu.setMinimumWidth(220)
        ge.addWidget(self.ram, 2, 0)
        ge.addWidget(self.hilos, 2, 1)
        ge.addWidget(self.gpu, 2, 2)
        ge.setColumnStretch(2, 1)
        lay.addWidget(equipo)

        # ── Rendimiento: recomendación calculada (se puede ajustar a mano) ──
        rend = QGridLayout()
        rend.setHorizontalSpacing(24)
        rend.addWidget(QLabel("Procesos de OCR (Tesseract en paralelo)", objectName="sub"), 0, 0)
        rend.addWidget(QLabel("Libros en paralelo (post-proceso)", objectName="sub"), 0, 1)
        filaocr = QHBoxLayout()
        self.ocr_slider = QSlider(Qt.Horizontal, minimum=1, maximum=MAX_OCR_JOBS)
        self.ocr_valor = QLabel()
        self.ocr_valor.setMinimumWidth(24)
        self.ocr_slider.valueChanged.connect(lambda v: self.ocr_valor.setText(str(v)))
        filaocr.addWidget(self.ocr_slider, 1)
        filaocr.addWidget(self.ocr_valor)
        rend.addLayout(filaocr, 1, 0)
        self.jobs = QSpinBox(minimum=1, maximum=4)
        self.jobs.setMaximumWidth(90)
        rend.addWidget(self.jobs, 1, 1)
        self.btn_recomendado = QPushButton("Usar recomendado")
        self.btn_recomendado.clicked.connect(self.aplicar_recomendacion)
        rend.addWidget(self.btn_recomendado, 1, 2)
        rend.setColumnStretch(0, 1)
        lay.addLayout(rend)
        self.recomendacion = QLabel(objectName="sub")
        self.recomendacion.setWordWrap(True)
        lay.addWidget(self.recomendacion)

        for w in (self.ram, self.hilos):
            w.valueChanged.connect(self._equipo_cambiado)
        self.gpu.currentTextChanged.connect(self._equipo_cambiado)
        self.ocr_slider.valueChanged.connect(self._marcar_personalizado)
        self.jobs.valueChanged.connect(self._marcar_personalizado)

    def _seccion_marcas(self):
        lay, _ = self._card("MARCAS DE AGUA", "Logo, texto al pie y marca diagonal")
        self._bloque_marca(lay)

    def _seccion_carpetas(self):
        lay, _ = self._card("CARPETAS DE TRABAJO", "Dónde se leen y se dejan los libros")
        bloque = QFrame(objectName="selector")
        gc = QGridLayout(bloque)
        gc.setContentsMargins(14, 10, 14, 12)
        gc.setHorizontalSpacing(10)
        gc.setVerticalSpacing(6)
        cab_c = QHBoxLayout()
        cab_c.addWidget(QLabel("Carpetas de trabajo", objectName="subtitulo"))
        cab_c.addStretch()
        self.btn_carpetas_def = QPushButton(f"Usar {DATOS_DIR}")
        self.btn_carpetas_def.clicked.connect(self.carpetas_por_defecto)
        cab_c.addWidget(self.btn_carpetas_def)
        gc.addLayout(cab_c, 0, 0, 1, 3)
        self.campo_carpeta = {}
        self.boton_carpeta = {}
        self.botones_carpeta = []
        for fila_c, (clave, nombre, _, ayuda) in enumerate(CARPETAS, 1):
            etiqueta = QLabel(nombre)
            etiqueta.setStyleSheet("font-weight:700;")
            etiqueta.setToolTip(ayuda)
            etiqueta.setMinimumWidth(96)
            gc.addWidget(etiqueta, fila_c, 0)
            campo = QLineEdit(readOnly=True)
            campo.setToolTip(ayuda)
            gc.addWidget(campo, fila_c, 1)
            b = QPushButton("Elegir…")
            b.clicked.connect(lambda _=False, c=clave, n=nombre: self.elegir_carpeta(c, n))
            gc.addWidget(b, fila_c, 2)
            self.campo_carpeta[clave] = campo
            self.boton_carpeta[clave] = b
            self.botones_carpeta.append(b)
        gc.setColumnStretch(1, 1)
        self.nota_internas = QLabel(objectName="sub")
        self.nota_internas.setWordWrap(True)
        gc.addWidget(self.nota_internas, len(CARPETAS) + 1, 0, 1, 3)
        self.aviso_carpetas = QLabel(objectName="aviso")
        self.aviso_carpetas.setWordWrap(True)
        gc.addWidget(self.aviso_carpetas, len(CARPETAS) + 2, 0, 1, 3)
        lay.addWidget(bloque)

    def _seccion_configuracion_general(self):
        lay, _ = self._card("CONFIGURACIÓN", "Sede")
        nota = QLabel("Elige la sede donde se usa este equipo. Otras secciones (como Crear contenedores "
                      "GDOC) toman de aquí los datos que dependen de la sede.", objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        fila = QHBoxLayout()
        fila.addWidget(QLabel("Sede", objectName="subtitulo"))
        self.sede = QComboBox()
        self.sede.addItem("Elige la sede…", "")
        for sede in SEDES:
            self.sede.addItem(sede, sede)
        self.sede.setMinimumWidth(240)
        indice = self.sede.findData(self.ajustes.value("sede", ""))
        self.sede.setCurrentIndex(max(indice, 0))
        self.sede.currentIndexChanged.connect(self._sede_cambiada)
        fila.addWidget(self.sede)
        fila.addStretch()
        lay.addLayout(fila)

    def _sede_cambiada(self, *_):
        self.ajustes.setValue("sede", self.sede.currentData())
        self._pintar_sede()
        self._pintar_gdoc_config()

    def _pintar_sede(self):
        sede = self.ajustes.value("sede", "") or "sin elegir"
        self.nav_pie.setText(f"Sede: {sede}  ·  v{VERSION}\n{os.uname().nodename}")

    # ── Versión y actualización ──

    def _seccion_version(self):
        lay, _ = self._card("CONFIGURACIÓN", "Versión del programa")
        nota = QLabel("Busca en GitHub si hay una versión más nueva y la instala. Los ajustes y las "
                      "carpetas de datos se conservan.", objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        fila = QHBoxLayout()
        fila.addWidget(QLabel("Instalada", objectName="subtitulo"))
        fila.addWidget(QLabel(f"v{VERSION}"))
        fila.addSpacing(16)
        self.btn_buscar_act = QPushButton("Buscar actualización")
        self.btn_buscar_act.clicked.connect(self._buscar_actualizacion)
        fila.addWidget(self.btn_buscar_act)
        self.btn_instalar_act = QPushButton("Actualizar", objectName="primario")
        self.btn_instalar_act.clicked.connect(self._instalar_actualizacion)
        self.btn_instalar_act.hide()
        fila.addWidget(self.btn_instalar_act)
        fila.addStretch()
        lay.addLayout(fila)
        self.estado_act = QLabel(objectName="sub")
        self.estado_act.setWordWrap(True)
        self.estado_act.hide()
        lay.addWidget(self.estado_act)
        self.version_nueva = None

    def _estado_actualizacion(self, texto, tipo="sub"):
        self.estado_act.setText(texto)
        self.estado_act.setObjectName(tipo)
        self.estado_act.style().unpolish(self.estado_act)
        self.estado_act.style().polish(self.estado_act)
        self.estado_act.show()

    def _en_segundo_plano(self, paso, funcion):
        def correr():
            try:
                resultado = funcion()
            except ACT.ErrorActualizacion as e:
                resultado = e
            except Exception as e:  # cualquier otro fallo se muestra, no tumba la ventana
                resultado = ACT.ErrorActualizacion(str(e))
            self.actualizacion_lista.emit(paso, resultado)
        threading.Thread(target=correr, daemon=True).start()

    def _buscar_actualizacion(self):
        self.btn_buscar_act.setEnabled(False)
        self.btn_instalar_act.hide()
        self._estado_actualizacion("Buscando en GitHub…")
        self._en_segundo_plano("buscar", ACT.buscar)

    def _instalar_actualizacion(self):
        if self.ejecucion or getattr(self, "iny_ejecucion", None):
            self._estado_actualizacion("Hay un procesamiento en curso. Espera a que termine para actualizar.",
                                       "aviso")
            return
        r = QMessageBox.question(
            self, "Actualizar",
            f"Se instalará la versión {self.version_nueva} y el programa se reiniciará.\n"
            "Los ajustes y las carpetas de datos se conservan.\n¿Continuar?")
        if r != QMessageBox.Yes:
            return
        self.btn_buscar_act.setEnabled(False)
        self.btn_instalar_act.setEnabled(False)
        self._estado_actualizacion(f"Instalando la versión {self.version_nueva}…")
        version = self.version_nueva
        self._en_segundo_plano("instalar", lambda: ACT.instalar(version, registro=lambda _: None))

    def _actualizacion_lista(self, paso, resultado):
        self.btn_buscar_act.setEnabled(True)
        self.btn_instalar_act.setEnabled(True)
        if isinstance(resultado, ACT.ErrorActualizacion):
            self._estado_actualizacion(str(resultado), "aviso")
            return
        if paso == "buscar":
            if resultado["hay_nueva"]:
                self.version_nueva = resultado["remota"]
                self.btn_instalar_act.setText(f"Actualizar a v{self.version_nueva}")
                self.btn_instalar_act.show()
                self._estado_actualizacion(f"Hay una versión nueva: v{self.version_nueva}.")
            elif not resultado["remota"]:
                self._estado_actualizacion("Aún no hay versiones publicadas en GitHub.")
            else:
                self._estado_actualizacion(f"✓ Tienes la última versión (v{VERSION}).")
            return
        self._estado_actualizacion(f"✓ Actualizado a v{ACT.version_local()}. Reiniciando…")
        QMessageBox.information(self, "Actualizar",
                                f"Se instaló la versión {ACT.version_local()}. El programa se reiniciará.")
        self._reiniciar()

    def _reiniciar(self):
        self.ajustes.sync()
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])

    def _seccion_gdoc(self):
        """XLS con la plantilla layout_importar_contenedores: una fila por PDF de la carpeta elegida."""
        lay, _ = self._card("CREAR CONTENEDORES GDOC", "Configuración previa")
        nota = QLabel("Se genera un Excel (.xls) con una fila por cada PDF de la carpeta de PDF (solo los que están "
                      "directamente en ella; subcarpetas como SIN METADATOS no se incluyen). Columnas fijas: "
                      "resguardo ARCHIVO, estatus DENTRO, nivel1 CONSORCIO D3LF1N y nivel2 CAMARA DE "
                      "REPRESENTANTES.", objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        fila_c = QHBoxLayout()
        fila_c.setSpacing(12)
        etiqueta = QLabel("Carpeta de PDF", objectName="subtitulo")
        etiqueta.setToolTip("Donde están los PDF de los libros terminados")
        fila_c.addWidget(etiqueta)
        self.gdoc_campo = QLineEdit(readOnly=True)
        fila_c.addWidget(self.gdoc_campo, 1)
        b = QPushButton("Elegir…")
        b.clicked.connect(self._gdoc_elegir)
        fila_c.addWidget(b)
        lay.addLayout(fila_c)
        fila = QHBoxLayout()
        fila.setSpacing(12)
        fila.addWidget(QLabel("Entrega (nivel3)", objectName="subtitulo"))
        self.gdoc_entrega = QComboBox()
        for n in CG.ENTREGAS:
            self.gdoc_entrega.addItem(f"ENTREGA {n}", n)
        self.gdoc_entrega.setMinimumWidth(170)
        guardada = self.gdoc_entrega.findData(int(self.ajustes.value("gdoc_entrega", 1)))
        self.gdoc_entrega.setCurrentIndex(max(guardada, 0))
        self.gdoc_entrega.currentIndexChanged.connect(
            lambda _: self.ajustes.setValue("gdoc_entrega", self.gdoc_entrega.currentData()))
        fila.addWidget(self.gdoc_entrega)
        fila.addSpacing(24)
        fila.addWidget(QLabel("Sede (Campo_1)", objectName="subtitulo"))
        self.gdoc_sede = QLabel()
        fila.addWidget(self.gdoc_sede)
        fila.addStretch()
        lay.addLayout(fila)

        lay, fila = self._card("CREAR CONTENEDORES GDOC", "Libros")
        cab = QHBoxLayout()
        self.gdoc_buscar = QLineEdit()
        self.gdoc_buscar.setPlaceholderText("Buscar libro, p. ej. AHFCPC")
        self.gdoc_buscar.setClearButtonEnabled(True)
        self.gdoc_buscar.textChanged.connect(self._gdoc_filtrar)
        cab.addWidget(self.gdoc_buscar, 1)
        for texto, accion in (("Todos", lambda: self._gdoc_marcar(True)),
                              ("Ninguno", lambda: self._gdoc_marcar(False)),
                              ("Actualizar", self.refrescar_gdoc)):
            b = QPushButton(texto)
            b.clicked.connect(accion)
            cab.addWidget(b)
        lay.addLayout(cab)
        self.gdoc_titulo = QLabel(objectName="sub")
        lay.addWidget(self.gdoc_titulo)

        self.gdoc_tabla = QTableWidget(0, 4)
        self.gdoc_tabla.setHorizontalHeaderLabels(["", "Código (contenedor)", "Páginas", "Tamaño"])
        self.gdoc_tabla.verticalHeader().setVisible(False)
        self.gdoc_tabla.setSelectionMode(QTableWidget.NoSelection)
        self.gdoc_tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        self.gdoc_tabla.setFocusPolicy(Qt.NoFocus)
        self.gdoc_tabla.setAlternatingRowColors(True)
        h = self.gdoc_tabla.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.gdoc_tabla.verticalHeader().setDefaultSectionSize(34)
        self.gdoc_tabla.setMinimumHeight(240)
        self.gdoc_tabla.itemChanged.connect(self._gdoc_titulo)
        lay.addWidget(self.gdoc_tabla)

        abajo = QHBoxLayout()
        self.gdoc_aviso = QLabel(objectName="aviso")
        self.gdoc_aviso.setWordWrap(True)
        abajo.addWidget(self.gdoc_aviso, 1)
        self.btn_gdoc = QPushButton("Generar Excel", objectName="primario")
        self.btn_gdoc.clicked.connect(self.generar_gdoc)
        abajo.addWidget(self.btn_gdoc)
        lay.addLayout(abajo)
        self.gdoc_resultado = QLabel()
        self.gdoc_resultado.setWordWrap(True)
        self.gdoc_resultado.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.gdoc_resultado)

        self.gdoc_libros = []          # [(código, ruta, páginas o None)]
        self._pintar_gdoc_config()

    def _seccion_ocricr(self):
        """CSV de registro OCR-ICR: una fila por PDF, con los datos de su carpeta de TIFF."""
        lay, _ = self._card("CREAR REGISTRO OCR-ICR", "Configuración previa")
        nota = QLabel("Se genera un CSV con una fila por cada PDF de la carpeta de PDF. De la carpeta de "
                      "TIFF del libro (con su mismo código) se toman el número de imágenes y el peso. "
                      "PESO DE PDF va en KB, igual que la columna Tamaño del Explorador de Windows; PESO DE CARPETA "
                      "TIFF en GB; FECHA es la de hoy.",
                      objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.ocricr_campo = {}
        for i, (clave, nombre, ayuda) in enumerate((
                ("pdf", "Carpeta de PDF", "Donde están los PDF de los libros ya procesados"),
                ("tiff", "Carpeta de TIFF", "Donde está una carpeta de TIFF por libro"))):
            etiqueta = QLabel(nombre, objectName="subtitulo")
            etiqueta.setToolTip(ayuda)
            grid.addWidget(etiqueta, i, 0)
            campo = QLineEdit(readOnly=True)
            campo.setToolTip(ayuda)
            grid.addWidget(campo, i, 1)
            b = QPushButton("Elegir…")
            b.clicked.connect(lambda _=False, c=clave, n=nombre: self._ocricr_elegir(c, n))
            grid.addWidget(b, i, 2)
            self.ocricr_campo[clave] = campo
        grid.addWidget(QLabel("Entrega", objectName="subtitulo"), 2, 0)
        self.ocricr_entrega = QComboBox()
        for n in RO.ENTREGAS:
            self.ocricr_entrega.addItem(f"ENTREGA {n:02d}", n)
        self.ocricr_entrega.setMinimumWidth(170)
        guardada = self.ocricr_entrega.findData(int(self.ajustes.value("ocricr_entrega", 1)))
        self.ocricr_entrega.setCurrentIndex(max(guardada, 0))
        self.ocricr_entrega.currentIndexChanged.connect(
            lambda _: self.ajustes.setValue("ocricr_entrega", self.ocricr_entrega.currentData()))
        grid.addWidget(self.ocricr_entrega, 2, 1, Qt.AlignLeft)
        # El SHA-256 obliga a leer cada PDF completo: es lo que más demora. Sin él,
        # la columna HASH queda vacía (sirve para revisar páginas, pesos e imágenes).
        self.ocricr_hash = QCheckBox("Calcular HASH (SHA-256 de cada PDF)")
        self.ocricr_hash.setToolTip("Sin marcar, el CSV sale mucho más rápido pero con la columna HASH "
                                    "vacía. Para entregar, déjalo marcado.")
        self.ocricr_hash.setChecked(str(self.ajustes.value("ocricr_hash", "true")).lower() == "true")
        self.ocricr_hash.toggled.connect(lambda si: self.ajustes.setValue("ocricr_hash", "true" if si else "false"))
        grid.addWidget(self.ocricr_hash, 3, 1, Qt.AlignLeft)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        lay, _ = self._card("CREAR REGISTRO OCR-ICR", "Libros")
        cab = QHBoxLayout()
        self.ocricr_buscar = QLineEdit()
        self.ocricr_buscar.setPlaceholderText("Buscar libro, p. ej. AHFCPC")
        self.ocricr_buscar.setClearButtonEnabled(True)
        self.ocricr_buscar.textChanged.connect(self._ocricr_filtrar)
        cab.addWidget(self.ocricr_buscar, 1)
        for texto, accion in (("Todos", lambda: self._ocricr_marcar(True)),
                              ("Ninguno", lambda: self._ocricr_marcar(False)),
                              ("Actualizar", self.refrescar_ocricr)):
            b = QPushButton(texto)
            b.clicked.connect(accion)
            cab.addWidget(b)
        lay.addLayout(cab)
        self.ocricr_titulo = QLabel(objectName="sub")
        lay.addWidget(self.ocricr_titulo)

        self.ocricr_tabla = QTableWidget(0, 7)
        self.ocricr_tabla.setHorizontalHeaderLabels(
            ["", "Código", "Páginas PDF", "Imágenes TIFF", "Peso PDF", "Peso TIFF (GB)", "Estado"])
        self.ocricr_tabla.verticalHeader().setVisible(False)
        self.ocricr_tabla.setSelectionMode(QTableWidget.NoSelection)
        self.ocricr_tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        self.ocricr_tabla.setFocusPolicy(Qt.NoFocus)
        self.ocricr_tabla.setAlternatingRowColors(True)
        h = self.ocricr_tabla.horizontalHeader()
        for col in range(7):
            h.setSectionResizeMode(col, QHeaderView.Stretch if col in (1, 6) else QHeaderView.ResizeToContents)
        self.ocricr_tabla.verticalHeader().setDefaultSectionSize(34)
        self.ocricr_tabla.setMinimumHeight(240)
        self.ocricr_tabla.itemChanged.connect(self._ocricr_titulo)
        lay.addWidget(self.ocricr_tabla)

        abajo = QHBoxLayout()
        self.ocricr_aviso = QLabel(objectName="aviso")
        self.ocricr_aviso.setWordWrap(True)
        abajo.addWidget(self.ocricr_aviso, 1)
        self.btn_ocricr = QPushButton("Generar CSV", objectName="primario")
        self.btn_ocricr.clicked.connect(self.generar_ocricr)
        abajo.addWidget(self.btn_ocricr)
        lay.addLayout(abajo)
        self.ocricr_resultado = QLabel()
        self.ocricr_resultado.setWordWrap(True)
        self.ocricr_resultado.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.ocricr_resultado)

        self.ocricr_libros = []        # [{codigo, pdf, paginas, imagenes, bytes_tiff, leido}]
        self.ocricr_contador = 0

    def _seccion_metadatos(self):
        """PDF que quedaron sin metadatos (OUTPUT/SIN METADATOS) + un CSV, que puede
        ser distinto al del procesamiento: inyecta los que estén y avisa los que no."""
        lay, fila = self._card("INYECTAR METADATOS", "PDF sin metadatos")
        nota = QLabel(f"Los libros que no estaban en el CSV se entregan en OUTPUT/{SIN_META_SUBDIR}. "
                      "Elige un CSV que los incluya: a los que aparezcan se les escriben los metadatos "
                      "y pasan a OUTPUT; los demás se quedan y se listan abajo.", objectName="sub")
        nota.setWordWrap(True)
        lay.addWidget(nota)
        tarjeta_pdf = lay.parentWidget()
        self.meta_iny = PanelMetadatos(self, "INYECTAR METADATOS", "CSV para inyectar", "csv_inyectar",
                                       "meta_config_inyectar", cfg_inicial=self.meta.cfg)
        self.meta_iny.cambiado.connect(self.refrescar_sin_meta)
        self.col.removeWidget(tarjeta_pdf)  # la lista de PDF va después del CSV y sus campos
        self.col.addWidget(tarjeta_pdf)

        cab = QHBoxLayout()
        self.iny_titulo = QLabel(objectName="sub")
        cab.addWidget(self.iny_titulo)
        cab.addStretch()
        b = QPushButton("Abrir carpeta")
        b.clicked.connect(lambda: self.abrir(self.carpetas["output"] / SIN_META_SUBDIR))
        cab.addWidget(b)
        b = QPushButton("Actualizar")
        b.clicked.connect(self.refrescar_sin_meta)
        cab.addWidget(b)
        lay.addLayout(cab)

        self.iny_tabla = QTableWidget(0, 3)
        self.iny_tabla.setHorizontalHeaderLabels(["Libro", "Tamaño", "En el CSV elegido"])
        self.iny_tabla.verticalHeader().setVisible(False)
        self.iny_tabla.setSelectionMode(QTableWidget.NoSelection)
        self.iny_tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        self.iny_tabla.setFocusPolicy(Qt.NoFocus)
        h = self.iny_tabla.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Fixed)
        self.iny_tabla.setColumnWidth(2, 210)
        self.iny_tabla.verticalHeader().setDefaultSectionSize(34)
        self.iny_tabla.setMinimumHeight(200)
        lay.addWidget(self.iny_tabla)

        abajo = QHBoxLayout()
        self.iny_aviso = QLabel(objectName="aviso")
        self.iny_aviso.setWordWrap(True)
        abajo.addWidget(self.iny_aviso, 1)
        self.btn_inyectar = QPushButton("Inyectar metadatos", objectName="primario")
        self.btn_inyectar.clicked.connect(self.inyectar)
        abajo.addWidget(self.btn_inyectar)
        lay.addLayout(abajo)

        self.iny_resultado = QLabel()
        self.iny_resultado.setWordWrap(True)
        self.iny_resultado.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.iny_resultado)
        self.iny_log = QPlainTextEdit(readOnly=True)
        self.iny_log.setMaximumBlockCount(2000)
        self.iny_log.setMinimumHeight(120)
        lay.addWidget(self.iny_log)

        self.iny_ejecucion = None
        self.iny_resultados_txt = None

    def _bloque_marca(self, lay):
        """Logo, texto al pie y marca diagonal: cada uno con posición, transparencia
        y tamaño propios, y una vista previa que usa los mismos cálculos que el PDF."""
        bloque = QFrame(objectName="selector")
        h = QVBoxLayout(bloque)             # arriba los controles, abajo la vista previa
        h.setContentsMargins(14, 10, 14, 12)
        h.setSpacing(18)

        izq = QVBoxLayout()
        izq.setSpacing(8)
        cab = QHBoxLayout()
        cab.addWidget(QLabel("Marca de agua", objectName="subtitulo"))
        cab.addStretch()
        self.btn_marca_def = QPushButton("Restablecer predeterminados")
        self.btn_marca_def.clicked.connect(self.marca_predeterminada)
        cab.addWidget(self.btn_marca_def)
        izq.addLayout(cab)

        cols = QGridLayout()
        cols.setHorizontalSpacing(14)
        cols.setVerticalSpacing(4)
        self.marca_ctrl = {}

        # Encabezado de cada columna: casilla para usar o no el elemento (los tres
        # son opcionales) y el elemento en sí
        self.marca_activa = {}
        for col, (elem, nombre) in enumerate((("logo", "Logo"), ("texto", "Texto al pie"),
                                              ("diagonal", "Marca diagonal"))):
            casilla = QCheckBox(nombre)
            casilla.setStyleSheet("font-weight:700;")
            casilla.setToolTip(f"Opcional: desmárcalo para no estampar {nombre.lower()}.")
            cols.addWidget(casilla, 0, col)
            self.marca_activa[elem] = casilla
        self.diagonal = self.marca_activa["diagonal"]
        caja, self.logo_img, self.logo_nombre, self.logo_detalle, self.btn_logo = self._selector("Logo")
        self.btn_logo.clicked.connect(self.elegir_logo)
        cols.addWidget(caja, 1, 0)

        caja_txt = QFrame(objectName="selector")
        ht = QVBoxLayout(caja_txt)
        ht.setContentsMargins(10, 8, 10, 8)
        self.texto_marca = ComboCompleto()
        self.texto_marca.addItems(TEXTOS_MARCA)
        # Un texto largo no debe ensanchar la ventana: el combo usa solo su columna
        self.texto_marca.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.texto_marca.setMinimumContentsLength(8)
        self.texto_marca.currentTextChanged.connect(self.texto_marca.setToolTip)
        self.texto_marca.currentIndexChanged.connect(
            lambda _: self._aplicando or self.ajustes.setValue("texto_marca", self.texto_marca.currentText()))
        self.texto_marca.setToolTip(self.texto_marca.currentText())
        ht.addWidget(self.texto_marca)
        # El texto elegido, completo y en varias líneas si no cabe
        self.texto_marca_completo = QLabel(self.texto_marca.currentText(), objectName="sub")
        self.texto_marca_completo.setWordWrap(True)
        self.texto_marca_completo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.texto_marca.currentTextChanged.connect(self.texto_marca_completo.setText)
        ht.addWidget(self.texto_marca_completo)
        cols.addWidget(caja_txt, 1, 1)

        caja_diag = QFrame(objectName="selector")
        hd = QVBoxLayout(caja_diag)
        hd.setContentsMargins(10, 8, 10, 8)
        self.diagonal_texto = QLabel(f"«{MARCA_DIAGONAL}»")
        self.diagonal_texto.setToolTip("Texto grande y tenue, en diagonal de abajo-izquierda\n"
                                       "a arriba-derecha, en todas las páginas.")
        hd.addWidget(self.diagonal_texto)
        cols.addWidget(caja_diag, 1, 2)

        # Controles: posición, transparencia y tamaño de cada elemento
        for col, (elem, unidad, explica, minimo, maximo) in enumerate(
                [("logo", "%", "% del ancho", 5, 60), ("texto", "pt", "pt", 4, 48),
                 ("diagonal", "%", "% de la diagonal", 20, 100)]):
            cols.addWidget(QLabel("Posición", objectName="sub"), 2, col)
            pos = QComboBox()
            for p in MA.POSICIONES:
                pos.addItem(p.replace("-", " ").capitalize(), p)
            cols.addWidget(pos, 3, col)

            cols.addWidget(QLabel("Transparencia", objectName="sub"), 4, col)
            trans = QSpinBox(minimum=0, maximum=100, suffix=" %")
            trans.setToolTip("0 % = totalmente opaco · 100 % = invisible")
            cols.addWidget(trans, 5, col)

            cols.addWidget(QLabel(f"Tamaño ({explica})", objectName="sub"), 6, col)
            tam = QSpinBox(minimum=minimo, maximum=maximo, suffix=f" {unidad}")
            cols.addWidget(tam, 7, col)

            for w, senal in ((pos, pos.currentIndexChanged), (trans, trans.valueChanged), (tam, tam.valueChanged)):
                senal.connect(self._marca_cambiada)
            self.marca_ctrl[elem] = {"pos": pos, "trans": trans, "tam": tam}
            cols.setColumnStretch(col, 1)
        izq.addLayout(cols)
        h.addLayout(izq)

        der = QVBoxLayout()
        der.setSpacing(6)
        der.addWidget(QLabel("Vista previa · página A4 de ejemplo", objectName="subtitulo"))
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#ffffff;border:1px solid #b9cbe0;")
        der.addWidget(self.preview, 0, Qt.AlignHCenter)
        h.addLayout(der)

        for casilla in self.marca_activa.values():
            casilla.toggled.connect(self._marca_cambiada)
        self.texto_marca.currentIndexChanged.connect(self._marca_cambiada)
        self.timer_preview = QTimer(self, singleShot=True, interval=150, timeout=self._pintar_preview)
        lay.addWidget(bloque)

    # ── Crear contenedores GDOC ────────────────────────────────────────────

    def _pintar_gdoc_config(self):
        if not hasattr(self, "gdoc_sede"):
            return
        sede = self.ajustes.value("sede", "")
        if sede:
            self.gdoc_sede.setText(sede)
            self.gdoc_sede.setObjectName("subtitulo")
            if "sede" in self.gdoc_aviso.text():
                self.gdoc_aviso.setText("")
        else:
            self.gdoc_sede.setText("Sin elegir: elígela en Configuración")
            self.gdoc_sede.setObjectName("aviso")
        self.gdoc_sede.style().unpolish(self.gdoc_sede)
        self.gdoc_sede.style().polish(self.gdoc_sede)

    def _gdoc_carpeta(self):
        return Path(self.ajustes.value("gdoc_pdf", "") or self.carpetas["output"])

    def _gdoc_elegir(self):
        ruta = QFileDialog.getExistingDirectory(self, "Carpeta de PDF", str(self._gdoc_carpeta()))
        if ruta:
            self.ajustes.setValue("gdoc_pdf", ruta)
            self.refrescar_gdoc()

    def refrescar_gdoc(self):
        """Lista los PDF de la carpeta elegida; las páginas se leen aparte para no congelar la ventana."""
        self.gdoc_campo.setText(str(self._gdoc_carpeta()))
        pdfs = CG.libros_output(self._gdoc_carpeta())
        self.gdoc_libros = [[p.stem, p, None] for p in pdfs]
        self.gdoc_tabla.blockSignals(True)
        self.gdoc_tabla.setRowCount(0)
        for codigo, pdf, _ in self.gdoc_libros:
            fila = self.gdoc_tabla.rowCount()
            self.gdoc_tabla.insertRow(fila)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            self.gdoc_tabla.setItem(fila, 0, chk)
            self.gdoc_tabla.setItem(fila, 1, QTableWidgetItem(codigo))
            pag = QTableWidgetItem("…")
            pag.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.gdoc_tabla.setItem(fila, 2, pag)
            peso = QTableWidgetItem(formato_gb(pdf.stat().st_size))
            peso.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.gdoc_tabla.setItem(fila, 3, peso)
        self.gdoc_tabla.blockSignals(False)
        self._gdoc_filtrar()
        self.gdoc_contador = getattr(self, "gdoc_contador", 0) + 1
        threading.Thread(target=self._gdoc_contar_paginas, args=(self.gdoc_contador,), daemon=True).start()

    def _gdoc_contar_paginas(self, turno):
        for i, (_, pdf, _) in enumerate(list(self.gdoc_libros)):
            if turno != self.gdoc_contador:      # se pidió otra lista: abandonar
                return
            n = CG.paginas_pdf(pdf)
            self.paginas_listas.emit(turno, i, -1 if n is None else n)

    def _gdoc_pagina_lista(self, turno, i, n):
        if turno != self.gdoc_contador or i >= len(self.gdoc_libros):
            return
        self.gdoc_libros[i][2] = None if n < 0 else n
        self.gdoc_tabla.blockSignals(True)
        self.gdoc_tabla.item(i, 2).setText("no se pudo leer" if n < 0 else str(n))
        self.gdoc_tabla.blockSignals(False)
        self._gdoc_titulo()

    def _gdoc_filtrar(self, *_):
        texto = self.gdoc_buscar.text().strip().upper()
        for fila in range(self.gdoc_tabla.rowCount()):
            codigo = self.gdoc_tabla.item(fila, 1).text().upper()
            self.gdoc_tabla.setRowHidden(fila, bool(texto) and texto not in codigo)
        self._gdoc_titulo()

    def _gdoc_marcar(self, marcar):
        self.gdoc_tabla.blockSignals(True)
        for fila in range(self.gdoc_tabla.rowCount()):
            if not self.gdoc_tabla.isRowHidden(fila):
                self.gdoc_tabla.item(fila, 0).setCheckState(Qt.Checked if marcar else Qt.Unchecked)
        self.gdoc_tabla.blockSignals(False)
        self._gdoc_titulo()

    def _gdoc_seleccion(self):
        return [i for i in range(self.gdoc_tabla.rowCount())
                if self.gdoc_tabla.item(i, 0).checkState() == Qt.Checked]

    def _gdoc_titulo(self, *_):
        total = self.gdoc_tabla.rowCount()
        if not total:
            self.gdoc_titulo.setText(f"No hay PDF en {self._gdoc_carpeta()}")
            return
        pendientes = sum(1 for _, _, n in self.gdoc_libros if n is None)
        texto = f"{len(self._gdoc_seleccion())} de {total} libros marcados"
        if pendientes:
            texto += f" · leyendo páginas ({total - pendientes}/{total})"
        self.gdoc_titulo.setText(texto)

    def generar_gdoc(self):
        sede = self.ajustes.value("sede", "")
        seleccion = self._gdoc_seleccion()
        if not sede:
            self.gdoc_aviso.setText("Falta elegir la sede en Configuración.")
            return
        if not seleccion:
            self.gdoc_aviso.setText("Marca al menos un libro.")
            return
        sin_paginas = [self.gdoc_libros[i][0] for i in seleccion if self.gdoc_libros[i][2] is None]
        if sin_paginas:
            self.gdoc_aviso.setText("Aún se están leyendo las páginas (o no se pudieron leer) de: "
                                    + ", ".join(sin_paginas[:5]) + (" …" if len(sin_paginas) > 5 else ""))
            return
        self.gdoc_aviso.setText("")
        entrega = self.gdoc_entrega.currentData()
        sugerido = Path(self.carpetas["registro"]) / f"contenedores_ENTREGA_{entrega}_{sede.replace(' ', '_')}.xls"
        destino, _ = QFileDialog.getSaveFileName(self, "Guardar Excel de contenedores", str(sugerido),
                                                 "Excel (*.xls)")
        if not destino:
            return
        if Path(destino).suffix.lower() != ".xls":
            destino += ".xls"
        filas = [CG.fila(self.gdoc_libros[i][0], self.gdoc_libros[i][2], entrega, sede) for i in seleccion]
        try:
            Path(destino).parent.mkdir(parents=True, exist_ok=True)
            CG.escribir_xls(destino, filas)
        except OSError as e:
            QMessageBox.warning(self, "Generar Excel", f"No se pudo guardar el Excel.\n{e}")
            return
        paginas = sum(f[10] for f in filas)
        paginas_txt = f"{paginas:,}".replace(",", ".")
        self.gdoc_resultado.setText(f"✓ {len(filas)} libro(s), {paginas_txt} páginas · ENTREGA {entrega} · "
                                    f"sede {sede}\n{destino}")

    # ── Crear registro OCR-ICR ─────────────────────────────────────────────

    def _ocricr_carpeta(self, clave):
        por_defecto = self.carpetas["output" if clave == "pdf" else "input"]
        return Path(self.ajustes.value(f"ocricr_{clave}", "") or por_defecto)

    def _ocricr_elegir(self, clave, nombre):
        ruta = QFileDialog.getExistingDirectory(self, nombre, str(self._ocricr_carpeta(clave)))
        if ruta:
            self.ajustes.setValue(f"ocricr_{clave}", ruta)
            self.refrescar_ocricr()

    def refrescar_ocricr(self):
        """Lista los PDF; páginas y TIFF se leen aparte para no congelar la ventana."""
        if not hasattr(self, "ocricr_tabla"):
            return
        for clave, campo in self.ocricr_campo.items():
            campo.setText(str(self._ocricr_carpeta(clave)))
        pdfs = RO.libros_pdf(self._ocricr_carpeta("pdf"))
        self.ocricr_libros = [{"codigo": p.stem, "pdf": p, "paginas": None, "imagenes": None,
                               "bytes_tiff": None, "leido": False} for p in pdfs]
        self.ocricr_tabla.blockSignals(True)
        self.ocricr_tabla.setRowCount(0)
        for libro in self.ocricr_libros:
            fila = self.ocricr_tabla.rowCount()
            self.ocricr_tabla.insertRow(fila)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            self.ocricr_tabla.setItem(fila, 0, chk)
            self.ocricr_tabla.setItem(fila, 1, QTableWidgetItem(libro["codigo"]))
            textos = ["…", "…", RO.tamano_explorador(libro["pdf"].stat().st_size), "…", "leyendo…"]
            for col, texto in enumerate(textos, 2):
                item = QTableWidgetItem(texto)
                if col < 6:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.ocricr_tabla.setItem(fila, col, item)
        self.ocricr_tabla.blockSignals(False)
        self._ocricr_filtrar()
        self.ocricr_contador += 1
        tiff_dir = self._ocricr_carpeta("tiff")
        threading.Thread(target=self._ocricr_leer, args=(self.ocricr_contador, tiff_dir), daemon=True).start()

    def _ocricr_leer(self, turno, tiff_dir):
        for i, libro in enumerate(list(self.ocricr_libros)):
            if turno != self.ocricr_contador:      # se pidió otra lista: abandonar
                return
            datos = {"fila": i, "paginas": CG.paginas_pdf(libro["pdf"]), "imagenes": None, "bytes_tiff": None}
            try:
                carpeta = RO.carpeta_tiff(tiff_dir, libro["codigo"])
                if carpeta:
                    datos["imagenes"], datos["bytes_tiff"] = RO.datos_tiff(carpeta)
            except OSError:
                pass
            self.ocricr_evento.emit("fila", turno, datos)

    def _ocricr_estado(self, libro):
        if libro["imagenes"] is None:
            return "Sin carpeta de TIFF", "error"
        if libro["paginas"] is None:
            return "No se pudo leer el PDF", "error"
        if libro["paginas"] != libro["imagenes"]:
            return "Páginas ≠ imágenes", "warn"
        return "✓ Listo", "ok"

    def _ocricr_evento(self, tipo, n, datos):
        if tipo == "fila":
            if n != self.ocricr_contador or datos["fila"] >= len(self.ocricr_libros):
                return
            i = datos["fila"]
            libro = self.ocricr_libros[i]
            libro.update(paginas=datos["paginas"], imagenes=datos["imagenes"],
                         bytes_tiff=datos["bytes_tiff"], leido=True)
            texto, tipo_estado = self._ocricr_estado(libro)
            self.ocricr_tabla.blockSignals(True)
            self.ocricr_tabla.item(i, 2).setText("—" if libro["paginas"] is None else str(libro["paginas"]))
            self.ocricr_tabla.item(i, 3).setText("—" if libro["imagenes"] is None else str(libro["imagenes"]))
            self.ocricr_tabla.item(i, 5).setText(
                "—" if libro["bytes_tiff"] is None else RO.decimal_coma(libro["bytes_tiff"] / 1024 ** 3))
            estado = self.ocricr_tabla.item(i, 6)
            estado.setText(texto)
            estado.setForeground(QColor(TEMAS[self.tema][tipo_estado][1]))
            self.ocricr_tabla.blockSignals(False)
            self._ocricr_titulo()
        elif tipo == "hash":
            self.ocricr_resultado.setText(f"Calculando SHA-256… {n} de {datos} libros")
        elif tipo == "fin":
            self._ocricr_fin(datos)

    def _ocricr_filtrar(self, *_):
        texto = self.ocricr_buscar.text().strip().upper()
        for fila in range(self.ocricr_tabla.rowCount()):
            codigo = self.ocricr_tabla.item(fila, 1).text().upper()
            self.ocricr_tabla.setRowHidden(fila, bool(texto) and texto not in codigo)
        self._ocricr_titulo()

    def _ocricr_marcar(self, marcar):
        self.ocricr_tabla.blockSignals(True)
        for fila in range(self.ocricr_tabla.rowCount()):
            if not self.ocricr_tabla.isRowHidden(fila):
                self.ocricr_tabla.item(fila, 0).setCheckState(Qt.Checked if marcar else Qt.Unchecked)
        self.ocricr_tabla.blockSignals(False)
        self._ocricr_titulo()

    def _ocricr_seleccion(self):
        return [i for i in range(self.ocricr_tabla.rowCount())
                if self.ocricr_tabla.item(i, 0).checkState() == Qt.Checked]

    def _ocricr_titulo(self, *_):
        total = self.ocricr_tabla.rowCount()
        if not total:
            self.ocricr_titulo.setText(f"No hay PDF en {self._ocricr_carpeta('pdf')}")
            return
        leidos = sum(1 for x in self.ocricr_libros if x["leido"])
        texto = f"{len(self._ocricr_seleccion())} de {total} libros marcados"
        if leidos < total:
            texto += f" · leyendo páginas y TIFF ({leidos}/{total})"
        self.ocricr_titulo.setText(texto)

    def generar_ocricr(self):
        seleccion = self._ocricr_seleccion()
        if not seleccion:
            self.ocricr_aviso.setText("Marca al menos un libro.")
            return
        libros = [self.ocricr_libros[i] for i in seleccion]
        if not all(x["leido"] for x in libros):
            self.ocricr_aviso.setText("Aún se están leyendo las páginas y los TIFF. Espera un momento.")
            return
        sin_tiff = [x["codigo"] for x in libros if x["imagenes"] is None]
        if sin_tiff:
            self.ocricr_aviso.setText("Sin carpeta de TIFF (desmárcalos o revisa la carpeta): "
                                      + ", ".join(sin_tiff[:5]) + (" …" if len(sin_tiff) > 5 else ""))
            return
        distintos = [x for x in libros if x["paginas"] != x["imagenes"]]
        if distintos:
            def detalle(x):
                pdf = "no se pudo leer el PDF" if x["paginas"] is None else f'{x["paginas"]} páginas en el PDF'
                return f'• {x["codigo"]}: {pdf} y {x["imagenes"]} imágenes TIFF'
            if len(distintos) == 1:
                texto = f'El libro {distintos[0]["codigo"]} no coincide:'
            else:
                texto = f"{len(distintos)} libros no coinciden:"
            lineas = [detalle(x) for x in distintos[:15]]
            if len(distintos) > 15:
                lineas.append(f"… y {len(distintos) - 15} más")
            r = QMessageBox.question(
                self, "Generar registro",
                texto + "\n\n" + "\n".join(lineas) + "\n\n¿Deseas generar el CSV aun así?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self.ocricr_aviso.setText("")
        entrega = self.ocricr_entrega.currentData()
        sede = (self.ajustes.value("sede", "") or "").replace(" ", "_")
        nombre = f"registro_OCR-ICR_ENTREGA_{entrega:02d}" + (f"_{sede}" if sede else "") + ".csv"
        sugerido = Path(self.carpetas["registro"]) / nombre
        destino, _ = QFileDialog.getSaveFileName(self, "Guardar registro OCR-ICR", str(sugerido), "CSV (*.csv)")
        if not destino:
            return
        self.btn_ocricr.setEnabled(False)
        con_hash = self.ocricr_hash.isChecked()
        if con_hash:
            self.ocricr_resultado.setText(f"Calculando SHA-256… 0 de {len(libros)} libros")
        threading.Thread(target=self._ocricr_escribir, args=(libros, entrega, destino, con_hash),
                         daemon=True).start()

    def _ocricr_escribir(self, libros, entrega, destino, con_hash=True):
        try:
            filas = []
            for n, x in enumerate(libros, 1):
                filas.append(RO.fila(x["codigo"], x["imagenes"], x["pdf"].stat().st_size, x["bytes_tiff"],
                                     entrega, RO.sha256(x["pdf"]) if con_hash else ""))
                if con_hash:
                    self.ocricr_evento.emit("hash", n, len(libros))
            Path(destino).parent.mkdir(parents=True, exist_ok=True)
            RO.escribir_csv(destino, filas)
            self.ocricr_evento.emit("fin", 0, {"filas": filas, "destino": destino, "entrega": entrega,
                                               "con_hash": con_hash})
        except OSError as e:
            self.ocricr_evento.emit("fin", 0, {"error": str(e)})

    def _ocricr_fin(self, datos):
        self.btn_ocricr.setEnabled(True)
        if "error" in datos:
            self.ocricr_resultado.setText("")
            QMessageBox.warning(self, "Generar registro", f"No se pudo generar el CSV.\n{datos['error']}")
            return
        imagenes = sum(f[2] for f in datos["filas"])
        imagenes_txt = f"{imagenes:,}".replace(",", ".")
        sin_hash = "" if datos.get("con_hash", True) else " · sin HASH"
        self.ocricr_resultado.setText(f"✓ {len(datos['filas'])} libro(s), {imagenes_txt} imágenes · "
                                      f"ENTREGA {datos['entrega']:02d}{sin_hash}\n{datos['destino']}")

    # ── Inyectar metadatos ─────────────────────────────────────────────────

    def cargar_csv_inyectar(self, ruta):
        self.meta_iny.cargar(ruta, avisar=False)

    def refrescar_sin_meta(self):
        """Lista los PDF de OUTPUT/SIN METADATOS y si están en el CSV elegido."""
        carpeta = self.carpetas["output"] / SIN_META_SUBDIR
        try:
            pdfs = sorted(p for p in carpeta.iterdir() if p.suffix.lower() == ".pdf")
        except OSError:
            pdfs = []
        self.iny_tabla.setRowCount(0)
        estan = 0
        for pdf in pdfs:
            fila = self.iny_tabla.rowCount()
            self.iny_tabla.insertRow(fila)
            self.iny_tabla.setItem(fila, 0, QTableWidgetItem(pdf.stem))
            peso = QTableWidgetItem(formato_gb(pdf.stat().st_size))
            peso.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.iny_tabla.setItem(fila, 1, peso)
            if self.meta_iny.codigos is None:
                estado = pill("Elige el CSV", "gris")
            elif pdf.stem in self.meta_iny.codigos:
                estado = pill("Está: se inyectará", "ok")
                estan += 1
            else:
                estado = pill("No está en este CSV", "warn")
            celda = QWidget()
            hc = QHBoxLayout(celda)
            hc.setContentsMargins(6, 4, 6, 4)
            hc.addWidget(estado)
            hc.addStretch()
            self.iny_tabla.setCellWidget(fila, 2, celda)
        if not pdfs:
            self.iny_titulo.setText(f"No hay PDF en {carpeta}")
        elif self.meta_iny.codigos is None:
            self.iny_titulo.setText(f"{len(pdfs)} PDF sin metadatos")
        else:
            self.iny_titulo.setText(f"{len(pdfs)} PDF sin metadatos · {estan} están en el CSV elegido · "
                                    f"{len(pdfs) - estan} no están")
        self._estado_inyectar()

    def _estado_inyectar(self):
        ocupado = bool(self.ejecucion or self.iny_ejecucion)
        self.btn_inyectar.setEnabled(not ocupado and self.iny_tabla.rowCount() > 0)

    def inyectar(self):
        if self.ejecucion:
            self.iny_aviso.setText("Espera a que termine el procesamiento.")
            return
        if self.meta_iny.codigos is None:
            self.iny_aviso.setText("Falta elegir el CSV para inyectar.")
            return
        self.iny_aviso.setText("")
        r = QMessageBox.question(
            self, "Inyectar metadatos",
            "A los PDF que estén en el CSV se les escribirán los metadatos y pasarán a OUTPUT; "
            "el registro se actualiza a OK.\nLos que no estén se quedan en "
            f"{SIN_META_SUBDIR}.\n¿Continuar?")
        if r != QMessageBox.Yes:
            return
        args = ["--csv", self.meta_iny.ruta, "--meta-config", self.meta_iny.escribir_json(META_JSON_INY),
                "--output-dir", str(self.carpetas["output"]),
                "--registro-dir", str(self.carpetas["registro"])]
        self.iny_log.clear()
        self.iny_resultado.setText("")
        self.iny_resultados_txt = None
        self.iny_ejecucion = Ejecucion(args, INYECTAR_SH)
        self.iny_ejecucion.linea.connect(self._iny_linea)
        self.iny_ejecucion.terminado.connect(self._iny_terminado)
        self.btn_iniciar.setEnabled(False)
        self._estado_inyectar()

    def _iny_linea(self, texto):
        self.iny_log.appendPlainText(texto)
        m = re.search(r"Resultados de esta ejecución: (.+)$", texto)
        if m:
            self.iny_resultados_txt = Path(m.group(1).strip())

    def _iny_terminado(self, codigo):
        self.iny_ejecucion = None
        self.btn_iniciar.setEnabled(True)
        ok, falta, error = [], [], []
        try:
            lineas = self.iny_resultados_txt.read_text(encoding="utf-8").splitlines()
        except (OSError, AttributeError):
            lineas = []
        for linea in lineas:
            estado, _, resto = linea.partition("|")
            libro, _, motivo = resto.partition("|")
            if estado == "OK":
                ok.append(libro)
            elif estado == "FALTA":
                falta.append(libro)
            else:
                error.append(f"{libro} ({motivo})")
        partes = [f"✓ {len(ok)} con metadatos inyectados (ya están en OUTPUT)."]
        if falta:
            partes.append(f"⚠ {len(falta)} siguen sin entrada en el CSV: {', '.join(falta)}")
        if error:
            partes.append(f"✗ {len(error)} con error: {', '.join(error)}")
        if not lineas and codigo != 0:
            partes = [f"✗ La inyección terminó con error ({codigo}). Revisa el log."]
        self.iny_resultado.setText("\n".join(partes))
        self.refrescar_sin_meta()
        self.refrescar_gdoc()
        QApplication.alert(self)

    # ── Marca de agua ──────────────────────────────────────────────────────

    def _config_marca(self):
        """Posición, opacidad y tamaño de cada elemento, en el formato de marca_agua.py."""
        cfg = {}
        for elem, c in self.marca_ctrl.items():
            tam = c["tam"].value()
            cfg[elem] = {
                "posicion": c["pos"].currentData(),
                "opacidad": round(1 - c["trans"].value() / 100, 2),
                "tamano": tam if elem == "texto" else round(tam / 100, 2),
            }
        return cfg

    def _poner_marca(self, cfg):
        cfg = MA.combinar(cfg)
        self._aplicando = True
        for elem, c in self.marca_ctrl.items():
            v = cfg[elem]
            c["pos"].setCurrentIndex(max(c["pos"].findData(v["posicion"]), 0))
            c["trans"].setValue(round((1 - v["opacidad"]) * 100))
            c["tam"].setValue(round(v["tamano"]) if elem == "texto" else round(v["tamano"] * 100))
        self._aplicando = False
        self._marca_cambiada()

    def marca_predeterminada(self):
        self._poner_marca({})

    def _marca_cambiada(self, *_):
        if self._aplicando:
            return
        libre = self.btn_iniciar.isEnabled()
        propios = {"logo": [self.btn_logo], "texto": [self.texto_marca], "diagonal": [self.diagonal_texto]}
        for elem, casilla in self.marca_activa.items():
            for w in [*self.marca_ctrl[elem].values(), *propios[elem]]:
                w.setEnabled(casilla.isChecked() and libre)
            self.ajustes.setValue(elem if elem == "diagonal" else f"{elem}_activo",
                                  "true" if casilla.isChecked() else "false")
        self.ajustes.setValue("marca_config", json.dumps(self._config_marca()))
        self.timer_preview.start()

    def _pintar_preview(self):
        cfg = MA.combinar(self._config_marca())
        cfg["texto"]["texto"] = self.texto_marca.currentText() if self.marca_activa["texto"].isChecked() else ""
        cfg["diagonal"]["texto"] = MARCA_DIAGONAL if self.diagonal.isChecked() else ""
        logo = self.logo_ruta if self.marca_activa["logo"].isChecked() else None
        try:
            datos = MA.vista_previa(cfg, logo)
        except Exception as e:      # un logo dañado no debe tumbar la interfaz
            self.preview.setText(f"Sin vista previa:\n{e}")
            return
        buf = QBuffer(self)
        buf.setData(QByteArray(datos))
        buf.open(QIODevice.ReadOnly)
        doc = QPdfDocument(self)
        doc.load(buf)
        # Se dibuja a la resolución real de la pantalla para que se vea nítida
        escala = self.preview.devicePixelRatioF()
        imagen = doc.render(0, QSize(round((PREVIEW_W - 2) * escala), round((PREVIEW_H - 2) * escala)))
        pix = QPixmap.fromImage(imagen)
        pix.setDevicePixelRatio(escala)
        self.preview.setPixmap(pix)
        doc.close()
        buf.close()

    def _seccion_libros(self):
        lay, fila = self._card(1, "Libros a procesar")
        # Lo que se usará al procesar; se ajusta en la pestaña COMPONENTES
        self.resumen_rend = QLabel(objectName="sub")
        lay.addWidget(self.resumen_rend)

        # Búsqueda: muestra solo los libros cuyo nombre coincide (p. ej. "AHFCPC").
        # Todos / Ninguno actúan sobre los que se ven; "Solo estos" marca los que
        # coinciden y desmarca el resto.
        busq = QHBoxLayout()
        self.buscar = QLineEdit()
        self.buscar.setPlaceholderText("Buscar libro, p. ej. AHFCPC")
        self.buscar.setClearButtonEnabled(True)
        self.buscar.textChanged.connect(self._filtrar_libros)
        busq.addWidget(self.buscar, 1)
        self.modo_busqueda = QComboBox()
        self.modo_busqueda.addItem("Empieza con", "inicio")
        self.modo_busqueda.addItem("Contiene", "contiene")
        self.modo_busqueda.addItem("Termina con", "fin")
        self.modo_busqueda.currentIndexChanged.connect(self._filtrar_libros)
        self.modo_busqueda.currentIndexChanged.connect(
            lambda _: self.ajustes.setValue("modo_busqueda", self.modo_busqueda.currentData()))
        busq.addWidget(self.modo_busqueda)
        self.btn_solo_estos = QPushButton("Solo estos")
        self.btn_solo_estos.setToolTip("Marca los libros que coinciden con la búsqueda y desmarca los demás")
        self.btn_solo_estos.clicked.connect(self._solo_coincidentes)
        busq.addWidget(self.btn_solo_estos)
        lay.addLayout(busq)

        cab = QHBoxLayout()
        self.libros_titulo = QLabel("Libros en INPUT", objectName="sub")
        cab.addWidget(self.libros_titulo)
        cab.addStretch()
        b = QPushButton("Todos")
        b.setToolTip("Marca los libros que se ven en la lista")
        b.clicked.connect(lambda: self._marcar_todos(True))
        cab.addWidget(b)
        b = QPushButton("Ninguno")
        b.setToolTip("Desmarca los libros que se ven en la lista")
        b.clicked.connect(lambda: self._marcar_todos(False))
        cab.addWidget(b)
        b = QPushButton("Actualizar")
        b.clicked.connect(self.refrescar_libros)
        cab.addWidget(b)
        lay.addLayout(cab)

        self.tabla = QTableWidget(0, 5)
        self.tabla.setHorizontalHeaderLabels(["", "Libro", "Páginas", "Tamaño", "Estado"])
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setSelectionMode(QTableWidget.NoSelection)
        self.tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tabla.setFocusPolicy(Qt.NoFocus)
        h = self.tabla.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tabla.setColumnWidth(4, 210)
        self.tabla.verticalHeader().setDefaultSectionSize(34)
        self.tabla.setMinimumHeight(220)
        self.tabla.itemChanged.connect(self._actualizar_titulo_libros)
        lay.addWidget(self.tabla)

        abajo = QHBoxLayout()
        self.png = QCheckBox("Exportar también PNG de cada página")
        self.png.setToolTip("Opcional: guarda cada página como PNG (sin marca de agua) en la carpeta PNG.")
        self.png.toggled.connect(self._activar_png)
        self.png.toggled.connect(lambda v: self.ajustes.setValue("png", "true" if v else "false"))
        abajo.addWidget(self.png)
        abajo.addSpacing(16)
        # Entrega de estos libros: va en el registro OCR-ICR del día (carpeta REGISTRO)
        abajo.addWidget(QLabel("Entrega", objectName="subtitulo"))
        self.entrega_proc = QComboBox()
        for n in RO.ENTREGAS:
            self.entrega_proc.addItem(f"ENTREGA {n:02d}", n)
        self.entrega_proc.setToolTip("Se escribe en el registro OCR-ICR del día, en la carpeta REGISTRO")
        self.entrega_proc.setCurrentIndex(max(self.entrega_proc.findData(int(self.ajustes.value("entrega_proc", 1))), 0))
        self.entrega_proc.currentIndexChanged.connect(
            lambda _: self.ajustes.setValue("entrega_proc", self.entrega_proc.currentData()))
        abajo.addWidget(self.entrega_proc)
        abajo.addSpacing(16)
        self.aviso = QLabel(objectName="aviso")
        abajo.addWidget(self.aviso, 1)
        self.btn_iniciar = QPushButton("▶  Iniciar procesamiento", objectName="primario")
        self.btn_iniciar.clicked.connect(self.iniciar)
        abajo.addWidget(self.btn_iniciar)
        lay.addLayout(abajo)

    def _seccion_proceso(self):
        lay, fila = self._card(2, "En proceso")
        self.btn_detener = QPushButton("■  Detener")
        self.btn_detener.setEnabled(False)
        self.btn_detener.clicked.connect(self.detener)
        fila.addWidget(self.btn_detener)

        self.activos_lay = QVBoxLayout()
        self.activos_lay.setSpacing(6)
        self.sin_activos = QLabel("Nada en proceso.", objectName="sub")
        self.activos_lay.addWidget(self.sin_activos)
        lay.addLayout(self.activos_lay)

        recursos = QGridLayout()
        recursos.setHorizontalSpacing(16)
        self.rec = {}
        for i, (clave, titulo) in enumerate([("cpu", "CPU"), ("ram", "RAM"), ("disco", "Disco de OUTPUT")]):
            recursos.addWidget(QLabel(titulo, objectName="sub"), 0, i)
            barra = QProgressBar(maximum=100, textVisible=False)
            barra.setFixedHeight(8)
            recursos.addWidget(barra, 1, i)
            texto = QLabel()
            recursos.addWidget(texto, 2, i)
            self.rec[clave] = (barra, texto)
        lay.addLayout(recursos)

        lay.addWidget(QLabel("Log en vivo", objectName="sub"))
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(3000)
        self.log.setMinimumHeight(160)
        lay.addWidget(self.log)

    def _seccion_resultados(self):
        lay, fila = self._card(3, "Resultados de esta ejecución")
        for texto, clave in [("Abrir OUTPUT", "output"), ("Abrir REVIEW", "review")]:
            b = QPushButton(texto)
            b.clicked.connect(lambda _=False, c=clave: self.abrir(self.carpetas[c]))
            fila.addWidget(b)

        metricas = QHBoxLayout()
        self.metrica = {}
        for clave, titulo, tipo in [("ok", "Exitosos", ""), ("sin_meta", "Sin metadatos", ""),
                                    ("review", "En REVIEW", "error"), ("reintentar", "Por reintentar", "warn")]:
            caja = QFrame(objectName="metrica")
            v = QVBoxLayout(caja)
            v.setContentsMargins(12, 8, 12, 8)
            v.addWidget(QLabel(titulo, objectName="sub"))
            valor = QLabel("0", objectName="metricaValor")
            valor.setProperty("tipo", tipo)
            v.addWidget(valor)
            metricas.addWidget(caja)
            self.metrica[clave] = valor
        lay.addLayout(metricas)

        self.lista_res = QVBoxLayout()
        self.lista_res.setSpacing(4)
        lay.addLayout(self.lista_res)

    # ── Configuración ──────────────────────────────────────────────────────

    def _restaurar_ajustes(self):
        self._restaurar_carpetas()
        self.refrescar_gdoc()
        self.refrescar_ocricr()
        csv_iny = self.ajustes.value("csv_inyectar", "")
        if csv_iny and Path(csv_iny).is_file():
            self.cargar_csv_inyectar(csv_iny)
        else:
            self.refrescar_sin_meta()
        csv = self.ajustes.value("csv", "")
        if not (csv and Path(csv).is_file()) and CSV_INCLUIDO.is_file():
            csv = str(CSV_INCLUIDO)
        if csv and Path(csv).is_file():
            self.cargar_csv(csv)
        logo = self.ajustes.value("logo", "")
        if not (logo and Path(logo).is_file()) and LOGO_INCLUIDO.is_file():
            logo = str(LOGO_INCLUIDO)
        if logo and Path(logo).is_file():
            self.cargar_logo(logo)
        self.png.setChecked(self.ajustes.value("png", "false") == "true")

        indice = self.modo_busqueda.findData(self.ajustes.value("modo_busqueda", "inicio"))
        self.modo_busqueda.setCurrentIndex(max(indice, 0))

        # Marca de agua: se lee todo antes de tocar controles (cada cambio guarda)
        try:
            marca = json.loads(self.ajustes.value("marca_config", "{}") or "{}")
        except ValueError:
            marca = {}
        # Logo y texto, activos salvo que se hayan desmarcado; la diagonal, al revés
        activos = {"logo": self.ajustes.value("logo_activo", "true") == "true",
                   "texto": self.ajustes.value("texto_activo", "true") == "true",
                   "diagonal": self.ajustes.value("diagonal", "false") == "true"}
        # Todo con _aplicando: cada cambio guarda, y no debe pisar lo que falta leer
        self._aplicando = True
        guardado = self.ajustes.value("texto_marca", "")
        indice = self.texto_marca.findText(TEXTOS_MARCA_ANTERIORES.get(guardado, guardado))
        self.texto_marca.setCurrentIndex(max(indice, 0))
        for elem, casilla in self.marca_activa.items():
            casilla.setChecked(activos[elem])
        self._aplicando = False
        self._poner_marca(marca)

        # Equipo: lo guardado o, la primera vez, lo que se detecte
        self._aplicando = True
        if self.ajustes.value("ram"):
            self.ram.setValue(int(self.ajustes.value("ram")))
            self.hilos.setValue(int(self.ajustes.value("hilos")))
            self.gpu.setCurrentText(self.ajustes.value("gpu", SIN_GPU))
        else:
            self._cargar_hardware()
        self._aplicando = False

        # Rendimiento: lo último que se usó, o la recomendación
        if self.ajustes.value("ocr_jobs"):
            self._aplicando = True
            self.ocr_slider.setValue(int(self.ajustes.value("ocr_jobs")))
            self.jobs.setValue(int(self.ajustes.value("jobs", 1)))
            self._aplicando = False
            self._marcar_personalizado()
        else:
            self.aplicar_recomendacion()

    # ── Carpetas de trabajo ────────────────────────────────────────────────

    def _restaurar_carpetas(self):
        elegidas = {clave: DATOS_DIR / nombre for clave, nombre, _, _ in CARPETAS}
        for clave, _, _, _ in CARPETAS:
            guardada = self.ajustes.value(f"carpeta_{clave}", "")
            if guardada:
                elegidas[clave] = Path(guardada)
        self.carpetas = con_internas(elegidas)
        self._pintar_carpetas()

    def _pintar_carpetas(self):
        for clave, campo in self.campo_carpeta.items():
            campo.setText(str(self.carpetas[clave]))
        self.nota_internas.setText(
            f"TEMPORALES y REGISTRO se crean solas junto a OUTPUT, en {Path(self.carpetas['output']).parent}.")
        avisos = []
        if not self.carpetas["input"].is_dir():
            avisos.append(f"La carpeta INPUT no existe: {self.carpetas['input']}")
        if not mismo_disco(self.carpetas["input"], self.carpetas["review"]):
            avisos.append("INPUT y REVIEW están en discos distintos: mandar un libro a REVIEW será una copia lenta.")
        self.aviso_carpetas.setText("\n".join(avisos))
        self.aviso_carpetas.setVisible(bool(avisos))
        self._activar_png()

    def _activar_png(self, *_):
        # La carpeta PNG solo se usa (y solo se elige) si se exportan PNG
        activo = self.png.isChecked() and self.btn_iniciar.isEnabled()
        self.campo_carpeta["png"].setEnabled(activo)
        self.boton_carpeta["png"].setEnabled(activo)

    def elegir_carpeta(self, clave, nombre):
        ruta = QFileDialog.getExistingDirectory(self, f"Carpeta {nombre}", str(self._existente(self.carpetas[clave])))
        if not ruta:
            return
        self.carpetas[clave] = Path(ruta)
        self.carpetas = con_internas(self.carpetas)
        self.ajustes.setValue(f"carpeta_{clave}", ruta)
        self._pintar_carpetas()
        if clave in ("input", "output"):
            self.refrescar_libros()
            self.refrescar_sin_meta()
            self.refrescar_gdoc()

    def carpetas_por_defecto(self):
        for clave, _, _, _ in CARPETAS:
            self.ajustes.remove(f"carpeta_{clave}")
        self.carpetas = carpetas_por_defecto()
        self._pintar_carpetas()
        self.refrescar_libros()

    @staticmethod
    def _existente(ruta):
        """La ruta o, si aún no existe, su primer padre que sí exista."""
        ruta = Path(ruta)
        while not ruta.exists() and ruta != ruta.parent:
            ruta = ruta.parent
        return ruta

    def _cargar_hardware(self):
        ram, hilos, gpus = detectar_hardware()
        self.ram.setValue(ram)
        self.hilos.setValue(hilos)
        self.gpu.clear()
        self.gpu.addItems([*gpus, SIN_GPU])
        self.gpu.setCurrentIndex(0)

    def detectar_equipo(self):
        self._cargar_hardware()
        self.aplicar_recomendacion()

    def _equipo_cambiado(self, *_):
        if self._aplicando:
            return
        self.ajustes.setValue("ram", self.ram.value())
        self.ajustes.setValue("hilos", self.hilos.value())
        self.ajustes.setValue("gpu", self.gpu.currentText())
        self.aplicar_recomendacion()

    def aplicar_recomendacion(self):
        ocr, libros, _ = recomendar(self.ram.value(), self.hilos.value(), self.gpu.currentText())
        self._aplicando = True
        self.ocr_slider.setMaximum(max(MAX_OCR_JOBS, self.hilos.value()))
        self.ocr_slider.setValue(ocr)
        self.jobs.setValue(libros)
        self._aplicando = False
        self._marcar_personalizado()

    def _marcar_personalizado(self, *_):
        if self._aplicando:
            return
        self.ajustes.setValue("ocr_jobs", self.ocr_slider.value())
        self.ajustes.setValue("jobs", self.jobs.value())
        ocr, libros, explicacion = recomendar(self.ram.value(), self.hilos.value(), self.gpu.currentText())
        recomendada = (self.ocr_slider.value(), self.jobs.value()) == (ocr, libros)
        self.resumen_rend.setText(
            f"Rendimiento: {self.ocr_slider.value()} procesos de OCR · {self.jobs.value()} libro(s) en paralelo "
            f"({'recomendado' if recomendada else 'personalizado'}; se ajusta en la pestaña COMPONENTES)")
        if recomendada:
            self.recomendacion.setText(f"✓ Configuración recomendada para este equipo. {explicacion}")
            self.btn_recomendado.setEnabled(False)
        else:
            self.recomendacion.setText(
                f"Configuración personalizada. La recomendada para este equipo es {ocr} procesos "
                f"de OCR y {libros} libro(s) en paralelo.")
            self.btn_recomendado.setEnabled(True)

    def cargar_csv(self, ruta):
        self.meta.cargar(ruta, avisar=False)

    # ── Tema claro / oscuro ────────────────────────────────────────────────

    def aplicar_tema(self, nombre):
        self.tema = nombre
        self.ajustes.setValue("tema", nombre)
        app = QApplication.instance()
        app.setPalette(paleta(nombre))
        app.setStyleSheet(estilo(nombre))
        for b, _, _ in getattr(self, "botones_nav", []):
            b.setIcon(icono_nav(b.property("icono"), nombre))
        self.btn_tema.setText("☀  Modo claro" if nombre == "oscuro" else "☾  Modo oscuro")
        for panel in (getattr(self, "meta", None), getattr(self, "meta_iny", None)):
            if panel:
                panel.pintar_detalle()

    def cambiar_tema(self):
        self.aplicar_tema("claro" if self.tema == "oscuro" else "oscuro")

    def elegir_logo(self):
        ruta, _ = QFileDialog.getOpenFileName(self, "Logo de la marca de agua", self.ajustes.value("logo", str(Path.home())),
                                              "Imágenes (*.png *.jpg *.jpeg)")
        if ruta:
            self.cargar_logo(ruta)

    def cargar_logo(self, ruta):
        pix = QPixmap(ruta)
        if pix.isNull():
            QMessageBox.warning(self, "Logo", "No se pudo abrir la imagen.")
            return
        self.logo_ruta = ruta
        self.ajustes.setValue("logo", ruta)
        self.logo_img.setPixmap(pix.scaled(56, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.logo_nombre.setText(Path(ruta).name)
        self.logo_detalle.setText(f"{pix.width()} × {pix.height()} px")
        self.timer_preview.start()

    def refrescar_libros(self):
        self.libros_titulo.setText("Libros en INPUT (leyendo…)")
        self.escaner.escanear(self.carpetas["input"])

    def _mostrar_libros(self, libros):
        previos = self._seleccionados() if self.tabla.rowCount() else None
        self.libros = libros
        self.tabla.blockSignals(True)
        self.tabla.setRowCount(0)
        for libro in libros:
            fila = self.tabla.rowCount()
            self.tabla.insertRow(fila)
            nombre = libro["libro"]
            bloqueado = (self.carpetas["temp"] / nombre).exists()
            ya_hecho = (self.carpetas["output"] / f"{nombre}.pdf").exists() or \
                (self.carpetas["output"] / SIN_META_SUBDIR / f"{nombre}.pdf").exists()

            chk = QTableWidgetItem()
            if bloqueado or ya_hecho:
                chk.setFlags(Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Unchecked)
            else:
                chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                marcado = previos is None or nombre in previos
                chk.setCheckState(Qt.Checked if marcado else Qt.Unchecked)
            chk.setData(Qt.UserRole, nombre)
            self.tabla.setItem(fila, 0, chk)
            self.tabla.setItem(fila, 1, QTableWidgetItem(libro["carpeta"]))
            pag = QTableWidgetItem(str(libro["paginas"]))
            pag.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.tabla.setItem(fila, 2, pag)
            peso = QTableWidgetItem(formato_gb(libro["peso"]))
            peso.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.tabla.setItem(fila, 3, peso)

            if bloqueado:
                estado = pill("En proceso (otra terminal)", "gris")
            elif ya_hecho:
                estado = pill("Ya está en OUTPUT", "gris")
            elif libro["paginas"] == 0:
                estado = pill("Sin TIFF", "error")
            elif self.meta.codigos is None:
                estado = pill("Elige el CSV", "gris")
            elif nombre in self.meta.codigos:
                estado = pill("En CSV", "ok")
            else:
                estado = pill("Sin metadatos", "warn")
            celda = QWidget()
            hc = QHBoxLayout(celda)
            hc.setContentsMargins(6, 4, 6, 4)
            hc.addWidget(estado)
            hc.addStretch()
            self.tabla.setCellWidget(fila, 4, celda)
        self.tabla.blockSignals(False)
        self._filtrar_libros()     # la búsqueda se mantiene al actualizar la lista

    def _seleccionados(self):
        sel = []
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, 0)
            if item.checkState() == Qt.Checked:
                sel.append(item.data(Qt.UserRole))
        return sel

    def _marcar_todos(self, marcar):
        """Marca o desmarca los libros visibles (los que deja ver la búsqueda)."""
        self.tabla.blockSignals(True)
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, 0)
            if item.flags() & Qt.ItemIsUserCheckable and not self.tabla.isRowHidden(fila):
                item.setCheckState(Qt.Checked if marcar else Qt.Unchecked)
        self.tabla.blockSignals(False)
        self._actualizar_titulo_libros()

    def _coincide(self, fila):
        texto = self.buscar.text().strip().upper()
        if not texto:
            return True
        # Se busca en el nombre de la carpeta (con " OK" incluido) y en el del libro
        nombres = (self.tabla.item(fila, 1).text().upper(), self.tabla.item(fila, 0).data(Qt.UserRole).upper())
        modo = self.modo_busqueda.currentData()
        if modo == "inicio":
            return any(n.startswith(texto) for n in nombres)
        if modo == "fin":
            return any(n.endswith(texto) for n in nombres)
        return any(texto in n for n in nombres)

    def _filtrar_libros(self, *_):
        for fila in range(self.tabla.rowCount()):
            self.tabla.setRowHidden(fila, not self._coincide(fila))
        self._actualizar_titulo_libros()

    def _solo_coincidentes(self):
        self.tabla.blockSignals(True)
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, 0)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if self._coincide(fila) else Qt.Unchecked)
        self.tabla.blockSignals(False)
        self._actualizar_titulo_libros()

    def _actualizar_titulo_libros(self, *_):
        total = self.tabla.rowCount()
        if not total:
            self.libros_titulo.setText(f"Libros en INPUT (no hay carpetas en {self.carpetas['input']})")
            return
        texto = f"Libros en INPUT ({len(self._seleccionados())} de {total} seleccionados"
        if self.buscar.text().strip():
            visibles = sum(not self.tabla.isRowHidden(f) for f in range(total))
            texto += f" · {visibles} coinciden con «{self.buscar.text().strip()}»"
        self.libros_titulo.setText(texto + ")")

    # ── Ejecutar ───────────────────────────────────────────────────────────

    def iniciar(self):
        seleccion = self._seleccionados()
        faltan = []
        if self.meta.codigos is None:
            faltan.append("el CSV de metadatos (pestaña METADATOS)")
        if self.marca_activa["logo"].isChecked() and not self.logo_ruta:
            faltan.append("el logo (pestaña MARCAS DE AGUA), o desmarcar Logo")
        if not seleccion:
            faltan.append("al menos un libro")
        if not self.carpetas["input"].is_dir():
            faltan.append("una carpeta INPUT que exista (pestaña CARPETAS DE TRABAJO)")
        if faltan:
            self.aviso.setText("Falta elegir " + ", ".join(faltan) + ".")
            return
        self.aviso.setText("")

        paginas = sum(l["paginas"] for l in self.libros if l["libro"] in seleccion)
        r = QMessageBox.question(
            self, "Iniciar procesamiento",
            f"Se procesarán {len(seleccion)} libro(s), {paginas:,} páginas.\n\n".replace(",", ".")
            + f"Los PDF finales quedarán en:\n{self.carpetas['output']}\n\n"
            + "Al terminar cada libro, sus TIFF se borran de INPUT.\n¿Continuar?")
        if r != QMessageBox.Yes:
            return

        self.ajustes.setValue("jobs", self.jobs.value())
        self.ajustes.setValue("ocr_jobs", self.ocr_slider.value())
        self.ajustes.setValue("png", "true" if self.png.isChecked() else "false")

        logo = self.logo_ruta if self.marca_activa["logo"].isChecked() else ""
        args = ["--csv", self.ajustes.value("csv"), "--logo", logo,
                "--jobs", str(self.jobs.value()), "--ocr-jobs", str(self.ocr_slider.value())]
        if self.png.isChecked():
            args.append("--png")
        args += ["--entrega", str(self.entrega_proc.currentData())]
        self.ajustes.setValue("texto_marca", self.texto_marca.currentText())
        args += ["--texto-marca",
                 self.texto_marca.currentText() if self.marca_activa["texto"].isChecked() else ""]
        if self.diagonal.isChecked():
            args += ["--marca-diagonal", MARCA_DIAGONAL]
        MARCA_JSON.parent.mkdir(parents=True, exist_ok=True)
        MARCA_JSON.write_text(json.dumps(self._config_marca(), indent=2), encoding="utf-8")
        args += ["--marca-config", str(MARCA_JSON), "--meta-config", self.meta.escribir_json(META_JSON)]
        for clave, _, opcion, *_ in [*CARPETAS, *CARPETAS_INTERNAS]:
            args += [opcion, str(self.carpetas[clave])]
        for libro in seleccion:
            args += ["--book", libro]

        self.paginas = {l["libro"]: l["paginas"] for l in self.libros}
        self.resultados_txt = None
        self._limpiar_activos()
        self._limpiar_resultados()
        self.log.clear()

        self.ejecucion = Ejecucion(args)
        self.ejecucion.linea.connect(self._linea)
        self.ejecucion.terminado.connect(self._terminado)
        self._en_marcha(True)
        self._estado_inyectar()

    def detener(self):
        if not self.ejecucion:
            return
        r = QMessageBox.question(
            self, "Detener",
            "Los libros en proceso se liberan y quedan en INPUT para la próxima ejecución.\n¿Detener ahora?")
        if r == QMessageBox.Yes:
            self.btn_detener.setEnabled(False)
            self.estado_general.setText("Deteniendo…")
            self.ejecucion.detener()

    def _en_marcha(self, si):
        self.btn_iniciar.setEnabled(not si)
        self.btn_detener.setEnabled(si)
        for w in (self.tabla, self.ocr_slider, self.jobs, self.png, self.entrega_proc, self.ram, self.hilos,
                  self.gpu, self.btn_recomendado, self.btn_carpetas_def, *self.botones_carpeta,
                  self.texto_marca, *self.marca_activa.values(), self.btn_logo, self.btn_marca_def,
                  *self.meta.widgets,
                  *(w for c in self.marca_ctrl.values() for w in c.values())):
            w.setEnabled(not si)
        self._activar_png()
        self._marca_cambiada()
        if not si:
            self._marcar_personalizado()
        tipo_pill(self.estado_general, "Procesando…" if si else "Listo para procesar", "info" if si else "ok")

    def _terminado(self, codigo):
        self.ejecucion = None
        self.refrescar_sin_meta()
        self.refrescar_gdoc()
        self.refrescar_ocricr()
        self._leer_resultados()
        self._limpiar_activos()
        self._en_marcha(False)
        self.ejecucion = None
        self.refrescar_libros()
        self.setWindowTitle(TITULO)
        if codigo in (130, 143):
            tipo_pill(self.estado_general, "Detenido", "warn")
        elif codigo != 0:
            tipo_pill(self.estado_general, f"Terminó con error ({codigo})", "error")
        QApplication.alert(self)

    # ── Seguimiento del log ────────────────────────────────────────────────

    def _linea(self, texto):
        self.log.appendPlainText(texto)

        m = re.search(r"Resultados de esta ejecución: (.+)$", texto)
        if m:
            self.resultados_txt = Path(m.group(1).strip())
            return
        m = re.search(r"PROCESANDO LIBRO: (.+?)\s*$", texto)
        if m:
            self._fase(m.group(1), 2, 0)
            return
        for libro in list(self.paginas):
            etiqueta = f"[{libro}]"
            if etiqueta not in texto:
                continue
            if "Completado en" in texto or "Enviando a REVIEW" in texto or "Queda en INPUT" in texto \
                    or "Interrumpido" in texto:
                self._quitar_activo(libro)
                self._leer_resultados()
                return
            for clave, paso, pct in FASES:
                if clave in texto:
                    self._fase(libro, paso, pct)
                    return

    def _fase(self, libro, paso, pct):
        if libro not in self.activos:
            self.sin_activos.hide()
            caja = QWidget()
            v = QVBoxLayout(caja)
            v.setContentsMargins(0, 0, 0, 4)
            v.setSpacing(4)
            fila = QHBoxLayout()
            nombre = QLabel()
            detalle = QLabel(objectName="sub")
            fila.addWidget(nombre)
            fila.addStretch()
            fila.addWidget(detalle)
            v.addLayout(fila)
            barra = QProgressBar(maximum=100, textVisible=False)
            barra.setFixedHeight(8)
            v.addWidget(barra)
            self.activos_lay.addWidget(caja)
            self.activos[libro] = {"caja": caja, "nombre": nombre, "detalle": detalle, "barra": barra}
        a = self.activos[libro]
        a["paso"] = paso
        a["nombre"].setText(f"{libro} · {NOMBRE_PASO[paso]} (paso {paso} de 7)")
        if paso > 2:
            a["barra"].setValue(pct)
            a["detalle"].setText(f"{pct}%")

    def _quitar_activo(self, libro):
        a = self.activos.pop(libro, None)
        if a:
            a["caja"].deleteLater()
        if not self.activos:
            self.sin_activos.show()

    def _limpiar_activos(self):
        for libro in list(self.activos):
            self._quitar_activo(libro)

    def _tic(self):
        cpu = psutil.cpu_percent(None)
        ram = psutil.virtual_memory()
        barra, texto = self.rec["cpu"]
        barra.setValue(int(cpu))
        texto.setText(f"{cpu:.0f}%")
        barra, texto = self.rec["ram"]
        barra.setValue(int(ram.percent))
        texto.setText(f"{(ram.total - ram.available) / 1024 ** 3:.1f} / {ram.total / 1024 ** 3:.1f} GB")
        barra, texto = self.rec["disco"]
        try:
            d = psutil.disk_usage(str(self._existente(self.carpetas["output"])))
            barra.setValue(int(d.percent))
            texto.setText(f"{formato_gb(d.used)} / {formato_gb(d.total)}")
        except OSError:
            texto.setText("No disponible")

        # Avance del OCR: páginas ya reconocidas en TEMPORALES/<libro>/tesseract
        for libro, a in self.activos.items():
            if a.get("paso") != 2:
                continue
            total = self.paginas.get(libro) or 0
            try:
                hechas = sum(1 for e in os.scandir(self.carpetas["temp"] / libro / "tesseract") if e.name.endswith(".txt"))
            except OSError:
                hechas = 0
            if total:
                pct = int(70 * min(hechas, total) / total)
                a["barra"].setValue(pct)
                a["detalle"].setText(f"{hechas} / {total} páginas")

        if self.ejecucion:
            self._leer_resultados()
            if self.activos:
                prom = sum(a["barra"].value() for a in self.activos.values()) // len(self.activos)
                self.setWindowTitle(f"{TITULO} · {prom}%")

    # ── Resultados ─────────────────────────────────────────────────────────

    def _limpiar_resultados(self):
        for valor in self.metrica.values():
            valor.setText("0")
        while self.lista_res.count():
            w = self.lista_res.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._vistos = set()

    def _leer_resultados(self):
        if not self.resultados_txt or not self.resultados_txt.is_file():
            return
        cuenta = {"ok": 0, "sin_meta": 0, "review": 0, "reintentar": 0}
        try:
            lineas = self.resultados_txt.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for linea in lineas:
            estado, _, resto = linea.partition("|")
            libro, _, motivo = resto.partition("|")
            if estado == "OK":
                cuenta["ok"] += 1
            elif estado == "SIN_META":
                cuenta["ok"] += 1
                cuenta["sin_meta"] += 1
            elif estado in ("REVIEW", "ERROR"):
                cuenta["review"] += 1
            elif estado == "REINTENTAR":
                cuenta["reintentar"] += 1
            if linea not in self._vistos and estado != "OMITIDO":
                self._vistos.add(linea)
                self._fila_resultado(estado, libro, motivo)
        for clave, n in cuenta.items():
            self.metrica[clave].setText(str(n))

    def _fila_resultado(self, estado, libro, motivo):
        fila = QWidget()
        h = QHBoxLayout(fila)
        h.setContentsMargins(0, 4, 0, 4)
        tipo, texto = {
            "OK": ("ok", "PDF listo"), "SIN_META": ("warn", f"PDF listo, sin metadatos (OUTPUT/{SIN_META_SUBDIR})"),
            "REVIEW": ("error", motivo), "ERROR": ("error", motivo),
            "REINTENTAR": ("warn", motivo or "Queda en INPUT"),
        }.get(estado, ("gris", motivo))
        h.addWidget(pill(estado.replace("_", " ").capitalize(), tipo))
        lbl = QLabel(f"{libro}  ·  {texto}")
        lbl.setWordWrap(True)
        h.addWidget(lbl, 1)
        if estado in ("OK", "SIN_META"):
            b = QPushButton("Abrir PDF")
            sub = SIN_META_SUBDIR if estado == "SIN_META" else ""
            b.clicked.connect(lambda _=False, p=self.carpetas["output"] / sub / f"{libro}.pdf": self.abrir(p))
            h.addWidget(b)
        elif estado == "REVIEW":
            b = QPushButton("Ver carpeta")
            b.clicked.connect(lambda _=False, p=self.carpetas["review"] / libro: self.abrir(p if p.exists() else self.carpetas["review"]))
            h.addWidget(b)
        self.lista_res.addWidget(fila)

    def abrir(self, ruta):
        if not Path(ruta).exists():
            QMessageBox.information(self, "Abrir", f"No existe:\n{ruta}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta)))

    # ── Cierre ─────────────────────────────────────────────────────────────

    def closeEvent(self, evento):
        if self.ejecucion:
            r = QMessageBox.question(
                self, "Cerrar",
                "Hay un procesamiento en marcha. Si cierras, se detiene y los libros en proceso "
                "quedan en INPUT.\n¿Cerrar de todos modos?")
            if r != QMessageBox.Yes:
                evento.ignore()
                return
            self.ejecucion.detener()
            try:
                self.ejecucion.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        evento.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Procesamiento OCR")
    app.setDesktopFileName("procesamiento-ocr")
    app.setWindowIcon(QIcon(str(ICONO)))
    app.setStyle("Fusion")
    # Fuentes de la web (Inter y Barlow) si están en interfaz/fuentes/; si no, las del sistema
    for ttf in (Path(__file__).resolve().parent / "fuentes").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(ttf))
    fuente = QFont()
    fuente.setFamilies(FUENTE_TEXTO)
    fuente.setPixelSize(13)
    app.setFont(fuente)
    ventana = Ventana()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
