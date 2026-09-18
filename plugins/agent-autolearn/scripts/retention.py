#!/usr/bin/env python3
"""Retention for local review artifacts and snapshot refs. Dry-run unless --apply.

Usage: retention.py [--repo DIR] [--days N] [--keep N] [--apply]
prepare_review.py applies the same policy after each preparation (PRE_PR_RETENTION_DAYS=0
disables it). Never touched: the current coverage snapshot, runs that can still be finalized,
the newest runs of each branch, versioned pr-reviews/ content, and other branches' refs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from ledger import ReviewError, digest_file, read_json

DEFAULT_DAYS = 30
DEFAULT_KEEP = 5


def configured_days():
    try:
        return max(0, int(os.environ.get('PRE_PR_RETENTION_DAYS', DEFAULT_DAYS)))
    except ValueError:
        return DEFAULT_DAYS


def run_entries(root):
    entries = []
    if not root.is_dir() or root.is_symlink():
        return entries
    for path in root.iterdir():
        meta = path / 'run.json'
        # Only real run directories created by the helper are candidates.
        if path.is_symlink() or not path.is_dir() or not meta.is_file():
            continue
        try:
            run = read_json(meta)
            if not isinstance(run, dict):
                continue
            entries.append({'path': path, 'run': run, 'mtime': meta.stat().st_mtime})
        except (ReviewError, OSError):
            continue
    return sorted(entries, key=lambda e: e['mtime'], reverse=True)


def finalizable(run):
    """A prepared run stays useful while its ledger is still the one it was prepared against."""
    if run.get('status') != 'prepared':
        return False
    try:
        return digest_file(run['ledger']) == run.get('ledger_digest')
    except (OSError, KeyError, TypeError):
        return True


def plan(repo, days=DEFAULT_DAYS, keep=DEFAULT_KEEP, branch=None, protect=(), now=None):
    from prepare_review import output
    repo = Path(repo).resolve()
    now = time.time() if now is None else now
    entries = run_entries(repo / '.pre-pr-review')
    kept_per_branch, remove_runs, kept_trees = {}, [], set(protect)
    for entry in entries:
        run = entry['run']
        name = run.get('branch')
        rank = kept_per_branch.get(name, 0)
        old = now - entry['mtime'] > days * 86400
        if old and rank >= keep and not finalizable(run):
            remove_runs.append(entry['path'])
            continue
        kept_per_branch[name] = rank + 1
        kept_trees.update(t for t in (run.get('tree'), run.get('since_tree')) if isinstance(t, str))
    remove_refs = []
    if branch:
        # Only this branch: another branch's ledger is not visible from this checkout.
        prefix = f'refs/pre-pr-review/{hashlib.sha256(branch.encode()).hexdigest()[:16]}/'
        for ref in output(repo, 'for-each-ref', '--format=%(refname)', prefix).splitlines():
            if ref.rsplit('/', 1)[-1] not in kept_trees:
                remove_refs.append(ref)
    return remove_runs, remove_refs


def cleanup(repo, days=None, keep=DEFAULT_KEEP, branch=None, protect=(), apply=True):
    from prepare_review import git
    days = configured_days() if days is None else days
    if days <= 0:
        return {'enabled': False, 'runs_removed': 0, 'refs_removed': 0}
    runs, refs = plan(repo, days, keep, branch, protect)
    if apply:
        for path in runs:
            shutil.rmtree(path)
        for ref in refs:
            git(repo, 'update-ref', '-d', ref)
    return {'enabled': True, 'applied': apply, 'days': days, 'keep': keep,
            'runs_removed': len(runs), 'refs_removed': len(refs),
            'runs': [p.name for p in runs], 'refs': refs}


def ledger_trees(ledger, keep=DEFAULT_KEEP):
    """Current coverage plus the last accepted snapshots of this branch."""
    passes = sorted(ledger.get('pasadas', []), key=lambda p: p['n'])
    trees = [p.get('tree') for p in (passes[-keep:] if keep > 0 else [])]
    trees.append(ledger.get('coverage', {}).get('tree'))
    return {t for t in trees if isinstance(t, str)}


def main():
    from prepare_review import output
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--days', type=int, default=None)
    parser.add_argument('--keep', type=int, default=DEFAULT_KEEP)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        repo = Path(output(args.repo, 'rev-parse', '--show-toplevel'))
        branch = output(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False) or None
        protect = set()
        for path in (repo / 'pr-reviews').glob('*/ledger.json'):
            ledger = read_json(path)
            if isinstance(ledger, dict) and ledger.get('rama') == branch:
                protect |= ledger_trees(ledger, args.keep)
        # Without this branch's ledger its snapshots cannot be told apart: leave every ref.
        result = cleanup(repo, args.days, args.keep, branch if protect else None, protect, args.apply)
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'retention.py: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
