"""Regression tests for the public preparation/finalization CLI protocol.

Run from the repository root:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/agent-autolearn/tests -v
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
from ledger import huella  # Public fingerprint API used by reviewer/aggregator clients.


def fingerprint(finding):
    return huella(finding['category'], finding['file'], finding['symbol'])


def finding(symbol='parse', **changes):
    value = {
        'title': 'Unchecked input', 'category': 'correctness', 'file': 'app.py',
        'symbol': symbol, 'line': 1, 'severity': 'MEDIUM',
        'evidence': 'app.py:1 accepts an empty input before indexing it.',
        'why': 'An empty request raises an exception.',
        'fix': 'Validate the input before indexing it.', 'confidence': 'alta',
        'decision_externa': False,
    }
    value.update(changes)
    return value


class ReviewRepo:
    """A real repository with no inherited Git configuration or network use."""

    def __init__(self, case, root, extra=None):
        self.case = case
        self.root = root
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(
            HOME=str(root.parent), XDG_CONFIG_HOME=str(root.parent),
            GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME='Review Test', GIT_AUTHOR_EMAIL='review@example.invalid',
            GIT_COMMITTER_NAME='Review Test', GIT_COMMITTER_EMAIL='review@example.invalid',
            GIT_OPTIONAL_LOCKS='0', PYTHONDONTWRITEBYTECODE='1',
        )
        self.git('init', '--quiet')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/development')
        files = {'app.py': 'value = 0\n', 'old.txt': 'original\n',
                 'package-lock.json': '{"version": 1}\n', '.gitignore': '*.log\n'}
        files.update(extra or {})
        for name, contents in files.items():
            self.write(name, contents)
        self.write('tracked.log', 'tracked despite ignore\n')
        self.git('add', '.')
        self.git('add', '--force', 'tracked.log')
        self.git('commit', '--quiet', '--no-gpg-sign', '-m', 'baseline')
        self.git('checkout', '--quiet', '-b', 'feature')

    def command(self, argv, expected=0):
        result = subprocess.run(argv, cwd=self.root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.case.assertEqual(result.returncode, expected,
                              f'{argv}\nstdout: {result.stdout}\nstderr: {result.stderr}')
        return result.stdout

    def git(self, *args):
        return self.command(['git', *args]).strip()

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def write_json(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + '\n')

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text())

    def prepare(self, *args, base=None):
        output = self.command([sys.executable, str(SCRIPTS / 'prepare_review.py'),
                               '--repo', str(self.root), *(['--base', base] if base else []), *args])
        run = json.loads(output)
        persisted = self.read_json(Path(run['run_dir']) / 'run.json')
        for key, value in persisted.items():
            self.case.assertEqual(run[key], value)
        return run

    def results(self, run, raw=None, decisions=None, statuses=None):
        """Write valid reviewer artifacts; tests mutate copies to exercise rejection."""
        raw, statuses = raw or {}, statuses or {}
        verifications = []
        curated_findings = []
        for reviewer in run['expected_reviewers']:
            checks = [dict(fingerprint=h, reviewer=reviewer,
                           status=statuses.get(h, 'open'),
                           evidence=f'app.py:1 checked the current path for {h}.')
                      for h in run['assignments'][reviewer]]
            verifications.extend(checks)
            findings = copy.deepcopy(raw.get(reviewer, []))
            self.write_json(Path(run['run_dir']) / f'{reviewer}.json', {
                'reviewer': reviewer, 'scope': 'The prepared delta and assigned findings.',
                'sin_hallazgos_en': ['Other reviewed paths have no findings.'],
                'findings': findings, 'verifications': checks,
            })
            for value in findings:
                curated_findings.append(dict(value, status='open', reviewer=reviewer,
                                             detectado_por=[reviewer], sources=[fingerprint(value)]))
        curated = {'schema_version': 2, 'validated': True,
                   'findings': curated_findings if decisions is None else copy.deepcopy(decisions),
                   'verifications': verifications}
        self.write_json(Path(run['run_dir']) / 'curated.json', curated)
        return curated

    def finalize(self, run, expected=0):
        run_dir = Path(run['run_dir'])
        output = self.command([sys.executable, str(SCRIPTS / 'ledger.py'),
                               '--run-dir', str(run_dir), '--curated', str(run_dir / 'curated.json')],
                              expected=expected)
        result = json.loads(output)
        self.case.assertEqual(result, self.read_json(run_dir / 'clasificacion.json'))
        self.case.assertEqual(result['resumen']['coverage_complete'], expected == 0)
        if expected == 0:
            ledger = self.read_json(run['ledger'])
            self.case.assertEqual(ledger['coverage']['tree'], run['tree'])
            self.case.assertEqual(ledger['coverage']['pass_n'], run['pass_n'])
            self.case.assertEqual(self.git('rev-parse', run['snapshot_ref']), run['tree'])
            self.case.assertEqual(self.read_json(run_dir / 'run.json')['status'], 'finalized')
        else:
            self.case.assertFalse(result['resumen']['ledger_updated'])
            self.case.assertTrue(result['resumen']['errors'])
            self.case.assertEqual(self.read_json(run_dir / 'run.json')['status'], 'prepared')
        return result

    def complete(self, run, **kwargs):
        self.results(run, **kwargs)
        return self.finalize(run)

    def assert_rejected(self, run):
        before = Path(run['ledger']).read_bytes()
        refs = self.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/pre-pr-review/')
        result = self.finalize(run, expected=2)
        self.case.assertEqual(Path(run['ledger']).read_bytes(), before)
        self.case.assertEqual(self.git('for-each-ref', '--format=%(refname) %(objectname)',
                                       'refs/pre-pr-review/'), refs)
        return result

    def records(self, run):
        return {r['huella']: r for r in self.read_json(run['ledger'])['hallazgos']}


class ReviewStateTests(unittest.TestCase):
    def clone_shared(self, repo):
        clone = copy.copy(repo)
        clone.root = self.temp / f'clone-{self.repo_count}'
        repo.command(['git', 'clone', '--quiet', '--no-local', '--branch', 'feature',
                      str(repo.root), str(clone.root)])
        self.assertEqual(clone.git('for-each-ref', '--format=%(refname)', 'refs/pre-pr-review/'), '')
        return clone

    def test_shared_commit_recovers_exact_snapshot_and_history_in_fresh_clone(self):
        for dirty_first in (False, True):
            with self.subTest(dirty_first=dirty_first):
                repo = self.repo({'pr-reviews/old.md': 'excluded from snapshots\n'})
                repo.write('app.py', 'value = 1\n')
                if not dirty_first:
                    repo.git('add', 'app.py')
                    repo.git('commit', '--quiet', '-m', 'reviewed code')
                first = repo.prepare()
                repo.complete(first, raw={'code-reviewer': [finding()]})
                repo.git('add', 'app.py', 'pr-reviews')
                repo.git('commit', '--quiet', '-m', 'share code and review')
                shared_commit = repo.git('rev-parse', 'HEAD')
                repo.write('app.py', 'value = 2\n')
                repo.git('add', 'app.py')
                repo.git('commit', '--quiet', '-m', 'new work')
                clone = self.clone_shared(repo)
                clone.command(['git', 'cat-file', '-e', first['tree']], expected=1)
                ledger = clone.root / Path(first['ledger']).relative_to(repo.root.resolve())
                before = ledger.read_bytes()
                index_before = (clone.root / '.git/index').read_bytes()
                run = clone.prepare()
                self.assertEqual(run['mode'], 'incremental')
                self.assertEqual(run['since_tree'], first['tree'])
                self.assertEqual(run['recovered_from_commit'], shared_commit if dirty_first else first['head'])
                self.assertEqual(run['delta_files'], ['app.py'])
                self.assertEqual(run['assignments']['code-reviewer'], [fingerprint(finding())])
                self.assertEqual(run['pass_n'], 2)
                self.assertEqual(ledger.read_bytes(), before)
                self.assertEqual((clone.root / '.git/index').read_bytes(), index_before)
                self.assertEqual(clone.git('rev-parse', first['snapshot_ref']), first['tree'])
                clone.complete(run, statuses={fingerprint(finding()): 'closed'})

    def test_unshared_dirty_content_never_uses_a_similar_commit(self):
        repo = self.repo({'pr-reviews/old.md': 'excluded\n'})
        first = self.seed(repo, {'code-reviewer': [finding()]})
        repo.git('add', 'pr-reviews')  # Publish the ledger, but not reviewed app.py.
        repo.git('commit', '--quiet', '-m', 'share review only')
        clone = self.clone_shared(repo)
        run = clone.prepare()
        self.assertEqual(run['mode'], 'completo')
        self.assertIsNone(run['recovered_from_commit'])
        self.assertIn('sin commit coincidente', run['reason'])
        self.assertEqual(run['pass_n'], 2)
        self.assertEqual(run['assignments']['code-reviewer'], [fingerprint(finding())])

    def test_snapshot_filter_change_invalidates_even_an_available_tree(self):
        repo = self.repo()
        first = self.seed(repo)
        ledger = repo.read_json(first['ledger'])
        self.assertEqual(ledger['coverage']['snapshot_format'], first['snapshot_format'])
        ledger['coverage']['snapshot_format'] = 'older-filter-definition'
        repo.write_json(first['ledger'], ledger)
        run = repo.prepare()
        self.assertEqual(run['mode'], 'completo')
        self.assertIn('filtros/formato', run['reason'])
        self.assertIsNone(run['recovered_from_commit'])

    def test_compact_finalization_preserves_full_evidence_and_failures(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        bug = finding(severity='HIGH', evidence='Concrete evidence. ' * 1000)
        repo.results(run, raw={'code-reviewer': [bug]})
        argv = [sys.executable, str(SCRIPTS / 'ledger.py'), '--run-dir', run['run_dir'],
                '--curated', str(Path(run['run_dir']) / 'curated.json'), '--summary']
        output = repo.command(argv)
        summary = json.loads(output)
        full = repo.read_json(summary['classification'])
        self.assertEqual(summary['resumen'], full['resumen'])
        self.assertEqual(summary['report'], run['report'])
        self.assertFalse(summary['resumen']['review_complete'])
        self.assertEqual(full['hallazgos'][0]['evidence'], bug['evidence'])
        self.assertEqual(repo.records(run)[fingerprint(bug)]['evidence'], bug['evidence'])
        self.assertLess(len(output), len(json.dumps(full)) // 10)
        # An invalid re-finalization still reports the error, never a compact success.
        failure = json.loads(repo.command(argv, expected=2))
        self.assertEqual(failure['resumen']['veredicto'], 'REVISION INCOMPLETA')
        self.assertTrue(failure['resumen']['errors'])

    def test_shared_check_owner_and_empty_results_never_claim_success(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        self.assertIn('test-reviewer', run['expected_reviewers'])
        self.assertEqual(run['check_owner'], 'test-reviewer')
        checks = repo.read_json(Path(run['run_dir']) / 'shared-checks.json')
        self.assertEqual(checks['tree'], run['tree'])
        self.assertEqual(checks['head'], run['head'])
        self.assertEqual(checks['status'], 'pending')
        self.assertEqual(checks['checks'], [])
        repo.complete(run)
        unchanged = repo.prepare()
        self.assertIsNone(unchanged['check_owner'])
        self.assertEqual(unchanged['expected_reviewers'], [])
        repo.write('package-lock.json', '{"version": 2}\n')
        lock_run = repo.prepare()
        self.assertEqual(lock_run['expected_reviewers'], ['security-reviewer'])
        self.assertEqual(lock_run['check_owner'], 'security-reviewer')

    def test_ready_stops_with_followups_but_new_blockers_reopen_review(self):
        repo = self.repo()
        medium = finding()
        external = finding('external', severity='HIGH', decision_externa='infra')
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        result = repo.complete(first, raw={'code-reviewer': [medium, external]})
        self.assertTrue(result['resumen']['review_complete'])
        self.assertEqual(result['bloqueantes'], [])
        self.assertEqual(result['seguimiento'], [fingerprint(medium)])
        self.assertEqual(len(result['hallazgos']), 2)  # External risk stays visible.
        self.assertEqual(repo.records(first)[fingerprint(medium)]['estado'], 'abierto')

        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        high = finding('other_bug', severity='HIGH')
        result = repo.complete(second, raw={'code-reviewer': [high]})
        self.assertFalse(result['resumen']['review_complete'])
        self.assertEqual(result['bloqueantes'], [fingerprint(high)])
        self.assertEqual(result['seguimiento'], [fingerprint(medium)])

        repo.write('app.py', 'value = 3\n')
        third = repo.prepare()
        repo.results(third)
        Path(third['run_dir'], 'code-reviewer.json').unlink()
        result = repo.assert_rejected(third)
        self.assertFalse(result['resumen']['review_complete'])

    def test_missing_regression_test_stays_with_original_finding(self):
        repo = self.repo()
        bug = finding(severity='HIGH', fix='Validate input; test empty input returns 400.')
        first = self.seed(repo, {'code-reviewer': [bug]})
        original = fingerprint(bug)
        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        residual = finding(severity='MEDIUM', evidence='Input is guarded; regression test absent.',
                           why='Removing the guard would silently restore the bug.',
                           fix='Test empty input returns 400 and fails without the guard.')
        missing = finding(category='missing-test', severity='MEDIUM')
        # Test coverage may be delegated to code for a small delta. Schedule specialist explicitly.
        if 'test-reviewer' not in second['expected_reviewers']:
            second['expected_reviewers'].append('test-reviewer')
            second['assignments']['test-reviewer'] = []
            repo.write_json(Path(second['run_dir']) / 'run.json', second)
        decision = dict(residual, status='open', reviewer='code-reviewer',
                        detectado_por=['code-reviewer', 'test-reviewer'],
                        sources=[original, fingerprint(missing)])
        result = repo.complete(second, raw={'code-reviewer': [residual], 'test-reviewer': [missing]},
                               decisions=[decision])
        self.assertEqual(result['resumen']['conteo']['NUEVO'], 0)
        self.assertTrue(result['resumen']['review_complete'])
        self.assertEqual(result['seguimiento'], [original])
        self.assertEqual(len(result['hallazgos']), 1)
        record = repo.records(second)[original]
        self.assertEqual(record['severidad'], 'MEDIUM')
        self.assertEqual(record['fix'], residual['fix'])
        self.assertIn(fingerprint(missing), record['aliases'])

        repo.write('app.py', 'value = 3\n')
        third = repo.prepare()
        self.assertIn(original, third['assignments']['code-reviewer'])
        result = repo.complete(third, statuses={original: 'closed'})
        self.assertTrue(result['resumen']['review_complete'])
        self.assertEqual(result['seguimiento'], [])
        self.assertEqual(result['resumen']['cerrados_esta_pasada'], 1)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-state-tests-')
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.repo_count = 0

    def repo(self, extra=None):
        self.repo_count += 1
        return ReviewRepo(self, self.temp / f'repo-{self.repo_count}', extra)

    def seed(self, repo, raw=None):
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        repo.complete(run, raw=raw)
        return run

    def test_default_base_ignores_remote_head_and_production_branches(self):
        repo = self.repo()
        development = repo.git('rev-parse', 'development')
        repo.write('app.py', 'value = 1\n')
        repo.git('add', 'app.py')
        repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'feature change')
        for branch in ('main', 'master', 'prod', 'production'):
            repo.git('branch', branch)
        repo.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        repo.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        repo.git('update-ref', 'refs/remotes/origin/development', development)
        # Even a divergent local development must not supersede the remote base.
        repo.git('branch', '--force', 'development', 'HEAD')
        run = repo.prepare()
        self.assertEqual(run['base'], 'refs/remotes/origin/development')
        self.assertEqual(run['merge_base'], development)
        self.assertEqual(run['delta_files'], ['app.py'])

    def test_other_bases_and_missing_development_are_rejected(self):
        repo = self.repo()
        for branch in ('main', 'master', 'prod', 'production'):
            repo.git('branch', branch)
            for requested in (branch, f'origin/{branch}', f'refs/heads/{branch}'):
                with self.subTest(base=requested):
                    repo.command([sys.executable, str(SCRIPTS / 'prepare_review.py'),
                                  '--repo', str(repo.root), '--base', requested], expected=2)
        repo.git('branch', '-D', 'development')
        repo.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        repo.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        repo.command([sys.executable, str(SCRIPTS / 'prepare_review.py'),
                      '--repo', str(repo.root)], expected=2)
        self.assertFalse((repo.root / '.pre-pr-review').exists())

    def test_development_aliases_keep_full_then_incremental_coverage(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare('--full')
        self.assertEqual(first['base'], 'refs/heads/development')
        repo.complete(first)
        repo.git('update-ref', 'refs/remotes/origin/development', 'development')
        for alias in ('development', 'origin/development', 'refs/heads/development',
                      'refs/remotes/origin/development'):
            with self.subTest(alias=alias):
                old = repo.read_json(first['ledger'])
                old['coverage']['base'] = alias
                repo.write_json(first['ledger'], old)
                run = repo.prepare(base=alias)
                self.assertEqual(run['mode'], 'incremental')
                self.assertEqual(run['status'], 'unchanged')
                self.assertEqual(run['expected_reviewers'], [])
        repo.write('README.md', 'New documentation.\n')
        incremental = repo.prepare()
        self.assertEqual(incremental['mode'], 'incremental')
        self.assertEqual(incremental['delta_files'], ['README.md'])
        repo.complete(incremental)
        full = repo.prepare('--full')
        self.assertEqual(full['mode'], 'completo')
        self.assertEqual(set(full['delta_files']), {'README.md', 'app.py'})
        self.assertGreater(full['pass_n'], incremental['pass_n'])

    def test_development_merge_base_change_still_requires_full(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        repo.git('add', 'app.py')
        repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'reviewed feature')
        first = repo.prepare()
        repo.complete(first)
        repo.git('branch', '--force', 'development', 'HEAD')
        repo.write('README.md', 'A subsequent change.\n')
        run = repo.prepare()
        self.assertEqual(run['mode'], 'completo')
        self.assertNotEqual(run['merge_base'], first['merge_base'])
        self.assertEqual(run['pass_n'], first['pass_n'] + 1)

    def test_dirty_rerun_and_commit_of_reviewed_contents_have_no_delta(self):
        repo = self.repo()
        initial = self.seed(repo, {'code-reviewer': [finding()]})
        self.assertEqual(initial['mode'], 'completo')
        for committed in (False, True):
            with self.subTest(committed=committed):
                if committed:
                    repo.git('add', 'app.py')
                    repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'reviewed contents')
                run = repo.prepare()
                self.assertEqual(run['tree'], initial['tree'])
                self.assertEqual(run['status'], 'unchanged')
                self.assertEqual(run['delta_files'], [])
                self.assertEqual(run['expected_reviewers'], [])
                self.assertEqual(run['assignments'], {})
                self.assertEqual(Path(run['run_dir'], 'new.patch').read_text(), '')
        for option in ('--verify-pending', '--full'):
            with self.subTest(option=option):
                run = repo.prepare(option)
                self.assertEqual(run['status'], 'prepared')
                self.assertEqual(run['assignments']['code-reviewer'], [fingerprint(finding())])

    def test_committed_change_and_local_reversion_are_net_empty(self):
        repo = self.repo({'pr-reviews/old-report.md': 'previous report\n'})
        repo.write('app.py', 'value = 1\n')
        repo.git('add', 'app.py')
        repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'temporary change')
        repo.write('app.py', 'value = 0\n')
        repo.write('pr-reviews/old-report.md', 'new report\n')
        run = repo.prepare()
        self.assertEqual(run['tree'], run['base_tree'])
        self.assertEqual(run['status'], 'unchanged')
        self.assertEqual(run['delta_files'], [])
        self.assertEqual(Path(run['run_dir'], 'new.patch').read_text(), '')

    def test_snapshot_effective_contents_and_repository_are_preserved(self):
        repo = self.repo({'remove.txt': 'delete me\n'})
        repo.write('app.py', 'staged = 1\n')
        repo.git('add', 'app.py')
        repo.write('app.py', 'effective = 2\n')
        repo.write('new.py', 'untracked = True\n')
        repo.write('tracked.log', 'tracked update\n')
        repo.write('ignored.log', 'ignored untracked\n')
        (repo.root / 'old.txt').rename(repo.root / 'renamed.txt')
        (repo.root / 'remove.txt').unlink()
        for folder in ('pr-reviews', '.pre-pr-review', '.agents', 'node_modules'):
            repo.write(f'{folder}/artifact.py', 'artifact = True\n')
        index = (repo.root / '.git/index').read_bytes()
        staged = repo.git('diff', '--cached')
        local = repo.git('diff')
        head = repo.git('rev-parse', 'HEAD')
        run = repo.prepare()
        self.assertEqual(set(run['delta_files']),
                         {'app.py', 'new.py', 'tracked.log', 'old.txt', 'renamed.txt', 'remove.txt'})
        self.assertEqual(repo.git('show', f"{run['tree']}:app.py"), 'effective = 2')
        self.assertEqual(repo.git('show', f"{run['tree']}:new.py"), 'untracked = True')
        self.assertEqual(repo.git('show', f"{run['tree']}:tracked.log"), 'tracked update')
        self.assertEqual(repo.git('show', ':app.py'), 'staged = 1')
        names = repo.git('ls-tree', '-r', '--name-only', run['tree']).splitlines()
        self.assertNotIn('old.txt', names)
        self.assertNotIn('remove.txt', names)
        self.assertNotIn('ignored.log', names)
        self.assertEqual(repo.git('show', f"{run['tree']}:renamed.txt"), 'original')
        repo.complete(run)
        self.assertEqual((repo.root / '.git/index').read_bytes(), index)
        self.assertEqual(repo.git('diff', '--cached'), staged)
        self.assertEqual(repo.git('diff'), local)
        self.assertEqual(repo.git('rev-parse', 'HEAD'), head)
        self.assertEqual((repo.root / 'new.py').read_text(), 'untracked = True\n')
        self.assertEqual((repo.root / 'ignored.log').read_text(), 'ignored untracked\n')
        self.assertEqual(repo.prepare()['status'], 'unchanged')

    def test_lockfile_only_routes_security_with_empty_shared_patch(self):
        repo = self.repo()
        repo.write('package-lock.json', '{"version": 2}\n')
        run = repo.prepare()
        self.assertEqual(run['status'], 'prepared')
        self.assertEqual(run['expected_reviewers'], ['security-reviewer'])
        self.assertEqual(Path(run['run_dir'], 'new.patch').read_text(), '')
        dependencies = repo.read_json(Path(run['run_dir']) / 'dependencies.json')
        self.assertEqual(dependencies['files'], ['package-lock.json'])
        self.assertEqual(dependencies['before_tree'], run['since_tree'])
        self.assertEqual(dependencies['after_tree'], run['tree'])
        self.assertIn('+{"version": 2}', repo.git('diff', dependencies['before_tree'],
                                                 dependencies['after_tree'], '--', 'package-lock.json'))
        repo.complete(run)

    def test_routing_uses_current_delta_and_escalates_tests_and_risky_code(self):
        repo = self.repo({'package.json': '{"dependencies":{"next":"1.0.0"}}\n',
                          'src/page.ts': 'const value = 0;\n', 'go.mod': 'module example.invalid/app\n',
                          'internal/core/domain/model.go': 'package domain\nvar value = 0\n'})
        repo.write('src/page.ts', 'const value = 1;\n')
        repo.write('internal/core/domain/model.go', 'package domain\nvar value = 1\n')
        full = repo.prepare()
        self.assertTrue({'code-reviewer', 'security-reviewer', 'edge-case-reviewer',
                         'regression-reviewer', 'test-reviewer', 'nextjs-architecture-reviewer',
                         'go-architecture-reviewer'} <= set(full['expected_reviewers']))
        repo.complete(full)
        cases = [
            ('README.md', 'Current documentation.\n', {'security-reviewer'},
             {'code-reviewer', 'nextjs-architecture-reviewer', 'go-architecture-reviewer'}),
            ('app.py', 'value = 2\n', {'code-reviewer', 'security-reviewer'},
             {'test-reviewer', 'edge-case-reviewer', 'regression-reviewer'}),
            ('tests/test_app.py', 'def test_value():\n    assert 2 == 2\n',
             {'code-reviewer', 'security-reviewer', 'test-reviewer'}, set()),
            ('app.py', 'async def fetch():\n    return 2\n',
             {'code-reviewer', 'security-reviewer', 'test-reviewer', 'edge-case-reviewer',
              'regression-reviewer'}, set()),
        ]
        for name, contents, required, skipped in cases:
            with self.subTest(path=name, contents=contents):
                repo.write(name, contents)
                run = repo.prepare()
                self.assertEqual(run['mode'], 'incremental')
                self.assertEqual(run['delta_files'], [name])
                self.assertTrue(required <= set(run['expected_reviewers']))
                self.assertTrue(skipped <= set(run['skipped_reviewers']))
                self.assertTrue(all(run['reviewer_reasons'][r] for r in required))
                self.assertTrue(all(run['skipped_reviewers'][r] for r in skipped))
                if name == 'README.md':
                    self.assertNotIn('src/page.ts', Path(run['run_dir'], 'new.patch').read_text())
                    self.assertIn('src/page.ts', Path(run['run_dir'], 'full.patch').read_text())
                repo.complete(run)

    def test_full_fallbacks_assign_pending_with_empty_delta_and_preserve_history(self):
        for cause in ('legacy', 'missing-tree', 'rewritten-head', 'changed-base'):
            with self.subTest(cause=cause):
                repo = self.repo()
                repo.write('app.py', 'value = 1\n')
                repo.git('add', 'app.py')
                repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'feature')
                first = repo.prepare()
                repo.complete(first, raw={'code-reviewer': [finding()]})
                repo.write('app.py', 'value = 0\n')
                old = repo.read_json(first['ledger'])
                base = 'development'
                if cause == 'legacy':
                    old.pop('schema_version')
                    old.pop('coverage')
                    old['pasadas'][0]['n'] = 8
                    repo.write_json(first['ledger'], old)
                elif cause == 'missing-tree':
                    old['coverage']['tree'] = '0' * 40
                    repo.write_json(first['ledger'], old)
                elif cause == 'rewritten-head':
                    repo.git('commit', '--amend', '--quiet', '--no-gpg-sign', '-m', 'rewritten feature')
                else:
                    old['coverage']['base'] = 'main'
                    repo.write_json(first['ledger'], old)
                run = repo.prepare(base=base)
                self.assertEqual(run['mode'], 'completo')
                self.assertEqual(run['status'], 'prepared')
                self.assertEqual(run['delta_files'], [])
                self.assertEqual(run['pass_n'], max(p['n'] for p in old['pasadas']) + 1)
                self.assertEqual(run['assignments'], {'code-reviewer': [fingerprint(finding())]})
                pending = repo.read_json(Path(run['run_dir']) / 'pending/code-reviewer.json')
                self.assertEqual(pending, old['hallazgos'])
                repo.complete(run)
                new = repo.read_json(run['ledger'])
                self.assertEqual(new['pasadas'][:-1], old['pasadas'])
                self.assertEqual(new['hallazgos'][0]['history'][:-1], old['hallazgos'][0]['history'])
                self.assertEqual(new['hallazgos'][0]['estado'], 'abierto')

    def test_incomplete_results_never_close_findings_or_advance_coverage(self):
        repo = self.repo()
        old_code, old_security = finding('old_code'), finding('old_security', category='security')
        first = self.seed(repo, {'code-reviewer': [old_code], 'security-reviewer': [old_security]})
        new_code, new_security = finding('new_code'), finding('new_security', category='security')
        cases = ('missing-reviewer', 'malformed-reviewer', 'duplicate-identity', 'invalid-finding',
                 'duplicate-finding', 'missing-verification', 'duplicate-verification',
                 'omitted-source', 'duplicated-source', 'omitted-curated-verification',
                 'changed-curated-verification')
        for defect in cases:
            with self.subTest(defect=defect):
                run = repo.prepare('--verify-pending')
                curated = repo.results(run, raw={'code-reviewer': [new_code],
                                                'security-reviewer': [new_security]},
                                       statuses={fingerprint(old_code): 'closed'})
                path = Path(run['run_dir']) / 'security-reviewer.json'
                reviewer = repo.read_json(path)
                if defect == 'missing-reviewer':
                    path.unlink()
                elif defect == 'malformed-reviewer':
                    path.write_text('{')
                elif defect == 'duplicate-identity':
                    reviewer['reviewer'] = 'code-reviewer'
                elif defect == 'invalid-finding':
                    reviewer['findings'][0]['severity'] = 'URGENT'
                elif defect == 'duplicate-finding':
                    reviewer['findings'].append(copy.deepcopy(reviewer['findings'][0]))
                elif defect == 'missing-verification':
                    reviewer['verifications'] = []
                elif defect == 'duplicate-verification':
                    reviewer['verifications'].append(copy.deepcopy(reviewer['verifications'][0]))
                elif defect == 'omitted-source':
                    curated['findings'].pop()
                elif defect == 'duplicated-source':
                    curated['findings'][1]['sources'] = curated['findings'][0]['sources']
                    curated['findings'][1]['reviewer'] = 'code-reviewer'
                    curated['findings'][1]['detectado_por'] = ['code-reviewer']
                elif defect == 'omitted-curated-verification':
                    curated['verifications'].pop()
                else:
                    curated['verifications'][0]['evidence'] = 'Different aggregator evidence.'
                if defect not in {'missing-reviewer', 'malformed-reviewer'}:
                    repo.write_json(path, reviewer)
                repo.write_json(Path(run['run_dir']) / 'curated.json', curated)
                repo.assert_rejected(run)
                self.assertEqual(repo.records(first)[fingerprint(old_code)]['estado'], 'abierto')
        # A corrected incomplete run can finish; an unscheduled artifact supplies no coverage.
        repo.results(run, statuses={fingerprint(old_code): 'closed'})
        Path(run['run_dir'], 'nextjs-architecture-reviewer.json').write_text('{stale invalid artifact')
        result = repo.finalize(run)
        self.assertEqual(result['resumen']['cerrados_esta_pasada'], 1)
        self.assertEqual(repo.records(run)[fingerprint(old_security)]['estado'], 'abierto')

    def test_final_decisions_and_all_historical_states_survive_later_passes(self):
        repo = self.repo()
        close, sibling, discarded, accepted, external, adjusted = (
            finding('close'), finding('sibling'), finding('discarded'), finding('accepted'),
            finding('external', severity='HIGH', decision_externa='Requires provider decision.'),
            finding('adjusted', severity='HIGH'))
        repo.write_json(repo.root / 'pr-reviews/accepted.json', {
            'aceptados': [{'huella': fingerprint(accepted), 'techo_severidad': 'MEDIUM'}]})
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        curated = repo.results(run, raw={'code-reviewer': [close, sibling, discarded, accepted,
                                                         external, adjusted]})
        by_id = {fingerprint(f): f for f in curated['findings']}
        by_id[fingerprint(discarded)].update(status='discarded', discard_reason='The caller already validates.')
        final_fields = {'severity': 'LOW', 'evidence': 'app.py:1 the validated impact is limited.',
                        'fix': 'Add a fallback for the optional display value.'}
        by_id[fingerprint(adjusted)].update(final_fields)
        repo.write_json(Path(run['run_dir']) / 'curated.json', curated)
        repo.finalize(run)
        second = repo.prepare('--verify-pending')
        result = repo.complete(second, statuses={fingerprint(close): 'closed'})
        self.assertEqual(result['resumen']['cerrados_esta_pasada'], 1)
        third = repo.prepare('--verify-pending')
        result = repo.complete(third)
        records = repo.records(third)
        expected_states = [(close, 'cerrado'), (sibling, 'abierto'), (discarded, 'descartado'),
                           (accepted, 'aceptado'), (external, 'abierto'), (adjusted, 'abierto')]
        for original, status in expected_states:
            with self.subTest(symbol=original['symbol']):
                self.assertEqual(records[fingerprint(original)]['estado'], status)
        for field, value in final_fields.items():
            self.assertEqual(records[fingerprint(adjusted)][field], value)
        self.assertEqual(records[fingerprint(adjusted)]['severidad'], 'LOW')
        self.assertEqual(records[fingerprint(external)]['decision_externa'], external['decision_externa'])
        self.assertEqual(records[fingerprint(discarded)]['discard_reason'], 'The caller already validates.')
        self.assertEqual(result['resumen']['severity_counts']['HIGH'], 0)
        self.assertEqual(result['resumen']['suprimidos_por_aceptado'], 1)
        self.assertEqual(result['resumen']['veredicto'], 'LISTO PARA PR')
        self.assertEqual(records[fingerprint(close)]['last_verification']['status'], 'closed')
        self.assertTrue(records[fingerprint(close)]['last_verification']['evidence'])

    def test_only_verified_closure_is_regression_and_shared_location_is_not_causal(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                repo = self.repo()
                original = finding()
                first = self.seed(repo, {'code-reviewer': [original]})
                if verified:
                    closed = repo.prepare('--verify-pending')
                    repo.complete(closed, statuses={fingerprint(original): 'closed'})
                else:
                    # Old ledgers inferred closure from coverage, with no reviewer evidence.
                    old = repo.read_json(first['ledger'])
                    old.pop('schema_version')
                    old.pop('coverage')
                    old['hallazgos'][0].update(estado='cerrado', cerrado_en=1)
                    repo.write_json(first['ledger'], old)
                run = repo.prepare('--full')
                same_symbol = finding(category='security')
                same_category = finding(symbol='other_function')
                result = repo.complete(run, raw={'code-reviewer': [original, same_symbol, same_category]})
                counts = result['resumen']['conteo']
                self.assertEqual(counts['REGRESION'], int(verified))
                self.assertEqual(counts['REINCIDENTE'], int(not verified))
                self.assertEqual(counts['NUEVO'], 2)
                self.assertEqual(counts['INTRODUCIDO_POR'], 0)
                self.assertEqual(repo.records(first)[fingerprint(original)]['estado'], 'abierto')

    def test_acceptance_ceiling_applies_to_carried_and_updated_findings(self):
        repo = self.repo()
        original = finding()
        acceptance_path = repo.root / 'pr-reviews/accepted.json'
        acceptance = {'categoria': original['category'], 'archivo': original['file'],
                      'simbolo': original['symbol'], 'techo_severidad': 'MEDIUM'}
        repo.write_json(acceptance_path, {'aceptados': [acceptance]})
        run = self.seed(repo, {'code-reviewer': [original]})
        self.assertEqual(repo.records(run)[fingerprint(original)]['estado'], 'aceptado')
        for ceiling, updated in [('LOW', None), ('MEDIUM', finding(severity='HIGH'))]:
            with self.subTest(ceiling=ceiling, updated=bool(updated)):
                acceptance['techo_severidad'] = ceiling
                repo.write_json(acceptance_path, {'aceptados': [acceptance]})
                run = repo.prepare('--verify-pending')
                result = repo.complete(run, raw={'code-reviewer': [updated]} if updated else None)
                record = repo.records(run)[fingerprint(original)]
                self.assertEqual(record['estado'], 'abierto')
                self.assertEqual(record['rompe_techo_aceptado'],
                                 {'techo': ceiling, 'ahora': 'HIGH' if updated else 'MEDIUM'})
                self.assertEqual(result['resumen']['suprimidos_por_aceptado'], 0)
                self.assertNotIn('aceptado', record)

    def test_alias_chain_preserves_acceptance_and_history_after_reload(self):
        repo = self.repo()
        a, b, c = (finding(name) for name in ('alias_a', 'alias_b', 'alias_c'))
        repo.write_json(repo.root / 'pr-reviews/accepted.json', {
            'aceptados': [{'huella': fingerprint(a), 'techo_severidad': 'MEDIUM'}]})
        first = self.seed(repo, {'code-reviewer': [a]})
        initial_history = repo.records(first)[fingerprint(a)]['history']
        previous = a
        for current in (b, c):
            run = repo.prepare('--verify-pending')
            curated = repo.results(run, raw={'code-reviewer': [current]})
            curated['findings'][0]['aliases'] = [fingerprint(previous)]
            repo.write_json(Path(run['run_dir']) / 'curated.json', curated)
            repo.finalize(run)
            self.assertEqual(repo.records(run)[fingerprint(current)]['estado'], 'aceptado')
            previous = current
        run = repo.prepare('--verify-pending')
        self.assertEqual(run['assignments'], {'code-reviewer': [fingerprint(c)]})
        repo.complete(run)
        ledger = repo.read_json(run['ledger'])
        records = repo.records(run)
        self.assertEqual(ledger['aliases'], {fingerprint(a): fingerprint(c), fingerprint(b): fingerprint(c)})
        self.assertEqual(set(records[fingerprint(c)]['aliases']), {fingerprint(a), fingerprint(b)})
        self.assertEqual(records[fingerprint(c)]['estado'], 'aceptado')
        self.assertEqual(records[fingerprint(a)]['history'][:len(initial_history)], initial_history)
        for merged in (a, b):
            record = records[fingerprint(merged)]
            self.assertEqual(record['estado'], 'descartado')
            self.assertEqual(record['merged_into'], fingerprint(c))
            self.assertTrue(any('decision' in event for event in record['history']))
            self.assertTrue(any('merged_into' in event for event in record['history']))

    def test_stale_worktree_head_and_ledger_reject_without_advancing_baseline(self):
        for changed in ('worktree', 'head', 'ledger'):
            with self.subTest(changed=changed):
                repo = self.repo()
                first = self.seed(repo, {'code-reviewer': [finding()]})
                old_coverage = repo.read_json(first['ledger'])['coverage']
                repo.write('app.py', 'value = 2\n')
                run = repo.prepare()
                repo.results(run, statuses={fingerprint(finding()): 'closed'})
                if changed == 'worktree':
                    repo.write('app.py', 'value = 3\n')
                elif changed == 'head':
                    repo.git('add', 'app.py')
                    repo.git('commit', '--quiet', '--no-gpg-sign', '-m', 'commit during review')
                else:
                    ledger = repo.read_json(first['ledger'])
                    ledger['audit_note'] = 'Updated by another finalization owner.'
                    repo.write_json(first['ledger'], ledger)
                repo.assert_rejected(run)
                self.assertEqual(repo.read_json(first['ledger'])['coverage'], old_coverage)
                self.assertEqual(repo.records(first)[fingerprint(finding())]['estado'], 'abierto')


if __name__ == '__main__':
    unittest.main()
