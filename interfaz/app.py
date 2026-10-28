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

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import psutil
from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN (mismas rutas que main.sh)
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent.parent
MAIN_SH = SCRIPT_DIR / "main.sh"
ICONO = Path(__file__).resolve().parent / "icono.svg"

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
TEXTOS_MARCA = [
    "CONGRESO DE LA REPÚBLICA - REPOSITORIO CÁMARA DE REPRESENTANTES",
]


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
CODIGO_OK = re.compile(r"^[A-Za-z0-9._-]+$")

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

# Paleta de technomart.com.co. El modo oscuro usa el azul noche del hero y el
# footer de la web como fondo, y el celeste de sus titulares como acento.
TEMAS = {
    "claro": {
        "fondo": "#f3f6f9", "card": "#ffffff", "barra": "#ffffff", "borde": "#dfe7f0",
        "borde_selector": "#b9cbe0", "texto": "#13233a", "texto_sub": "#607087",
        "acento": "#0c5aa6", "rojo": "#c01120", "rojo_hover": "#a50e1b", "rojo_off": "#e3a3a9",
        "boton_borde": "#13233a", "boton_hover_fondo": "#13233a", "boton_hover_texto": "#ffffff",
        "off": "#9fb0c4", "log_fondo": "#071b33", "log_texto": "#b9cbe0", "scroll": "#b9cbe0",
        "ok": ("#e1f3e6", "#1b6b34"), "warn": ("#fdf1dc", "#8a5300"), "error": ("#fbe3e5", "#c01120"),
        "gris": ("#eef2f6", "#607087"), "info": ("#e1ecf8", "#0c5aa6"),
    },
    "oscuro": {
        "fondo": "#071b33", "card": "#0d2442", "barra": "#041326", "borde": "#1d3a5f",
        "borde_selector": "#2b4c75", "texto": "#e6edf5", "texto_sub": "#9fb0c4",
        "acento": "#75d6ff", "rojo": "#e12d3d", "rojo_hover": "#c01120", "rojo_off": "#5a2a33",
        "boton_borde": "#9fb0c4", "boton_hover_fondo": "#e6edf5", "boton_hover_texto": "#071b33",
        "off": "#4d6584", "log_fondo": "#041326", "log_texto": "#b9cbe0", "scroll": "#2b4c75",
        "ok": ("#123d24", "#8fdba8"), "warn": ("#3d2c0b", "#f5c46b"), "error": ("#4a1219", "#ff8a95"),
        "gris": ("#16304f", "#9fb0c4"), "info": ("#0f3157", "#75d6ff"),
    },
}

FUENTE_TEXTO = ["Inter", "Segoe UI", "Noto Sans", "DejaVu Sans", "sans-serif"]
FUENTE_TITULOS = '"Barlow", "Inter", "Segoe UI", "DejaVu Sans", sans-serif'


