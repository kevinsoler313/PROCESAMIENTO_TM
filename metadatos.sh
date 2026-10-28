#!/usr/bin/env bash
# =============================================================================
# PROYECTO: Digitalización Libros Patrimoniales - Colombia
# SCRIPT:   metadatos.sh  ─  FUNCIONES DE METADATOS (se carga con `source`)
# DESC:     Lo comparten main.sh (paso 5) e inyectar_metadatos.sh, para que
#           ambos lean el CSV y escriban los metadatos exactamente igual.
#
#   meta_indice  <csv>                 → índice CODIGO|fondo|subfondo|equipo|resolucion
#   meta_buscar  <codigo> <indice>     → la línea del índice, o nada si no está
#   meta_escribir <libro> <pdf> <linea> → escribe los metadatos con exiftool
#
#   Carpeta dentro de OUTPUT donde quedan los PDF sin metadatos:
#   SIN_META_SUBDIR
# =============================================================================

SIN_META_SUBDIR="SIN METADATOS"

# CSV sin encabezado: código, fondo, subfondo, equipo, resolución
meta_indice() {
    awk -F',' '
    {
        # Limpiar espacios en todos los campos relevantes
        gsub(/\r/, "")
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $3)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $4)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $5)
        # Filas con código alfanumérico (p. ej. AHFCP0001) o numérico (legado)
        if ($1 ~ /^[A-Za-z0-9._-]+$/) {
            printf "%s|%s|%s|%s|%s\n", $1, $2, $3, $4, $5
            # Códigos puramente numéricos (legado): añadir variante con padding
            # de 6 dígitos por si las carpetas vienen sin ceros a la izquierda.
            if ($1 ~ /^[0-9]+$/) {
                printf "%06d|%s|%s|%s|%s\n", $1, $2, $3, $4, $5
            }
        }
    }' "$1" | sort -u
}

meta_buscar() {
    local codigo="$1" indice="$2" meta
    # Comparación de campo con awk: evita que el código se interprete como regex
    meta=$(awk -F'|' -v code="$codigo" '$1 == code { print; exit }' "$indice" 2>/dev/null || true)
    # Compatibilidad con códigos numéricos antiguos sin ceros (3106 → 003106)
    if [[ -z "$meta" && "$codigo" =~ ^[0-9]+$ ]]; then
        meta=$(awk -F'|' -v code="$(printf '%06d' "$codigo")" '$1 == code { print; exit }' "$indice" 2>/dev/null || true)
    fi
    printf '%s' "$meta"
}

meta_escribir() {
    local libro="$1" pdf="$2" meta="$3"
    local fondo subfondo equipo resolucion
    IFS='|' read -r _ fondo subfondo equipo resolucion <<< "$meta"
    # Limpiar espacios residuales
    subfondo="${subfondo//  / }"
    equipo="${equipo//  / }"
    exiftool -q -overwrite_original \
        -Title="$libro" \
        -Subject="Congreso de la República - ${subfondo}" \
        -Keywords="Fondo: ${fondo}, Subfondo: ${subfondo}, Código: ${libro}, Resolución: ${resolucion}, Equipo: ${equipo}" \
        -Author="Congreso de la República" \
        -Creator="$equipo" \
        -Producer="Sistema de Digitalización - Congreso de la República" \
        -Description="Documento digitalizado - Código: ${libro}" \
        "$pdf" 2>/dev/null
}
