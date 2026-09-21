#!/usr/bin/env python3
"""Sincroniza las corridas de /pre-pr-review con una API Agent Autolearn. Opcional y offline primero.

Uso:
  review_sync.py setup [PERFIL] [--url URL] [--default] [--no-repo] [--no-alias]
                                   # todo en uno: valida el token, guarda el perfil, lo asigna al repo actual y crea el alias
  review_sync.py configure --profile NOMBRE --url URL [--default]   # token: AGENT_AUTOLEARN_TOKEN o prompt oculto
  review_sync.py profiles
  review_sync.py use NOMBRE [--repo DIR]                             # escribe .agent-autolearn.json en el repo
  review_sync.py status [--repo DIR] [--json]
  review_sync.py enqueue RUN_DIR [--quiet]                           # solo local, sin red
  review_sync.py push [--repo DIR] [--timeout 10] [--retry-failed] [--quiet]

Perfil activo: AGENT_AUTOLEARN_PROFILE > .agent-autolearn.json del repo > perfil por defecto > ninguno
(sin perfil la sincronizacion queda desactivada; no es un error). El token de cada perfil decide el
workspace de destino en la API. Nada de esto cambia el veredicto ni bloquea una revision.

Cola: <repo>/.pre-pr-review/sync/queue/<client_run_id>.json; progreso: .../sync/state.json.
client_run_id = UUIDv5(NS, "<remoto normalizado>|<rama>|<nombre del informe de la pasada>"): estable por
pasada, recalculable desde el ledger para enlazar previous_run_id sin adivinar por fecha o rama.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from ledger import huella  # noqa: E402  Misma huella que el ledger.

PLUGIN_ROOT_DEFAULT = SCRIPTS.parent
NS = uuid.uuid5(uuid.NAMESPACE_URL, 'agent-autolearn/review-run')
REPO_FILE = '.agent-autolearn.json'
DEFAULT_URL = 'https://agent-autolearn.josephluihs.workers.dev'  # setup la usa si no hay --url ni AGENT_AUTOLEARN_URL
MAX_TEXT = 4000
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
LOCK_STALE_SECONDS = 600


class SyncError(Exception):
    """Error permanente de un envio: queda sync_failed hasta un --retry-failed explicito."""


class Transient(Exception):
    """Fallo temporal (red, 429, 5xx): el envio queda pending para el proximo push."""


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f'HTTP {status}')
        self.status = status
        self.body = body

    def message(self):
        try:
            err = json.loads(self.body)['error']
            return f"HTTP {self.status} {err.get('code')}: {err.get('message')}"
        except (ValueError, KeyError, TypeError):
            return f'HTTP {self.status}'


# ---------- utilidades ----------

def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError):
        return default


def write_json(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.decode('utf-8', errors='replace').strip() if result.returncode == 0 else None


def repo_root(path):
    root = git(path or '.', 'rev-parse', '--show-toplevel')
    if not root:
        raise SyncError(f'{path or "."} no es un repositorio git.')
    return Path(root).resolve()


def normalize_remote(remote):
    """host/owner/repo en minusculas, sin credenciales, esquema ni .git. None si no es un remoto."""
    if not remote:
        return None
    s = remote.strip()
    scp = re.match(r'^(?:[^@\s/]+@)?([^:\s/]+):(?!//)(.+)$', s)
    if scp and '://' not in s:
        host, path = scp.group(1), scp.group(2)
    else:
        m = re.match(r'^[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?([^/:\s]+)(?::\d+)?(/.*)?$', s, re.I)
        if not m:
            m2 = re.match(r'^([\w.-]+\.[a-z]{2,})/(.+)$', s, re.I)
            if not m2:
                return None
            host, path = m2.group(1), m2.group(2)
        else:
            host, path = m.group(1), m.group(2) or ''
    path = re.sub(r'\.git$', '', path.strip('/'), flags=re.I).strip('/')
    if not host or not path:
        return None
    return f'{host.lower()}/{path.lower()}'


# ---------- depuracion (antes de que nada salga del equipo) ----------

SECRET_PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('github_token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b')),
    ('anthropic_key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai_key', re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}')),
    ('stripe_key', re.compile(r'\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b')),
    ('slack_token', re.compile(r'\bxox[abpors]-[A-Za-z0-9-]{10,}')),
    ('autolearn_token', re.compile(r'\balt_[A-Za-z0-9_-]{20,}')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ('bearer', re.compile(r'\b[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}=*')),
]
URL_CREDENTIALS = re.compile(r'\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@', re.I)
ASSIGNED_SECRET = re.compile(
    r'\b((?:[A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY)[A-Za-z0-9_]*)\s*[:=]\s*)(["\']?)[^\s"\']{6,}\2',
    re.I)
SENSITIVE_FILE = re.compile(r'(^|/)\.env(\.|$)|\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519|ecdsa)$', re.I)


def scrub(text, redactions):
    if text is None:
        return None
    out = str(text)
    for kind, pattern in SECRET_PATTERNS:
        out, n = pattern.subn(f'[REDACTED:{kind}]', out)
        if n:
            redactions[kind] = redactions.get(kind, 0) + n
    out, n = URL_CREDENTIALS.subn(r'\1[REDACTED]@', out)
    if n:
        redactions['url_credentials'] = redactions.get('url_credentials', 0) + n
    out, n = ASSIGNED_SECRET.subn(r'\1[REDACTED]', out)
    if n:
        redactions['assigned_secret'] = redactions.get('assigned_secret', 0) + n
    return out


def clean_text(text, redactions, limit=MAX_TEXT, file=None):
    if text is None:
        return None
    if file and SENSITIVE_FILE.search(str(file)):
        redactions['sensitive_file'] = redactions.get('sensitive_file', 0) + 1
        return '[omitido: archivo sensible]'
    out = scrub(text, redactions)
    if len(out) > limit:
        redactions['truncated'] = redactions.get('truncated', 0) + 1
        out = out[:limit] + '…[truncado]'
    return out


# ---------- configuracion y perfiles ----------

def config_path():
    explicit = os.environ.get('AGENT_AUTOLEARN_CONFIG')
    if explicit:
        return Path(explicit)
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'agent-autolearn' / 'config.json'


def load_config():
    data = read_json(config_path(), {}) or {}
    data.setdefault('profiles', {})
    return data


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    write_json(path, data, mode=0o600)


def token_prefix(token):
    return (token[:8] + '…') if token else '—'


def resolve_profile(repo=None):
    """(nombre, origen) o (None, motivo). No valida que el perfil exista localmente."""
    env = os.environ.get('AGENT_AUTOLEARN_PROFILE')
    if env:
        return env, 'variable AGENT_AUTOLEARN_PROFILE'
    if repo:
        chosen = (read_json(Path(repo) / REPO_FILE, {}) or {}).get('profile')
        if isinstance(chosen, str) and chosen:
            return chosen, f'{REPO_FILE} del repo'
    default = load_config().get('default')
    if default:
        return default, 'perfil por defecto'
    return None, 'sin perfil activo'


def profile_config(name):
    profile = load_config()['profiles'].get(name) if name else None
    if not profile or not profile.get('url') or not profile.get('token'):
        return None
    return profile


# ---------- HTTP ----------

class Client:
    def __init__(self, profile, timeout, backoff=None):
        self.base = profile['url'].rstrip('/')
        self.token = profile['token']
        self.timeout = timeout
        self.backoff = backoff if backoff is not None else float(os.environ.get('AGENT_AUTOLEARN_BACKOFF', '0.5'))

    def request(self, method, path, body=None, key=None, if_match=None):
        """(status, json, etag). Reintenta fallos temporales 3 veces; errores 4xx no se reintentan."""
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers = {'authorization': f'Bearer {self.token}', 'user-agent': 'agent-autolearn-sync/1',
                   'accept': 'application/json'}
        if data is not None:
            headers['content-type'] = 'application/json'
        if key:
            headers['idempotency-key'] = key
        if if_match:
            headers['if-match'] = f'"{if_match}"'
        last = None
        for attempt in range(3):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    raw = res.read().decode('utf-8', errors='replace')
                    etag = (res.headers.get('etag') or '').strip('"').replace('W/', '').strip('"') or None
                    return res.status, (json.loads(raw) if raw else None), etag
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode('utf-8', errors='replace')
                if exc.code in TRANSIENT:
                    last = HttpError(exc.code, raw)
                    continue
                raise HttpError(exc.code, raw) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = exc
                continue
        detail = last.message() if isinstance(last, HttpError) else f'sin conexion ({type(last).__name__})'
        raise Transient(detail)


# ---------- construccion del envio (offline) ----------

def locale_key(path):
    """Orden equivalente a String.localeCompare (ICU raiz) para rutas: primario sin mayusculas, '_' antes de '-'."""
    return (path.lower().replace('_', '\x01'), path.swapcase())


def content_hash(files):
    """Igual que la API: un archivo → sha256 del contenido; varios → sha256 del manifiesto ordenado."""
    if len(files) == 1:
        return hashlib.sha256(files[0][1]).hexdigest()
    ordered = sorted(files, key=lambda f: locale_key(f[0]))
    manifest = ''.join(f'{hashlib.sha256(content).hexdigest()}  {path}\n' for path, content in ordered)
    return hashlib.sha256(manifest.encode('utf-8')).hexdigest()


def plugin_components(completed_reviewers):
    """Componentes realmente usados: (type, name, [rutas relativas a la raiz del plugin])."""
    from prepare_review import REVIEWER_RULES
    comps = [('orchestrator', 'pre-pr-review', ['skills/pre-pr-review/SKILL.md', 'agents/review-aggregator.md'])]
    root = Path(os.environ.get('CLAUDE_PLUGIN_ROOT') or PLUGIN_ROOT_DEFAULT)
    refs = sorted(p.relative_to(root).as_posix() for p in (root / 'skills/pre-pr-review/references').glob('*.md'))
    comps[0][2].extend(refs)
    skills = {}
    for reviewer in completed_reviewers:
        comps.append(('reviewer', reviewer, [f'agents/{reviewer}.md']))
        for rule in REVIEWER_RULES.get(reviewer, []):
            parts = rule.split('/')
            if parts[0] == 'skills' and parts[1] != 'pre-pr-review':
                skills[parts[1]] = rule
    comps.extend(('skill', name, [path]) for name, path in sorted(skills.items()))
    return comps


def plugin_git_source(root):
    """(raiz del checkout, prefijo del plugin dentro del repo) o (None, motivo).

    Claude Code ejecuta los plugins instalados desde ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/,
    que no es un checkout de git. En ese caso se usa, por orden: AGENT_AUTOLEARN_PLUGIN_REPO (un clon del repo
    del plugin) o el clon del marketplace en ~/.claude/plugins/marketplaces/<marketplace>/.
    """
    top = git(root, 'rev-parse', '--show-toplevel')
    if top:
        top = Path(top).resolve()
        return top, (root.relative_to(top).as_posix() if root != top else '')
    candidates = []
    if os.environ.get('AGENT_AUTOLEARN_PLUGIN_REPO'):
        candidates.append(Path(os.environ['AGENT_AUTOLEARN_PLUGIN_REPO']).expanduser())
    if root.parent.parent.parent.name == 'cache':
        candidates.append(root.parent.parent.parent.parent / 'marketplaces' / root.parent.parent.name)
    plugin_name = (read_json(root / '.claude-plugin' / 'plugin.json', {}) or {}).get('name') or root.parent.name
    for cand in candidates:
        ctop = git(cand, 'rev-parse', '--show-toplevel')
        if not ctop:
            continue
        ctop = Path(ctop).resolve()
        market = read_json(ctop / '.claude-plugin' / 'marketplace.json', {}) or {}
        source = next((p.get('source') for p in market.get('plugins', []) if p.get('name') == plugin_name), None)
        prefix = source.strip('./').rstrip('/') if isinstance(source, str) else ''
        if (ctop / prefix / '.claude-plugin' / 'plugin.json').is_file():
            return ctop, prefix
    return None, ('El plugin instalado no esta en un checkout de git y no se encontro un clon del repo del plugin '
                  '(define AGENT_AUTOLEARN_PLUGIN_REPO): no hay commit que registrar para sus versiones.')


def matching_commit(top, repo_paths, files, depth=200):
    """Commit mas reciente (desde HEAD) donde TODAS las rutas tienen exactamente el contenido usado."""
    want = {}
    for repo_path, f in zip(repo_paths, files):
        blob = subprocess.run(['git', '-C', str(top), 'hash-object', '--no-filters', str(f)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if blob.returncode != 0:
            return None
        want[repo_path] = blob.stdout.decode().strip()
    log = git(top, 'log', f'-n{depth}', '--format=%H', 'HEAD', '--', *repo_paths)
    for commit in (log or '').split():
        tree = git(top, 'ls-tree', commit, '--', *repo_paths) or ''
        have = {line.split('\t', 1)[1]: line.split()[2] for line in tree.splitlines() if '\t' in line}
        if have == want:
            return commit
    return None


def component_versions(completed_reviewers):
    """(lista de versiones, motivo si alguna no se puede registrar).

    La version registrada es el commit cuyo contenido coincide byte a byte con los archivos que realmente se
    usaron; la API verifica ese mismo hash contra el commit. Si no hay coincidencia (cambios locales, clon
    desactualizado), ese componente no se registra en lugar de declarar una version falsa.
    """
    root = Path(os.environ.get('CLAUDE_PLUGIN_ROOT') or PLUGIN_ROOT_DEFAULT).resolve()
    top, prefix_or_reason = plugin_git_source(root)
    if top is None:
        return [], prefix_or_reason
    prefix = prefix_or_reason
    manifest = read_json(root / '.claude-plugin' / 'plugin.json', {}) or {}
    versions, skipped = [], []
    for kind, name, rel_paths in plugin_components(completed_reviewers):
        existing = [(f'{prefix}/{rel}' if prefix else rel, root / rel) for rel in rel_paths if (root / rel).is_file()]
        if not existing:
            continue
        repo_paths = [p for p, _ in existing]
        commit = matching_commit(top, repo_paths, [f for _, f in existing])
        if not commit:
            skipped.append(f'{kind}:{name}')
            continue
        versions.append({'type': kind, 'name': name, 'commit': commit,
                         'content_hash': content_hash([(p, f.read_bytes()) for p, f in existing]),
                         'plugin_version': manifest.get('version'), 'paths': sorted(repo_paths)})
    reason = (f'Sin commit que coincida con el contenido usado (cambios locales o clon desactualizado), no registrados: '
              f'{", ".join(skipped)}.') if skipped else None
    return versions, reason


def run_started_at(run_dir):
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})-(\d{6})-p\d+', Path(run_dir).name)
    if not m:
        return None
    y, mo, d, h, mi, s, us = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def client_run_id(remote, branch, report_name):
    return str(uuid.uuid5(NS, f'{remote}|{branch}|{report_name}'))


def previous_client_run_id(run, remote):
    ledger = read_json(run.get('ledger'), {}) or {}
    prev = next((p for p in ledger.get('pasadas', []) if p.get('n') == run.get('pass_n', 0) - 1), None)
    return client_run_id(remote, run['branch'], prev['informe']) if prev and prev.get('informe') else None


def patch_size(run_dir, delta_files):
    try:
        lines = (Path(run_dir) / 'new.patch').read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return {'files_changed': len(delta_files or []), 'lines_changed': None}
    changed = sum(1 for l in lines if (l.startswith('+') or l.startswith('-')) and not l.startswith(('+++', '---')))
    return {'files_changed': len(delta_files or []), 'lines_changed': changed}


MAX_PATCH_FILES = 300


def file_kind(path):
    lower = path.lower()
    if re.search(r'(^|/)(tests?|__tests__|spec)/|[._-](test|spec)\.[a-z]+$|_test\.go$', lower):
        return 'test'
    if lower.endswith(('.md', '.mdx', '.rst', '.txt')) or lower.startswith('docs/'):
        return 'docs'
    if lower.endswith(('.json', '.jsonc', '.yaml', '.yml', '.toml', '.lock', '.ini', '.env.example')) or lower.startswith('.'):
        return 'config'
    return 'src'


def patch_breakdown(run_dir):
    """Tamaño del patch que leen los revisores, por archivo. Solo contadores y rutas: nunca contenido."""
    try:
        text = (Path(run_dir) / 'new.patch').read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return None
    files, current = {}, None
    for line in text.splitlines(keepends=True):
        m = re.match(r'diff --git a/(\S+) b/', line)
        if m:
            current = files.setdefault(m.group(1), {'path': m.group(1), 'bytes': 0, 'added': 0, 'removed': 0})
        if current is None:
            continue
        current['bytes'] += len(line.encode('utf-8'))
        if line.startswith('+') and not line.startswith('+++'):
            current['added'] += 1
        elif line.startswith('-') and not line.startswith('---'):
            current['removed'] += 1
    rows = sorted(files.values(), key=lambda f: (-f['bytes'], f['path']))
    for row in rows:
        row['kind'] = file_kind(row['path'])
    by_kind = {}
    for row in rows:
        by_kind[row['kind']] = by_kind.get(row['kind'], 0) + row['bytes']
    return {'bytes': len(text.encode('utf-8')), 'files_count': len(rows),
            'lines_added': sum(r['added'] for r in rows), 'lines_removed': sum(r['removed'] for r in rows),
            'bytes_by_kind': by_kind, 'files': rows[:MAX_PATCH_FILES], 'files_truncated': len(rows) > MAX_PATCH_FILES}


def reviewer_searches(run_dir):
    counts = {}
    for path in sorted((Path(run_dir) / 'searches').glob('*.json')):
        data = read_json(path)
        entries = data.get('searches', []) if isinstance(data, dict) else data if isinstance(data, list) else []
        counts[path.stem] = sum(1 for e in entries if isinstance(e, dict) and isinstance(e.get('query'), str) and e['query'].strip())
    return counts


TURN_FIELDS = ('requests', 'tool_calls', 'single_tool_turns', 'parallel_turns')


def turn_profiles(run_dir):
    """{reviewer: contadores de turnos} desde usage/*.json. Archivos sin perfil (plugin anterior) no aportan nada."""
    out = {}
    for path in sorted((Path(run_dir) / 'usage').glob('*.json')):
        value = read_json(path)
        if not isinstance(value, dict) or not isinstance(value.get('reviewer'), str) or value.get('estimated'):
            continue
        if not all(type(value.get(k)) is int for k in (*TURN_FIELDS, 'peak_context')):
            continue
        row = out.setdefault(value['reviewer'], {k: 0 for k in (*TURN_FIELDS, 'peak_context')})
        for k in TURN_FIELDS:
            row[k] += value[k]
        row['peak_context'] = max(row['peak_context'], value['peak_context'])
    return out


def run_diagnostics(run, run_dir, redactions):
    """Por que la corrida costo lo que costo: motivo del modo, tamaño del patch y actividad por revisor."""
    from review_usage import repeated_searches
    run_dir = Path(run_dir)
    searches = repeated_searches(run_dir)
    per_reviewer = reviewer_searches(run_dir)
    reasons = run.get('reviewer_reasons') or {}
    assignments = run.get('assignments') or {}
    skipped = run.get('skipped_reviewers') or {}
    reviewers = []
    turns = turn_profiles(run_dir)
    names = sorted({*run.get('expected_reviewers', []), *reasons, *skipped})
    for name in [*names, *(['review-aggregator'] if 'review-aggregator' in turns else [])]:
        reviewers.append({'reviewer': name, 'status': 'skipped' if name in skipped else 'expected',
                          'reason': clean_text(skipped.get(name) or reasons.get(name), redactions, 500),
                          'pending_assigned': len(assignments.get(name) or []),
                          'searches': per_reviewer.get(name), **turns.get(name, {})})
    body = {'schema_version': 1, 'mode_reason': clean_text(run.get('reason'), redactions, 300),
            'since_tree': run.get('since_tree'), 'base_tree': run.get('base_tree'),
            'plugin_version': run.get('plugin_version'), 'patch': patch_breakdown(run_dir),
            'reviewers': reviewers[:50],
            'searches': {'distinct': searches['distinct'], 'repeated': searches['repeated']}}
    return {k: v for k, v in body.items() if v is not None}


def reviewer_files(run_dir, expected):
    out = {}
    for reviewer in expected:
        data = read_json(Path(run_dir) / f'{reviewer}.json')
        if isinstance(data, dict) and data.get('reviewer') == reviewer and isinstance(data.get('findings'), list):
            out[reviewer] = data
    return out


def usage_records(run_dir):
    records = []
    for path in sorted((Path(run_dir) / 'usage').glob('*.json')):
        value = read_json(path)
        if not isinstance(value, dict) or value.get('agent_key') != path.stem or not isinstance(value.get('reviewer'), str):
            continue
        if not re.fullmatch(r'[\w.:-]{1,128}', path.stem):
            continue
        missing = [f for f in value.get('missing_fields', []) if isinstance(f, str)][:20]
        fields = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
        requests = value.get('requests') if type(value.get('requests')) is int else None
        body = {'reviewer': value['reviewer'],
                'models': [m for m in value.get('models', []) if isinstance(m, str)][:10],
                'requests': requests,
                # Un contador con peticiones sin medir es desconocido, no la suma parcial.
                **{f: (None if f in missing or type(value.get(f)) is not int or not requests else value[f]) for f in fields},
                'source': value.get('source'), 'estimated': bool(value.get('estimated', False)),
                'output_provisional': bool(value.get('output_provisional', False)),
                'complete': value.get('complete') if isinstance(value.get('complete'), bool) else None,
                'missing_fields': missing}
        records.append({'agent_key': path.stem, 'body': body})
    return records


def build_result(run, run_dir, redactions):
    expected = run.get('expected_reviewers', [])
    present = reviewer_files(run_dir, expected)
    clasif = read_json(Path(run_dir) / 'clasificacion.json', {}) or {}
    resumen = clasif.get('resumen', {}) if isinstance(clasif, dict) else {}
    finalized = run.get('status') == 'finalized'
    reviewers = [{'reviewer': r, 'status': 'completed' if r in present else 'missing',
                  **({} if r in present else {'reason': 'Sin informe del revisor en la corrida.'})} for r in expected]
    for r, reason in (run.get('skipped_reviewers') or {}).items():
        if r not in expected:
            reviewers.append({'reviewer': r, 'status': 'skipped', 'reason': clean_text(reason, redactions, 500)})
    complete = finalized and all(r in present for r in expected) and resumen.get('coverage_complete') is True
    notes = None
    if not finalized:
        errors = resumen.get('errors') or []
        notes = clean_text('Finalizacion fallida: ' + '; '.join(map(str, errors))[:1500], redactions, 2000)

    findings, raw = [], []
    if finalized:
        curated = read_json(Path(run_dir) / 'curated.json', {}) or {}
        ledger = read_json(run.get('ledger'), {}) or {}
        records = {r['huella']: r for r in ledger.get('hallazgos', []) if isinstance(r, dict) and r.get('huella')}
        # El estado de ESTA pasada sale de su clasificacion.json (foto al finalizar), no del ledger vivo,
        # que ya puede reflejar pasadas posteriores si la corrida se encola tarde.
        for key in ('descartados', 'cerrados', 'suprimidos', 'hallazgos'):
            for r in clasif.get(key, []) if isinstance(clasif, dict) else []:
                if isinstance(r, dict) and r.get('huella'):
                    records[r['huella']] = r
        aliases = ledger.get('aliases', {}) or {}

        def canonical(fp):
            seen = set()
            while fp in aliases and fp not in seen:
                seen.add(fp)
                fp = aliases[fp]
            return fp

        verifications = {v['fingerprint']: v for v in curated.get('verifications', []) if isinstance(v, dict) and v.get('fingerprint')}
        source_to_canonical, sent = {}, {}

        def finding_body(fp, src, record, sources, verification):
            file = src.get('file') or record.get('archivo')
            decision = record.get('estado') or {'open': 'abierto', 'discarded': 'descartado'}.get(src.get('status'), 'abierto')
            body = {
                'fingerprint': fp,
                'aliases': [a for a in sorted(set(record.get('aliases', []) + list(src.get('aliases', []))) - {fp})
                            if isinstance(a, str) and 4 <= len(a) <= 128][:50],
                'sources': sorted(set(sources))[:20],
                'severity': src.get('severity') or record.get('severidad'),
                'category': src.get('category') or record.get('categoria'),
                'title': clean_text(src.get('title') or record.get('titulo') or '(sin titulo)', redactions, 500),
                'file': file, 'symbol': src.get('symbol') or record.get('simbolo'),
                'line': src.get('line') if type(src.get('line')) is int else record.get('linea'),
                'evidence': clean_text(src.get('evidence') or record.get('evidence'), redactions, file=file),
                'why': clean_text(src.get('why') or record.get('why'), redactions, file=file),
                'fix': clean_text(src.get('fix') or record.get('fix'), redactions, file=file),
                'confidence': src.get('confidence') or record.get('confidence'),
                'decision': decision,
                'discard_reason': clean_text(src.get('discard_reason'), redactions, 1000) if decision == 'descartado' else None,
                'pass_status': record.get('estado_pasada') if src.get('status') == 'open' else None,
                'external_decision': (str(src.get('decision_externa') or record.get('decision_externa'))[:50]
                                      if (src.get('decision_externa') or record.get('decision_externa')) else None),
            }
            if verification:
                body['verification'] = {'status': verification.get('status'), 'reviewer': verification.get('reviewer'),
                                        'evidence': clean_text(verification.get('evidence'), redactions, file=file)}
            return {k: v for k, v in body.items() if v is not None}

        for src in curated.get('findings', []):
            if not isinstance(src, dict):
                continue
            fp = canonical(huella(src.get('category'), src.get('file'), src.get('symbol')))
            if fp in sent:
                continue
            for s in src.get('sources', []):
                source_to_canonical[s] = fp
            sources = src.get('detectado_por') or ([src['reviewer']] if src.get('reviewer') else [])
            if not sources:
                continue
            sent[fp] = finding_body(fp, src, records.get(fp, {}), sources, verifications.get(fp))
        # Pendientes revalidados en esta pasada (cerrados, abiertos o descartados por su revisor).
        for fp, verification in verifications.items():
            fp_c = canonical(fp)
            if fp_c in sent or fp_c not in records:
                continue
            record = records[fp_c]
            sources = record.get('detectado_por') or [verification.get('reviewer')]
            sent[fp_c] = finding_body(fp_c, {}, record, [s for s in sources if s], verification)
        findings = list(sent.values())[:1000]
        for reviewer, data in present.items():
            for f in data['findings']:
                if not isinstance(f, dict):
                    continue
                fp = huella(f.get('category'), f.get('file'), f.get('symbol'))
                obs = {'fingerprint': fp, 'reviewer': reviewer, 'severity': f.get('severity'), 'category': f.get('category'),
                       'title': clean_text(f.get('title') or '(sin titulo)', redactions, 500), 'file': f.get('file'),
                       'symbol': f.get('symbol'), 'line': f.get('line') if type(f.get('line')) is int else None,
                       'merged_into': source_to_canonical.get(fp)}
                raw.append({k: v for k, v in obs.items() if v is not None})

    finished = resumen.get('finished_at')
    if not finished:
        stat = (Path(run_dir) / 'clasificacion.json')
        finished = (datetime.fromtimestamp(stat.stat().st_mtime, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                    if stat.exists() else None)
    body = {'schema_version': 1, 'status': 'completed' if finalized else 'incomplete',
            'verdict': resumen.get('veredicto') or ('REVISION INCOMPLETA' if not finalized else None),
            'coverage': {'complete': bool(complete), 'reviewers': reviewers, **({'notes': notes} if notes else {})},
            'findings': findings, 'raw_observations': raw[:4000] if raw else None,
            'counts': {'total': len(findings)}, 'finished_at': finished}
    return {k: v for k, v in body.items() if v is not None}, sorted(present)


def enqueue(run_dir, quiet=False):
    run_dir = Path(run_dir).resolve()
    run = read_json(run_dir / 'run.json')
    if not isinstance(run, dict):
        raise SyncError(f'{run_dir} no contiene un run.json valido.')
    repo = Path(run['repo']).resolve()
    profile, origin = resolve_profile(repo)
    if not profile:
        return {'queued': False, 'reason': 'Sincronizacion desactivada: no hay perfil activo.'}
    if run.get('status') == 'unchanged':
        return {'queued': False, 'reason': 'Corrida sin cambios: no hay revision que sincronizar.'}
    if run.get('status') != 'finalized' and not (run_dir / 'clasificacion.json').exists():
        return {'queued': False, 'reason': 'La corrida aun no esta finalizada.'}
    remote = normalize_remote(git(repo, 'remote', 'get-url', 'origin'))
    if not remote:
        return {'queued': False, 'reason': 'El repo no tiene un remoto origin reconocible; nunca se envia la ruta local.'}
    ensure_ignored(repo)
    redactions = {}
    result, completed = build_result(run, run_dir, redactions)
    versions, versions_note = component_versions(completed)
    cid = client_run_id(remote, run['branch'], Path(run['report']).name)
    item = {
        'schema_version': 1, 'client_run_id': cid, 'profile': profile, 'enqueued_at': now_iso(),
        'run_dir': str(run_dir), 'remote': remote, 'component_versions': versions, 'component_versions_note': versions_note,
        'run': {'schema_version': 1, 'client_run_id': cid, 'branch': run['branch'], 'head_commit': run['head'],
                'base_ref': run.get('base'), 'merge_base': run.get('merge_base'), 'snapshot_hash': run['tree'],
                'snapshot_format': run.get('snapshot_format'), 'mode': run['mode'], 'pass_number': run.get('pass_n'),
                'previous_run_id': previous_client_run_id(run, remote),
                'expected_reviewers': run.get('expected_reviewers', []),
                'size': patch_size(run_dir, run.get('delta_files')), 'status': 'running',
                'started_at': run_started_at(run_dir)},
        'result': result,
        'usage': usage_records(run_dir),
        'diagnostics': run_diagnostics(run, run_dir, redactions),
        'redactions': redactions,
    }
    item['run'] = {k: v for k, v in item['run'].items() if v is not None}
    write_json(queue_dir(repo) / f'{cid}.json', item)
    state = load_state(repo)
    entry = state['runs'].setdefault(cid, {'state': 'pending'})
    if entry.get('state') == 'sent' and entry.get('result_hash') != digest(result):
        entry['state'] = 'pending'  # Resultado nuevo de la misma pasada (reanudacion): se vuelve a enviar.
    save_state(repo, state)
    return {'queued': True, 'client_run_id': cid, 'profile': profile, 'profile_source': origin,
            'findings': len(result.get('findings', [])), 'usage_agents': len(item['usage']),
            'component_versions': len(versions), 'component_versions_note': versions_note, 'redactions': redactions}


def ensure_ignored(repo):
    """Los artifacts locales nunca se commitean: si el repo no los ignora, se excluyen solo en este clon."""
    probe = repo / '.pre-pr-review' / 'sync'
    probe.mkdir(parents=True, exist_ok=True)
    ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', str(probe / 'state.json')],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not ignored:
        exclude = git(repo, 'rev-parse', '--git-path', 'info/exclude')
        if exclude:
            path = Path(exclude) if Path(exclude).is_absolute() else repo / exclude
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write('\n.pre-pr-review/\n')


# ---------- estado y cola ----------

def queue_dir(repo):
    return Path(repo) / '.pre-pr-review' / 'sync' / 'queue'


def state_path(repo):
    return Path(repo) / '.pre-pr-review' / 'sync' / 'state.json'


def load_state(repo):
    state = read_json(state_path(repo), {}) or {}
    state.setdefault('runs', {})
    return state


def save_state(repo, state):
    write_json(state_path(repo), state)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class push_lock:
    def __init__(self, repo):
        self.path = Path(repo) / '.pre-pr-review' / 'sync' / 'push.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                return False
        return False

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def current_diagnostics(item):
    """Se relee de la corrida si sigue en disco: cubre corridas encoladas antes de existir el diagnostico."""
    run = read_json(Path(item['run_dir']) / 'run.json') if item.get('run_dir') else None
    if isinstance(run, dict):
        return run_diagnostics(run, item['run_dir'], {})
    return item.get('diagnostics')


def push_item(client, item, entry, run_dir_usage, diagnostics=None):
    """Avanza un envio paso a paso. Cada paso es idempotente y queda registrado antes del siguiente."""
    cid = item['client_run_id']

    def call(method, path, body=None, key=None, if_match=None):
        try:
            return client.request(method, path, body, key, if_match)
        except HttpError as exc:
            if exc.status == 412:
                raise
            raise SyncError(exc.message()) from None

    if not entry.get('repository_id'):
        _, data, _ = call('POST', '/v1/repositories', {'remote': item['remote']}, f'{cid}:repository')
        entry['repository_id'] = data['id']
    ids = entry.setdefault('component_version_ids', {})
    for cv in item.get('component_versions', []):
        k = f"{cv['type']}:{cv['name']}"
        if k not in ids:
            _, data, _ = call('POST', '/v1/component-versions', cv, f'{cid}:cv:{k}')
            ids[k] = data['id']
    if not entry.get('run_id'):
        body = dict(item['run'], repository_id=entry['repository_id'],
                    component_version_ids=[ids[f"{cv['type']}:{cv['name']}"] for cv in item.get('component_versions', [])])
        _, data, etag = call('POST', '/v1/review-runs', body, f'{cid}:run')
        entry['run_id'], entry['etag'] = data['id'], etag
    result_hash = digest(item['result'])
    if entry.get('result_hash') != result_hash:
        path = f"/v1/review-runs/{entry['run_id']}/result"
        try:
            _, _, etag = call('PUT', path, item['result'], if_match=entry.get('etag') or '1')
        except HttpError:  # 412: otra escritura cambio la corrida. Se relee el ETag una sola vez.
            _, _, current = call('GET', f"/v1/review-runs/{entry['run_id']}")
            try:
                _, _, etag = call('PUT', path, item['result'], if_match=current)
            except HttpError as exc:
                raise SyncError(exc.message()) from None
        entry['etag'], entry['result_hash'] = etag, result_hash
    if diagnostics and entry.get('diagnostics_hash') != digest(diagnostics):
        try:
            client.request('PUT', f"/v1/review-runs/{entry['run_id']}/diagnostics", diagnostics)
            entry['diagnostics_hash'] = digest(diagnostics)
        except HttpError as exc:
            # Un servidor anterior sin el endpoint no bloquea el resultado ni la telemetria.
            if exc.status not in (404, 405):
                raise SyncError(exc.message()) from None
    sent_usage = entry.setdefault('usage', {})
    for record in run_dir_usage:
        h = digest(record['body'])
        if sent_usage.get(record['agent_key']) != h:
            call('PUT', f"/v1/review-runs/{entry['run_id']}/usage/{record['agent_key']}", record['body'])
            sent_usage[record['agent_key']] = h


def push(repo, timeout=10.0, retry_failed=False):
    repo = repo_root(repo)
    summary = {'sent': 0, 'pending': 0, 'sync_failed': 0, 'skipped': 0, 'errors': []}
    # En orden de encolado: una pasada se envia antes que la siguiente y enlaza previous_run_id directo.
    items = sorted(queue_dir(repo).glob('*.json'), key=lambda p: ((read_json(p, {}) or {}).get('enqueued_at') or '', p.name))
    if not items:
        return summary
    with push_lock(repo) as acquired:
        if not acquired:
            summary['errors'].append('Otro push esta en curso para este repo.')
            return summary
        state = load_state(repo)
        stop_transient = False
        for path in items:
            item = read_json(path)
            if not isinstance(item, dict) or not item.get('client_run_id'):
                continue
            cid = item['client_run_id']
            entry = state['runs'].setdefault(cid, {'state': 'pending'})
            if entry.get('state') == 'sync_failed' and not retry_failed:
                summary['sync_failed'] += 1
                continue
            # La telemetria puede llegar despues del resultado: se relee de la corrida si sigue en disco.
            usage = usage_records(item['run_dir']) if Path(item['run_dir'], 'run.json').exists() else item.get('usage', [])
            diagnostics = current_diagnostics(item)
            if entry.get('state') == 'sent':
                pending_usage = [u for u in usage if entry.get('usage', {}).get(u['agent_key']) != digest(u['body'])]
                pending_diag = diagnostics and entry.get('diagnostics_hash') != digest(diagnostics)
                if not pending_usage and not pending_diag:
                    summary['skipped'] += 1
                    continue
            if stop_transient:
                summary['pending'] += 1
                continue
            profile = profile_config(item.get('profile'))
            entry['attempted_at'] = now_iso()
            if not profile:
                entry.update(state='sync_failed', error=f"El perfil '{item.get('profile')}' no esta configurado en este equipo.")
                summary['sync_failed'] += 1
                summary['errors'].append(f"{cid[:8]}: {entry['error']}")
                continue
            try:
                push_item(Client(profile, timeout), item, entry, usage, diagnostics)
                entry.update(state='sent', error=None, sent_at=now_iso())
                summary['sent'] += 1
            except Transient as exc:
                entry.update(state='pending', error=f'Temporal: {exc}')
                summary['pending'] += 1
                summary['errors'].append(f"{cid[:8]}: {entry['error']}")
                stop_transient = True  # El servicio no responde: no se insiste con el resto de la cola.
            except SyncError as exc:
                entry.update(state='sync_failed', error=str(exc))
                summary['sync_failed'] += 1
                summary['errors'].append(f'{cid[:8]}: {exc}')
            finally:
                save_state(repo, state)
    return summary


# ---------- comandos ----------

def normalize_url(raw):
    url = re.sub(r'/v1$', '', raw.strip().rstrip('/'))
    if not re.match(r'^https?://[^\s/]+', url):
        raise SyncError('--url debe ser http(s)://host[:puerto].')
    return url


def read_token(profile):
    token = os.environ.get('AGENT_AUTOLEARN_TOKEN')
    if not token:
        if not sys.stdin.isatty():
            raise SyncError('Define AGENT_AUTOLEARN_TOKEN o ejecuta el comando en una terminal para escribir el token.')
        token = getpass.getpass(f'Token de instalacion para {profile} (no se muestra): ')
    token = token.strip()
    if not token:
        raise SyncError('Token vacio.')
    return token


def save_profile(name, url, token, default):
    """Solo marca el perfil por defecto si se pide: un repo sin .agent-autolearn.json no debe acabar
    enviando a un workspace que nadie eligio."""
    data = load_config()
    data['profiles'][name] = {'url': url, 'token': token}
    if default:
        data['default'] = name
    save_config(data)
    return data.get('default') == name


def cmd_configure(args):
    url = normalize_url(args.url)
    token = read_token(args.profile)
    is_default = save_profile(args.profile, url, token, args.default)
    print(f"Perfil '{args.profile}' guardado en {config_path()} (token {token_prefix(token)}).")
    if is_default:
        print('Es el perfil por defecto.')


def stable_script_path():
    """Ruta del script que sobrevive a /plugin update: el clon del marketplace, no la cache versionada."""
    here = Path(__file__).resolve()
    marketplace = Path.home() / '.claude' / 'plugins' / 'marketplaces' / 'agent-autolearn' / 'plugins' / 'agent-autolearn' / 'scripts' / here.name
    return marketplace if marketplace.is_file() else here


def is_windows():
    return os.name == 'nt'


def python_cmd():
    """python3 en macOS/Linux; en Windows el interprete actual, porque python3 no suele existir."""
    return f'"{Path(sys.executable).as_posix()}"' if is_windows() else 'python3'


def powershell_profile():
    """Perfil de PowerShell del usuario (todas las consolas). Respeta Documentos redirigido a OneDrive."""
    for exe in ('pwsh', 'powershell'):
        path = shutil.which(exe)
        if not path:
            continue
        out = subprocess.run([path, '-NoProfile', '-NonInteractive', '-Command', '$PROFILE.CurrentUserAllHosts'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        value = out.stdout.decode('utf-8', errors='replace').strip()
        if out.returncode == 0 and value:
            return Path(value)
    return None


def install_powershell_function():
    profile = powershell_profile()
    if not profile:
        return None, None
    current = profile.read_text(encoding='utf-8-sig') if profile.exists() else ''
    if re.search(r'^\s*function\s+review-sync\b', current, re.M | re.I):
        return profile, 'present'
    profile.parent.mkdir(parents=True, exist_ok=True)
    sep = '' if not current or current.endswith('\n') else '\n'
    with profile.open('a', encoding='utf-8') as fh:
        fh.write(f"{sep}# agent-autolearn: cliente de sincronizacion\n"
                 f"function review-sync {{ & '{sys.executable}' '{stable_script_path()}' @args }}\n")
    return profile, 'added'


def shell_rc():
    shell = Path(os.environ.get('SHELL') or '').name
    if shell == 'zsh':
        return Path(os.environ.get('ZDOTDIR') or Path.home()) / '.zshrc'
    if shell == 'bash':
        return Path.home() / '.bashrc'
    return None


def alias_line():
    if is_windows():  # Git Bash: rutas con / y el interprete actual entre comillas
        return f"alias review-sync='{python_cmd()} \"{Path(stable_script_path()).as_posix()}\"'"
    return f'alias review-sync="python3 {stable_script_path()}"'


def install_alias():
    """(rc, estado) con estado 'added' | 'present' | None si no hay zsh, bash ni PowerShell (Windows)."""
    rc = shell_rc()
    if not rc:
        return install_powershell_function() if is_windows() else (None, None)
    current = rc.read_text(encoding='utf-8') if rc.exists() else ''
    if re.search(r'^\s*alias review-sync=', current, re.M):
        return rc, 'present'
    sep = '' if not current or current.endswith('\n') else '\n'
    with rc.open('a', encoding='utf-8') as fh:
        fh.write(f'{sep}# agent-autolearn: cliente de sincronizacion\n{alias_line()}\n')
    return rc, 'added'


def cmd_setup(args):
    data = load_config()
    existing = data['profiles'].get(args.profile) or {}
    url = normalize_url(args.url or os.environ.get('AGENT_AUTOLEARN_URL') or existing.get('url') or DEFAULT_URL)
    print(f'Instancia: {url}')
    print('Crea el token en el panel: workspace de destino → Workspace → Tokens → Crear token (Instalacion, ingest + read).')
    token = read_token(args.profile)
    try:
        _, me, _ = Client({'url': url, 'token': token}, args.timeout, backoff=0).request('GET', '/v1/me')
    except HttpError as exc:
        raise SyncError(f'La API rechazo el token ({exc.message()}). No se guardo nada.') from None
    except Transient as exc:
        raise SyncError(f'No se pudo contactar {url} ({exc}). No se guardo nada.') from None
    workspace = (me.get('organization') or {}).get('name') or '?'
    caps = sorted(k for k, v in (me.get('capabilities') or {}).items() if v)
    if 'ingest' not in caps:
        raise SyncError(f"El token de '{workspace}' no tiene permiso ingest. Crea uno de instalacion con ingest + read.")
    is_default = save_profile(args.profile, url, token, args.default)
    print(f"✓ Perfil '{args.profile}' → workspace '{workspace}' (token {token_prefix(token)}){' · por defecto' if is_default else ''}")

    if not args.no_repo:
        root = git(args.repo, 'rev-parse', '--show-toplevel')
        if root:
            write_json(Path(root) / REPO_FILE, {'profile': args.profile})
            print(f"✓ {Path(root).name}: este repo envia sus revisiones a '{workspace}' ({REPO_FILE})")
        else:
            print(f"· No estas en un repo git: en cada repo ejecuta  review-sync use {args.profile}")

    if not args.no_alias:
        rc, alias_state = install_alias()
        if alias_state == 'added':
            if rc.suffix == '.ps1':
                print(f'✓ Funcion review-sync añadida a {rc}. Abre una PowerShell nueva. Si dice que la ejecucion de scripts '
                      'esta deshabilitada: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned')
            else:
                print(f'✓ Alias review-sync añadido a {rc}. Abre una terminal nueva o ejecuta: source {rc}')
        elif alias_state is None:
            manual = (f"function review-sync {{ & '{sys.executable}' '{stable_script_path()}' @args }}"
                      if is_windows() else alias_line())
            print(f'· Shell no reconocida; añadelo a mano: {manual}')


def cmd_profiles(_args):
    data = load_config()
    if not data['profiles']:
        print('Sin perfiles. Crea uno con: review_sync.py configure --profile NOMBRE --url URL')
        return
    for name, p in sorted(data['profiles'].items()):
        mark = ' (por defecto)' if data.get('default') == name else ''
        print(f"{name}{mark}\t{p.get('url')}\t{token_prefix(p.get('token'))}")


def cmd_use(args):
    repo = repo_root(args.repo)
    write_json(repo / REPO_FILE, {'profile': args.name})
    note = '' if profile_config(args.name) else f" Aviso: '{args.name}' no esta configurado en este equipo."
    print(f"{repo / REPO_FILE}: este repo sincroniza con el perfil '{args.name}'.{note}")


def cmd_status(args):
    repo = repo_root(args.repo)
    name, origin = resolve_profile(repo)
    profile = profile_config(name)
    out = {'repo': str(repo), 'profile': name, 'profile_source': origin, 'configured': bool(profile),
           'url': profile['url'] if profile else None, 'token': token_prefix(profile['token']) if profile else None}
    if profile:
        try:
            _, me, _ = Client(profile, args.timeout, backoff=0).request('GET', '/v1/me')
            out['workspace'] = (me.get('organization') or {}).get('name')
            out['actor'] = (me.get('actor') or {}).get('label')
            out['capabilities'] = sorted(k for k, v in (me.get('capabilities') or {}).items() if v)
        except HttpError as exc:
            out['workspace_error'] = exc.message()
        except Transient as exc:
            out['workspace_error'] = f'No se pudo contactar la API: {exc}'
    state = load_state(repo)
    counts = {'pending': 0, 'sent': 0, 'sync_failed': 0}
    items = []
    for path in sorted(queue_dir(repo).glob('*.json')):
        cid = path.stem
        entry = state['runs'].get(cid, {'state': 'pending'})
        counts[entry.get('state', 'pending')] = counts.get(entry.get('state', 'pending'), 0) + 1
        item = read_json(path, {}) or {}
        items.append({'client_run_id': cid, 'state': entry.get('state', 'pending'), 'profile': item.get('profile'),
                      'run_id': entry.get('run_id'), 'error': entry.get('error'),
                      'note': item.get('component_versions_note')})
    out['queue'] = counts
    out['items'] = items
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not name:
        print('Sincronizacion desactivada: no hay perfil activo.')
    else:
        print(f"Perfil: {name} ({origin}){'' if profile else ' — NO configurado en este equipo'}")
        if profile:
            print(f"API: {out['url']}  token {out['token']}")
            if out.get('workspace'):
                print(f"Workspace: {out['workspace']} · como {out.get('actor')} · permisos: {', '.join(out['capabilities'])}")
            else:
                print(f"Workspace: desconocido ({out.get('workspace_error')})")
    print(f"Cola: {counts['pending']} pendientes · {counts['sent']} enviadas · {counts['sync_failed']} con error")
    for it in items:
        if it['error'] or it['note']:
            print(f"  {it['client_run_id'][:8]} [{it['state']}] {it['error'] or ''}{' · ' if it['error'] and it['note'] else ''}{it['note'] or ''}")


def cmd_enqueue(args):
    result = enqueue(args.run_dir)
    if not args.quiet or not result.get('queued'):
        print(json.dumps(result, ensure_ascii=False) if args.quiet else json.dumps(result, indent=2, ensure_ascii=False))


def cmd_push(args):
    repo = repo_root(args.repo)
    name, _ = resolve_profile(repo)
    if not name and not any(queue_dir(repo).glob('*.json')):
        if not args.quiet:
            print('Sincronizacion desactivada: no hay perfil activo.')
        return
    summary = push(repo, args.timeout, args.retry_failed)
    if not args.quiet:
        print(json.dumps(summary, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('configure')
    p.add_argument('--profile', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--default', action='store_true')
    p.set_defaults(fn=cmd_configure)
    p = sub.add_parser('setup')
    p.add_argument('profile', nargs='?', default='personal')
    p.add_argument('--url')
    p.add_argument('--default', action='store_true')
    p.add_argument('--repo', default='.')
    p.add_argument('--no-repo', action='store_true')
    p.add_argument('--no-alias', action='store_true')
    p.add_argument('--timeout', type=float, default=10.0)
    p.set_defaults(fn=cmd_setup)
    sub.add_parser('profiles').set_defaults(fn=cmd_profiles)
    p = sub.add_parser('use')
    p.add_argument('name')
    p.add_argument('--repo', default='.')
    p.set_defaults(fn=cmd_use)
    p = sub.add_parser('status')
    p.add_argument('--repo', default='.')
    p.add_argument('--json', action='store_true')
    p.add_argument('--timeout', type=float, default=5.0)
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser('enqueue')
    p.add_argument('run_dir')
    p.add_argument('--quiet', action='store_true')
    p.set_defaults(fn=cmd_enqueue)
    p = sub.add_parser('push')
    p.add_argument('--repo', default='.')
    p.add_argument('--timeout', type=float, default=10.0)
    p.add_argument('--retry-failed', action='store_true')
    p.add_argument('--quiet', action='store_true')
    p.set_defaults(fn=cmd_push)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except SyncError as exc:
        print(f'review_sync.py: {exc}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f'review_sync.py: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