def estilo(nombre_tema):
    """Hoja de estilos completa del tema. Los colores de estado van por propiedad
    'tipo' (pill, métricas), así un cambio de tema los repinta todos."""
    t = TEMAS[nombre_tema]
    pills = "".join(
        f'QLabel#pill[tipo="{k}"] {{ background: {t[k][0]}; color: {t[k][1]}; }}\n'
        for k in ("ok", "warn", "error", "gris", "info"))
    return f"""
QMainWindow, QScrollArea, #contenido {{ background: {t['fondo']}; }}
QWidget {{ color: {t['texto']}; }}
QFrame#barra {{ background: {t['barra']}; border-bottom: 3px solid {t['rojo']}; }}
QFrame#logoChip {{ background: #ffffff; border-radius: 8px; }}
QFrame#separador {{ background: {t['borde']}; }}
QFrame#lineaRoja {{ background: {t['rojo']}; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['borde']}; border-radius: 10px; }}
QLabel#antetitulo {{ color: {t['acento']}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }}
QLabel#titulo {{ font-family: {FUENTE_TITULOS}; font-size: 18px; font-weight: 700; }}
QLabel#appTitulo {{ font-family: {FUENTE_TITULOS}; font-size: 20px; font-weight: 700; }}
QLabel#sub {{ color: {t['texto_sub']}; font-size: 12px; }}
QLabel#subtitulo {{ font-weight: 700; font-size: 13px; }}
QSpinBox, QComboBox, QLineEdit {{ padding: 4px 6px; min-height: 22px; }}
QLineEdit[readOnly="true"] {{ background: {t['card']}; border: 1px solid {t['borde']}; border-radius: 4px; }}
QLineEdit:disabled {{ color: {t['off']}; background: transparent; }}
QLabel#aviso {{ color: {t['rojo']}; }}
QLabel#miniatura {{ background: #ffffff; border: 1px solid {t['borde']}; border-radius: 4px;
                    font-size: 11px; color: #607087; }}
QLabel#pill {{ border-radius: 6px; padding: 2px 8px; font-size: 12px; }}
{pills}
QLabel#metricaValor {{ font-family: {FUENTE_TITULOS}; font-size: 26px; font-weight: 700; }}
QLabel#metricaValor[tipo="error"] {{ color: {t['error'][1]}; }}
QLabel#metricaValor[tipo="warn"] {{ color: {t['warn'][1]}; }}
QFrame#metrica {{ background: {t['fondo']}; border: 1px solid {t['borde']}; border-radius: 8px; }}
QFrame#selector {{ border: 1px dashed {t['borde_selector']}; border-radius: 8px; background: {t['fondo']}; }}
QPushButton {{ padding: 7px 16px; border: 1px solid {t['boton_borde']}; border-radius: 6px;
               background: transparent; color: {t['texto']}; font-weight: 600; }}
QPushButton:hover {{ background: {t['boton_hover_fondo']}; color: {t['boton_hover_texto']}; }}
QPushButton:disabled {{ color: {t['off']}; border-color: {t['borde']}; background: transparent; }}
QPushButton#primario {{ background: {t['rojo']}; color: #ffffff; border: none; padding: 9px 22px; font-size: 13px; }}
QPushButton#primario:hover {{ background: {t['rojo_hover']}; }}
QPushButton#primario:disabled {{ background: {t['rojo_off']}; color: #ffffffaa; }}
QPushButton#tema {{ border: 1px solid {t['borde']}; padding: 6px 12px; }}
QProgressBar {{ border: none; background: {t['borde']}; border-radius: 4px; }}
QProgressBar::chunk {{ background: {t['acento']}; border-radius: 4px; }}
QSlider::groove:horizontal {{ height: 4px; background: {t['borde']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['acento']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 16px; margin: -6px 0; border-radius: 8px; background: {t['rojo']}; }}
QTableWidget {{ background: {t['card']}; border: none; gridline-color: {t['borde']}; }}
QTableWidget::item:disabled {{ color: {t['off']}; }}
QHeaderView::section {{ background: {t['card']}; border: none; border-bottom: 2px solid {t['borde']};
                        padding: 6px 4px; color: {t['texto_sub']}; font-weight: 600; }}
QPlainTextEdit {{ background: {t['log_fondo']}; color: {t['log_texto']}; border: 1px solid {t['borde']};
                  border-radius: 8px; font-family: "DejaVu Sans Mono", monospace; font-size: 11px; padding: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QMessageBox, QFileDialog, QToolTip {{ background: {t['card']}; color: {t['texto']}; }}
"""


