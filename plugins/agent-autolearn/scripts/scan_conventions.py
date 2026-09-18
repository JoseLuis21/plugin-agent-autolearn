#!/usr/bin/env python3
"""Escanea las lineas AÑADIDAS de un diff unificado y emite candidatos a hallazgo
de convencion: debug olvidado, URLs de desarrollo hardcodeadas y texto en español.

Es un pre-filtro deterministico: produce candidatos, NO hallazgos. El agente
convention-reviewer confirma o descarta cada uno leyendo el archivo real.

Uso:  scan_conventions.py <new.patch>     -> JSON a stdout
"""
import json
import re
import sys

# --------------------------------------------------------------------------
# R1 — debug olvidado, por stack
# --------------------------------------------------------------------------
# console.error y console.warn NO se marcan: suelen ser manejo de error legitimo.
DEBUG_RULES = [
    ("debug-console", r"\bconsole\.(log|debug|dir|trace|table|time|timeEnd|group)\s*\("),
    ("debug-console", r"(?<![\w.])debugger\s*;?"),
    ("debug-print-go", r"\bfmt\.Print(f|ln)?\s*\("),
    ("debug-print-go", r"\bspew\.(Dump|Printf)\s*\("),
    ("debug-print-php", r"\b(var_dump|print_r|var_export|dd|dump)\s*\("),
    ("debug-print-php", r"\berror_log\s*\("),
    ("debug-print-py", r"(?<![\w.])print\s*\(", ),
    ("debug-marker", r"(//|#|/\*)\s*(TODO|FIXME|HACK|XXX)\b.*\b(remove|quitar|borrar|temp|temporal)\b"),
]
# El ruido de print( en Python es alto y el equipo no usa Python: desactivado.
DEBUG_RULES = [r for r in DEBUG_RULES if r[0] != "debug-print-py"]

# --------------------------------------------------------------------------
# R2 — URLs y puertos de desarrollo
# --------------------------------------------------------------------------
DEVURL_RULES = [
    ("dev-url", r"\blocalhost\b"),
    ("dev-url", r"\b127\.0\.0\.1\b"),
    ("dev-url", r"\b0\.0\.0\.0\b"),
    ("dev-url", r"\b\w+\.ngrok(-free)?\.(io|app)\b"),
    ("dev-url", r"https?://[\w.-]*\.local\b"),
    ("dev-port", r"https?://[\w.-]+:(3000|3001|4200|5000|5173|8000|8080|8081|9000)\b"),
]

# Rutas donde una URL de desarrollo es esperable y NO es hallazgo.
DEVURL_ALLOWED_PATHS = re.compile(
    r"(^|/)("
    r"\.env\.(example|sample|template|dist)"
    r"|docker-compose[\w.-]*\.ya?ml"
    r"|Dockerfile[\w.-]*"
    r"|Makefile"
    r"|README[\w.-]*"
    r"|CHANGELOG[\w.-]*"
    r"|\.devcontainer/.*"
    r"|.*\.(md|mdx|txt)"
    r")$",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# R3 — español en el codigo
# --------------------------------------------------------------------------
# Tier A: señales fuertes. Una sola ocurrencia basta para marcar candidato.
# Sustantivos y adjetivos: se buscan como palabra completa (con plural opcional).
SPANISH_NOUNS = """
usuario|cliente|apellido|contrasena|contrasenna|correo|telefono|direccion|
fecha|precio|monto|importe|cantidad|pedido|factura|producto|proveedor|
empresa|sucursal|comision|impuesto|descuento|ganancia|perdida|saldo|cuenta|
respuesta|solicitud|mensaje|consulta|busqueda|resultado|archivo|carpeta|
campo|valor|cadena|numero|tamano|tamanno|ancho|alto|largo|sesion|permiso|
contador|listado|detalle|nombre|apodo|clave|llave|etiqueta|estado|nivel|
paso|prueba|intento|aviso|alerta|ayuda|inicio|fin|limite|rango|orden|
pagina|pantalla|boton|formulario|tabla|fila|columna|grupo|equipo|agente
"""
# Verbos: se buscan por raiz, aceptando las terminaciones de conjugacion comunes.
SPANISH_VERB_STEMS = """
guard|obten|elimin|borr|busc|envi|actualiz|modific|calcul|mostr|ocult|
carg|descarg|agreg|quit|cre|edit|consult|registr|ingres|revis|comprob|
intent|valid|proces|gener|verific|confirm|cancel|aprob|rechaz|asign|
devolv|devuelv|recib|realiz|ejecut|filtr|orden|marc|cont|sum
"""
# Adjetivos y participios frecuentes, con genero y numero.
SPANISH_ADJS = """
vacio|lleno|nuevo|viejo|antiguo|primero|ultimo|siguiente|anterior|
verdadero|falso|correcto|incorrecto|invalido|valido|disponible|pendiente|
aprobado|rechazado|activo|inactivo|eliminado|creado|guardado|enviado|
encontrado|repetido|duplicado|obligatorio|requerido|exitoso|fallido
"""

def _alts(block):
    return "|".join(w.strip() for w in block.split("|") if w.strip())

# Terminaciones verbales del español. Cubren infinitivo, participio, gerundio
# y las personas mas usadas en nombres de funcion y mensajes.
_VERB_END = r"(ar|er|ir|a|e|o|as|es|an|en|amos|emos|imos|ado|ido|ada|ida|ados|idos|adas|idas|ando|iendo|aron|eron|ara|era|aria|eria)"

SPANISH_STRONG_RE = re.compile(
    r"(?<![a-z])(?:"
    + r"(?:" + _alts(SPANISH_NOUNS) + r")(?:es|s)?"
    + r"|(?:" + _alts(SPANISH_ADJS) + r")(?:s|a|as|os)?"
    + r"|(?:" + _alts(SPANISH_VERB_STEMS) + r")" + _VERB_END
    + r")(?![a-z])",
    re.IGNORECASE,
)

# Morfologia exclusiva del español: -mente, -cion/-sion, -idad, -miento.
# El ingles usa -ly, -tion, -ity, -ment, asi que casi no hay colision.
# -sion queda fuera a proposito: session, version, decision son ingles.
SPANISH_MORPH_RE = re.compile(
    r"(?<![a-z])[a-z]{3,}(mente|cion|ciones|idad|idades|miento|mientos|anza|azgo)(?![a-z])",
    re.IGNORECASE,
)

# Caracteres que en la practica solo aparecen en español.
SPANISH_CHARS_RE = re.compile(r"[ñÑáéíóúÁÉÍÓÚ¿¡]")

# Tier B: palabras funcionales cortas. Hacen falta DOS o mas en la misma linea.
SPANISH_WEAK_RE = re.compile(
    r"(?<![a-z])(que|para|con|los|las|del|por|una|uno|este|esta|esto|pero|"
    r"como|mas|cuando|donde|porque|entonces|desde|hasta|entre|sobre|cada|"
    r"todos|todas|solo|tambien|siempre|nunca|aqui|alli|debe|puede|hace|"
    r"tiene|hay|ser|estar|son|sea|fue|era|nos|les|una|sus|muy)(?![a-z])",
    re.IGNORECASE,
)

# camelCase y PascalCase esconden las palabras de los buscadores de arriba:
# `usuarioActual` no matchea /usuario\b/. Se parte el identificador antes de buscar.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[_\-]+")

def split_identifiers(text):
    """`getUsuarioActual` -> `get Usuario Actual`, para que el matcher lo vea."""
    return _CAMEL_SPLIT.sub(" ", text)

# Rutas que no son codigo del equipo.
SKIP_PATHS = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|\.next|out|coverage|__snapshots__|"
    r"third_party|generated)/|"
    r"\.(lock|sum|min\.js|min\.css|svg|png|jpe?g|gif|ico|woff2?|ttf|pdf)$|"
    r"(package-lock\.json|yarn\.lock|composer\.lock|go\.sum)$",
    re.IGNORECASE,
)


