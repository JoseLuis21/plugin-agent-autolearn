#!/usr/bin/env python3
"""Validate the build/test facts published by run.check_owner, without running anything.

Usage: shared_checks.py --run-dir DIR
Writes the verdict into shared-checks.json under `validation`, so consumers read one file.
Exit 0: reusable facts (or nothing to reuse). Exit 1: contradictory/unanchored facts; reviewers
treat them as absent evidence. Schema: skills/pre-pr-review/references/shared-checks.md.
"""
import argparse
import json
from pathlib import Path
import sys

from ledger import ReviewError, atomic_json, read_json

CHECK_STATUS = {'passed', 'failed', 'unavailable', 'not_run'}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def check_errors(check, index, run_dir):
    label = f'checks[{index}]'
    if not isinstance(check, dict):
        return [f'{label}: must be an object.']
    errors = []
    command = check.get('command')
    if not (isinstance(command, list) and command and all(text(part) for part in command)):
        errors.append(f'{label}: command must be a non-empty argv list.')
    for name in ('scope', 'environment', 'evidence'):
        if not text(check.get(name)):
            errors.append(f'{label}: missing {name}.')
    cwd = check.get('cwd', '.')
    if not isinstance(cwd, str) or Path(cwd).is_absolute() or '..' in Path(cwd).parts:
        errors.append(f'{label}: cwd must be relative to the repo.')
    status, code = check.get('status'), check.get('exit_code')
    if status not in CHECK_STATUS:
        return errors + [f'{label}: invalid status.']
    if code is not None and type(code) is not int:
        errors.append(f'{label}: exit_code must be an integer or null.')
    elif status == 'passed' and code != 0:
        errors.append(f'{label}: passed requires exit_code 0, found {code}.')
    elif status == 'failed' and code == 0:
        errors.append(f'{label}: failed contradicts exit_code 0.')
    elif status == 'not_run' and code is not None:
        errors.append(f'{label}: not_run cannot carry an exit_code.')
    log = check.get('log')
    if status in {'passed', 'failed'}:
        target = (run_dir / log).resolve() if text(log) else None
        if target is None or not target.is_relative_to(run_dir) or not target.is_file():
            errors.append(f'{label}: {status} requires its full log inside RUN_DIR.')
    elif log is not None and not text(log):
        errors.append(f'{label}: log must be a path or null.')
    tests_run = check.get('tests_run')
    if tests_run is not None:
        if type(tests_run) is not int or tests_run < 0:
            errors.append(f'{label}: tests_run must be a non-negative integer.')
        elif status == 'passed' and tests_run == 0:
            errors.append(f'{label}: passed with zero tests executed proves nothing.')
    return errors


def validate(run, run_dir):
    """Returns {'status': valid|pending|not_applicable|invalid, 'errors': [...]}."""
    run_dir = Path(run_dir).resolve()
    try:
        data = read_json(run_dir / 'shared-checks.json')
    except ReviewError as exc:
        return {'status': 'invalid', 'errors': [str(exc)]}
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        return {'status': 'invalid', 'errors': ['shared-checks.json: invalid schema.']}
    errors = []
    for name in ('tree', 'head'):
        if data.get(name) != run.get(name):
            errors.append(f'{name} does not match run.json: facts belong to another snapshot.')
    if data.get('owner') != run.get('check_owner'):
        errors.append('owner does not match run.check_owner.')
    status, checks = data.get('status'), data.get('checks')
    if not isinstance(checks, list):
        errors.append('checks must be a list.')
        checks = []
    if status not in {'pending', 'complete', 'not_applicable'}:
        errors.append('invalid status.')
    elif status == 'not_applicable' and (run.get('check_owner') or checks):
        errors.append('not_applicable contradicts an assigned owner or published checks.')
    elif status == 'pending' and checks:
        errors.append('pending cannot carry checks; publish with status complete.')
    elif status == 'complete' and not checks and not text(data.get('reason')):
        errors.append('complete without checks requires a reason.')
    for index, check in enumerate(checks):
        errors.extend(check_errors(check, index, run_dir))
    if errors:
        return {'status': 'invalid', 'errors': errors}
    return {'status': {'complete': 'valid'}.get(status, status), 'errors': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args()
    try:
        run_dir = Path(args.run_dir).resolve()
        result = validate(read_json(run_dir / 'run.json'), run_dir)
        path = run_dir / 'shared-checks.json'
        data = read_json(path)
        if isinstance(data, dict):
            atomic_json(path, dict(data, validation={'reusable': result['status'] == 'valid', **result}))
    except (ReviewError, OSError, KeyError, TypeError) as exc:
        result = {'status': 'invalid', 'errors': [str(exc)]}
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result['status'] == 'invalid' else 0


if __name__ == '__main__':
    sys.exit(main())
