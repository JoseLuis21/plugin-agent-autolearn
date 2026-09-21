#!/usr/bin/env python3
"""Everything the aggregator always loads, in one file, plus the exact curation schema.

Usage: aggregate_context.py --run-dir DIR

Writes RUN_DIR/aggregate-context.md (to read in one turn) and RUN_DIR/curated.draft.json (source
fields to reuse by code). Deterministic and read-only over the run: it never decides, merges,
discards or recalibrates anything. The draft is not a curation and ledger.py rejects it as is.
"""
import argparse
import json
from pathlib import Path
import sys
import textwrap

from ledger import ReviewError, canonical, huella, load_ledger, read_json, reviewer_result, atomic_json

WIDTH = 170          # the file reader truncates very long lines
CHUNK_CHARS = 60_000  # one read stays well under the reader's size limit
CHUNK_LINES = 1500
RANK = {name: i for i, name in enumerate(('BLOCKER', 'HIGH', 'MEDIUM', 'LOW', 'NIT'))}

SCHEMA = '''curated.json = {"schema_version": 2, "validated": true, "findings": [...], "verifications": [...]}

Cada entrada de findings es una DECISION sobre una o varias huellas crudas:
  category, file, symbol, title, evidence, why, fix   strings no vacios (file relativo, sin "..")
  severity      BLOCKER | HIGH | MEDIUM | LOW | NIT   (la final, recalibrada con evidencia)
  line          entero >= 0
  confidence    alta | media | baja
  decision_externa   false, o un string con quien decide
  status        open | discarded   (discarded exige discard_reason; nunca closed: cerrar es una verificacion)
  sources       huellas crudas que cubre esta decision. Cada huella cruda aparece en EXACTAMENTE una decision,
                tambien los falsos positivos (como discarded). Fusionar = varias huellas en sources.
  detectado_por exactamente los revisores de esas sources; reviewer = uno de ellos (el dueño principal)
  aliases       opcional: huellas historicas/del ledger de la misma causa raiz (las sources se añaden solas)
  new_evidence  obligatorio para dejar open algo que el ledger tiene descartado (ver "Ledger previo")
  introducido_por   opcional: {fingerprint previo, before, after, causal_evidence}; solo con prueba causal

La huella de una decision sale de su category|file|symbol finales: dos decisiones no pueden compartirla.
verifications = copia EXACTA de cada verificacion asignada de los revisores (estan en curated.draft.json).
Una decision sobre una huella verificada debe tener el mismo status que su verificacion.
Reutiliza por codigo los campos sin cambios desde curated.draft.json (indexado por huella cruda); escribe a mano
solo lo que decides: fusiones, descartes, severidad final, why/fix conciliados y criterio de cierre.'''


def optional(path, default=None):
    """Artifacts that may legitimately be absent or unreadable: reported, never fatal."""
    try:
        return read_json(path) if Path(path).exists() else default
    except (ReviewError, OSError, ValueError):
        return default


def wrap(label, text):
    lines = []
    for i, part in enumerate(str(text or '').splitlines() or ['']):
        prefix = f'{label}: ' if i == 0 else '    '
        lines.extend(textwrap.wrap(part, WIDTH, initial_indent=prefix, subsequent_indent='    ',
                                   break_long_words=True, break_on_hyphens=False) or [prefix.rstrip()])
    return lines


def compact(value, limit=WIDTH):
    text = json.dumps(value, ensure_ascii=False)
    return textwrap.wrap(text, limit, break_long_words=True, break_on_hyphens=False) or ['']