def parse_added_lines(patch_text):
    """Devuelve [(file, new_line_no, text)] de las lineas añadidas."""
    out = []
    current_file = None
    new_lineno = 0
    for raw in patch_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current_file = None if path == "/dev/null" else re.sub(r"^b/", "", path)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            new_lineno = int(m.group(1))
            continue
        if current_file is None:
            continue
        if raw.startswith("+"):
            out.append((current_file, new_lineno, raw[1:]))
            new_lineno += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_lineno += 1
    return out


def scan(added):
    candidates = []
    for path, lineno, text in added:
        if SKIP_PATHS.search(path):
            continue
        stripped = text.strip()
        if not stripped:
            continue

        for rule, pattern in DEBUG_RULES:
            if re.search(pattern, text):
                candidates.append(dict(rule=rule, file=path, line=lineno, text=stripped[:200]))
                break

        if not DEVURL_ALLOWED_PATHS.search(path):
            for rule, pattern in DEVURL_RULES:
                if re.search(pattern, text, re.IGNORECASE):
                    candidates.append(dict(rule=rule, file=path, line=lineno, text=stripped[:200]))
                    break

        hits = []
        # Una linea que ya se marco como debug se va a borrar: no la contamos
        # tambien como español. Evita duplicar el mismo trabajo en el informe.
        already_debug = candidates and candidates[-1]["file"] == path \
            and candidates[-1]["line"] == lineno \
            and candidates[-1]["rule"].startswith("debug")
        if not already_debug:
            probe = split_identifiers(text)
            if SPANISH_CHARS_RE.search(text):
                hits.append("caracteres")
            strong = {m.group(0).lower() for m in SPANISH_STRONG_RE.finditer(probe)}
            morph = {m.group(0).lower() for m in SPANISH_MORPH_RE.finditer(probe)}
            found = sorted(strong | morph)
            if found:
                hits.append("palabras:" + ",".join(found)[:100])
            elif len({w.lower() for w in SPANISH_WEAK_RE.findall(probe)}) >= 2:
                hits.append("frase")
        if hits:
            candidates.append(
                dict(rule="spanish", file=path, line=lineno, text=stripped[:200], signal="; ".join(hits))
            )
    return candidates


def main():
    if len(sys.argv) < 2:
        print("uso: scan_conventions.py <new.patch>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        added = parse_added_lines(fh.read())
    candidates = scan(added)
    summary = {}
    for c in candidates:
        summary[c["rule"]] = summary.get(c["rule"], 0) + 1
    json.dump(
        {"added_lines_scanned": len(added), "summary": summary, "candidates": candidates},
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
