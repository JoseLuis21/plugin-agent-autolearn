#!/usr/bin/env python3
"""Prepare a review of a filtered Git tree, without changing the user's index.

Usage: prepare_review.py [--repo DIR] [--base REF] [--full] [--verify-pending]
                        [--run-dir DIR] [--ledger FILE] [--no-resume] [--no-cleanup]
                        [--recheck-rules | --keep-rules-coverage]
See references/review-state.md for artifacts and the finalization protocol.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

from ledger import ReviewError, load_ledger, read_json, atomic_json, digest_file, reviewer_result
from scan_conventions import parse_added_lines, scan
from scan_client_server import CODE_EXT, batch_reader, scan as scan_boundary

SCHEMA_VERSION = 2
MAX_GRAPH_FILES = 8000
EXCLUDED_DIRS = {
    'vendor', 'node_modules', 'dist', 'build', '.next', 'mocks', '__snapshots__',
    '.claude', '.agents', '.cursor', '.windsurf', 'openspec', 'pr-reviews',
    '.pre-pr-review', '__pycache__',
}
EXCLUDED_NAMES = ['*.pb.go', '*_gen.go', '*.generated.*', '*.min.js', '*.min.css', '*.svg', '.DS_Store']
LOCKFILES = {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'composer.lock', 'Gemfile.lock', 'go.sum'}
REVIEWERS = [
    'code-reviewer', 'security-reviewer', 'edge-case-reviewer', 'regression-reviewer',
    'test-reviewer', 'convention-reviewer', 'nextjs-architecture-reviewer', 'go-architecture-reviewer',
]
EXCLUDE_SPECS = ([f':(glob,exclude)**/{d}/**' for d in sorted(EXCLUDED_DIRS)]
                 + [f':(glob,exclude)**/{name}' for name in EXCLUDED_NAMES])
PATCH_SPECS = ['.'] + [f':(glob,exclude)**/{name}' for name in sorted(LOCKFILES)]
DEVELOPMENT_REFS = {'development', 'origin/development',
                    'refs/heads/development', 'refs/remotes/origin/development'}
SNAPSHOT_FORMAT = hashlib.sha256(json.dumps({
    'algorithm': 1, 'directories': sorted(EXCLUDED_DIRS), 'names': sorted(EXCLUDED_NAMES),
}, sort_keys=True).encode()).hexdigest()
RECOVERY_COMMITS = 50
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = 'skills/pre-pr-review/references/'
# What each reviewer judges with. A change means older content was covered under other rules.
COMMON_RULES = [REFERENCES + 'findings-contract.md', REFERENCES + 'findings-rules.md']
REVIEWER_RULES = {
    'code-reviewer': [],
    'security-reviewer': [REFERENCES + 'owasp.md', 'skills/owasp-security/SKILL.md'],
    'edge-case-reviewer': ['skills/resilience/SKILL.md'],
    'regression-reviewer': ['skills/safe-refactor/SKILL.md'],
    'test-reviewer': ['skills/testing/SKILL.md'],
    'convention-reviewer': ['scripts/scan_conventions.py'],
    'nextjs-architecture-reviewer': [REFERENCES + 'nextjs-architecture.md', REFERENCES + 'stacks.md',
                                     'skills/development-nextjs/SKILL.md', 'scripts/scan_client_server.py'],
    'go-architecture-reviewer': [REFERENCES + 'go-architecture.md', REFERENCES + 'stacks.md',
                                 'skills/development-golang/SKILL.md'],
}
# Identity of a preparation: equal values mean the same review, so its artifacts can be resumed.
RESUME_KEYS = ('schema_version', 'repo', 'branch', 'base', 'merge_base', 'head', 'tree', 'base_tree',
               'since_tree', 'mode', 'ledger', 'ledger_digest', 'expected_reviewers', 'assignments',
               'check_owner', 'snapshot_format', 'rules', 'rules_accepted', 'full_window_reviewers')


def git(repo, *args, env=None, data=None, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    if check and result.returncode:
        raise ReviewError(result.stderr.decode(errors='replace').strip() or 'git command failed')
    return result


def output(repo, *args, **kwargs):
    return git(repo, *args, **kwargs).stdout.decode('utf-8', errors='surrogateescape').strip()


def excluded(path):
    import fnmatch
    parts = Path(path).parts
    return any(p in EXCLUDED_DIRS for p in parts) or any(fnmatch.fnmatch(parts[-1], p) for p in EXCLUDED_NAMES)


def snapshot_tree(repo, revision=None):
    """Filtered revision, or the effective worktree (including staged, ignored tracked files)."""
    with tempfile.TemporaryDirectory(prefix='pre-pr-index-') as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / 'index'))
        if revision:
            git(repo, 'read-tree', revision, env=env)
        else:
            if git(repo, 'ls-files', '--unmerged').stdout:
                raise ReviewError('Resolve unmerged index entries before reviewing.')
            # Import the real index as data. No checkout, stash, commit, or real-index write.
            entries = git(repo, 'ls-files', '--stage', '-z').stdout
            git(repo, 'read-tree', '--empty', env=env)
            git(repo, 'update-index', '-z', '--index-info', env=env, data=entries)
            git(repo, 'add', '--all', '--', '.', *EXCLUDE_SPECS, env=env)
        names = git(repo, 'ls-files', '-z', env=env).stdout.split(b'\0')
        remove = [p for p in names if p and excluded(os.fsdecode(p))]
        if remove:
            git(repo, 'update-index', '--force-remove', '-z', '--stdin', env=env,
                data=b'\0'.join(remove) + b'\0')
        return output(repo, 'write-tree', env=env)


def changed_files(repo, before, after):
    # No rename collapsing: both the old and new paths need scope/owner handling.
    raw = git(repo, 'diff', '--no-renames', '--name-only', '-z', before, after).stdout
    return [os.fsdecode(p) for p in raw.split(b'\0') if p]


def recover_snapshot(repo, coverage, head, branch):
    """Rebuild only from reachable commits; never substitute an approximate baseline."""
    expected = coverage.get('tree', '')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected):
        return None
    anchor = coverage['head']
    # Dirty reviewed content may have been committed later. Bound the search, keeping the
    # recorded anchor even on a long branch. No network or model calls are required.
    descendants = output(repo, 'rev-list', f'--max-count={RECOVERY_COMMITS}',
                         '--ancestry-path', f'{anchor}..{head}').splitlines()
    for commit in dict.fromkeys([anchor, *descendants]):
        if snapshot_tree(repo, commit) == expected:
            ref = f'refs/pre-pr-review/{hashlib.sha256(branch.encode()).hexdigest()[:16]}/{expected}'
            git(repo, 'update-ref', ref, expected)
            return commit
    return None


def plugin_version():
    try:
        return str(read_json(PLUGIN_ROOT / '.claude-plugin' / 'plugin.json')['version'])
    except (ReviewError, KeyError, TypeError):
        return None


def rules_digests():
    """{reviewer: {rule file: digest}}; a missing file is recorded, never guessed."""
    def digest(relative):
        return (digest_file(PLUGIN_ROOT / relative) or 'missing')[:16]
    return {name: {path: digest(path) for path in [f'agents/{name}.md', *COMMON_RULES, *extra]}
            for name, extra in REVIEWER_RULES.items()}


def rules_changed(covered, current):
    """Reviewers whose previous coverage was produced with different rule files."""
    if not isinstance(covered, dict):
        return {}
    changed = {}
    for name, digests in current.items():
        before = covered.get(name)
        if isinstance(before, dict) and before != digests:
            changed[name] = sorted(p for p in set(before) | set(digests) if before.get(p) != digests.get(p))
    return changed


def resumable_run(repo, run, ledger):
    """Unfinished preparation of exactly this review with the most reusable results (newest on a tie)."""
    from retention import run_entries
    previous = {r['huella']: r for r in ledger['hallazgos']}
    best = None
    for entry in run_entries(repo / '.pre-pr-review'):
        old = entry['run']
        if old.get('status') != 'prepared' or any(old.get(k) != run.get(k) for k in RESUME_KEYS):
            continue
        done, missing = [], []
        for reviewer in old['expected_reviewers']:
            try:
                required = old['assignments'][reviewer]
                if not all(h in previous for h in required):
                    raise ReviewError('stale assignment')
                reviewer_result(entry['path'], reviewer, required)
                done.append(reviewer)
            except (ReviewError, OSError, KeyError, TypeError):
                missing.append(reviewer)
        if best is None or len(done) > len(best[2]):
            best = (entry['path'], old, done, missing)
    return best


def owner(record):
    candidates = [record.get('reviewer')] + record.get('detectado_por', [])
    return next((r for r in candidates if r in REVIEWERS), 'code-reviewer')


def boundary_scan(repo, tree, names, files):
    """Grafo de imports del snapshot: modulos de servidor alcanzables desde "use client".

    El build de Next falla por esto, asi que se comprueba con el arbol entero: la cadena
    que rompe suele cruzar archivos que el diff no toca.
    """
    sources = [p for p in names if p.endswith(CODE_EXT)]
    if len(sources) > MAX_GRAPH_FILES:
        return {'candidates': [], 'skipped': f'{len(sources)} archivos JS/TS: grafo no resuelto.',
                'client_roots': 0, 'modules_scanned': len(sources), 'client_graph': 0,
                'truncated': True, 'omitted': 0}
    return scan_boundary(batch_reader(repo, tree, names), names, set(files))


def route(repo, tree, files, patch, pending, full):
    selected, reasons = set(), {}
    boundary = {'candidates': [], 'client_roots': 0, 'modules_scanned': 0,
                'client_graph': 0, 'truncated': False, 'omitted': 0,
                'skipped': 'No aplica: el delta no trae codigo JS/TS de un proyecto Next.js.'}
    if files:
        selected.add('security-reviewer')
        reasons['security-reviewer'] = 'Toda superficie nueva, incluidos docs, tests, config y dependencias.'
    code = [p for p in files if Path(p).suffix.lower() not in {'.md', '.mdx', '.txt', '.rst', '.png', '.jpg', '.jpeg', '.gif'}
            and Path(p).name not in LOCKFILES]
    if code:
        selected.add('code-reviewer')
        reasons['code-reviewer'] = 'Correctitud y checklist de limites, contratos y tests del delta.'
        risk = re.search(r'\b(async|await|goroutine|mutex|transaction|timeout|retry|migration|schema|export)\b', patch, re.I)
        if full or len(code) > 15 or len(patch.splitlines()) > 800 or risk:
            selected.update(['edge-case-reviewer', 'regression-reviewer', 'test-reviewer'])
            for name in ('edge-case-reviewer', 'regression-reviewer', 'test-reviewer'):
                reasons[name] = 'Validacion completa o delta grande/con señales de riesgo.'
        elif any(re.search(r'(^|/)(tests?|__tests__)(/|\.)|(_test\.|\.(test|spec)\.)', p) for p in code):
            selected.add('test-reviewer')
            reasons['test-reviewer'] = 'Cambios en tests.'
    candidates = scan(parse_added_lines(patch))
    if candidates:
        selected.add('convention-reviewer')
        reasons['convention-reviewer'] = 'Candidatos del escaner compartido.'
    for record in pending:
        name = owner(record)
        selected.add(name)
        reasons[name] = reasons.get(name, '') + ' Revalidaciones pendientes asignadas.'
    names = output(repo, 'ls-tree', '-r', '--name-only', tree).splitlines()
    js = any(Path(p).suffix in {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'} for p in code)
    if js:
        nextjs = any(Path(p).name.startswith('next.config.') for p in names)
        if not nextjs:
            for p in names:
                if Path(p).name == 'package.json':
                    try:
                        package = json.loads(output(repo, 'show', f'{tree}:{p}'))
                        nextjs = any('next' in package.get(k, {}) for k in ('dependencies', 'devDependencies'))
                    except (ValueError, AttributeError):
                        continue
                    if nextjs:
                        break
        if nextjs:
            selected.add('nextjs-architecture-reviewer')
            reasons['nextjs-architecture-reviewer'] = 'Codigo JS/TS del delta en stack Next.js.'
            boundary = boundary_scan(repo, tree, names, files)
            if any(c['en_delta'] for c in boundary['candidates']):
                reasons['nextjs-architecture-reviewer'] += (
                    ' El grafo de imports alcanza modulos de servidor desde un Client Component.')
    if any(p.endswith('.go') for p in code) and any(Path(p).name == 'go.mod' for p in names):
        if any(re.search(r'(^|/)internal/core/(?:[^/]+/)*(ports|adapters|domain|services)/', p) for p in names):
            selected.add('go-architecture-reviewer')
            reasons['go-architecture-reviewer'] = 'Codigo Go del delta en estructura hexagonal.'
    skipped = {r: ('Sin candidatos ni pendientes.' if r == 'convention-reviewer' else
                   'Sin superficie del stack en el delta ni pendientes.' if 'architecture' in r else
                   'Checklist asignado a code-reviewer; sin señal de escalacion.' if r in {
                       'edge-case-reviewer', 'regression-reviewer', 'test-reviewer'} and code else
                   'Sin superficie relevante ni pendientes.') for r in REVIEWERS if r not in selected}
    context = {'manifest_files': [p for p in names if Path(p).name in {'package.json', 'go.mod', 'composer.json'} or Path(p).name.startswith('next.config.')],
               'policy_files': [os.fsdecode(p) for p in git(repo, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').stdout.split(b'\0')
                                if p and Path(os.fsdecode(p)).name in {'AGENTS.md', 'CLAUDE.md', 'rules.txt', 'SKILL.md'}
                                and not any(part in {'node_modules', 'vendor', 'pr-reviews', '.pre-pr-review'} for part in Path(os.fsdecode(p)).parts)],
               'test_samples': [p for p in names if re.search(r'(_test\.|\.(test|spec)\.)', p)][:8]}
    return sorted(selected), reasons, skipped, candidates, context, boundary


def development_base(repo, requested=None):
    """Review against development only, independently of the remote's default/PR target."""
    if requested is not None and requested not in DEVELOPMENT_REFS:
        raise ReviewError('Review base must be development; main, prod and other bases are not allowed.')
    for ref in ('refs/remotes/origin/development', 'refs/heads/development'):
        if git(repo, 'rev-parse', '--verify', ref + '^{commit}', check=False).returncode == 0:
            return ref
    raise ReviewError('development was not found. Fetch origin development or create the local '
                      'development branch before reviewing; no other base will be used.')


