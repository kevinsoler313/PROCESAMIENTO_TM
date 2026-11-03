#!/usr/bin/env python3
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   actualizacion.py
# DESC:     Versión del programa y actualización desde GitHub.
#             · La versión instalada está en VERSION.
#             · Las versiones publicadas son las etiquetas vX.Y.Z del repositorio.
#             · Actualizar = avanzar (solo fast-forward) hasta la última etiqueta.
#               Los ajustes de la interfaz, el CSV, el logo, ocr_env y los datos
#               no están en el repositorio, así que no se tocan.
#             · Si cambian requirements.txt o instalar_interfaz.sh, se reinstala.
#           Lo usa la interfaz (Configuración › Buscar actualización).
# USO:      python3 actualizacion.py            # solo informa
#           python3 actualizacion.py --instalar # actualiza si hay versión nueva
# =============================================================================

import os
import re
import subprocess
import sys
from pathlib import Path

import psutil

RAIZ = Path(__file__).resolve().parent
ARCHIVO_VERSION = RAIZ / "VERSION"
REINSTALAR_SI_CAMBIA = ("requirements.txt", "instalar_interfaz.sh")
ETIQUETA = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# Sin preguntas de usuario/contraseña: el repositorio es público
ENTORNO_GIT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class ErrorActualizacion(Exception):
    pass


def version_local():
    try:
        return ARCHIVO_VERSION.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _tupla(version):
    m = ETIQUETA.match("v" + version.lstrip("v"))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _git(*args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", str(RAIZ), *args], capture_output=True, text=True,
                           timeout=timeout, env=ENTORNO_GIT)
    except FileNotFoundError:
        raise ErrorActualizacion("git no está instalado (sudo apt install git).")
    except subprocess.TimeoutExpired:
        raise ErrorActualizacion("GitHub no respondió a tiempo. Revisa la conexión a internet.")
    if r.returncode != 0:
        raise ErrorActualizacion((r.stderr or r.stdout).strip() or f"git {args[0]} falló")
    return r.stdout.strip()


def version_remota():
    """Última versión publicada en GitHub (sin descargar nada)."""
    if not (RAIZ / ".git").exists():
        raise ErrorActualizacion("Esta copia no se instaló con git clone; no se puede actualizar sola.")
    try:
        salida = _git("ls-remote", "--tags", "--refs", "origin", timeout=30)
    except ErrorActualizacion as e:
        if "resolve host" in str(e) or "unable to access" in str(e):
            raise ErrorActualizacion("No hay conexión con GitHub. Revisa la conexión a internet.")
        raise
    versiones = [linea.rsplit("refs/tags/", 1)[-1] for linea in salida.splitlines()]
    versiones = [v[1:] for v in versiones if ETIQUETA.match(v)]
    return max(versiones, key=_tupla) if versiones else None


def buscar():
    """{'local', 'remota', 'hay_nueva'}; lanza ErrorActualizacion si no se puede saber."""
    local, remota = version_local(), version_remota()
    return {"local": local, "remota": remota,
            "hay_nueva": bool(remota) and _tupla(remota) > _tupla(local)}


def procesamiento_en_curso():
    """True si main.sh o inyectar_metadatos.sh de esta copia están corriendo."""
    nombres = ("main.sh", "inyectar_metadatos.sh")
    rutas = {str(RAIZ / n) for n in nombres}
    for p in psutil.process_iter(["cmdline"]):
        try:
            args = p.info["cmdline"] or []
            if any(a in rutas for a in args):
                return True
            # bash main.sh lanzado desde la carpeta del programa
            if any(a in nombres for a in args) and Path(p.cwd()) == RAIZ:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def instalar(version, registro=print):
    """Lleva la copia a la etiqueta v<version>. Devuelve True si se reinstaló el entorno."""
    if procesamiento_en_curso():
        raise ErrorActualizacion("Hay un procesamiento en curso. Espera a que termine para actualizar.")
    cambios = _git("status", "--porcelain", "--untracked-files=no")
    if cambios:
        raise ErrorActualizacion("Esta copia tiene archivos del programa modificados a mano; "
                                 "no se actualiza para no perder esos cambios:\n" + cambios)
    etiqueta = f"v{version}"
    registro(f"Descargando {etiqueta}…")
    _git("fetch", "--tags", "origin", timeout=300)
    antes = _git("rev-parse", "HEAD")
    try:
        _git("merge", "--ff-only", etiqueta)
    except ErrorActualizacion:
        raise ErrorActualizacion(f"Esta copia tiene cambios propios que no están en {etiqueta}; "
                                 "hay que actualizarla a mano.")
    cambiados = _git("diff", "--name-only", antes, "HEAD").splitlines()
    if not any(a in cambiados for a in REINSTALAR_SI_CAMBIA):
        return False
    registro("Reinstalando librerías…")
    r = subprocess.run(["bash", str(RAIZ / "instalar_interfaz.sh")], capture_output=True, text=True)
    if r.returncode != 0:
        raise ErrorActualizacion("Se actualizó el código, pero falló instalar_interfaz.sh:\n"
                                 + (r.stderr or r.stdout).strip()[-800:])
    return True


def main():
    try:
        estado = buscar()
        print(f"Instalada: {estado['local']} · Publicada: {estado['remota'] or 'ninguna'}")
        if not estado["hay_nueva"]:
            print("Ya está en la última versión.")
            return 0
        if "--instalar" not in sys.argv:
            print("Hay una versión nueva. Para instalarla: python3 actualizacion.py --instalar")
            return 0
        instalar(estado["remota"])
        print(f"✅ Actualizado a {version_local()}. Vuelve a abrir el programa.")
        return 0
    except ErrorActualizacion as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
