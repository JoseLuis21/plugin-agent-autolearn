#!/usr/bin/env python3
"""Local, non-blocking token telemetry for marked pre-PR subagents.

hook: consume SubagentStop (reviewers) or Stop (orchestrator estimate) JSON on stdin.
summarize: aggregate one run to JSON/Markdown. compare: two summaries side by side.
Only transcript counters are retained; prompts, source code and tool results are never copied.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from ledger import atomic_json, read_json
from prepare_review import REVIEWERS, output

FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
MARKER = re.compile(r'^PRE_PR_USAGE: (\{[^\n]+\})$', re.MULTILINE)


def prompt_text(message):
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def transcript_usage(path):
    """Group marked turns; count each assistant message id once, including resumed turns."""
    groups, active = {}, None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError('invalid record')
            except ValueError:
                if active is not None:
                    groups[active]['incomplete'] = True
                continue
            message = record.get('message', {})
            if not isinstance(message, dict):
                continue
            if record.get('type') == 'user':
                markers = MARKER.findall(prompt_text(message))
                if markers:
                    try:
                        marker = json.loads(markers[-1])
                        active = (marker['run_dir'], marker['reviewer'])
                        if not all(isinstance(v, str) for v in active):
                            raise ValueError('invalid marker')
                        groups.setdefault(active, {'messages': {}, 'incomplete': False})
                    except (ValueError, KeyError, TypeError):
                        active = None
                continue
            if active is None or record.get('type') != 'assistant':
                continue
            # Duplicate streaming/tool blocks share an id. Counters may be updated at completion.
            add_usage(groups[active], message)
    return {key: usage_totals(group) for key, group in groups.items()}


def usage_totals(group):
    messages = list(group['messages'].values())
    missing = [field for field in FIELDS if not messages or any(m[field] is None for m in messages)]
    totals = {field: sum(m[field] or 0 for m in messages) for field in FIELDS}
    return dict(totals, requests=len(messages), models=sorted({m['model'] for m in messages}),
                missing_fields=missing, complete=bool(messages) and not missing and not group['incomplete'])


def add_usage(group, message):
    ident, model, usage = message.get('id'), message.get('model'), message.get('usage')
    if not isinstance(ident, str) or not ident or not isinstance(usage, dict):
        group['incomplete'] = True
        return
    previous = group['messages'].setdefault(ident, {'model': model if isinstance(model, str) else 'unknown',
                                                   **{field: None for field in FIELDS}})
    for field in FIELDS:
        value = usage.get(field)
        if type(value) is int and value >= 0:
            previous[field] = max(previous[field] or 0, value)
        elif field in usage:
            group['incomplete'] = True


def orchestrator_usage(path):
    """Estimate per run: from the prepare_review.py call to the turn that summarized the run.

    The main transcript holds the whole conversation, so the window is delimited by the
    orchestrator's own tool calls; tool results and user text can never bind or move a window.
    """
    windows, current = [], None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                if current is not None:
                    current['incomplete'] = True
                continue
            if not isinstance(record, dict) or record.get('isSidechain') is True:
                continue
            message = record.get('message', {})
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if record.get('type') == 'user':
                human = isinstance(content, str) or not any(b.get('type') == 'tool_result' for b in blocks)
                if human and current is not None and current['closing']:
                    windows.append(current)
                    current = None
                continue
            if record.get('type') != 'assistant':
                continue
            calls = [b for b in blocks if b.get('type') == 'tool_use' and isinstance(b.get('input'), dict)]
            commands = [str(b['input'].get('command', '')) for b in calls if b.get('name') == 'Bash']
            if any('prepare_review.py' in c for c in commands) and (current is None or current['run_dir']):
                if current is not None:
                    windows.append(current)
                current = {'messages': {}, 'incomplete': False, 'run_dir': None, 'closing': False}
            if current is None:
                continue
            add_usage(current, message)
            for call in calls:
                markers = MARKER.findall(str(call['input'].get('prompt', '')))
                if markers and current['run_dir'] is None:
                    try:
                        current['run_dir'] = str(json.loads(markers[-1])['run_dir'])
                    except (ValueError, KeyError, TypeError):
                        pass
            if any('review_usage.py' in c and 'summarize' in c for c in commands):
                current['closing'] = True
    if current is not None:
        windows.append(current)
    result = {}
    for window in windows:
        if not window['run_dir']:
            continue
        # A resumed review has several windows for the same run; request ids keep them disjoint.
        merged = result.setdefault(window['run_dir'], {'messages': {}, 'incomplete': False, 'closed': True})
        merged['messages'].update(window['messages'])
        merged['incomplete'] = merged['incomplete'] or window['incomplete']
        merged['closed'] = merged['closed'] and window['closing']
    return {run_dir: dict(usage_totals(group), closed=group['closed']) for run_dir, group in result.items()}


def collect_orchestrator(event):
    path = Path(event['transcript_path'])
    if 'prepare_review.py' not in path.read_text(encoding='utf-8', errors='replace'):
        return
    repo = Path(output(event['cwd'], 'rev-parse', '--show-toplevel')).resolve()
    session = event['session_id']
    if not isinstance(session, str) or not session:
        raise ValueError('Missing session identity')
    identity = 'orchestrator-' + hashlib.sha256(session.encode()).hexdigest()[:24]
    for directory, counters in orchestrator_usage(path).items():
        run_dir = Path(directory).resolve()
        if not run_dir.is_relative_to(repo / '.pre-pr-review'):
            continue
        run = read_json(run_dir / 'run.json')
        if Path(run['repo']).resolve() != repo or (run_dir / 'usage').is_symlink():
            continue
        atomic_json(run_dir / 'usage' / f'{identity}.json', {
            'schema_version': 1, 'run_dir': str(run_dir), 'reviewer': 'orchestrator', 'agent_key': identity,
            'source': 'claude_main_transcript_window', 'estimated': True, **counters})
        if (run_dir / 'usage-summary.json').exists():
            summarize(run_dir)


def collect(event):
    if event.get('hook_event_name') == 'Stop':
        return collect_orchestrator(event)
    if event.get('hook_event_name') != 'SubagentStop':
        return
    reviewer = event.get('agent_type', '').split(':')[-1]
    if reviewer not in [*REVIEWERS, 'review-aggregator']:
        return
    repo = Path(output(event['cwd'], 'rev-parse', '--show-toplevel')).resolve()
    groups = transcript_usage(event['agent_transcript_path'])
    session, agent = event['session_id'], event['agent_id']
    if not isinstance(session, str) or not isinstance(agent, str) or not session or not agent:
        raise ValueError('Missing session/agent identity')
    identity = hashlib.sha256(f'{session}|{agent}'.encode()).hexdigest()[:24]
    for (directory, marked_reviewer), counters in groups.items():
        run_dir = Path(directory).resolve()
        if marked_reviewer != reviewer or not run_dir.is_relative_to(repo / '.pre-pr-review'):
            continue
        run = read_json(run_dir / 'run.json')
        if Path(run['repo']).resolve() != repo:
            continue
        if reviewer not in [*run['expected_reviewers'], 'review-aggregator']:
            continue
        usage_dir = run_dir / 'usage'
        if usage_dir.is_symlink():
            raise ValueError('Usage directory must remain inside the run')
        # One file per actual agent: repeated hooks/resumes replace, fresh retries add.
        atomic_json(usage_dir / f'{identity}.json', {
            'schema_version': 1, 'run_dir': str(run_dir), 'reviewer': reviewer,
            'agent_key': identity, 'source': 'claude_transcript_assistant_usage', **counters,
        })


def repeated_searches(run_dir):
    """Same query and scope recorded by several reviewers. A report: nobody waits or skips evidence."""
    seen = {}
    for path in sorted((run_dir / 'searches').glob('*.json')):
        try:
            data = read_json(path)
        except ValueError:
            continue
        entries = data.get('searches', []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get('query'), str) and entry['query'].strip():
                key = (' '.join(entry['query'].lower().split()), ' '.join(str(entry.get('scope') or '').lower().split()))
                seen.setdefault(key, set()).add(path.stem)
    repeated = [{'query': q, 'scope': scope, 'reviewers': sorted(names)}
                for (q, scope), names in seen.items() if len(names) > 1]
    return {'distinct': len(seen), 'repeated': len(repeated),
            'queries': sorted(repeated, key=lambda r: (-len(r['reviewers']), r['query']))[:20]}


def findings_summary(run_dir):
    try:
        result = read_json(run_dir / 'clasificacion.json')
        resumen = result['resumen']
        return {'veredicto': resumen.get('veredicto'), 'coverage_complete': resumen.get('coverage_complete'),
                'severity_counts': resumen.get('severity_counts'), 'conteo': resumen.get('conteo'),
                'abiertos': len(result['hallazgos']) if 'hallazgos' in result else None,
                'metricas': result.get('metricas')}
    except (ValueError, KeyError, TypeError):
        return None


def summarize(run_dir, share=True):
    run_dir = Path(run_dir).resolve()
    run = read_json(run_dir / 'run.json')
    expected = list(run['expected_reviewers'])
    if run['status'] != 'unchanged':
        expected.append('review-aggregator')
    by_reviewer = {name: [] for name in expected}
    errors, orchestrators = [], []
    for path in sorted((run_dir / 'usage').glob('*.json')):
        try:
            value = read_json(path)
            if path.name.startswith('orchestrator-'):
                by_reviewer['orchestrator'] = orchestrators
            valid = (value['schema_version'] == 1 and value['run_dir'] == str(run_dir)
                     and value['reviewer'] in by_reviewer and value['agent_key'] == path.stem
                     and type(value['complete']) is bool
                     and type(value['requests']) is int and value['requests'] >= 0
                     and all(type(value[k]) is int and value[k] >= 0 for k in FIELDS)
                     and isinstance(value['models'], list)
                     and all(isinstance(m, str) for m in value['models'])
                     and isinstance(value['missing_fields'], list)
                     and all(k in FIELDS for k in value['missing_fields']))
            if not valid:
                raise ValueError('Invalid usage record')
            by_reviewer[value['reviewer']].append(value)
        except (ValueError, KeyError, TypeError, OSError):
            errors.append(path.name)
    by_reviewer.pop('orchestrator', None)
    main = None
    if orchestrators:
        # Estimated window of the main conversation; kept apart from the measured subagents.
        main = {'status': 'estimated' if all(e['complete'] and e.get('closed') for e in orchestrators) else 'partial',
                'sessions': len(orchestrators), 'requests': sum(e['requests'] for e in orchestrators),
                'models': sorted({m for e in orchestrators for m in e['models']}),
                **{k: sum(e[k] for e in orchestrators) for k in FIELDS},
                'reported_tokens': sum(sum(e[k] for k in FIELDS) for e in orchestrators)}
    rows = []
    for reviewer, entries in by_reviewer.items():
        counts = {k: sum(e[k] for e in entries)
                  if entries and all(k not in e.get('missing_fields', FIELDS) for e in entries) else None
                  for k in FIELDS}
        rows.append({'reviewer': reviewer, 'agents': len(entries),
                     'status': 'unavailable' if not entries else 'recorded' if all(e['complete'] for e in entries) else 'partial',
                     'requests': sum(e['requests'] for e in entries),
                     'models': sorted({m for e in entries for m in e['models']}),
                     'reported_tokens': sum(sum(e[k] for k in FIELDS) for e in entries) if entries else None, **counts})
    rows.sort(key=lambda row: (row['reported_tokens'] is None, -(row['reported_tokens'] or 0), row['reviewer']))
    observed = [row for row in rows if row['reported_tokens'] is not None]
    subtotal = sum(row['reported_tokens'] for row in observed) if observed else None
    result = {'schema_version': 1, 'branch': run['branch'], 'pass_n': run['pass_n'], 'mode': run['mode'],
              'plugin_version': run.get('plugin_version'), 'tree': run.get('tree'), 'since_tree': run.get('since_tree'),
              'scope': 'reviewers_and_aggregator_only',
              'orchestrator_tokens': main['reported_tokens'] if main else None, 'orchestrator': main,
              'total_with_orchestrator': (subtotal or 0) + main['reported_tokens'] if main else None,
              'findings': findings_summary(run_dir), 'searches': repeated_searches(run_dir),
              'status': 'not_applicable' if not expected else 'recorded' if not errors and all(r['status'] == 'recorded' for r in rows) else 'partial',
              'measured_agents': sum(row['agents'] for row in rows),
              'observed_tokens': subtotal,
              'totals': {k: sum(row[k] for row in observed if row[k] is not None)
                         if any(row[k] is not None for row in observed) else None for k in FIELDS},
              'reviewers': rows, 'invalid_records': errors,
              'limitations': [('Orchestrator is an estimated window of the main conversation, outside observed_tokens. '
                               if main else 'Excludes orchestrator. ')
                              + 'Excludes unobserved retries, nested agents and internal requests absent from transcripts.',
                              'Transcript counters are reported usage, not billing; output counters may be provisional depending on runtime.',
                              'No character-based estimates, prices, or final-request total_tokens rollups.']}
    atomic_json(run_dir / 'usage-summary.json', result)
    lines = ['# Tokens registrados de esta corrida', '',
             f"Rama: {run['branch']} · Pasada: {run['pass_n']} ({run['mode']}) · Medicion: {result['status']}", '',
             '| Revisor | Agentes | Peticiones | Entrada | Cache creada | Cache leida | Salida registrada | Total registrado | Estado |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    def cell(value):
        return 'N/D' if value is None else str(value)
    for row in rows:
        lines.append('| ' + ' | '.join(cell(row[k]) for k in ('reviewer', 'agents', 'requests', *FIELDS, 'reported_tokens', 'status')) + ' |')
    lines.extend(['', f"Subtotal observado: **{cell(result['observed_tokens'])}** tokens registrados.",
                  (f"Orquestador (estimado, {main['status']}): **{main['reported_tokens']}** tokens en {main['requests']} "
                   f"peticiones de la conversacion principal; con el: **{result['total_with_orchestrator']}**."
                   if main else 'Orquestador: N/D (se estima al terminar el turno, con el hook Stop).'),
                  f"Consultas: {result['searches']['distinct']} distintas, {result['searches']['repeated']} repetidas entre revisores.",
                  'El subtotal excluye el orquestador y cualquier ejecucion no capturada; N/D no significa cero.',
                  'Contadores del transcript, no factura: la salida puede ser provisional segun runtime.',
                  'No usa el total_tokens final como consumo acumulado ni estima por caracteres.', ''])
    (run_dir / 'usage-summary.md').write_text('\n'.join(lines))
    shared = Path(run['ledger']).parent if share and isinstance(run.get('ledger'), str) else None
    if shared and shared.is_dir() and not shared.is_symlink() and expected:
        # Counters only, next to the versioned ledger: travels with the branch, outside the snapshot.
        atomic_json(shared / 'usage' / f"p{run['pass_n']}-{run_dir.name[-8:]}.json", result)
    return result


def load_summary(path):
    path = Path(path).resolve()
    value = read_json(path / 'usage-summary.json' if path.is_dir() else path)
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('reviewers'), list):
        raise ValueError(f'Not a usage summary: {path}')
    return value


def compare(first, second):
    a, b = load_summary(first), load_summary(second)
    def cell(value):
        return 'N/D' if value is None else str(value)
    def delta(x, y):
        return 'N/D' if x is None or y is None else f'{y - x:+d}'
    same_code = bool(a.get('tree')) and a.get('tree') == b.get('tree') and a.get('since_tree') == b.get('since_tree')
    lines = ['# Comparacion de corridas', '',
             f"A: {a['branch']} p{a['pass_n']} ({a['mode']}) · plugin {cell(a.get('plugin_version'))}",
             f"B: {b['branch']} p{b['pass_n']} ({b['mode']}) · plugin {cell(b.get('plugin_version'))}",
             'Mismo codigo y ventana: si.' if same_code else
             'Mismo codigo y ventana: NO. Las diferencias mezclan codigo y version; no atribuyas el cambio al plugin.',
             '', '| Revisor | A | B | Delta |', '|---|---:|---:|---:|']
    rows_a = {r['reviewer']: r['reported_tokens'] for r in a['reviewers']}
    rows_b = {r['reviewer']: r['reported_tokens'] for r in b['reviewers']}
    for name in sorted(set(rows_a) | set(rows_b)):
        lines.append(f'| {name} | {cell(rows_a.get(name))} | {cell(rows_b.get(name))} | {delta(rows_a.get(name), rows_b.get(name))} |')
    for label, key in (('Subtotal observado', 'observed_tokens'), ('Orquestador (estimado)', 'orchestrator_tokens')):
        lines.append(f'| **{label}** | {cell(a.get(key))} | {cell(b.get(key))} | {delta(a.get(key), b.get(key))} |')
    fa, fb = a.get('findings') or {}, b.get('findings') or {}
    lines.extend(['', '| Hallazgos | A | B |', '|---|---|---|',
                  f"| Veredicto | {cell(fa.get('veredicto'))} | {cell(fb.get('veredicto'))} |",
                  f"| Abiertos | {cell(fa.get('abiertos'))} | {cell(fb.get('abiertos'))} |"])
    for severity in ('BLOCKER', 'HIGH', 'MEDIUM', 'LOW', 'NIT'):
        lines.append(f"| {severity} | {cell((fa.get('severity_counts') or {}).get(severity))} "
                     f"| {cell((fb.get('severity_counts') or {}).get(severity))} |")
    lines.extend(['', 'Tokens registrados, no factura. Menos hallazgos no implica peor calidad: '
                      'contrasta con una evaluacion de bugs conocidos (eval_review.py).', ''])
    return {'same_code': same_code, 'markdown': '\n'.join(lines)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('hook')
    summary = sub.add_parser('summarize')
    summary.add_argument('--run-dir', required=True)
    summary.add_argument('--no-share', action='store_true', help='Do not copy the counters next to the ledger.')
    versus = sub.add_parser('compare')
    versus.add_argument('first', help='Run directory, usage-summary.json or shared pr-reviews/.../usage file.')
    versus.add_argument('second')
    args = parser.parse_args()
    try:
        if args.command == 'hook':
            collect(json.load(sys.stdin))
        elif args.command == 'compare':
            print(compare(args.first, args.second)['markdown'])
        else:
            result = summarize(args.run_dir, share=not args.no_share)
            print(json.dumps({'status': result['status'], 'observed_tokens': result['observed_tokens'],
                              'measured_agents': result['measured_agents'],
                              'summary': str(Path(args.run_dir).resolve() / 'usage-summary.md')}))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        print(f'review_usage: {exc}', file=sys.stderr)
        # Telemetry must never block SubagentStop or trigger extra model turns.
        return 0 if args.command == 'hook' else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
