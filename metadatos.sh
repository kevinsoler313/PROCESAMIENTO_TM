#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   metadatos.sh  ─  FUNCIONES DE METADATOS (se carga con `source`)
# DESC:     Lo comparten main.sh (paso 5) e inyectar_metadatos.sh, para que
#           ambos lean el CSV y escriban los metadatos exactamente igual.
#           Qué columna va en cada campo lo decide metadatos.py, con la
#           configuración de META_CONFIG (--meta-config; vacío = la de siempre).
#
#   meta_indice  <csv>                 → índice: código y valores de cada campo,
#                                        separados por 0x1F (uno por línea)
#   meta_buscar  <codigo> <indice>     → la línea del índice, o nada si no está
#   meta_titulo  <linea>               → el título que se escribirá (para el log)
#   meta_escribir <libro> <pdf> <linea> → escribe los metadatos (pikepdf, PDF/A válido)
#
#   Carpeta dentro de OUTPUT donde quedan los PDF sin metadatos:
#   SIN_META_SUBDIR
# =============================================================================

SIN_META_SUBDIR="SIN METADATOS"
META_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/metadatos.py"
META_SEP=$'\x1f'
# Mismo orden que CAMPOS en metadatos.py
META_CAMPOS=(Title Subject Author Keywords Creator Producer Description)
META_CONFIG="${META_CONFIG:-}"

meta_indice() {
    local -a args=(indice --csv "$1")
    [[ -n "$META_CONFIG" ]] && args+=(--config "$META_CONFIG")
    python3 "$META_PY" "${args[@]}"
}

meta_buscar() {
    local codigo="$1" indice="$2" meta
    # Comparación de campo con awk: evita que el código se interprete como regex
    meta=$(awk -F"$META_SEP" -v code="$codigo" '$1 == code { print; exit }' "$indice" 2>/dev/null || true)
    # Compatibilidad con códigos numéricos antiguos sin ceros (3106 → 003106)
    if [[ -z "$meta" && "$codigo" =~ ^[0-9]+$ ]]; then
        meta=$(awk -F"$META_SEP" -v code="$(printf '%06d' "$codigo")" '$1 == code { print; exit }' "$indice" 2>/dev/null || true)
    fi
    printf '%s' "$meta"
}

meta_titulo() {
    local -a v
    IFS="$META_SEP" read -r -a v <<< "$1"
    printf '%s' "${v[1]:-}"
}

meta_escribir() {
    # Con pikepdf (metadatos.py escribir), no con exiftool: exiftool deja el
    # diccionario Info y el XMP distintos y el PDF deja de ser PDF/A-3B válido.
    # La línea va por variable de entorno: ningún valor del CSV se mete en código.
    local pdf="$2" meta="$3"
    META_LINEA="$meta" python3 "$META_PY" escribir --pdf "$pdf"
}
