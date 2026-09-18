#!/usr/bin/env python3
"""Resuelve el grafo de imports de un proyecto Next.js y detecta modulos de servidor
alcanzables desde un Client Component.

Es la comprobacion que rompe el build: un archivo con "use client" que importa, directa
o transitivamente, un modulo con `import "server-only"`, `next/headers` o un builtin de
Node. Un `"use server"` en el modulo importado corta la cadena: ahi la frontera es legal.

Es un pre-filtro deterministico: produce candidatos con su cadena de imports, NO
hallazgos. El agente nextjs-architecture-reviewer confirma leyendo los archivos reales.

Uso como libreria:  scan(read, names, delta) -> dict
Uso como CLI:       scan_client_server.py <repo> [<ref>]   -> JSON a stdout
"""
import json
from pathlib import PurePosixPath
import posixpath
import re
import subprocess
import sys

CODE_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs')
INDEX_NAMES = tuple('/index' + ext for ext in CODE_EXT)
MAX_FILE_BYTES = 400_000
MAX_VISITED = 6000
MAX_CANDIDATES = 40

# Modulos que solo existen en el servidor. Importarlos desde el grafo cliente no es
# una opinion de arquitectura: el build de Next falla con un error explicito.
SERVER_MARKERS = {
    'server-only': 'el modulo se declara exclusivo de servidor',
    'next/headers': 'headers() y cookies() solo existen en el servidor',
}
# Builtins de Node sin polyfill en el bundle de cliente: "Module not found".
NODE_BUILTINS = {
    'fs', 'fs/promises', 'child_process', 'dns', 'dns/promises', 'net', 'tls',
    'worker_threads', 'cluster', 'v8', 'vm', 'readline', 'dgram', 'module',
}

# Prologo de directivas: comentarios ya blanqueados, luego "use x" sueltas.
DIRECTIVE_RE = re.compile(r'\s*(?P<q>["\'])use (?P<name>[a-z ]+)(?P=q)\s*;?')
IMPORT_RE = re.compile(
    r"""\bimport\s+(?P<type>type\s+)?(?:(?P<bindings>[\w*{},\s$]+?)\s+from\s+)?['"](?P<spec>[^'"]+)['"]""")
EXPORT_FROM_RE = re.compile(
    r"""\bexport\s+(?P<type>type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+['"](?P<spec>[^'"]+)['"]""")
CALL_RE = re.compile(r"""\b(?:require|import)\s*\(\s*['"](?P<spec>[^'"]+)['"]\s*\)""")


def blank_comments(text):
    """Blanquea comentarios conservando offsets y saltos de linea.

    Recorre el texto en vez de aplicar una regex porque `/*` y `*/` aparecen dentro de
    literales: un `"@/*": ["./*"]` de tsconfig y un glob `"**/*.ts"` mas abajo se comen
    medio archivo si el `/*` de un string abre un comentario.
    """
    out, index, size, quote = list(text), 0, len(text), None
    while index < size:
        char = text[index]
        if quote:
            if char == '\\':
                index += 2
                continue
            # Solo el template literal cruza lineas: cortar ahi acota el daño de una
            # comilla suelta en texto JSX a su propia linea.
            if char == quote or (char == '\n' and quote != '`'):
                quote = None
            index += 1
            continue
        if char in '"\'`':
            quote = char
        elif char == '/' and index + 1 < size and text[index + 1] in '/*':
            if text[index + 1] == '/':
                end = text.find('\n', index)
                end = size if end < 0 else end
            else:
                end = text.find('*/', index + 2)
                end = size if end < 0 else end + 2
            for position in range(index, end):
                if out[position] != '\n':
                    out[position] = ' '
            index = end
            continue
        index += 1
    return ''.join(out)


def directives(text):
    """Directivas del prologo ("use client", "use server"), no de cualquier linea."""
    found, position = set(), 0
    while True:
        match = DIRECTIVE_RE.match(text, position)
        if not match:
            return found
        found.add(match.group('name').strip())
        position = match.end()


def imports(text):
    """[(spec, linea)] de los imports con valor en runtime. `import type` no cuenta:
    TypeScript lo borra al compilar y no llega al bundle."""
    found, offsets = [], None
    for regex in (IMPORT_RE, EXPORT_FROM_RE, CALL_RE):
        for match in regex.finditer(text):
            if match.groupdict().get('type'):
                continue
            if offsets is None:
                offsets = [m.start() for m in re.finditer(r'\n', text)]
            line = sum(1 for o in offsets if o < match.start()) + 1
            found.append((match.group('spec'), line))
    return found


def strip_jsonc(text):
    """tsconfig.json admite comentarios y comas colgantes; json no."""
    return re.sub(r',(\s*[}\]])', r'\1', blank_comments(text))


def aliases(read, names):
    """[(prefijo, [destinos])] de compilerOptions.paths, ordenados por especificidad."""
    rules = []
    for path in names:
        if PurePosixPath(path).name not in ('tsconfig.json', 'jsconfig.json'):
            continue
        if any(part in ('node_modules', 'vendor') for part in PurePosixPath(path).parts):
            continue
        text = read(path)
        if not text:
            continue
        try:
            config = json.loads(strip_jsonc(text))
        except ValueError:
            continue
        options = config.get('compilerOptions') or {}
        if not isinstance(options, dict):
            continue
        root = posixpath.dirname(path)
        base = posixpath.normpath(posixpath.join(root, options.get('baseUrl') or '.'))
        paths = options.get('paths') or {}
        if not isinstance(paths, dict):
            continue
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            resolved = [posixpath.normpath(posixpath.join(base, t))
                        for t in targets if isinstance(t, str)]
            rules.append((pattern, [t for t in resolved if not t.startswith('..')]))
    rules.sort(key=lambda rule: len(rule[0].split('*')[0]), reverse=True)
    return rules