def prepare(args):
    repo = Path(output(args.repo, 'rev-parse', '--show-toplevel'))
    head = output(repo, 'rev-parse', 'HEAD')
    branch = output(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False) or f'detached-{head[:12]}'
    base = development_base(repo, args.base)
    merge_base = output(repo, 'merge-base', base, head)
    slug = 'pr-' + re.sub(r'[^A-Za-z0-9._-]', '-', branch)
    branch_key = hashlib.sha256(branch.encode()).hexdigest()[:12]
    ledger_path = Path(args.ledger).resolve() if args.ledger else repo / 'pr-reviews' / f'{slug}-{branch_key}' / 'ledger.json'
    legacy_path = repo / 'pr-reviews' / slug / 'ledger.json'
    if not args.ledger and not ledger_path.exists() and legacy_path.exists():
        legacy = load_ledger(legacy_path)
        if legacy.get('rama') == branch:
            ledger_path = legacy_path
    ledger = load_ledger(ledger_path)
    digest = digest_file(ledger_path)
    pass_n = max((p['n'] for p in ledger['pasadas']), default=0) + 1
    tree = snapshot_tree(repo)
    base_tree = snapshot_tree(repo, merge_base)
    coverage = ledger.get('coverage', {})
    mode, reason, since = 'completo', 'Primera pasada o ledger legado.', base_tree
    recovered_from = None
    if args.full:
        reason = '--full solicitado.'
    elif coverage and ledger.get('schema_version') == SCHEMA_VERSION:
        if coverage.get('base') not in DEVELOPMENT_REFS or coverage.get('merge_base') != merge_base:
            reason = 'Cambio de base/merge-base: revalidacion completa.'
        elif coverage.get('snapshot_format', SNAPSHOT_FORMAT) != SNAPSHOT_FORMAT:
            reason = 'Cambio de filtros/formato del snapshot: revalidacion completa.'
        elif git(repo, 'merge-base', '--is-ancestor', coverage.get('head', ''), head, check=False).returncode:
            reason = 'Historia reescrita o head previo ausente: revalidacion completa.'
        elif git(repo, 'cat-file', '-e', coverage.get('tree', '') + '^{tree}', check=False).returncode:
            recovered_from = recover_snapshot(repo, coverage, head, branch)
            if recovered_from:
                mode, reason, since = ('incremental',
                    f'Snapshot reconstruido y verificado desde commit {recovered_from}.', coverage['tree'])
            else:
                reason = 'Snapshot previo ausente y sin commit coincidente en la busqueda acotada: revalidacion completa.'
        else:
            mode, reason, since = 'incremental', 'Delta desde la ultima cobertura completa.', coverage['tree']
    files = changed_files(repo, since, tree)
    active = [r for r in ledger['hallazgos'] if r.get('estado') in {'abierto', 'aceptado'}]
    # Impact may cross files; a full fallback also needs explicit revalidation
    # even when its net patch is empty and the old snapshot cannot be compared.
    def diff(before):
        return git(repo, 'diff', '--no-ext-diff', '--no-textconv', before, tree, '--', *PATCH_SPECS).stdout.decode('utf-8', errors='replace')
    patch = diff(since)
    full_patch = diff(base_tree) if since != base_tree else None
    rules = rules_digests()
    changed = rules_changed(coverage.get('rules'), rules) if mode == 'incremental' else {}
    accepted_rules, full_window = [], []
    if changed:
        # Rules only matter where the branch has surface for them; the rest needs no decision.
        branch_files = changed_files(repo, base_tree, tree)
        surface, _, _, full_candidates, _, full_boundary = route(
            repo, tree, branch_files, patch if full_patch is None else full_patch, [], True)
        accepted_rules = sorted(set(changed) - set(surface))
        changed = {name: paths for name, paths in changed.items() if name in surface}
        if args.keep_rules_coverage:
            accepted_rules, changed = sorted([*accepted_rules, *changed]), {}
        elif args.recheck_rules:
            full_window, changed = sorted(changed), {}
    pending = active if files or mode == 'completo' or args.verify_pending or full_window else []
    unchanged = not files and not args.full and not pending and not full_window
    selected, reasons, skipped, candidates, context, boundary = route(repo, tree, files, patch, pending, mode == 'completo')
    for name in full_window:
        reasons[name] = (reasons.get(name, '') + ' Sus reglas cambiaron: revisa full.patch como ventana.').strip()
        skipped.pop(name, None)
    selected = sorted({*selected, *full_window})
    if 'convention-reviewer' in full_window:
        candidates = full_candidates
    if 'nextjs-architecture-reviewer' in full_window:
        boundary = full_boundary
    if unchanged:
        selected, reasons = [], {}
        skipped = {r: 'Snapshot sin cambios; no se solicita revalidacion.' for r in REVIEWERS}
    assignments = {r: [h['huella'] for h in pending if owner(h) == r] for r in selected}
    check_owner = next((r for r in ('test-reviewer', 'code-reviewer', 'security-reviewer')
                        if r in selected), None)
    locks = [p for p in files if Path(p).name in LOCKFILES]
    # Dismissed before in the code under review: context for reviewers, never a filter.
    window = set(changed_files(repo, base_tree, tree)) if mode == 'completo' or full_window else set(files)
    dismissed = [] if unchanged else [
        {'huella': r['huella'], 'category': r.get('categoria'), 'file': r.get('archivo'), 'symbol': r.get('simbolo'),
         'title': r.get('titulo'), 'discard_reason': r.get('discard_reason'), 'discard_pass': r.get('discard_pass'),
         'file_changed_since_discard': (None if not r.get('discard_blob') else
             output(repo, 'rev-parse', '--verify', '--quiet', f"{tree}:{r.get('archivo')}", check=False) != r['discard_blob'])}
        for r in ledger['hallazgos']
        if r.get('estado') == 'descartado' and not r.get('merged_into') and r.get('archivo') in window]
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S-%f')
    identity = dict(schema_version=SCHEMA_VERSION, repo=str(repo), branch=branch, base=base,
                    merge_base=merge_base, head=head, tree=tree, base_tree=base_tree, since_tree=since,
                    mode=mode, ledger=str(ledger_path), ledger_digest=digest, expected_reviewers=selected,
                    assignments=assignments, check_owner=check_owner, snapshot_format=SNAPSHOT_FORMAT,
                    rules=rules, rules_accepted=accepted_rules, full_window_reviewers=full_window)
    if not args.run_dir and not args.no_resume and not unchanged:
        found = resumable_run(repo, identity, ledger)
        if found:
            # Same code, ledger, rules and assignments: valid results are kept, nothing is re-prepared.
            old_dir, old, done, missing = found
            checks = read_json(old_dir / 'shared-checks.json', {'status': 'invalid'})
            return {'run_dir': str(old_dir), **old, 'active_findings': len(active), 'resumed': True,
                    'completed_reviewers': done, 'pending_reviewers': missing,
                    'shared_checks_status': checks.get('status') if isinstance(checks, dict) else 'invalid'}
    run_dir = Path(args.run_dir).resolve() if args.run_dir else repo / '.pre-pr-review' / f'{stamp}-p{pass_n}-{uuid.uuid4().hex[:8]}'
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ReviewError('RUN_DIR must be new/empty; previous artifacts are retained for resume.')
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'pending').mkdir()
    for r in selected:
        atomic_json(run_dir / 'pending' / f'{r}.json', [h for h in pending if owner(h) == r])
    (run_dir / 'new.patch').write_text(patch)
    if full_patch is not None:
        (run_dir / 'full.patch').write_text(full_patch)
    if dismissed:
        atomic_json(run_dir / 'discarded.json', {
            'instruction': 'Descartes previos en archivos de esta ventana. No son un filtro: si el motivo ya no se '
                           'sostiene, reporta el hallazgo con new_evidence (que cambio o que prueba lo refuta).',
            'discarded': dismissed})
    atomic_json(run_dir / '_conventions-raw.json', {'candidates': candidates, 'added_lines_scanned': len(parse_added_lines(patch))})
    boundary['instruction'] = (
        'Frontera cliente/servidor resuelta por grafo de imports: candidatos, no hallazgos. '
        'nextjs-architecture confirma cada cadena leyendo los archivos reales antes de reportar.')
    atomic_json(run_dir / '_client-server-raw.json', boundary)
    atomic_json(run_dir / 'searches.json', {'searches': []})
    atomic_json(run_dir / 'shared-checks.json', {
        'schema_version': 1, 'tree': tree, 'head': head, 'owner': check_owner,
        'status': 'pending' if check_owner else 'not_applicable', 'checks': [],
    })
    (run_dir / 'searches').mkdir()
    atomic_json(run_dir / 'repo-context.json', context)
    atomic_json(run_dir / 'dependencies.json', {'files': locks, 'before_tree': since, 'after_tree': tree,
                'instruction': 'Seguridad: leer diff de cada lockfile a demanda con git diff BEFORE AFTER -- PATH. Cambios de resolucion tambien son superficie.'})
    report = ledger_path.parent / f'{stamp}-p{pass_n}.md'
    run = dict(identity, reason=reason, pass_n=pass_n, status='unchanged' if unchanged else 'prepared',
               report=str(report), delta_files=files, reviewer_reasons=reasons, skipped_reviewers=skipped,
               dependency_files=locks, recovered_from_commit=recovered_from, plugin_version=plugin_version(),
               rules_changed=changed, discarded_context=len(dismissed),
               snapshot_ref=f'refs/pre-pr-review/{hashlib.sha256(branch.encode()).hexdigest()[:16]}/{tree}')
    atomic_json(run_dir / 'run.json', run)
    brief = [f'# Pre-PR: pasada {pass_n} ({mode})', f'Rama: {branch}; base: {base}; HEAD: {head}',
             f'Snapshot: {since} → {tree}', reason, f'Archivos del delta: {len(files)}; lineas de patch: {len(patch.splitlines())}.',
             'run.json contiene rutas, asignaciones y cobertura. new.patch se lee una vez; full.patch es contexto a demanda.',
             'pending/<reviewer>.json: revalidaciones obligatorias por huella. dependencies.json: lockfiles para seguridad.',
             'searches.json: indice compartido de consultas/resultados. Reutilizar antes de buscar.',
             'Sin barridos generales: completar ubicaciones del delta y seguir callers afectados cuando haya evidencia.',
             'No editar codigo. Si el archivo cambio despues del snapshot, parar y preparar otra corrida.']
    if dismissed:
        brief.append(f'discarded.json: {len(dismissed)} descartes previos en esta ventana; reabrir exige new_evidence.')
    if full_window:
        brief.append('Reglas cambiadas: ' + ', '.join(full_window) + ' usan full.patch como ventana en esta pasada.')
    (run_dir / 'brief.md').write_text('\n'.join(brief) + '\n')
    result = {'run_dir': str(run_dir), **run, 'active_findings': len(active), 'resumed': False}
    if not args.no_cleanup:
        from retention import cleanup, ledger_trees
        try:
            # After the run exists, so its own trees are protected like every retained run.
            result['cleanup'] = cleanup(repo, branch=branch, protect={tree, since, base_tree} | ledger_trees(ledger))
        except (ReviewError, OSError) as exc:
            result['cleanup'] = {'enabled': True, 'error': str(exc), 'runs_removed': 0, 'refs_removed': 0}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--base', help='Compatibility option: development only (local/origin aliases accepted).')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--verify-pending', action='store_true')
    parser.add_argument('--run-dir')
    parser.add_argument('--ledger')
    parser.add_argument('--no-resume', action='store_true', help='Always prepare a new run directory.')
    parser.add_argument('--no-cleanup', action='store_true', help='Skip the retention policy for this preparation.')
    rules = parser.add_mutually_exclusive_group()
    rules.add_argument('--recheck-rules', action='store_true',
                       help='Reviewers whose rules changed review the whole branch again (user decision).')
    rules.add_argument('--keep-rules-coverage', action='store_true',
                       help='Keep coverage produced with previous rules (user decision).')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args), indent=2, ensure_ascii=True))
    except (ReviewError, OSError, ValueError, TypeError) as exc:
        print(f'prepare_review.py: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