def build(run_dir):
    run_dir = Path(run_dir).resolve()
    run = read_json(run_dir / 'run.json')
    ledger = load_ledger(Path(run['ledger']))
    aliases = ledger.get('aliases', {})
    previous = {r['huella']: r for r in ledger['hallazgos']}
    out = ['# Contexto del agregador', '',
           'Generado por aggregate_context.py. Es TODO lo que el Paso 1 pide cargar: no vuelvas a abrir brief.md, run.json,',
           'los <reviewer>.json, pending/, shared-checks.json, discarded.json, review-state.md ni findings-contract.md.',
           'No decide nada: curar (verificar HIGH/BLOCKER en el arbol, fusionar, descartar, recalibrar, conciliar fixes) es tu trabajo.',
           '', '## Corrida', '']
    out.extend((run_dir / 'brief.md').read_text(encoding='utf-8').splitlines())
    facts = {k: run.get(k) for k in ('repo', 'branch', 'base', 'mode', 'pass_n', 'reason', 'report', 'ledger', 'check_owner',
                                     'expected_reviewers', 'assignments', 'skipped_reviewers', 'reviewer_reasons',
                                     'full_window_reviewers', 'recovered_from_commit')}
    facts['snapshot'] = f"{str(run.get('since_tree'))[:7]} → {str(run.get('tree'))[:7]}"
    facts['delta_files'] = len(run.get('delta_files') or [])
    out.append('')
    for key, value in facts.items():
        out.extend(compact({key: value}))
    out.extend(['', 'Archivos del delta:'])
    out.extend(textwrap.wrap(', '.join(run.get('delta_files') or []) or '(ninguno)', WIDTH, break_long_words=False, break_on_hyphens=False))

    checks = optional(run_dir / 'shared-checks.json', {}) or {}
    out.extend(['', '## Checks compartidos', ''])
    out.extend(compact({k: checks.get(k) for k in ('owner', 'status', 'validation')}))
    for check in checks.get('checks') or []:
        out.extend(compact(check))

    out.extend(['', '## Resultados de los revisores', ''])
    raw, draft, verifications, problems = {}, {}, [], []
    for reviewer in run.get('expected_reviewers', []):
        required = (run.get('assignments') or {}).get(reviewer, [])
        try:
            reviewer_result(run_dir, reviewer, required)
        except (ReviewError, OSError, KeyError, TypeError) as exc:
            problems.append(f'{reviewer}: {exc}')
        data = optional(run_dir / f'{reviewer}.json')
        if not isinstance(data, dict):
            out.extend([f'### {reviewer}', 'SIN RESULTADO VALIDO: cobertura incompleta.', ''])
            continue
        findings = [f for f in data.get('findings') or [] if isinstance(f, dict)]
        out.append(f"### {reviewer} — {len(findings)} hallazgos, {len(data.get('verifications') or [])} verificaciones")
        out.extend(wrap('scope', data.get('scope')))
        out.extend(wrap('sin_hallazgos_en', '; '.join(str(x) for x in data.get('sin_hallazgos_en') or [])))
        extra = {k: v for k, v in data.items() if k not in {'reviewer', 'scope', 'sin_hallazgos_en', 'findings', 'verifications'}}
        if extra:
            out.extend(compact(extra))
        for finding in sorted(findings, key=lambda f: RANK.get(f.get('severity'), 9)):
            try:
                fp = huella(finding['category'], finding['file'], finding['symbol'])
            except (KeyError, AttributeError, TypeError):
                out.extend(compact({'hallazgo_invalido': finding}))
                continue
            raw.setdefault(fp, []).append(reviewer)
            draft.setdefault(fp, []).append(dict(finding, reviewer=reviewer))
            out.append(f"#### [{fp}] {finding.get('severity')} · {finding.get('category')} — {finding.get('title')}")
            out.append(f"{finding.get('file')}:{finding.get('line')} · {finding.get('symbol')} · confianza {finding.get('confidence')}"
                       f" · decision_externa {json.dumps(finding.get('decision_externa', False), ensure_ascii=False)}")
            for label in ('evidence', 'why', 'fix'):
                out.extend(wrap(label, finding.get(label)))
            rest = {k: v for k, v in finding.items() if k not in {'severity', 'category', 'title', 'file', 'line', 'symbol',
                                                                  'confidence', 'decision_externa', 'evidence', 'why', 'fix'}}
            if rest:
                out.extend(compact(rest))
            out.append('')
        for verification in data.get('verifications') or []:
            verifications.append(verification)
            out.extend(compact({'verificacion': verification}))
        out.append('')
    if problems:
        at = out.index('## Corrida')
        out[at:at] = ['**COBERTURA INCOMPLETA segun la validacion del ledger:**', *[f'- {p}' for p in problems], '']

    out.extend(['## Huellas crudas', '',
                'Una huella reportada por varios revisores ya es la misma identidad: va en una sola decision.', ''])
    for fp, reviewers in sorted(raw.items()):
        out.append(f"- {fp}: {', '.join(reviewers)}")

    out.extend(['', '## Pendientes asignados (revalidaciones obligatorias)', ''])
    pending = []
    for reviewer in run.get('expected_reviewers', []):
        for record in optional(run_dir / 'pending' / f'{reviewer}.json', []) or []:
            pending.append(record)
            out.extend(wrap(f"- [{record.get('huella')}] {record.get('severidad')} {record.get('estado')} · dueño {reviewer}",
                            f"{record.get('titulo')} — {record.get('archivo')}:{record.get('linea')} ({record.get('simbolo')})"))
    if not pending:
        out.append('(ninguno)')

    out.extend(['', '## Ledger previo que toca a estas huellas', ''])
    touched = False
    for fp in sorted(raw):
        root = canonical(fp, aliases)
        record = previous.get(root)
        if not record:
            continue
        touched = True
        note = f"- {fp}" + (f' (alias de {root})' if root != fp else '') + f": ya existe, estado {record.get('estado')}, visto en pasadas {record.get('visto_en')}"
        if record.get('estado') == 'descartado' and not record.get('merged_into'):
            note += (f". DESCARTADO antes (pasada {record.get('discard_pass')}): {record.get('discard_reason')}. "
                     'Repite el descarte, o deja open con new_evidence explicando que cambio.')
        out.extend(textwrap.wrap(note, WIDTH, subsequent_indent='  ', break_long_words=False, break_on_hyphens=False))
    if not touched:
        out.append('(ninguna huella cruda existia en el ledger: todas son nuevas)')
    dismissed = optional(run_dir / 'discarded.json')
    if isinstance(dismissed, dict):
        out.extend(['', '### discarded.json', ''])
        for item in dismissed.get('discarded') or []:
            out.extend(compact(item))

    out.extend(['', '## Consultas registradas por los revisores', ''])
    for path in sorted((run_dir / 'searches').glob('*.json')):
        data = optional(path)
        entries = data.get('searches', []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get('query'):
                out.extend(textwrap.wrap(f"- {path.stem}: {entry.get('query')} @ {entry.get('scope')} → "
                                         f"{json.dumps(entry.get('results'), ensure_ascii=False)[:400]}", WIDTH, subsequent_indent='  '))

    out.extend(['', '## Esquema exacto de curated.json (lo que valida ledger.py)', '', *SCHEMA.splitlines()])
    contract = Path(__file__).resolve().parents[1] / 'skills' / 'pre-pr-review' / 'references' / 'findings-contract.md'
    out.extend(['', '## findings-contract.md (calibracion)', ''])
    for line in contract.read_text(encoding='utf-8').splitlines():
        out.extend(textwrap.wrap(line, WIDTH, break_long_words=False, break_on_hyphens=False) or [''])

    (run_dir / 'aggregate-context.md').write_text('\n'.join(out) + '\n', encoding='utf-8')
    atomic_json(run_dir / 'curated.draft.json', {
        'schema_version': 2, 'validated': False,
        'instruction': 'NO es una curacion: campos originales por huella cruda para reutilizarlos por codigo. '
                       'Sin fusionar, verificar ni recalibrar. ledger.py lo rechaza tal cual.',
        'by_fingerprint': draft, 'verifications': verifications})
    ranges, start, chars = [], 1, 0
    for number, line in enumerate(out, 1):
        chars += len(line) + 1
        if chars >= CHUNK_CHARS or number - start + 1 >= CHUNK_LINES:
            ranges.append({'offset': start, 'limit': number - start + 1})
            start, chars = number + 1, 0
    if start <= len(out):
        ranges.append({'offset': start, 'limit': len(out) - start + 1})
    return {'context': str(run_dir / 'aggregate-context.md'), 'lines': len(out), 'read_in_one_turn': ranges,
            'draft': str(run_dir / 'curated.draft.json'), 'raw_fingerprints': len(raw),
            'reviewers': len(run.get('expected_reviewers', [])), 'assigned_verifications': len(verifications),
            'coverage_problems': problems}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.run_dir), ensure_ascii=False, indent=1))
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as exc:
        # The aggregator can always fall back to reading the artifacts one by one.
        print(f'aggregate_context.py: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