def resolve(spec, importer, index, rules):
    """Ruta del repo que cubre el import, o None si es un paquete externo."""
    bases = []
    if spec.startswith('.'):
        bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec)))
    else:
        for pattern, targets in rules:
            if '*' in pattern:
                prefix, suffix = pattern.split('*', 1)
                if spec.startswith(prefix) and spec.endswith(suffix):
                    middle = spec[len(prefix):len(spec) - len(suffix) or None]
                    bases.extend(target.replace('*', middle) if '*' in target
                                 else posixpath.join(target, middle) for target in targets)
            elif spec == pattern:
                bases.extend(targets)
    for base in bases:
        base = posixpath.normpath(base)
        for candidate in (base, *(base + ext for ext in CODE_EXT), *(base + name for name in INDEX_NAMES)):
            if candidate in index:
                return candidate
    return None


def marker_for(spec):
    if spec in SERVER_MARKERS:
        return spec, SERVER_MARKERS[spec]
    if spec.startswith('node:') or spec in NODE_BUILTINS:
        return spec, 'builtin de Node sin equivalente en el navegador'
    return None, None


def scan(read, names, delta=()):
    """Candidatos de frontera cliente/servidor.

    read(path) -> texto o None. names: rutas del snapshot. delta: rutas cambiadas.
    """
    delta = set(delta)
    index = {p for p in names if p.endswith(CODE_EXT) and not p.endswith('.d.ts')
             and not any(part in ('node_modules', 'vendor', '.next', 'dist', 'build')
                         for part in PurePosixPath(p).parts)}
    rules = aliases(read, names)
    parsed, roots = {}, []

    def parse(path):
        if path not in parsed:
            text = read(path) or ''
            if len(text) > MAX_FILE_BYTES:
                text = text[:MAX_FILE_BYTES]
            clean = blank_comments(text)
            parsed[path] = (directives(clean), imports(clean))
        return parsed[path]

    for path in sorted(index):
        if 'client' in parse(path)[0]:
            roots.append(path)

    candidates = []
    parent, origin = {}, {}
    queue = [(path, path) for path in roots]
    for path, root in queue:
        parent[path], origin[path] = None, root
    visited, truncated = set(), False
    while queue:
        if len(visited) >= MAX_VISITED:
            truncated = True
            break
        path, root = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        markers, seen = [], set()
        for spec, line in parse(path)[1]:
            marker, why = marker_for(spec)
            if marker:
                # Un modulo es un solo problema, aunque importe varios modulos de servidor.
                if marker not in seen:
                    seen.add(marker)
                    markers.append({'spec': marker, 'line': line, 'reason': why})
                continue
            target = resolve(spec, path, index, rules)
            if target is None or target in visited:
                continue
            if 'server' in parse(target)[0]:
                # "use server": frontera legal. Next serializa la llamada, no la empaqueta.
                continue
            if target not in parent:
                parent[target], origin[target] = path, origin.get(path, root)
            queue.append((target, origin.get(path, root)))
        if markers:
            chain, cursor = [], path
            while cursor is not None:
                chain.append(cursor)
                cursor = parent.get(cursor)
            chain.reverse()
            touched = [p for p in chain if p in delta]
            candidates.append({
                'kind': 'server-module-in-client-graph',
                'module': path, 'line': markers[0]['line'],
                'marker': markers[0]['spec'], 'markers': markers,
                'client_root': origin.get(path, root), 'chain': chain,
                'delta_files_in_chain': touched, 'en_delta': bool(touched),
            })
    candidates.sort(key=lambda c: (not c['en_delta'], len(c['chain']), c['module']))
    return {
        'candidates': candidates[:MAX_CANDIDATES],
        'client_roots': len(roots), 'modules_scanned': len(index),
        'client_graph': len(visited), 'truncated': truncated,
        'omitted': max(0, len(candidates) - MAX_CANDIDATES),
    }


def batch_reader(repo, ref, paths):
    """Precarga el contenido de `paths` en una sola invocacion de git."""
    wanted = [p for p in paths if p.endswith(CODE_EXT + ('.json',)) and not p.endswith('.d.ts')]
    if not wanted:
        return lambda path: None
    process = subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    request = ''.join(f'{ref}:{path}\n' for path in wanted).encode('utf-8', errors='surrogateescape')
    out, _ = process.communicate(request)
    sources, position = {}, 0
    for path in wanted:
        end = out.find(b'\n', position)
        if end < 0:
            break
        header = out[position:end].split()
        position = end + 1
        if len(header) < 3 or header[1] != b'blob':
            continue  # missing / no es un blob
        size = int(header[2])
        if size <= MAX_FILE_BYTES:
            sources[path] = out[position:position + size].decode('utf-8', errors='replace')
        position += size + 1
    return sources.get


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    repo, ref = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'HEAD'
    names = subprocess.run(['git', '-C', repo, 'ls-tree', '-r', '--name-only', ref],
                           stdout=subprocess.PIPE).stdout.decode('utf-8', errors='replace').splitlines()
    print(json.dumps(scan(batch_reader(repo, ref, names), names), indent=2, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
