"""Resume, locking, discards, rule coverage, shared checks, metrics, retention and evals.

Same public CLIs and real repositories as test_review_state; no model or network calls.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from test_review_state import SCRIPTS, ReviewRepo, finding, fingerprint


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-extension-tests-')
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.repo_count = 0

    def repo(self):
        self.repo_count += 1
        return ReviewRepo(self, self.temp / f'repo-{self.repo_count}')

    def script(self, repo, name, *args, expected=0):
        return repo.command([sys.executable, str(SCRIPTS / name), *args], expected=expected)

    def curated_with(self, repo, run, raw, **changes):
        curated = repo.results(run, raw={'code-reviewer': [raw]})
        curated['findings'][0].update(changes)
        repo.write_json(Path(run['run_dir']) / 'curated.json', curated)

    # ---- resume -------------------------------------------------------------------------
    def test_interrupted_run_resumes_only_missing_reviewers_until_code_changes(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        self.assertFalse(first['resumed'])
        repo.write_json(Path(first['run_dir']) / 'security-reviewer.json', {
            'reviewer': 'security-reviewer', 'scope': 'delta', 'sin_hallazgos_en': [],
            'findings': [], 'verifications': []})
        # A result with the wrong identity is not coverage: that reviewer still has to run.
        repo.write_json(Path(first['run_dir']) / 'code-reviewer.json', {
            'reviewer': 'test-reviewer', 'scope': 'delta', 'sin_hallazgos_en': [],
            'findings': [], 'verifications': []})
        again = repo.prepare()
        self.assertTrue(again['resumed'])
        self.assertEqual(again['run_dir'], first['run_dir'])
        self.assertEqual(again['completed_reviewers'], ['security-reviewer'])
        self.assertIn('code-reviewer', again['pending_reviewers'])
        self.assertNotIn('security-reviewer', again['pending_reviewers'])
        self.assertEqual(again['shared_checks_status'], 'pending')
        self.assertEqual(len(list((repo.root / '.pre-pr-review').iterdir())), 1)
        self.assertNotEqual(repo.prepare('--no-resume')['run_dir'], first['run_dir'])
        repo.write('app.py', 'value = 2\n')
        changed = repo.prepare()
        self.assertFalse(changed['resumed'])
        self.assertNotEqual(changed['run_dir'], first['run_dir'])
        # The resumed directory finalizes like any other run.
        repo.write('app.py', 'value = 1\n')
        resumed = repo.prepare()
        self.assertEqual(resumed['run_dir'], first['run_dir'])
        repo.complete(resumed)
        self.assertEqual(repo.prepare()['status'], 'unchanged')

    # ---- concurrent finalization ---------------------------------------------------------
    def test_second_finalization_waits_out_and_stale_lock_never_blocks_forever(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        repo.results(run)
        common = Path(repo.git('rev-parse', '--path-format=absolute', '--git-common-dir'))
        key = hashlib.sha256(str(Path(run['ledger']).resolve()).encode()).hexdigest()[:24]
        lock = common / 'pre-pr-review' / f'{key}.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('12345\n')
        result = repo.finalize(run, expected=2)
        self.assertIn('Another finalization', result['resumen']['errors'][0])
        self.assertFalse(Path(run['ledger']).exists())
        self.assertTrue(lock.exists())  # Someone else's lock is never removed while fresh.
        old = time.time() - 3600
        os.utime(lock, (old, old))
        repo.finalize(run)
        self.assertFalse(lock.exists())
        self.assertEqual(repo.git('status', '--porcelain', '--', 'pr-reviews').count('.lock'), 0)

    # ---- discards ------------------------------------------------------------------------
    def test_discard_is_context_and_reopens_only_with_new_evidence(self):
        repo = self.repo()
        dismissed = finding('dismissed')
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        self.assertFalse((Path(first['run_dir']) / 'discarded.json').exists())
        self.curated_with(repo, first, dismissed, status='discarded', discard_reason='The caller validates.')
        repo.finalize(first)
        record = repo.records(first)[fingerprint(dismissed)]
        self.assertEqual(record['discard_pass'], 1)
        self.assertEqual(record['discard_blob'], repo.git('rev-parse', f"{first['tree']}:app.py"))

        repo.write('old.txt', 'unrelated\n')
        unrelated = repo.prepare()
        self.assertEqual(unrelated['discarded_context'], 0)  # Not in this window: no prompt cost.
        repo.complete(unrelated)

        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        context = repo.read_json(Path(second['run_dir']) / 'discarded.json')['discarded']
        self.assertEqual([(c['huella'], c['discard_reason'], c['file_changed_since_discard']) for c in context],
                         [(fingerprint(dismissed), 'The caller validates.', True)])
        self.assertEqual(second['assignments']['code-reviewer'], [])  # Context, never a mandatory revalidation.
        self.curated_with(repo, second, dismissed)
        error = repo.assert_rejected(second)['resumen']['errors'][0]
        self.assertIn('new_evidence', error)
        self.assertIn('The caller validates.', error)
        # Repeating the discard needs no new evidence and is the cheap path for a known false positive.
        self.curated_with(repo, second, dismissed, status='discarded', discard_reason='Still validated upstream.')
        repo.finalize(second)
        self.assertEqual(repo.records(second)[fingerprint(dismissed)]['estado'], 'descartado')

        repo.write('app.py', 'value = 3\n')
        third = repo.prepare()
        self.curated_with(repo, third, dismissed, new_evidence='app.py:1 the caller no longer validates the input.')
        result = repo.finalize(third)
        record = repo.records(third)[fingerprint(dismissed)]
        self.assertEqual(record['estado'], 'abierto')
        self.assertEqual(record['reabierto_tras_descarte'], [4])
        self.assertEqual(record['new_evidence'], 'app.py:1 the caller no longer validates the input.')
        self.assertEqual(result['metricas']['reabiertos_tras_descarte'], 1)

    # ---- rule changes --------------------------------------------------------------------
    def age_rules(self, repo, run, *reviewers):
        ledger = repo.read_json(run['ledger'])
        for reviewer in reviewers:
            rules = ledger['coverage']['rules'][reviewer]
            rules[f'agents/{reviewer}.md'] = 'previous-version'
        repo.write_json(run['ledger'], ledger)

    def test_rule_change_is_reported_and_only_a_user_decision_renews_coverage(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        self.assertEqual(first['rules_changed'], {})
        repo.complete(first)
        current = repo.read_json(first['ledger'])['coverage']['rules']
        self.assertEqual(set(current), set(first['rules']))
        self.age_rules(repo, first, 'security-reviewer', 'go-architecture-reviewer')

        idle = repo.prepare()  # No delta: still nothing to launch, but the change is visible.
        self.assertEqual(idle['status'], 'unchanged')
        self.assertEqual(idle['rules_changed'], {'security-reviewer': ['agents/security-reviewer.md']})
        self.assertEqual(idle['rules_accepted'], ['go-architecture-reviewer'])  # No Go in this branch.

        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        self.assertEqual(second['mode'], 'incremental')
        self.assertEqual(list(second['rules_changed']), ['security-reviewer'])
        self.assertEqual(second['full_window_reviewers'], [])
        repo.complete(second)
        rules = repo.read_json(second['ledger'])['coverage']['rules']
        # Reviewing only the delta with new rules does not cover the older content.
        self.assertEqual(rules['security-reviewer']['agents/security-reviewer.md'], 'previous-version')
        self.assertEqual(rules['go-architecture-reviewer'], current['go-architecture-reviewer'])

        recheck = repo.prepare('--recheck-rules')  # No delta: only the affected reviewer, whole branch.
        self.assertEqual(recheck['status'], 'prepared')
        self.assertEqual(recheck['mode'], 'incremental')
        self.assertEqual(recheck['expected_reviewers'], ['security-reviewer'])
        self.assertEqual(recheck['full_window_reviewers'], ['security-reviewer'])
        self.assertEqual(recheck['rules_changed'], {})
        self.assertIn('value = 2', (Path(recheck['run_dir']) / 'full.patch').read_text())
        repo.complete(recheck)
        ledger = repo.read_json(recheck['ledger'])
        self.assertEqual(ledger['coverage']['rules'], current)
        self.assertEqual(ledger['pasadas'][-1]['full_window_reviewers'], ['security-reviewer'])
        self.assertEqual(repo.prepare()['rules_changed'], {})

    def test_keeping_rule_coverage_is_recorded_and_legacy_ledgers_never_warn(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        repo.complete(first)
        self.age_rules(repo, first, 'code-reviewer')
        repo.write('app.py', 'value = 2\n')
        kept = repo.prepare('--keep-rules-coverage')
        self.assertEqual(kept['rules_changed'], {})
        self.assertEqual(kept['rules_accepted'], ['code-reviewer'])
        repo.complete(kept)
        ledger = repo.read_json(kept['ledger'])
        self.assertEqual(ledger['coverage']['rules'], kept['rules'])
        self.assertEqual(ledger['pasadas'][-1]['rules_accepted'], ['code-reviewer'])

        del ledger['coverage']['rules']  # Ledger written before rules were tracked.
        repo.write_json(kept['ledger'], ledger)
        repo.write('app.py', 'value = 3\n')
        legacy = repo.prepare()
        self.assertEqual((legacy['mode'], legacy['rules_changed'], legacy['rules_accepted']), ('incremental', {}, []))
        repo.complete(legacy)
        self.assertEqual(repo.read_json(legacy['ledger'])['coverage']['rules'], legacy['rules'])

    # ---- shared checks -------------------------------------------------------------------
    def test_shared_checks_are_validated_without_running_or_changing_the_verdict(self):
        repo = self.repo()
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        run_dir = Path(run['run_dir'])
        path = run_dir / 'shared-checks.json'

        def validate(expected):
            return json.loads(self.script(repo, 'shared_checks.py', '--run-dir', str(run_dir), expected=expected))

        self.assertEqual(validate(0)['status'], 'pending')
        self.assertFalse(repo.read_json(path)['validation']['reusable'])
        check = {'command': ['python3', '-m', 'unittest'], 'cwd': '.', 'scope': 'Unit tests of the module.',
                 'environment': 'Local, no overrides.', 'status': 'passed', 'exit_code': 0,
                 'evidence': '12 tests passed.', 'log': 'checks/tests.log', 'tests_run': 12}
        base = dict(repo.read_json(path), status='complete')
        cases = {
            'contradicts exit code': dict(check, exit_code=1),
            'no log for an executed command': dict(check, log=None),
            'log outside the run': dict(check, log='../../app.py'),
            'zero tests prove nothing': dict(check, tests_run=0),
            'failed with success code': dict(check, status='failed'),
        }
        (run_dir / 'checks').mkdir()
        (run_dir / 'checks' / 'tests.log').write_text('Ran 12 tests\nOK\n')
        for label, broken in cases.items():
            with self.subTest(label):
                repo.write_json(path, dict(base, checks=[broken]))
                self.assertEqual(validate(1)['status'], 'invalid')
                self.assertFalse(repo.read_json(path)['validation']['reusable'])
        for label, document in {'another snapshot': dict(base, tree='0' * 40, checks=[check]),
                                'another owner': dict(base, owner='code-reviewer', checks=[check]),
                                'no checks and no reason': dict(base, checks=[])}.items():
            with self.subTest(label):
                repo.write_json(path, document)
                self.assertEqual(validate(1)['status'], 'invalid')
        failing = dict(check, status='failed', exit_code=1, evidence='2 of 12 tests failed.')
        repo.write_json(path, dict(base, checks=[check, failing]))
        self.assertEqual(validate(0), {'status': 'valid', 'errors': []})  # A red build is still a true fact.
        self.assertTrue(repo.read_json(path)['validation']['reusable'])
        self.assertEqual(repo.complete(run)['resumen']['shared_checks'], 'valid')

        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        repo.write_json(Path(second['run_dir']) / 'shared-checks.json',
                        dict(base, tree=second['tree'], head=second['head'], checks=[dict(check, exit_code=3)]))
        result = repo.complete(second)  # Reported, never turned into an incomplete review.
        self.assertEqual(result['resumen']['shared_checks'], 'invalid')
        self.assertTrue(result['resumen']['coverage_complete'])

    # ---- metrics -------------------------------------------------------------------------
    def test_metrics_count_false_positives_duplicates_and_rounds_to_approval(self):
        repo = self.repo()
        blocker, noise, twin = finding('blocker', severity='HIGH'), finding('noise'), finding('twin')
        repo.write('app.py', 'value = 1\n')
        first = repo.prepare()
        curated = repo.results(first, raw={'code-reviewer': [blocker, noise, twin]})
        by_id = {fingerprint(f): f for f in curated['findings']}
        by_id[fingerprint(noise)].update(status='discarded', discard_reason='Guarded by the caller.')
        by_id[fingerprint(blocker)]['sources'] = [fingerprint(blocker), fingerprint(twin)]
        curated['findings'].remove(by_id[fingerprint(twin)])
        repo.write_json(Path(first['run_dir']) / 'curated.json', curated)
        metrics = repo.finalize(first)['metricas']
        self.assertEqual((metrics['pasadas'], metrics['rondas_hasta_aprobacion']), (1, None))
        self.assertEqual((metrics['hallazgos_unicos'], metrics['falsos_positivos']), (2, 1))
        self.assertEqual(metrics['tasa_falsos_positivos'], 0.5)
        entry = repo.read_json(first['ledger'])['pasadas'][0]
        self.assertEqual((entry['review_complete'], entry['nuevos'], entry['descartados'], entry['fusionados']),
                         (False, 1, 1, 1))
        self.assertEqual(entry['plugin_version'], first['plugin_version'])

        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        metrics = repo.complete(second, statuses={fingerprint(blocker): 'closed'})['metricas']
        self.assertEqual((metrics['pasadas'], metrics['rondas_hasta_aprobacion']), (2, 2))
        self.assertEqual(metrics['por_estado'], {'abierto': 0, 'cerrado': 1, 'aceptado': 0, 'descartado': 1})

        ledger = repo.read_json(second['ledger'])
        del ledger['pasadas'][0]['review_complete']  # History older than the counter is not guessed.
        repo.write_json(second['ledger'], ledger)
        repo.write('app.py', 'value = 3\n')
        metrics = repo.complete(repo.prepare())['metricas']
        self.assertEqual((metrics['aprobacion_medible'], metrics['rondas_hasta_aprobacion']), (False, None))

    # ---- retention -----------------------------------------------------------------------
    def age(self, run, days):
        stamp = time.time() - days * 86400
        os.utime(Path(run['run_dir']) / 'run.json', (stamp, stamp))

    def test_retention_removes_only_old_finished_runs_and_superseded_refs(self):
        repo = self.repo()
        runs = []
        for value in range(1, 9):
            repo.write('app.py', f'value = {value}\n')
            run = repo.prepare('--no-cleanup')
            repo.complete(run)
            runs.append(run)
        repo.write('app.py', 'value = 99\n')
        abandoned = repo.prepare('--no-cleanup')       # Ledger moved on: can never be finalized.
        repo.write('app.py', 'value = 9\n')
        repo.complete(repo.prepare('--no-cleanup', '--no-resume'))
        repo.write('app.py', 'value = 10\n')
        waiting = repo.prepare('--no-cleanup')         # Still finalizable against the current ledger.
        for run in [*runs, abandoned, waiting]:
            self.age(run, 90)
        namespace = hashlib.sha256(b'feature').hexdigest()[:16]
        other = f'refs/pre-pr-review/{hashlib.sha256(b"other-branch").hexdigest()[:16]}/{runs[0]["tree"]}'
        repo.git('update-ref', other, runs[0]['tree'])
        refs_before = repo.git('for-each-ref', '--format=%(refname)', 'refs/pre-pr-review/').splitlines()
        self.assertEqual(len(refs_before), 10)

        dry = json.loads(self.script(repo, 'retention.py', '--repo', str(repo.root)))
        self.assertFalse(dry['applied'])
        self.assertTrue(all(Path(r['run_dir']).exists() for r in runs))

        disabled = repo.env.copy()
        repo.env['PRE_PR_RETENTION_DAYS'] = '0'
        self.assertEqual(repo.prepare('--no-resume')['cleanup'],
                         {'enabled': False, 'runs_removed': 0, 'refs_removed': 0})
        repo.env = disabled

        result = repo.prepare('--no-resume')
        removed = set(result['cleanup']['runs'])
        self.assertTrue(removed.isdisjoint({Path(result['run_dir']).name, Path(waiting['run_dir']).name}))
        self.assertTrue(Path(abandoned['run_dir']).exists())         # Among the newest of its branch.
        strict = json.loads(self.script(repo, 'retention.py', '--repo', str(repo.root), '--keep', '0', '--apply'))
        self.assertIn(Path(abandoned['run_dir']).name, strict['runs'])
        self.assertFalse(Path(abandoned['run_dir']).exists())
        self.assertTrue(Path(waiting['run_dir']).exists())           # Old, but it can still be finalized.
        self.assertTrue(Path(result['run_dir']).exists())            # Recent runs are never age candidates.
        self.assertFalse(Path(runs[0]['run_dir']).exists())
        refs = repo.git('for-each-ref', '--format=%(refname)', 'refs/pre-pr-review/').splitlines()
        self.assertIn(other, refs)                                   # Another branch is never judged from here.
        coverage = repo.read_json(result['ledger'])['coverage']
        self.assertIn(coverage['snapshot_ref'], refs)
        self.assertNotIn(f'refs/pre-pr-review/{namespace}/{runs[0]["tree"]}', refs)
        self.assertEqual(repo.git('rev-parse', coverage['snapshot_ref']), coverage['tree'])
        self.assertEqual(result['mode'], 'incremental')              # Cleanup never costs a full review.

    # ---- evals ---------------------------------------------------------------------------
    def test_eval_fixture_scores_seeded_bugs_decoys_and_gates_quality_loss(self):
        repo = self.repo()  # Only for its isolated Git environment.
        dest = self.temp / 'fixture'
        setup = json.loads(self.script(repo, 'eval_review.py', 'setup', '--case', 'go-orders', '--dest', str(dest)))
        self.assertEqual(setup['seeded_bugs'], 4)
        self.script(repo, 'eval_review.py', 'setup', '--case', 'go-orders', '--dest', str(dest), expected=2)
        prepared = json.loads(self.script(repo, 'prepare_review.py', '--repo', str(dest)))
        self.assertEqual(prepared['branch'], 'feature/eval')
        self.assertIn('go-architecture-reviewer', prepared['expected_reviewers'])
        adapter = 'internal/core/orders/adapters/postgres/repository.go'

        def record(symbol, file, line, title, category, severity='HIGH', estado='abierto'):
            return {'huella': hashlib.sha1(f'{category}{file}{symbol}'.encode()).hexdigest()[:10], 'estado': estado,
                    'severidad': severity, 'archivo': file, 'simbolo': symbol, 'linea': line, 'titulo': title,
                    'categoria': category, 'evidence': title, 'why': title}
        found = [record('ByCustomer', adapter, 27, 'Query armada con fmt.Sprintf', 'sql-injection', 'BLOCKER'),
                 record('TotalsByCustomer', 'internal/core/orders/services/totals.go', 20, 'Data race en map', 'race-condition', 'MEDIUM'),
                 record('ByID', adapter, 21, 'Posible SQL injection', 'sql-injection'),
                 record('New', adapter, 15, 'Falta validar db nil', 'nil-check', 'LOW'),
                 record('ByCustomer', adapter, 30, 'rows sin Close', 'resource-leak', estado='descartado')]
        ledger = dest / 'pr-reviews' / 'pr-feature-eval-x' / 'ledger.json'
        repo.write_json(ledger, {'hallazgos': found, 'pasadas': [{'n': 1, 'plugin_version': '9.9.9'}]})
        repo.write_json(ledger.parent / 'usage' / 'p1-abcdef12.json',
                        {'plugin_version': '9.9.9', 'observed_tokens': 1000, 'orchestrator_tokens': 50, 'status': 'recorded'})
        before = self.temp / 'before.json'
        score = json.loads(self.script(repo, 'eval_review.py', 'score', '--case', 'go-orders',
                                       '--repo', str(dest), '--out', str(before)))
        self.assertEqual(sorted(d['id'] for d in score['detected']), ['concurrent-map-write', 'sql-injection-by-customer'])
        self.assertEqual(sorted(score['missed']), ['domain-imports-adapter', 'ignored-query-error'])  # Discarded is not detected.
        self.assertEqual(score['recall'], 0.5)
        self.assertEqual(score['severity_underrated'], ['concurrent-map-write'])
        self.assertEqual([d['id'] for d in score['decoy_hits']], ['parametrized-by-id'])
        self.assertEqual([u['symbol'] for u in score['unexpected']], ['New'])
        self.assertEqual((score['plugin_version'], score['usage']['observed_tokens']), ('9.9.9', 1000))

        repo.write_json(ledger, {'hallazgos': found[1:], 'pasadas': [{'n': 1}]})
        after = self.temp / 'after.json'
        self.script(repo, 'eval_review.py', 'score', '--case', 'go-orders', '--repo', str(dest), '--out', str(after))
        worse = json.loads(self.script(repo, 'eval_review.py', 'compare', str(before), str(after), expected=1))
        self.assertFalse(worse['quality_preserved'])
        self.assertIn('sql-injection-by-customer', worse['quality_regressions'][0])
        same = json.loads(self.script(repo, 'eval_review.py', 'compare', str(before), str(before)))
        self.assertTrue(same['quality_preserved'])


if __name__ == '__main__':
    unittest.main()
