#!/usr/bin/env python3
"""Known-bug evaluations for /pre-pr-review: build a fixture repo, then score a finished review.

Usage: eval_review.py list
       eval_review.py setup --case NAME --dest DIR
       eval_review.py score --case NAME --repo DIR [--out FILE]
       eval_review.py compare BEFORE.json AFTER.json
Only the review itself spends tokens, and only when someone runs it on the fixture. Scoring is
deterministic: seeded bugs are matched by file plus symbol/line, never by a model.
Cases and expected.json schema: evals/README.md.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from ledger import SEVERIDADES, ReviewError, atomic_json, read_json

CASES = Path(__file__).resolve().parents[1] / 'evals' / 'cases'
IDENTITY = ['-c', 'user.name=Review Eval', '-c', 'user.email=eval@example.invalid', '-c', 'commit.gpgsign=false']


def load_case(name):
    root = CASES / name
    if not (root / 'expected.json').is_file():
        raise ReviewError(f'Unknown case {name}. Available: {", ".join(list_cases()) or "none"}')
    expected = read_json(root / 'expected.json')
    for spec in [*expected['bugs'], *expected.get('decoys', [])]:
        if not (isinstance(spec.get('id'), str) and isinstance(spec.get('file'), str)
                and (spec.get('symbols') or spec.get('lines'))):
            raise ReviewError(f'{name}: every bug/decoy needs id, file and symbols or lines.')
    return root, expected


def list_cases():
    return sorted(p.parent.name for p in CASES.glob('*/expected.json'))


def setup(name, dest):
    root, expected = load_case(name)
    dest = Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ReviewError('--dest must be new or empty: a fixture never overwrites a real repository.')
    def git(*args):
        result = subprocess.run(['git', '-C', str(dest), *IDENTITY, *args], capture_output=True, text=True)
        if result.returncode:
            raise ReviewError(result.stderr.strip() or 'git failed')
    shutil.copytree(root / 'base', dest, dirs_exist_ok=True)
    (dest / '.gitignore').write_text('.pre-pr-review/\nnode_modules/\n')
    git('init', '--quiet')
    git('symbolic-ref', 'HEAD', 'refs/heads/development')
    git('add', '--all')
    git('commit', '--quiet', '-m', 'Baseline')
    git('checkout', '--quiet', '-b', 'feature/eval')
    shutil.copytree(root / 'change', dest, dirs_exist_ok=True)
    git('add', '--all')
    git('commit', '--quiet', '-m', expected.get('commit', 'Feature under review'))
    return {'case': name, 'repo': str(dest), 'branch': 'feature/eval', 'seeded_bugs': len(expected['bugs']),
            'next': [f'Abrir Claude Code en {dest} y ejecutar: /pre-pr-review feature/eval',
                     f'python3 {Path(__file__).name} score --case {name} --repo {dest}']}


def matches(spec, record):
    if record.get('archivo') != spec['file']:
        return False
    symbols = {s.lower() for s in spec.get('symbols', [])}
    lines = spec.get('lines')
    located = ((record.get('simbolo') or '').strip().lower() in symbols
               or bool(lines) and type(record.get('linea')) is int and lines[0] <= record['linea'] <= lines[1])
    if not located:
        return False
    keywords = [k.lower() for k in spec.get('keywords', [])]
    haystack = ' '.join(str(record.get(k, '')) for k in ('categoria', 'titulo', 'evidence', 'why')).lower()
    return not keywords or any(k in haystack for k in keywords)


def score(name, repo):
    _, expected = load_case(name)
    repo = Path(repo).resolve()
    ledgers = sorted((repo / 'pr-reviews').glob('*/ledger.json'))
    if len(ledgers) != 1:
        raise ReviewError(f'Expected exactly one ledger under {repo}/pr-reviews; run the review first.')
    ledger = read_json(ledgers[0])
    records = [r for r in ledger['hallazgos'] if not r.get('merged_into')]
    reported = [r for r in records if r['estado'] != 'descartado']
    detected, missed, used = [], [], set()
    for bug in expected['bugs']:
        hits = [r for r in reported if matches(bug, r)]
        if not hits:
            missed.append(bug['id'])
            continue
        best = max(hits, key=lambda r: SEVERIDADES.index(r['severidad']))
        used.update(r['huella'] for r in hits)
        floor = bug.get('min_severity', 'LOW')
        detected.append({'id': bug['id'], 'huella': best['huella'], 'severity': best['severidad'],
                         'severity_ok': SEVERIDADES.index(best['severidad']) >= SEVERIDADES.index(floor)})
    decoy_hits = []
    for decoy in expected.get('decoys', []):
        for record in reported:
            if record['huella'] not in used and matches(decoy, record):
                decoy_hits.append({'id': decoy['id'], 'huella': record['huella'], 'title': record.get('titulo')})
                used.add(record['huella'])
    unexpected = [{'huella': r['huella'], 'file': r.get('archivo'), 'symbol': r.get('simbolo'),
                   'severity': r['severidad'], 'title': r.get('titulo')} for r in reported if r['huella'] not in used]
    usage = {}
    shared = sorted(ledgers[0].parent.glob('usage/p*.json'), key=lambda p: p.stat().st_mtime)
    if shared:
        summaries = [read_json(p) for p in shared]
        usage = {'plugin_version': summaries[-1].get('plugin_version'), 'passes_measured': len(summaries),
                 'observed_tokens': sum(s.get('observed_tokens') or 0 for s in summaries),
                 'orchestrator_tokens': sum(s.get('orchestrator_tokens') or 0 for s in summaries),
                 'measurement': sorted({s.get('status') for s in summaries})}
    total = len(expected['bugs'])
    return {'schema_version': 1, 'case': name, 'passes': len(ledger['pasadas']),
            'plugin_version': usage.get('plugin_version') or next(
                (p.get('plugin_version') for p in reversed(ledger['pasadas']) if p.get('plugin_version')), None),
            'seeded_bugs': total, 'detected': detected, 'missed': missed,
            'recall': round(len(detected) / total, 3) if total else None,
            'severity_underrated': [d['id'] for d in detected if not d['severity_ok']],
            'decoy_hits': decoy_hits,
            # Not seeded is not the same as wrong: a person judges these before calling them false positives.
            'unexpected': unexpected,
            'discarded_by_aggregator': sum(r['estado'] == 'descartado' for r in records),
            'usage': usage}


def compare(before, after):
    a, b = read_json(before), read_json(after)
    if a.get('case') != b.get('case'):
        raise ReviewError('Scores belong to different cases.')
    lost = sorted({d['id'] for d in a['detected']} - {d['id'] for d in b['detected']})
    regressions = []
    if lost:
        regressions.append('Bugs sembrados que dejaron de detectarse: ' + ', '.join(lost))
    if len(b['decoy_hits']) > len(a['decoy_hits']):
        regressions.append(f"Mas senuelos reportados: {len(a['decoy_hits'])} -> {len(b['decoy_hits'])}")
    newly_underrated = sorted(set(b['severity_underrated']) - set(a['severity_underrated']))
    if newly_underrated:
        regressions.append('Severidad ahora por debajo del minimo: ' + ', '.join(newly_underrated))
    ta, tb = a['usage'].get('observed_tokens'), b['usage'].get('observed_tokens')
    return {'case': a['case'], 'versions': [a.get('plugin_version'), b.get('plugin_version')],
            'recall': [a['recall'], b['recall']], 'unexpected': [len(a['unexpected']), len(b['unexpected'])],
            'decoy_hits': [len(a['decoy_hits']), len(b['decoy_hits'])],
            'observed_tokens': [ta, tb], 'token_delta': tb - ta if ta and tb else None,
            'quality_regressions': regressions, 'quality_preserved': not regressions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    build = sub.add_parser('setup')
    build.add_argument('--case', required=True)
    build.add_argument('--dest', required=True)
    grade = sub.add_parser('score')
    grade.add_argument('--case', required=True)
    grade.add_argument('--repo', required=True)
    grade.add_argument('--out')
    versus = sub.add_parser('compare')
    versus.add_argument('before')
    versus.add_argument('after')
    args = parser.parse_args()
    try:
        if args.command == 'list':
            result = {'cases': list_cases()}
        elif args.command == 'setup':
            result = setup(args.case, args.dest)
        elif args.command == 'score':
            result = score(args.case, args.repo)
            if args.out:
                atomic_json(args.out, result)
        else:
            result = compare(args.before, args.after)
    except (ReviewError, OSError, KeyError, TypeError) as exc:
        print(f'eval_review.py: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # A lost seeded bug fails the comparison, so a rule change can be gated on it.
    return 1 if args.command == 'compare' and not result['quality_preserved'] else 0


if __name__ == '__main__':
    sys.exit(main())
