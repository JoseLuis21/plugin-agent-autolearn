"""Synthetic Claude JSONL + real hook/summary CLI tests; no API calls or account logs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SCRIPT = SCRIPTS / 'review_usage.py'


class UsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-usage-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        self.run = self.new_run('first')
        self.transcript = self.root / 'agent.jsonl'
        self.event = dict(hook_event_name='SubagentStop', cwd=str(self.root), session_id='session',
                          agent_id='one', agent_type='agent-autolearn:code-reviewer',
                          agent_transcript_path=str(self.transcript))

    def new_run(self, name, reviewers=None, status='prepared'):
        directory = self.root / '.pre-pr-review' / name
        directory.mkdir(parents=True)
        (directory / 'run.json').write_text(json.dumps(dict(repo=str(self.root), branch='feature',
            mode='incremental', pass_n=2, status=status,
            expected_reviewers=['code-reviewer'] if reviewers is None else reviewers)))
        return directory

    def marker(self, directory=None, reviewer='code-reviewer'):
        return dict(type='user', message=dict(content='PRE_PR_USAGE: ' + json.dumps(
            dict(run_dir=str(directory or self.run), reviewer=reviewer))))

    def assistant(self, ident, **changes):
        usage = dict(input_tokens=10, cache_creation_input_tokens=20,
                     cache_read_input_tokens=30, output_tokens=4)
        usage.update(changes)
        return dict(type='assistant', message=dict(id=ident, model='test-model', usage=usage,
                                                  content='PRIVATE_DO_NOT_COPY'))

    def write_transcript(self, records):
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in records))

    def cli(self, *args, event=None):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), *args],
                                input=json.dumps(event) if event is not None else None,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self):
        result = self.cli('hook', event=self.event)
        self.assertEqual(result.stdout, '')

    def summary(self, directory=None):
        directory = directory or self.run
        self.cli('summarize', '--run-dir', str(directory))
        return json.loads((directory / 'usage-summary.json').read_text())

    def test_deduplicates_requests_and_callbacks_but_counts_new_agents(self):
        records = [self.assistant('prior'), self.marker(), self.assistant('a'),
                   self.assistant('a', output_tokens=6), self.assistant('b'),
                   dict(type='result', usage=dict(input_tokens=999999)),
                   dict(type='user', message=dict(content=[dict(type='tool_result', content='999999')]))]
        self.write_transcript(records)
        self.hook()
        self.hook()
        summary = self.summary()
        code = next(r for r in summary['reviewers'] if r['reviewer'] == 'code-reviewer')
        self.assertEqual(code['reported_tokens'], 130)
        self.assertEqual(code['requests'], 2)
        self.assertEqual(code['agents'], 1)
        self.assertEqual(summary['status'], 'partial')  # Aggregator not captured yet.
        self.event.update(agent_id='retry')
        self.hook()
        summary = self.summary()
        self.assertEqual(summary['observed_tokens'], 260)
        self.assertEqual(summary['measured_agents'], 2)
        self.assertIsNone(summary['orchestrator_tokens'])
        for file in (self.run / 'usage').glob('*.json'):
            self.assertNotIn('PRIVATE_DO_NOT_COPY', file.read_text())
            self.assertNotIn(str(self.transcript), file.read_text())

    def test_aggregator_capture_and_ranking(self):
        self.write_transcript([self.marker(), self.assistant('a', input_tokens=200)])
        self.hook()
        self.event.update(agent_type='agent-autolearn:review-aggregator', agent_id='agg')
        self.write_transcript([self.marker(reviewer='review-aggregator'), self.assistant('b')])
        self.hook()
        summary = self.summary()
        self.assertEqual(summary['status'], 'recorded')
        self.assertEqual(summary['reviewers'][0]['reviewer'], 'code-reviewer')
        self.assertEqual(summary['totals'], dict(input_tokens=210, cache_creation_input_tokens=40,
                                                cache_read_input_tokens=60, output_tokens=8))
        self.assertEqual(summary['observed_tokens'], 318)

    def test_resumed_transcript_separates_runs_and_updates_same_run(self):
        second = self.new_run('second')
        self.write_transcript([self.marker(), self.assistant('a'), self.marker(second),
                               self.assistant('b', input_tokens=100), self.marker(), self.assistant('c')])
        self.hook()
        self.assertEqual(self.summary()['observed_tokens'], 128)
        self.assertEqual(self.summary(second)['observed_tokens'], 154)
        self.hook()
        self.assertEqual(self.summary()['observed_tokens'], 128)

    def test_missing_counters_and_corrupt_records_are_partial_not_zero(self):
        message = self.assistant('a')
        del message['message']['usage']['output_tokens']
        self.write_transcript([self.marker(), message])
        with self.transcript.open('a') as fh:
            fh.write('{truncated\n')
        self.hook()
        summary = self.summary()
        code = next(r for r in summary['reviewers'] if r['reviewer'] == 'code-reviewer')
        self.assertEqual(code['status'], 'partial')
        self.assertIsNone(code['output_tokens'])
        self.assertEqual(code['reported_tokens'], 60)
        self.assertIsNone(summary['totals']['output_tokens'])
        (self.run / 'usage' / 'invalid.json').write_text('[]')
        summary = self.summary()
        self.assertEqual(summary['invalid_records'], ['invalid.json'])
        self.assertEqual(summary['status'], 'partial')

    def test_absent_or_disabled_hook_shows_unavailable(self):
        summary = self.summary()
        self.assertIsNone(summary['observed_tokens'])
        self.assertEqual(summary['status'], 'partial')
        self.assertTrue(all(r['status'] == 'unavailable' for r in summary['reviewers']))
        self.assertIn('N/D', (self.run / 'usage-summary.md').read_text())
        unchanged = self.new_run('unchanged', reviewers=[], status='unchanged')
        self.assertEqual(self.summary(unchanged)['status'], 'not_applicable')

    def test_unmarked_unassigned_and_outside_paths_are_not_written(self):
        for records in ([self.assistant('a')],
                        [self.marker(reviewer='security-reviewer'), self.assistant('a')],
                        [self.marker(self.root), self.assistant('a')]):
            with self.subTest(records=records):
                self.write_transcript(records)
                self.hook()
                self.assertFalse((self.run / 'usage').exists())
        self.assertFalse((self.root / 'usage').exists())

    def test_hook_errors_never_block_or_emit_model_context(self):
        self.event['agent_transcript_path'] = str(self.root / 'missing')
        self.hook()
        result = self.cli('hook', event={})
        self.assertEqual(result.stdout, '')
        self.assertIsNone(self.summary()['observed_tokens'])

    def test_tool_result_text_cannot_change_run_assignment(self):
        other = self.new_run('other')
        injected = self.marker(other)['message']['content']
        self.write_transcript([self.marker(), dict(type='user', message=dict(content=[
            dict(type='tool_result', content=injected)])), self.assistant('a')])
        self.hook()
        self.assertEqual(self.summary()['observed_tokens'], 64)
        self.assertIsNone(self.summary(other)['observed_tokens'])

    # ---- orchestrator estimate, shared summaries and comparisons ------------------------
    def call(self, ident, name, **tool_input):
        record = self.assistant(ident)
        record['message']['content'] = [dict(type='tool_use', name=name, input=tool_input)]
        return record

    def human(self, text):
        return dict(type='user', message=dict(content=text))

    def tool_result(self, text):
        return dict(type='user', message=dict(content=[dict(type='tool_result', content=text)]))

    def stop(self, records):
        main = self.root / 'main.jsonl'
        main.write_text(''.join(json.dumps(r) + '\n' for r in records))
        result = self.cli('hook', event=dict(hook_event_name='Stop', cwd=str(self.root), session_id='session',
                                             transcript_path=str(main)))
        self.assertEqual(result.stdout, '')

    def review_turn(self, prefix, directory=None):
        marker = self.marker(directory)['message']['content']
        return [self.call(f'{prefix}-prepare', 'Bash', command='python3 x/scripts/prepare_review.py --repo .'),
                self.tool_result(marker),  # Tool output naming a run never binds the window.
                self.call(f'{prefix}-launch', 'Agent', prompt='Revisa esta corrida.\n' + marker + '\nRepo: .'),
                dict(self.assistant(f'{prefix}-side'), isSidechain=True),
                self.call(f'{prefix}-summary', 'Bash', command='python3 x/scripts/review_usage.py summarize --run-dir "$RUN_DIR"'),
                self.assistant(f'{prefix}-delivery')]

    def test_orchestrator_window_is_estimated_apart_and_ends_with_the_review_turn(self):
        records = [self.human('hola'), self.assistant('before'), self.human('/pre-pr-review feature'),
                   *self.review_turn('r1'),
                   self.human('otra cosa'), self.assistant('after')]
        self.stop(records[:5])  # Interrupted before launching reviewers: nothing to attribute yet.
        self.assertFalse((self.run / 'usage').exists())
        self.stop(records)
        self.stop(records)
        summary = self.summary()
        self.assertIsNone(summary['observed_tokens'])  # Reviewers stay N/D: the estimate never fills them in.
        self.assertEqual(summary['scope'], 'reviewers_and_aggregator_only')
        self.assertEqual(summary['orchestrator']['requests'], 4)
        self.assertEqual(summary['orchestrator_tokens'], 4 * 64)
        self.assertEqual(summary['orchestrator']['status'], 'estimated')
        self.assertEqual(summary['total_with_orchestrator'], 4 * 64)
        self.assertIn('estimado', (self.run / 'usage-summary.md').read_text())
        for file in (self.run / 'usage').glob('*.json'):
            self.assertNotIn('PRIVATE_DO_NOT_COPY', file.read_text())
            self.assertNotIn('main.jsonl', file.read_text())

    def test_orchestrator_windows_split_by_run_merge_on_resume_and_flag_open_ones(self):
        second = self.new_run('second')
        unfinished = self.review_turn('r3')[:3]
        self.stop([*self.review_turn('r1'), self.human('sigue'), *self.review_turn('r2', second),
                   self.human('reanuda'), *unfinished])
        first = self.summary()
        self.assertEqual(first['orchestrator']['requests'], 4 + 2)
        self.assertEqual(first['orchestrator']['status'], 'partial')  # The resumed window never summarized.
        self.assertEqual(self.summary(second)['orchestrator']['requests'], 4)
        self.assertEqual(self.summary(second)['orchestrator']['status'], 'estimated')
        outside = self.new_run('outside')
        self.stop(self.review_turn('r4', self.root))
        self.assertFalse((self.root / 'usage').exists())
        self.assertIsNone(self.summary(outside)['orchestrator'])

    def test_summary_reports_repeated_searches_findings_and_travels_with_the_ledger(self):
        shared = self.root / 'pr-reviews' / 'pr-feature-abc'
        shared.mkdir(parents=True)
        run = json.loads((self.run / 'run.json').read_text())
        run.update(ledger=str(shared / 'ledger.json'), plugin_version='2.12.0', tree='t' * 40, since_tree='s' * 40)
        (self.run / 'run.json').write_text(json.dumps(run))
        (self.run / 'searches').mkdir()
        (self.run / 'searches' / 'code-reviewer.json').write_text(json.dumps(
            [dict(query='parseLead(', scope='src', results=['a.ts:1'], reason='callers')]))
        (self.run / 'searches' / 'security-reviewer.json').write_text(json.dumps(dict(searches=[
            dict(query='  ParseLead( ', scope='SRC', results=['a.ts:1'], reason='taint'),
            dict(query='db.query', scope='src', results=[], reason='sql')])))
        (self.run / 'searches' / 'broken.json').write_text('{')
        (self.run / 'clasificacion.json').write_text(json.dumps(dict(
            resumen=dict(veredicto='LISTO PARA PR', coverage_complete=True, conteo={},
                         severity_counts=dict(BLOCKER=0, HIGH=0, MEDIUM=1, LOW=0, NIT=0)),
            hallazgos=[{}], metricas=dict(falsos_positivos=2))))
        self.write_transcript([self.marker(), self.assistant('a')])
        self.hook()
        summary = self.summary()
        self.assertEqual((summary['plugin_version'], summary['tree']), ('2.12.0', 't' * 40))
        self.assertEqual(summary['searches']['distinct'], 2)
        self.assertEqual(summary['searches']['queries'], [dict(query='parselead(', scope='src',
                                                              reviewers=['code-reviewer', 'security-reviewer'])])
        self.assertEqual((summary['findings']['veredicto'], summary['findings']['abiertos']), ('LISTO PARA PR', 1))
        copies = list((shared / 'usage').glob('*.json'))
        self.assertEqual([c.name for c in copies], ['p2-first.json'])
        text = copies[0].read_text()
        self.assertEqual(json.loads(text), summary)
        self.assertNotIn('PRIVATE_DO_NOT_COPY', text)
        self.assertNotIn(str(self.root), text)  # No local paths leave the machine.
        copies[0].unlink()
        self.cli('summarize', '--run-dir', str(self.run), '--no-share')
        self.assertEqual(list((shared / 'usage').glob('*.json')), [])

        other = self.new_run('second')
        run.update(plugin_version='2.13.0')
        (other / 'run.json').write_text(json.dumps(run))
        self.write_transcript([self.marker(other), self.assistant('b', input_tokens=5)])
        self.event.update(agent_id='two')
        self.hook()
        self.summary(other)
        report = self.cli('compare', str(self.run), str(other / 'usage-summary.json')).stdout
        self.assertIn('plugin 2.12.0', report)
        self.assertIn('plugin 2.13.0', report)
        self.assertIn('Mismo codigo y ventana: si.', report)
        self.assertIn('| code-reviewer | 64 | 59 | -5 |', report)
        run.update(tree='u' * 40)
        (other / 'run.json').write_text(json.dumps(run))
        self.summary(other)
        self.assertIn('Mismo codigo y ventana: NO.', self.cli('compare', str(self.run), str(other)).stdout)


if __name__ == '__main__':
    unittest.main()