def paleta(nombre_tema):
    """Paleta de Qt del tema: con ella Fusion dibuja casillas, flechas y diálogos
    con los colores correctos (la hoja de estilos no los cubre)."""
    t = TEMAS[nombre_tema]
    p = QPalette()
    for rol, color in [
        (QPalette.Window, t["fondo"]), (QPalette.WindowText, t["texto"]),
        (QPalette.Base, t["card"]), (QPalette.AlternateBase, t["fondo"]),
        (QPalette.Text, t["texto"]), (QPalette.Button, t["card"]),
        (QPalette.ButtonText, t["texto"]), (QPalette.Highlight, t["acento"]),
        (QPalette.HighlightedText, "#ffffff" if nombre_tema == "claro" else "#071b33"),
        (QPalette.ToolTipBase, t["card"]), (QPalette.ToolTipText, t["texto"]),
        (QPalette.PlaceholderText, t["texto_sub"]), (QPalette.Link, t["acento"]),
    ]:
        p.setColor(rol, QColor(color))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(t["off"]))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t["off"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["off"]))
    return p


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
# CSV DE METADATOS (mismas reglas que load_csv_index de main.sh)
# ─────────────────────────────────────────────────────────────────────────────

def leer_csv(ruta):
    """Devuelve (códigos, nº de códigos, avisos). Columnas: código, fondo, subfondo, equipo, resolución."""
    codigos, vistos, avisos = set(), {}, []
    vacias = incompletas = res_raras = 0
    with open(ruta, encoding="utf-8", errors="replace") as f:
        for n, linea in enumerate(f, 1):
            campos = [c.strip() for c in linea.rstrip("\r\n").split(",")]
            if not any(campos):
                vacias += 1
                continue
            codigo = campos[0]
            if not CODIGO_OK.match(codigo):
                continue
            codigos.add(codigo)
            if codigo.isdigit():
                codigos.add(f"{int(codigo):06d}")
            if codigo in vistos:
                avisos.append(f"{codigo} repetido (líneas {vistos[codigo]} y {n})")
            vistos.setdefault(codigo, n)
            campos += [""] * (5 - len(campos))
            if not all(campos[1:5]):
                incompletas += 1
            elif campos[4].lower() != "300 ppp":
                res_raras += 1
    if incompletas:
        avisos.append(f"{incompletas} fila(s) con campos vacíos")
    if res_raras:
        avisos.append(f"{res_raras} fila(s) con resolución distinta de '300 ppp'")
    if vacias:
        avisos.append(f"{vacias} fila(s) vacías")
    return codigos, len(vistos), avisos


# ─────────────────────────────────────────────────────────────────────────────
# PROCESO main.sh EN SEGUNDO PLANO
# ─────────────────────────────────────────────────────────────────────────────

