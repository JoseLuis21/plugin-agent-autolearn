"""Coste sin perder cobertura: compuerta de arquitectura, indice del patch, contexto de la conversacion y cierre.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/agent-autolearn/tests -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
import prepare_review  # noqa: E402
import review_usage  # noqa: E402
from test_review_state import ReviewRepo  # noqa: E402

ARCH = 'nextjs-architecture-reviewer'
NEXT = {
    'package.json': '{"dependencies":{"next":"15.0.0"}}\n',
    'src/page.ts': 'export const value = 0;\n',
    'src/page.test.ts': 'it("works", () => {});\n',
    'app/_internal/leads.ts': 'export const limit = 10;\n',
    'components/widget.tsx': '"use client";\nimport { q } from "../lib/data";\nexport const Widget = () => q;\n',
    'lib/data.ts': 'import "server-only";\nexport const q = 1;\n',
}


class ArchitectureGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-cost-')
        self.addCleanup(temp.cleanup)
        self.repo = ReviewRepo(self, Path(temp.name) / 'repo', NEXT)
        self.repo.write('src/page.ts', 'export const value = 1;\n')
        full = self.repo.prepare()
        self.assertEqual(full['mode'], 'completo')
        self.assertIn(ARCH, full['expected_reviewers'], 'una validacion completa nunca omite la arquitectura')
        self.repo.complete(full)

    def incremental(self, name, contents):
        self.repo.write(name, contents)
        run = self.repo.prepare()
        self.assertEqual(run['mode'], 'incremental')
        self.repo.complete(run)
        return run

    def test_body_only_edits_skip_the_reviewer_and_say_why(self):
        run = self.incremental('src/page.ts', 'export const value = 2;\n')
        self.assertNotIn(ARCH, run['expected_reviewers'])
        self.assertIn('solo de cuerpo', run['skipped_reviewers'][ARCH])
        # Los especialistas de correctitud no dependen de esta compuerta.
        self.assertIn('code-reviewer', run['expected_reviewers'])
        self.assertIn('security-reviewer', run['expected_reviewers'])
        tests_only = self.incremental('src/page.test.ts', 'import { value } from "./page";\nit("works", () => value);\n')
        self.assertNotIn(ARCH, tests_only['expected_reviewers'], 'los imports de un test no son arquitectura')

    def test_anything_that_can_break_the_law_launches_it(self):
        cases = [
            ('src/page.ts', 'import { limit } from "../app/_internal/leads";\nexport const value = limit;\n', 'imports'),
            ('src/new-panel.tsx', 'export const Panel = () => null;\n', 'archivo nuevo'),
            ('app/_internal/leads.ts', 'export const limit = 20;\n', 'superficie'),
            ('src/page.ts', 'export const value = process.env.SECRET;\n', 'imports o APIs'),
        ]
        for name, contents, reason in cases:
            with self.subTest(path=name, reason=reason):
                run = self.incremental(name, contents)
                self.assertIn(ARCH, run['expected_reviewers'])
                self.assertIn(reason, run['reviewer_reasons'][ARCH])

    def test_a_client_server_chain_in_the_delta_launches_it_even_for_a_body_edit(self):
        run = self.incremental('lib/data.ts', 'import "server-only";\nexport const q = 2;\n')
        boundary = ReviewRepo.read_json(Path(run['run_dir']) / '_client-server-raw.json')
        self.assertTrue(any(c['en_delta'] for c in boundary['candidates']), 'el grafo se resuelve siempre')
        self.assertIn(ARCH, run['expected_reviewers'])

    def test_only_js_ts_counts_as_surface(self):
        self.assertIsNone(prepare_review.next_surface('diff --git a/x.py b/x.py\n+import os\n'), 'solo JS/TS cuenta')
        self.assertIsNone(prepare_review.next_surface(''))


class PatchIndexTests(unittest.TestCase):
    def test_ranges_locate_every_file_and_slices_only_drop_other_stacks(self):
        with tempfile.TemporaryDirectory(prefix='review-cost-') as temp:
            repo = ReviewRepo(self, Path(temp) / 'repo', NEXT)
            repo.write('src/page.ts', 'import x from "y";\nexport const value = 1;\n')
            repo.write('src/page.test.ts', 'it("changed", () => {});\n')
            repo.write('app.py', 'value = 5\n')
            repo.write('docs/notes.md', 'notas\n')
            run = repo.prepare()
            run_dir = Path(run['run_dir'])
            index = ReviewRepo.read_json(run_dir / 'patch-index.json')
            lines = (run_dir / 'new.patch').read_text().splitlines()
            self.assertEqual(index['total_lines'], len(lines))
            self.assertEqual({f['path'] for f in index['files']}, set(run['delta_files']))
            for entry in index['files']:
                self.assertTrue(lines[entry['start_line'] - 1].startswith(f"diff --git a/{entry['path']} "))
                block = lines[entry['start_line'] - 1:entry['end_line']]
                self.assertEqual(sum(1 for l in block if l.startswith('diff --git ')), 1, 'un rango, un archivo')
            kinds = {f['path']: f['kind'] for f in index['files']}
            self.assertEqual((kinds['src/page.test.ts'], kinds['docs/notes.md'], kinds['src/page.ts']), ('test', 'docs', 'src'))
            self.assertEqual(list(index['slices']), [ARCH], 'solo los revisores de stack reciben recorte')
            piece = index['slices'][ARCH]
            text = (run_dir / piece['path']).read_text()
            self.assertIn('src/page.ts', text)
            for omitted in ('app.py', 'docs/notes.md', 'src/page.test.ts'):
                self.assertIn(omitted, piece['omitted'])
                self.assertNotIn(f'a/{omitted}', text)
            self.assertIn(piece['path'], (run_dir / 'brief.md').read_text())
            # El patch completo sigue intacto para todos los demas.
            self.assertIn('a/app.py', (run_dir / 'new.patch').read_text())
            repo.complete(run)


class ConversationContextTests(unittest.TestCase):
    SESSION = '0f0f0f0f-1111-2222-3333-444444444444'

    def context(self, records, session=None):
        with tempfile.TemporaryDirectory(prefix='review-cost-') as temp:
            project = Path(temp) / 'projects' / '-some-project'
            project.mkdir(parents=True)
            (project / f'{self.SESSION}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records) + '{corrupt\n')
            env = {'CLAUDE_CONFIG_DIR': temp, 'CLAUDE_CODE_SESSION_ID': self.SESSION if session is None else session}
            with mock.patch.dict(os.environ, env):
                return prepare_review.conversation_context()

    @staticmethod
    def assistant(tokens, **extra):
        usage = {'input_tokens': 2, 'cache_read_input_tokens': tokens - 12, 'cache_creation_input_tokens': 10, 'output_tokens': 999}
        return dict(type='assistant', message={'id': 'm', 'usage': usage, 'content': 'PRIVATE'}, **extra)

    def test_levels_come_from_the_last_main_chain_request(self):
        self.assertEqual(self.context([self.assistant(40_000)]), {'tokens': 40_000, 'level': 'ok'})
        note = self.context([self.assistant(300_000), self.assistant(120_000)])
        self.assertEqual((note['tokens'], note['level']), (120_000, 'note'))
        ask = self.context([self.assistant(331_000), self.assistant(900_000, isSidechain=True)])
        self.assertEqual((ask['tokens'], ask['level']), (331_000, 'ask'), 'un subagente no es la conversacion')
        self.assertIn('sesion nueva', ask['warning'])
        self.assertNotIn('PRIVATE', json.dumps(ask))

    def test_it_is_best_effort_and_never_blocks(self):
        self.assertIsNone(self.context([self.assistant(200_000)], session='../../etc/passwd'))
        self.assertIsNone(self.context([self.assistant(200_000)], session=''))
        self.assertIsNone(self.context([{'type': 'user', 'message': {'content': 'hola'}}]))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/nonexistent', 'CLAUDE_CODE_SESSION_ID': self.SESSION}):
            self.assertIsNone(prepare_review.conversation_context())

    def test_prepare_reports_it_without_touching_the_run_identity(self):
        with tempfile.TemporaryDirectory(prefix='review-cost-') as temp:
            project = Path(temp) / 'cfg' / 'projects' / '-p'
            project.mkdir(parents=True)
            (project / f'{self.SESSION}.jsonl').write_text(json.dumps(self.assistant(200_000)) + '\n')
            repo = ReviewRepo(self, Path(temp) / 'repo')
            repo.env.update(CLAUDE_CONFIG_DIR=str(Path(temp) / 'cfg'), CLAUDE_CODE_SESSION_ID=self.SESSION)
            repo.write('app.py', 'value = 1\n')
            output = json.loads(repo.command([sys.executable, str(SCRIPTS / 'prepare_review.py'), '--repo', str(repo.root)]))
            self.assertEqual(output['conversation_context']['level'], 'ask')
            self.assertNotIn('conversation_context', ReviewRepo.read_json(Path(output['run_dir']) / 'run.json'))
            # Sesion nueva y corta: la misma corrida se reanuda, que es lo que promete el aviso.
            repo.env.pop('CLAUDE_CODE_SESSION_ID')
            again = json.loads(repo.command([sys.executable, str(SCRIPTS / 'prepare_review.py'), '--repo', str(repo.root)]))
            self.assertTrue(again['resumed'])
            self.assertEqual(again['run_dir'], output['run_dir'])
            self.assertIsNone(again['conversation_context'])


class FinishTests(unittest.TestCase):
    def test_one_command_delivers_summary_verdict_and_sync_status(self):
        with tempfile.TemporaryDirectory(prefix='review-cost-') as temp:
            repo = ReviewRepo(self, Path(temp) / 'repo')
            repo.write('app.py', 'value = 1\n')
            run = repo.prepare()
            from test_review_state import finding
            repo.complete(run, raw={'code-reviewer': [finding(severity='HIGH')]})
            env = {**repo.env, 'AGENT_AUTOLEARN_CONFIG': str(Path(temp) / 'none.json')}
            result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'review_usage.py'), 'finish', '--run-dir', run['run_dir']],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(result.stdout)
            self.assertEqual(packet['resumen']['veredicto'], ReviewRepo.read_json(Path(run['run_dir']) / 'clasificacion.json')['resumen']['veredicto'])
            self.assertEqual(len(packet['bloqueantes']), 1)
            self.assertTrue(packet['bloqueantes'][0]['title'])
            self.assertEqual(packet['report'], run['report'])
            self.assertTrue(Path(packet['usage']['summary']).exists())
            self.assertEqual(packet['sync']['queued'], False, 'sin perfil no se envia nada y no es un error')
            self.assertIn('perfil', packet['sync']['reason'])

    def test_turn_profile_counts_batched_tools_and_long_conversations_are_flagged(self):
        group = {'messages': {}, 'incomplete': False}
        def message(ident, tools, read):
            return {'id': ident, 'model': 'm', 'content': [{'type': 'tool_use', 'id': t, 'name': 'Read', 'input': {}} for t in tools],
                    'usage': {'input_tokens': 1, 'cache_creation_input_tokens': 9, 'cache_read_input_tokens': read, 'output_tokens': 5}}
        review_usage.add_usage(group, message('a', ['t1'], 1_000))
        review_usage.add_usage(group, message('a', ['t1', 't2', 't3'], 1_000))  # mismo id en streaming: no duplica
        review_usage.add_usage(group, message('b', ['t4'], 5_000))
        review_usage.add_usage(group, message('c', [], 7_000))
        totals = review_usage.usage_totals(group)
        self.assertEqual((totals['requests'], totals['tool_calls'], totals['parallel_turns'], totals['single_tool_turns']), (3, 4, 1, 1))
        self.assertEqual(totals['peak_context'], 7_010)
        json.dumps(totals)  # solo contadores serializables
        self.assertEqual(review_usage.profile([{'requests': 1}]), {k: None for k in review_usage.PROFILE}, 'archivos antiguos: desconocido')


if __name__ == '__main__':
    unittest.main()
