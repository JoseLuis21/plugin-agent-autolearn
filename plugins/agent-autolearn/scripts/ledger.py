#!/usr/bin/env python3
"""Validate curated results, then atomically finalize review history and coverage.

Usage: ledger.py --run-dir DIR --curated DIR/curated.json [--accepted FILE]
Only expected reviewer files from run.json are read. Invalid/incomplete input leaves
ledger and coverage intact and writes an incomplete clasificacion.json in RUN_DIR.
Schema and examples: skills/pre-pr-review/references/review-state.md.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

SEVERIDADES = ['NIT', 'LOW', 'MEDIUM', 'HIGH', 'BLOCKER']
STATUS = {'open': 'abierto', 'closed': 'cerrado', 'discarded': 'descartado'}
STALE_LOCK_SECONDS = 900


class ReviewError(ValueError):
    pass


def huella(categoria, archivo, simbolo):
    raw = f"{(categoria or '').strip().lower()}|{(archivo or '').strip()}|{(simbolo or '').strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def sitio(archivo, simbolo):
    return hashlib.sha1(f"{(archivo or '').strip()}|{(simbolo or '').strip().lower()}".encode()).hexdigest()[:10]


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise ReviewError(f'Missing JSON: {path}')
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ReviewError(f'Invalid JSON in {path}: {exc}') from exc


def digest_file(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as fh:
            name = fh.name
            json.dump(data, fh, indent=2, ensure_ascii=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def load_ledger(path):
    value = read_json(path, {'hallazgos': [], 'pasadas': []})
    require(isinstance(value, dict), 'Ledger must be an object.')
    for name in ('hallazgos', 'pasadas'):
        require(isinstance(value.get(name), list), f'Ledger requires {name} array.')
    seen = set()
    for record in value['hallazgos']:
        require(isinstance(record, dict) and isinstance(record.get('huella'), str) and record['huella'],
                'Invalid historical finding.')
        require(record['huella'] not in seen, 'Duplicate historical fingerprint.')
        seen.add(record['huella'])
        require(record.get('estado') in {'abierto', 'cerrado', 'aceptado', 'descartado'}, 'Invalid historical status.')
        require(record.get('severidad') in SEVERIDADES, 'Invalid historical severity.')
    for entry in value['pasadas']:
        require(isinstance(entry, dict) and type(entry.get('n')) is int and entry['n'] > 0, 'Invalid pass number in ledger.')
    aliases = value.get('aliases', {})
    require(isinstance(aliases, dict) and all(isinstance(k, str) and isinstance(v, str) and v in seen
                                           for k, v in aliases.items()), 'Invalid historical alias mapping.')
    return value


def validate_finding(finding, label):
    require(isinstance(finding, dict), f'{label}: finding must be an object.')
    for name in ('category', 'file', 'symbol', 'title', 'evidence', 'why', 'fix'):
        require(isinstance(finding.get(name), str) and finding[name].strip(), f'{label}: missing {name}.')
    require(finding.get('severity') in SEVERIDADES, f'{label}: invalid severity.')
    require(type(finding.get('line')) is int and finding['line'] >= 0, f'{label}: invalid line.')
    require(finding.get('confidence') in {'alta', 'media', 'baja'}, f'{label}: invalid confidence.')
    external = finding.get('decision_externa', False)
    require(external is False or isinstance(external, str) and bool(external.strip()), f'{label}: invalid decision_externa.')
    require(not Path(finding['file']).is_absolute() and '..' not in Path(finding['file']).parts, f'{label}: invalid file path.')
    return huella(finding['category'], finding['file'], finding['symbol'])


def validate_verification(value, reviewer, allowed):
    require(isinstance(value, dict), 'Invalid verification object.')
    fingerprint = value.get('fingerprint')
    require(isinstance(fingerprint, str) and fingerprint in allowed, 'Verification references an unknown/unassigned fingerprint.')
    require(value.get('reviewer') == reviewer, 'Verification reviewer identity does not match assignment.')
    require(value.get('status') in STATUS, 'Invalid verification status.')
    require(isinstance(value.get('evidence'), str) and value['evidence'].strip(), 'Verification requires concrete evidence.')
    return fingerprint


def reviewer_result(run_dir, reviewer, required):
    """One expected reviewer file: (finding fingerprints, verifications by fingerprint)."""
    data = read_json(Path(run_dir) / f'{reviewer}.json')
    require(isinstance(data, dict) and data.get('reviewer') == reviewer, f'Missing/duplicate/wrong reviewer identity: {reviewer}.')
    require(isinstance(data.get('scope'), str) and isinstance(data.get('sin_hallazgos_en'), list), f'{reviewer}: invalid coverage schema.')
    require(isinstance(data.get('findings'), list) and isinstance(data.get('verifications'), list), f'{reviewer}: missing findings/verifications arrays.')
    findings, verifications = set(), {}
    for finding in data['findings']:
        fingerprint = validate_finding(finding, reviewer)
        require(fingerprint not in findings, f'{reviewer}: duplicate finding fingerprint.')
        findings.add(fingerprint)
    for verification in data['verifications']:
        fingerprint = validate_verification(verification, reviewer, set(required))
        require(fingerprint not in verifications, f'{reviewer}: duplicate verification.')
        verifications[fingerprint] = verification
    require(set(verifications) == set(required), f'{reviewer}: missing assigned revalidations.')
    return findings, verifications


def validate_inputs(run, ledger, curated, run_dir):
    from prepare_review import REVIEWERS
    require(run.get('schema_version') == 2 and run.get('status') == 'prepared', 'Run is not ready for finalization.')
    expected = run.get('expected_reviewers')
    require(isinstance(expected, list) and len(expected) == len(set(expected)) and all(r in REVIEWERS for r in expected),
            'Invalid expected reviewer identities.')
    assignments = run.get('assignments')
    require(isinstance(assignments, dict) and set(assignments) == set(expected), 'Invalid assignments.')
    require(isinstance(curated, dict) and curated.get('schema_version') == 2, 'Invalid curation schema.')
    require(curated.get('validated') is True, 'Aggregator must validate curation before finalization.')
    require(isinstance(curated.get('findings'), list) and isinstance(curated.get('verifications'), list), 'Curation requires findings and verifications.')
    previous = {r['huella']: r for r in ledger['hallazgos']}
    raw, verification_index, assigned = {}, {}, set()
    for reviewer in expected:
        required = assignments[reviewer]
        require(isinstance(required, list) and all(isinstance(h, str) and h in previous for h in required), 'Invalid assigned fingerprints.')
        require(len(required) == len(set(required)) and not assigned.intersection(required), 'Duplicate pending assignment.')
        assigned.update(required)
        findings, verifications = reviewer_result(run_dir, reviewer, required)
        for fingerprint in findings:
            raw.setdefault(fingerprint, set()).add(reviewer)
        verification_index.update(verifications)
    # Only scheduled files are inputs; stale/extra JSON files cannot impersonate coverage.
    final_verifications = {}
    for verification in curated['verifications']:
        require(isinstance(verification, dict), 'Invalid curated verification.')
        fingerprint = verification.get('fingerprint')
        require(isinstance(fingerprint, str) and fingerprint not in final_verifications, 'Duplicate curated verification.')
        require(fingerprint in verification_index and verification == verification_index[fingerprint],
                'Curated verification must match its assigned reviewer evidence/status.')
        final_verifications[fingerprint] = verification
    require(set(final_verifications) == assigned, 'Curation omits assigned verifications.')
    decisions, used_sources, used_aliases = [], set(), {}
    known = set(raw) | set(previous) | set(ledger.get('aliases', {}))
    canonical_seen = set()
    for finding in curated['findings']:
        natural = validate_finding(finding, 'curation')
        fingerprint = canonical(natural, ledger.get('aliases', {}))
        require(fingerprint not in canonical_seen, 'Curation has duplicate canonical findings.')
        canonical_seen.add(fingerprint)
        status = finding.get('status')
        require(status in {'open', 'discarded'}, 'Curated finding status must be open or discarded; closure requires verification.')
        sources = finding.get('sources')
        require(isinstance(sources, list) and sources and all(isinstance(h, str) and h in raw for h in sources), 'Curation requires real source fingerprints.')
        require(not used_sources.intersection(sources) and len(sources) == len(set(sources)), 'A raw finding has multiple curated decisions.')
        used_sources.update(sources)
        reviewers = finding.get('detectado_por')
        source_reviewers = set().union(*(raw[h] for h in sources))
        require(isinstance(reviewers, list) and set(reviewers) == source_reviewers, 'Curation must preserve source reviewer identities.')
        require(finding.get('reviewer') in source_reviewers, 'Invalid primary reviewer in curation.')
        aliases = finding.get('aliases', [])
        require(isinstance(aliases, list) and all(isinstance(a, str) and a in known for a in aliases), 'Unknown root-cause alias.')
        aliases = aliases + sources
        aliases = sorted((set(aliases) | {canonical(a, ledger.get('aliases', {})) for a in aliases}) - {fingerprint})
        for alias in aliases:
            require(alias not in used_aliases or used_aliases[alias] == fingerprint, 'Alias points to multiple causes.')
            used_aliases[alias] = fingerprint
        for h in {fingerprint, *aliases}:
            if h in final_verifications:
                require(final_verifications[h]['status'] == status, 'Finding status conflicts with explicit verification.')
        if status == 'discarded':
            require(isinstance(finding.get('discard_reason'), str) and finding['discard_reason'].strip(), 'Discard requires a reason.')
        else:
            dismissed = [previous[h] for h in {fingerprint, *aliases}
                         if h in previous and previous[h]['estado'] == 'descartado' and not previous[h].get('merged_into')]
            if dismissed:
                # Never suppressed by script: the curator either repeats the discard or states what changed.
                require(isinstance(finding.get('new_evidence'), str) and finding['new_evidence'].strip(),
                        f"Finding {dismissed[0]['huella']} was discarded before "
                        f"({dismissed[0].get('discard_reason', 'sin motivo registrado')}); reopening requires new_evidence, "
                        'otherwise keep status discarded with discard_reason.')
        attribution = finding.get('introducido_por')
        if attribution:
            require(isinstance(attribution, dict) and attribution.get('fingerprint') in previous,
                    'Causal attribution must reference a previous finding.')
            require(all(isinstance(attribution.get(k), str) and attribution[k].strip()
                        for k in ('before', 'after', 'causal_evidence')), 'Causal attribution needs before/after and causal evidence.')
        decisions.append((fingerprint, aliases, finding))
    require(used_sources == set(raw), 'Every raw finding needs a curated decision, including discards.')
    require(not (set(used_aliases) & canonical_seen), 'A canonical finding cannot also alias another decision.')
    return decisions, final_verifications


def canonical(fingerprint, aliases):
    seen = set()
    while fingerprint in aliases:
        require(fingerprint not in seen, 'Cyclic root-cause aliases.')
        seen.add(fingerprint)
        fingerprint = aliases[fingerprint]
    return fingerprint


def accepted_map(path):
    data = read_json(path, {'aceptados': []}) if path else {'aceptados': []}
    require(isinstance(data, dict) and isinstance(data.get('aceptados'), list), 'Invalid accepted.json schema.')
    result = {}
    for entry in data['aceptados']:
        require(isinstance(entry, dict), 'Invalid acceptance.')
        fingerprint = entry.get('huella')
        if not fingerprint:
            require(all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('categoria', 'archivo', 'simbolo')), 'Acceptance needs fingerprint or category/file/symbol.')
            fingerprint = huella(entry['categoria'], entry['archivo'], entry['simbolo'])
        require(isinstance(fingerprint, str) and fingerprint not in result, 'Duplicate/invalid acceptance.')
        require(entry.get('techo_severidad', 'MEDIUM') in SEVERIDADES, 'Invalid acceptance ceiling.')
        result[fingerprint] = entry
    return result


def apply_results(run, old, decisions, verifications, accepted, blobs=None):
    ledger = copy.deepcopy(old)
    records = {h['huella']: h for h in ledger['hallazgos']}
    aliases = dict(ledger.get('aliases', {}))
    pass_n = run['pass_n']
    touched, closed = set(), []
    for fingerprint, verification in verifications.items():
        record = records[fingerprint]
        record.setdefault('history', []).append({'pass': pass_n, 'verification': copy.deepcopy(verification),
                                                  'previous_status': record['estado']})
        record['estado'] = STATUS[verification['status']]
        record['last_verification'] = copy.deepcopy(verification)
        if verification['status'] == 'closed':
            record['cerrado_en'] = pass_n
            closed.append(fingerprint)
        touched.add(fingerprint)
    labels = {name: 0 for name in ('NUEVO', 'REINCIDENTE', 'REGRESION', 'INTRODUCIDO_POR')}
    for fingerprint, merged, finding in decisions:
        prior = next((h for h in old['hallazgos'] if h['huella'] == fingerprint), {})
        record = records.setdefault(fingerprint, {'huella': fingerprint, 'history': []})
        record.setdefault('history', []).append({'pass': pass_n, 'decision': copy.deepcopy(finding)})
        # Keep full final evidence, fix, severity, external flag, source reviewers and optional fields.
        record.update({k: copy.deepcopy(v) for k, v in finding.items()
                       if k not in {'history', 'visto_en', 'merged_into', 'last_verification', 'cerrado_en'}})
        record.update(huella=fingerprint, sitio=sitio(finding['file'], finding['symbol']),
                      titulo=finding['title'], categoria=finding['category'], archivo=finding['file'],
                      simbolo=finding['symbol'], linea=finding['line'], severidad=finding['severity'],
                      estado=STATUS[finding['status']], decision_externa=finding.get('decision_externa', False),
                      visto_en=sorted(set(prior.get('visto_en', []) + [pass_n])))
        record['aliases'] = sorted(set(record.get('aliases', []) + merged))
        if finding['status'] == 'discarded':
            # Lets a later pass tell reviewers whether the dismissed code is still the same.
            record['discard_pass'] = pass_n
            record['discard_blob'] = (blobs or {}).get(finding['file'])
        elif prior.get('estado') == 'descartado' and not prior.get('merged_into'):
            record['reabierto_tras_descarte'] = sorted(set(record.get('reabierto_tras_descarte', []) + [pass_n]))
        for alias in merged:
            aliases[alias] = fingerprint
            if alias in records:
                records[alias]['merged_into'] = fingerprint
                records[alias]['estado'] = 'descartado'
                records[alias].setdefault('history', []).append({'pass': pass_n, 'merged_into': fingerprint,
                                                                'evidence': finding['evidence']})
        if finding['status'] == 'open':
            closure = prior.get('last_verification', {})
            verified_closed = (prior.get('estado') == 'cerrado' and isinstance(closure, dict)
                               and closure.get('status') == 'closed'
                               and closure.get('fingerprint') == fingerprint
                               and isinstance(closure.get('reviewer'), str) and bool(closure['reviewer'].strip())
                               and isinstance(closure.get('evidence'), str) and bool(closure['evidence'].strip()))
            label = ('INTRODUCIDO_POR' if finding.get('introducido_por') else
                     'REGRESION' if verified_closed else 'REINCIDENTE' if prior else 'NUEVO')
            record['estado_pasada'] = label
            labels[label] += 1
        touched.add(fingerprint)
    aliases = {alias: canonical(target, aliases) for alias, target in aliases.items()}
    for alias, target in aliases.items():
        records[target]['aliases'] = sorted(set(records[target].get('aliases', []) + [alias]))
        if alias in records:
            records[alias]['merged_into'] = target
    # Acceptance applies to carried records too. A lower/new ceiling can reopen them.
    for fingerprint, record in records.items():
        if record['estado'] not in {'abierto', 'aceptado'} or record.get('merged_into'):
            continue
        relevant = [accepted[h] for h in [fingerprint, *record.get('aliases', [])] if h in accepted]
        acceptance = min(relevant, key=lambda a: SEVERIDADES.index(a.get('techo_severidad', 'MEDIUM'))) if relevant else None
        record.pop('rompe_techo_aceptado', None)
        if acceptance and SEVERIDADES.index(record['severidad']) <= SEVERIDADES.index(acceptance.get('techo_severidad', 'MEDIUM')):
            record['estado'] = 'aceptado'
            record['aceptado'] = copy.deepcopy(acceptance)
        else:
            record['estado'] = 'abierto'
            record.pop('aceptado', None)
            if acceptance:
                record['rompe_techo_aceptado'] = {'techo': acceptance.get('techo_severidad', 'MEDIUM'), 'ahora': record['severidad']}
    coverage = {k: run[k] for k in ('tree', 'head', 'base', 'merge_base', 'snapshot_ref')}
    coverage['pass_n'] = pass_n
    if 'snapshot_format' in run:
        coverage['snapshot_format'] = run['snapshot_format']
    if isinstance(run.get('rules'), dict):
        # A reviewer's rules count as covered only after it saw the whole branch with them,
        # or after the user explicitly kept the previous coverage.
        covered = old.get('coverage', {}).get('rules')
        covered = covered if isinstance(covered, dict) else {}
        renewed = set(run.get('rules_accepted', [])) | set(run.get('full_window_reviewers', []))
        coverage['rules'] = {name: digests if run['mode'] == 'completo' or name in renewed or name not in covered
                             else covered[name] for name, digests in run['rules'].items()}
    ledger.update(schema_version=2, rama=run['branch'], merge_base=run['merge_base'], aliases=aliases,
                  coverage=coverage, hallazgos=list(records.values()))
    entry = {'n': pass_n, 'sha': run['head'], 'tree': run['tree'], 'modo': run['mode'],
             'informe': Path(run['report']).name, 'coverage_complete': True}
    for name in ('plugin_version', 'rules_accepted', 'full_window_reviewers'):
        if run.get(name):
            entry[name] = copy.deepcopy(run[name])
    ledger['pasadas'].append(entry)
    opened = [r for r in records.values() if r['estado'] == 'abierto' and not r.get('merged_into')]
    actionable = [r for r in opened if not r.get('decision_externa', False)]
    blocking = [r for r in actionable if r['severidad'] in {'BLOCKER', 'HIGH'}]
    follow_up = [r for r in actionable if r['severidad'] not in {'BLOCKER', 'HIGH'}]
    counts = {severity: sum(r['severidad'] == severity for r in actionable) for severity in SEVERIDADES}
    summary = {'pasada': pass_n, 'modo': run['mode'], 'coverage_complete': True,
               'review_complete': not blocking, 'conteo': labels,
               'severity_counts': counts, 'cerrados_esta_pasada': len(closed),
               'abiertos_no_reexaminados': sum(r['huella'] not in touched for r in opened),
               'suprimidos_por_aceptado': sum(r['estado'] == 'aceptado' for r in records.values()),
               'veredicto': 'NO SUBIR' if counts['BLOCKER'] else 'CORREGIR ANTES DE SUBIR' if counts['HIGH'] else 'LISTO PARA PR'}
    entry.update(review_complete=summary['review_complete'], veredicto=summary['veredicto'],
                 nuevos=labels['NUEVO'],
                 descartados=sum(f['status'] == 'discarded' for _, _, f in decisions),
                 fusionados=sum(len(merged) for _, merged, _ in decisions))
    return ledger, {'resumen': summary, 'metricas': ledger_metrics(ledger), 'hallazgos': opened,
                    'bloqueantes': [r['huella'] for r in blocking],
                    'seguimiento': [r['huella'] for r in follow_up],
                    'cerrados': [records[h] for h in closed],
                    'no_reexaminados': [r for r in opened if r['huella'] not in touched],
                    'suprimidos': [r for r in records.values() if r['estado'] == 'aceptado'],
                    'descartados': [r for r in records.values() if r['estado'] == 'descartado']}


def ledger_metrics(ledger):
    """Quality counters derived from the persisted history; no model judgement involved."""
    records = ledger['hallazgos']
    unique = [r for r in records if not r.get('merged_into')]
    states = {state: sum(r['estado'] == state for r in unique) for state in ('abierto', 'cerrado', 'aceptado', 'descartado')}
    passes = sorted(ledger['pasadas'], key=lambda p: p['n'])
    approved = next((i + 1 for i, p in enumerate(passes) if p.get('review_complete') is True), None)
    measured = all('review_complete' in p for p in passes)
    return {'pasadas': len(passes), 'pasadas_completas': sum(p.get('modo') == 'completo' for p in passes),
            # Passes recorded before this counter existed cannot be assumed approved or rejected.
            'rondas_hasta_aprobacion': approved if measured else None,
            'aprobacion_medible': measured,
            'hallazgos_unicos': len(unique), 'por_estado': states,
            'falsos_positivos': states['descartado'],
            'tasa_falsos_positivos': round(states['descartado'] / len(unique), 3) if unique else None,
            'duplicados_fusionados': sum(bool(r.get('merged_into')) for r in records),
            'reabiertos_tras_descarte': sum(bool(r.get('reabierto_tras_descarte')) for r in unique),
            'regresiones': sum(r.get('estado_pasada') == 'REGRESION' and r['estado'] == 'abierto' for r in unique)}


class ledger_lock:
    """One finalization per ledger on this machine. Lives in the Git dir, never in pr-reviews/."""

    def __init__(self, repo, ledger_path):
        from prepare_review import output
        # Relative on older Git; shared by every worktree of the repository.
        common = (Path(repo) / output(repo, 'rev-parse', '--git-common-dir')).resolve()
        key = hashlib.sha256(str(Path(ledger_path).resolve()).encode()).hexdigest()[:24]
        self.path = common / 'pre-pr-review' / f'{key}.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > STALE_LOCK_SECONDS
                except FileNotFoundError:
                    continue
                # A crashed finalization never blocks the branch forever; the digest still guards the write.
                if attempt == 0 and stale:
                    self.path.unlink(missing_ok=True)
                    continue
                raise ReviewError('Another finalization is running for this ledger; wait for it, then prepare again '
                                  f'if the ledger changed. Lock: {self.path}')
            with os.fdopen(descriptor, 'w') as fh:
                fh.write(f'{os.getpid()}\n')
            return self
        raise ReviewError(f'Could not acquire the ledger lock: {self.path}')

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def finalize(args):
    run_dir = Path(args.run_dir).resolve()
    run = read_json(run_dir / 'run.json')
    require(isinstance(run, dict), 'Invalid run metadata.')
    with ledger_lock(run['repo'], run['ledger']):
        return finalize_locked(args, run_dir, run)


def finalize_locked(args, run_dir, run):
    from prepare_review import git, output, snapshot_tree
    from shared_checks import validate as validate_checks
    ledger_path = Path(run['ledger'])
    old = load_ledger(ledger_path)
    require(digest_file(ledger_path) == run.get('ledger_digest'), 'Ledger changed since preparation; prepare a new run.')
    require(run.get('pass_n') == max((p['n'] for p in old['pasadas']), default=0) + 1, 'Stale/invalid pass number.')
    curated = read_json(args.curated)
    decisions, verifications = validate_inputs(run, old, curated, run_dir)
    accepted = accepted_map(args.accepted or Path(run['repo']) / 'pr-reviews' / 'accepted.json')
    require(output(run['repo'], 'rev-parse', 'HEAD') == run['head'], 'HEAD changed during review; prepare again.')
    require(output(run['repo'], 'merge-base', run['base'], 'HEAD') == run['merge_base'], 'Base changed during review; prepare again.')
    require(snapshot_tree(run['repo']) == run['tree'], 'Worktree changed during review; prepare again.')
    blobs = {f['file']: output(run['repo'], 'rev-parse', '--verify', '--quiet', f"{run['tree']}:{f['file']}", check=False) or None
             for _, _, f in decisions if f['status'] == 'discarded'}
    ledger, result = apply_results(run, old, decisions, verifications, accepted, blobs)
    # Informative: a wrong shared fact is not reusable evidence, but it never rewrites a verdict.
    result['resumen']['shared_checks'] = validate_checks(run, run_dir)['status']
    # Keep every accepted tree reachable. A failed ledger write never unpins the old tree.
    git(run['repo'], 'update-ref', run['snapshot_ref'], run['tree'])
    require(digest_file(ledger_path) == run.get('ledger_digest'), 'Concurrent ledger update; prepare again.')
    atomic_json(ledger_path, ledger)
    atomic_json(run_dir / 'clasificacion.json', result)
    run['status'] = 'finalized'
    atomic_json(run_dir / 'run.json', run)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--curated', required=True)
    parser.add_argument('--accepted')
    parser.add_argument('--summary', action='store_true',
                        help='Print only verdict/counts and artifact paths; retain full classification on disk.')
    args = parser.parse_args()
    try:
        result = finalize(args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {'resumen': {'coverage_complete': False, 'review_complete': False,
                              'veredicto': 'REVISION INCOMPLETA',
                              'errors': [str(exc)], 'ledger_updated': False}}
        atomic_json(Path(args.run_dir) / 'clasificacion.json', result)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 2
    displayed = ({'resumen': result['resumen'],
                  'classification': str(Path(args.run_dir).resolve() / 'clasificacion.json'),
                  'report': read_json(Path(args.run_dir) / 'run.json')['report']}
                 if args.summary else result)
    print(json.dumps(displayed, indent=2, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