class Ejecucion(QObject):
    linea = Signal(str)
    terminado = Signal(int)

    def __init__(self, args):
        super().__init__()
        self.proc = subprocess.Popen(
            ["bash", str(MAIN_SH), *args], cwd=SCRIPT_DIR,
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Procesamiento OCR")
        self.setWindowIcon(QIcon(str(ICONO)))
        self.resize(980, 900)
        self.ajustes = QSettings("TechnoMart", "ProcesamientoOCR")

        self.codigos_csv = None
        self.logo_ruta = None
        self.avisos_csv = []
        self.n_codigos = 0
        self.tema = "claro"
        self._aplicando = False   # True mientras la app cambia valores (no es el usuario)
        self.libros = []
        self.carpetas = carpetas_por_defecto()
        self._vistos = set()
        self.ejecucion = None
        self.activos = {}          # libro -> {"paso", "pct", "barra", "texto"}
        self.paginas = {}          # libro -> páginas (para el avance del OCR)
        self.resultados_txt = None

        cont = QWidget(objectName="contenido")
        self.col = QVBoxLayout(cont)
        self.col.setContentsMargins(22, 18, 22, 22)
        self.col.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(cont)

        # Barra con el logo fija arriba (no se desplaza) y debajo las tarjetas
        raiz = QWidget(objectName="contenido")
        v = QVBoxLayout(raiz)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._cabecera())
        v.addWidget(scroll, 1)
        self.setCentralWidget(raiz)

        self._seccion_configuracion()
        self._seccion_libros()
        self._seccion_proceso()
        self._seccion_resultados()
        self.col.addStretch()

        self.escaner = Escaner()
        self.escaner.listo.connect(self._mostrar_libros)

        self.reloj = QTimer(self, interval=1000, timeout=self._tic)
        self.reloj.start()
        psutil.cpu_percent(None)

        self.aplicar_tema(self.ajustes.value("tema", "claro"))
        self._restaurar_ajustes()
        self.refrescar_libros()

    # ── Construcción ────────────────────────────────────────────────────────

    def _card(self, paso, titulo):
        """Tarjeta con el encabezado de la web: línea roja + antetítulo + título."""
        card = QFrame(objectName="card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 16, 22, 18)
        lay.setSpacing(10)
        fila = QHBoxLayout()
        textos = QVBoxLayout()
        textos.setSpacing(2)
        ante = QHBoxLayout()
        ante.setSpacing(8)
        linea = QFrame(objectName="lineaRoja")
        linea.setFixedSize(22, 3)
        ante.addWidget(linea, 0, Qt.AlignVCenter)
        ante.addWidget(QLabel(f"PASO {paso}", objectName="antetitulo"))
        ante.addStretch()
        textos.addLayout(ante)
        textos.addWidget(QLabel(titulo, objectName="titulo"))
        fila.addLayout(textos)
        fila.addStretch()
        lay.addLayout(fila)
        self.col.addWidget(card)
        return lay, fila

    def _cabecera(self):
        barra = QFrame(objectName="barra")
        fila = QHBoxLayout(barra)
        fila.setContentsMargins(22, 10, 22, 10)
        fila.setSpacing(16)
        # El logo tiene fondo blanco: va en una ficha blanca para que luzca igual en modo oscuro
        chip = QFrame(objectName="logoChip")
        hc = QHBoxLayout(chip)
        hc.setContentsMargins(8, 4, 8, 4)
        logo = QLabel()
        pix = QPixmap(str(LOGO_TM))
        if not pix.isNull():
            logo.setPixmap(pix.scaledToHeight(48, Qt.SmoothTransformation))
        hc.addWidget(logo)
        fila.addWidget(chip)
        sep = QFrame(objectName="separador")
        sep.setFixedSize(1, 40)
        fila.addWidget(sep)
        textos = QVBoxLayout()
        textos.setSpacing(0)
        textos.addWidget(QLabel("Procesamiento OCR", objectName="appTitulo"))
        textos.addWidget(QLabel(f"Libros patrimoniales  ·  {os.uname().nodename}", objectName="sub"))
        fila.addLayout(textos)
        fila.addStretch()
        self.estado_general = pill("Listo para procesar", "ok")
        fila.addWidget(self.estado_general, 0, Qt.AlignVCenter)
        self.btn_tema = QPushButton(objectName="tema")
        self.btn_tema.setCursor(Qt.PointingHandCursor)
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

    def _seccion_configuracion(self):
        lay, fila = self._card(1, "Configuración")

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.addWidget(QLabel("CSV de metadatos", objectName="sub"), 0, 0)
        grid.addWidget(QLabel("Logo de la marca de agua", objectName="sub"), 0, 1)
        caja, _, self.csv_nombre, self.csv_detalle, b = self._selector("CSV")
        b.clicked.connect(self.elegir_csv)
        self.csv_detalle.linkActivated.connect(
            lambda _: QMessageBox.information(self, "Avisos del CSV", "\n".join(self.avisos_csv)))
        grid.addWidget(caja, 1, 0)
        caja, self.logo_img, self.logo_nombre, self.logo_detalle, b = self._selector("Logo")
        b.clicked.connect(self.elegir_logo)
        grid.addWidget(caja, 1, 1)

        # Texto que acompaña al logo, al pie de cada página: se elige de TEXTOS_MARCA
        grid.addWidget(QLabel("Texto de la marca de agua", objectName="sub"), 0, 2)
        caja_txt = QFrame(objectName="selector")
        ht = QVBoxLayout(caja_txt)
        ht.setContentsMargins(10, 8, 10, 8)
        ht.setSpacing(2)
        self.texto_marca = QComboBox()
        self.texto_marca.addItems(TEXTOS_MARCA)
        # Un texto largo no debe ensanchar la ventana: el combo usa solo su columna
        self.texto_marca.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.texto_marca.setMinimumContentsLength(18)
        self.texto_marca.currentTextChanged.connect(self.texto_marca.setToolTip)
        self.texto_marca.setToolTip(self.texto_marca.currentText())
        ht.addWidget(self.texto_marca)
        ht.addWidget(QLabel("Se estampa al pie de cada página", objectName="sub"))
        grid.addWidget(caja_txt, 1, 2)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        lay.addLayout(grid)

        # ── Carpetas de trabajo ──
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

    def _seccion_libros(self):
        lay, fila = self._card(2, "Libros a procesar")

        cab = QHBoxLayout()
        self.libros_titulo = QLabel("Libros en INPUT", objectName="sub")
        cab.addWidget(self.libros_titulo)
        cab.addStretch()
        b = QPushButton("Todos")
        b.clicked.connect(lambda: self._marcar_todos(True))
        cab.addWidget(b)
        b = QPushButton("Ninguno")
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
        abajo.addWidget(self.png)
        abajo.addSpacing(16)
        self.aviso = QLabel(objectName="aviso")
        abajo.addWidget(self.aviso, 1)
        self.btn_iniciar = QPushButton("▶  Iniciar procesamiento", objectName="primario")
        self.btn_iniciar.clicked.connect(self.iniciar)
        abajo.addWidget(self.btn_iniciar)
        lay.addLayout(abajo)

    def _seccion_proceso(self):
        lay, fila = self._card(3, "En proceso")
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
        lay, fila = self._card(4, "Resultados de esta ejecución")
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
        csv = self.ajustes.value("csv", "")
        if csv and Path(csv).is_file():
            self.cargar_csv(csv)
        logo = self.ajustes.value("logo", "")
        if logo and Path(logo).is_file():
            self.cargar_logo(logo)
        self.png.setChecked(self.ajustes.value("png", "false") == "true")

        indice = self.texto_marca.findText(self.ajustes.value("texto_marca", ""))
        self.texto_marca.setCurrentIndex(max(indice, 0))

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
        ocr, libros, explicacion = recomendar(self.ram.value(), self.hilos.value(), self.gpu.currentText())
        if (self.ocr_slider.value(), self.jobs.value()) == (ocr, libros):
            self.recomendacion.setText(f"✓ Configuración recomendada para este equipo. {explicacion}")
            self.btn_recomendado.setEnabled(False)
        else:
            self.recomendacion.setText(
                f"Configuración personalizada. La recomendada para este equipo es {ocr} procesos "
                f"de OCR y {libros} libro(s) en paralelo.")
            self.btn_recomendado.setEnabled(True)

    def elegir_csv(self):
        ruta, _ = QFileDialog.getOpenFileName(self, "CSV de metadatos", self.ajustes.value("csv", str(Path.home())),
                                              "CSV (*.csv);;Todos (*)")
        if ruta:
            self.cargar_csv(ruta)

    def cargar_csv(self, ruta):
        try:
            codigos, n_codigos, avisos = leer_csv(ruta)
        except OSError as e:
            QMessageBox.warning(self, "CSV de metadatos", f"No se pudo leer el CSV.\n{e}")
            return
        if not codigos:
            QMessageBox.warning(self, "CSV de metadatos", "El archivo no tiene códigos válidos.")
            return
        self.codigos_csv = codigos
        self.ajustes.setValue("csv", ruta)
        self.csv_nombre.setText(Path(ruta).name)
        self.avisos_csv = avisos
        self.n_codigos = n_codigos
        self._pintar_detalle_csv()
        self._mostrar_libros(self.libros)

    def _pintar_detalle_csv(self):
        # El enlace va en HTML, así que su color se pone a mano según el tema
        if self.codigos_csv is None:
            return
        detalle = f"{self.n_codigos:,} códigos".replace(",", ".")
        if self.avisos_csv:
            color = TEMAS[self.tema]["warn"][1]
            detalle += f' · <a href="#" style="color:{color};">{len(self.avisos_csv)} aviso(s)</a>'
        self.csv_detalle.setText(detalle)

    # ── Tema claro / oscuro ────────────────────────────────────────────────

    def aplicar_tema(self, nombre):
        self.tema = nombre
        self.ajustes.setValue("tema", nombre)
        app = QApplication.instance()
        app.setPalette(paleta(nombre))
        app.setStyleSheet(estilo(nombre))
        self.btn_tema.setText("☀  Modo claro" if nombre == "oscuro" else "☾  Modo oscuro")
        self._pintar_detalle_csv()

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
            ya_hecho = (self.carpetas["output"] / f"{nombre}.pdf").exists()

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
            elif self.codigos_csv is None:
                estado = pill("Elige el CSV", "gris")
            elif nombre in self.codigos_csv:
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
        self._actualizar_titulo_libros()

    def _seleccionados(self):
        sel = []
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, 0)
            if item.checkState() == Qt.Checked:
                sel.append(item.data(Qt.UserRole))
        return sel

    def _marcar_todos(self, marcar):
        self.tabla.blockSignals(True)
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, 0)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if marcar else Qt.Unchecked)
        self.tabla.blockSignals(False)
        self._actualizar_titulo_libros()

    def _actualizar_titulo_libros(self, *_):
        total = self.tabla.rowCount()
        self.libros_titulo.setText(f"Libros en INPUT ({len(self._seleccionados())} de {total} seleccionados)"
                                   if total else f"Libros en INPUT (no hay carpetas en {self.carpetas['input']})")

    # ── Ejecutar ───────────────────────────────────────────────────────────

    def iniciar(self):
        seleccion = self._seleccionados()
        faltan = []
        if self.codigos_csv is None:
            faltan.append("el CSV de metadatos")
        if not self.logo_ruta:
            faltan.append("el logo")
        if not seleccion:
            faltan.append("al menos un libro")
        if not self.carpetas["input"].is_dir():
            faltan.append("una carpeta INPUT que exista")
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

        args = ["--csv", self.ajustes.value("csv"), "--logo", self.logo_ruta,
                "--jobs", str(self.jobs.value()), "--ocr-jobs", str(self.ocr_slider.value())]
        if self.png.isChecked():
            args.append("--png")
        self.ajustes.setValue("texto_marca", self.texto_marca.currentText())
        args += ["--texto-marca", self.texto_marca.currentText()]
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
        for w in (self.tabla, self.ocr_slider, self.jobs, self.png, self.ram, self.hilos,
                  self.gpu, self.btn_recomendado, self.btn_carpetas_def, *self.botones_carpeta,
                  self.texto_marca):
            w.setEnabled(not si)
        self._activar_png()
        if not si:
            self._marcar_personalizado()
        tipo_pill(self.estado_general, "Procesando…" if si else "Listo para procesar", "info" if si else "ok")

    def _terminado(self, codigo):
        self._leer_resultados()
        self._limpiar_activos()
        self._en_marcha(False)
        self.ejecucion = None
        self.refrescar_libros()
        self.setWindowTitle("Procesamiento OCR")
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
                self.setWindowTitle(f"Procesamiento OCR · {prom}%")

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
            "OK": ("ok", "PDF listo"), "SIN_META": ("warn", "PDF listo, sin metadatos"),
            "REVIEW": ("error", motivo), "ERROR": ("error", motivo),
            "REINTENTAR": ("warn", motivo or "Queda en INPUT"),
        }.get(estado, ("gris", motivo))
        h.addWidget(pill(estado.replace("_", " ").capitalize(), tipo))
        lbl = QLabel(f"{libro}  ·  {texto}")
        lbl.setWordWrap(True)
        h.addWidget(lbl, 1)
        if estado in ("OK", "SIN_META"):
            b = QPushButton("Abrir PDF")
            b.clicked.connect(lambda _=False, p=self.carpetas["output"] / f"{libro}.pdf": self.abrir(p))
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
    fuente.setPointSize(10)
    app.setFont(fuente)
    ventana = Ventana()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
