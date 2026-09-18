"""El escaner de frontera cliente/servidor: lo que rompe el build de Next y lo que no.

Run from the repository root:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/agent-autolearn/tests -v
"""
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
from scan_client_server import scan  # noqa: E402

TSCONFIG = '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}}}'


def run(files, delta=()):
    files = dict(files)
    files.setdefault('tsconfig.json', TSCONFIG)
    return scan(files.get, list(files), delta)


class BoundaryTests(unittest.TestCase):
    def test_reports_the_real_build_break(self):
        """El caso que paso la revision y rompio el build en Cloudflare:
        page (servidor) -> wizard "use client" -> paso -> actions con server-only."""
        result = run({
            'src/app/lp/b/page.tsx': 'import FormWizard from "./mes/_components/FormWizard";\n',
            'src/app/lp/b/mes/_components/FormWizard.tsx':
                '"use client";\nimport { FormStepPhoneValidator } from "@/app/_components/_wizards/FormStepPhoneValidator";\n',
            'src/app/_components/_wizards/FormStepPhoneValidator.tsx':
                '"use client";\nimport { verifyLead } from "@/app/_actions/actions";\n',
            'src/app/_actions/actions.ts':
                'import "server-only";\nimport { headers } from "next/headers";\n',
        }, delta=['src/app/_components/_wizards/FormStepPhoneValidator.tsx'])

        candidates = result['candidates']
        self.assertEqual(len(candidates), 1, 'un modulo roto es un solo hallazgo')
        found = candidates[0]
        self.assertEqual([m['spec'] for m in found['markers']], ['server-only', 'next/headers'])
        self.assertEqual(found['module'], 'src/app/_actions/actions.ts')
        self.assertEqual(found['line'], 1)
        # La cadena arranca en el Client Component mas cercano: es la evidencia mas corta
        # que demuestra la rotura, no hace falta subir hasta la page.
        self.assertEqual(found['client_root'], 'src/app/_components/_wizards/FormStepPhoneValidator.tsx')
        self.assertEqual(found['chain'], [
            'src/app/_components/_wizards/FormStepPhoneValidator.tsx',
            'src/app/_actions/actions.ts',
        ])
        self.assertTrue(found['en_delta'])
        self.assertEqual(found['delta_files_in_chain'],
                         ['src/app/_components/_wizards/FormStepPhoneValidator.tsx'])

    def test_use_server_is_a_legal_boundary(self):
        """El mismo grafo con "use server" compila: Next serializa la llamada."""
        result = run({
            'src/app/page.tsx': '"use client";\nimport { save } from "@/app/_actions/save";\n',
            'src/app/_actions/save.ts': '"use server";\nimport "server-only";\nimport { headers } from "next/headers";\n',
        }, delta=['src/app/_actions/save.ts'])
        self.assertEqual(result['candidates'], [])

    def test_server_component_chain_is_not_reported(self):
        """Un Server Component que llega al DAL es exactamente lo correcto."""
        result = run({
            'src/app/page.tsx': 'import { getLeads } from "@/app/_internal/leads.dal";\n',
            'src/app/_internal/leads.dal.ts': 'import "server-only";\nimport { headers } from "next/headers";\n',
        }, delta=['src/app/_internal/leads.dal.ts'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['client_roots'], 0)

    def test_type_only_import_does_not_reach_the_bundle(self):
        result = run({
            'src/app/form.tsx': '"use client";\nimport type { Lead } from "@/app/_internal/leads.dal";\n',
            'src/app/_internal/leads.dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(result['candidates'], [])

    def test_commented_out_import_is_not_an_edge(self):
        result = run({
            'src/app/form.tsx': '"use client";\n// import { dal } from "@/app/_internal/leads.dal";\n'
                                '/* import "server-only"; */\n',
            'src/app/_internal/leads.dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(result['candidates'], [])

    def test_node_builtin_and_relative_import(self):
        result = run({
            'src/app/form.tsx': '"use client";\nimport { readConfig } from "./config";\n',
            'src/app/config.ts': 'import fs from "node:fs";\nexport const readConfig = () => fs.readFileSync("x");\n',
        }, delta=['src/app/config.ts'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['marker'], 'node:fs')
        self.assertEqual(result['candidates'][0]['module'], 'src/app/config.ts')

    def test_re_export_barrel_propagates(self):
        result = run({
            'src/app/form.tsx': '"use client";\nimport { verify } from "@/app/_actions";\n',
            'src/app/_actions/index.ts': 'export * from "./verify";\n',
            'src/app/_actions/verify.ts': 'import { cookies } from "next/headers";\n',
        }, delta=['src/app/form.tsx'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['chain'], [
            'src/app/form.tsx', 'src/app/_actions/index.ts', 'src/app/_actions/verify.ts'])

    def test_preexisting_break_is_marked_outside_the_delta(self):
        result = run({
            'src/app/form.tsx': '"use client";\nimport "@/app/_internal/leads.dal";\n',
            'src/app/_internal/leads.dal.ts': 'import "server-only";\n',
        }, delta=['README.md'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertFalse(result['candidates'][0]['en_delta'])

    def test_directive_after_a_licence_comment_still_counts(self):
        result = run({
            'src/app/form.tsx': '/* (c) Example */\n"use client";\nimport "@/app/dal";\n',
            'src/app/dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(len(result['candidates']), 1)

    def test_dynamic_import_is_an_edge(self):
        result = run({
            'src/app/form.tsx': '"use client";\nconst mod = await import("@/app/dal");\n',
            'src/app/dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(len(result['candidates']), 1)

    def test_tsconfig_globs_do_not_eat_the_alias_map(self):
        """`"@/*"` abre un /* y `"**/*.ts"` lo cierra: sin respetar los literales, el
        mapa de alias se pierde y el grafo se queda en los propios Client Components."""
        result = scan({
            'tsconfig.json': '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}},'
                             ' "include": ["**/*.ts", "**/*.tsx"]}',
            'src/app/form.tsx': '"use client";\nimport "@/app/dal";\n',
            'src/app/dal.ts': 'import "server-only";\n',
        }.get, ['tsconfig.json', 'src/app/form.tsx', 'src/app/dal.ts'], ['src/app/dal.ts'])
        self.assertEqual([c['module'] for c in result['candidates']], ['src/app/dal.ts'])

    def test_apostrophe_in_jsx_text_does_not_hide_later_imports(self):
        result = run({
            'src/app/form.tsx': '"use client";\n'
                                'const Note = () => <p>it\'s here</p>;\n'
                                'import "@/app/dal";\n',
            'src/app/dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(len(result['candidates']), 1)

    def test_url_in_a_string_is_not_a_comment(self):
        result = run({
            'src/app/form.tsx': '"use client";\nconst base = "https://api.example.com"; import "@/app/dal";\n',
            'src/app/dal.ts': 'import "server-only";\n',
        })
        self.assertEqual(len(result['candidates']), 1)


class PreparationTests(unittest.TestCase):
    """El escaner llega al artifact de la corrida, sobre el snapshot real (sin commitear)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='boundary-repo-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'repo'
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(
            HOME=str(self.root.parent), XDG_CONFIG_HOME=str(self.root.parent),
            GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME='Review Test', GIT_AUTHOR_EMAIL='review@example.invalid',
            GIT_COMMITTER_NAME='Review Test', GIT_COMMITTER_EMAIL='review@example.invalid',
            PYTHONDONTWRITEBYTECODE='1',
        )
        self.git('init', '--quiet')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/development')
        self.write('package.json', '{"name": "app", "dependencies": {"next": "16.3.1"}}\n')
        self.write('tsconfig.json', TSCONFIG + '\n')
        self.write('src/app/lp/b/page.tsx', 'export default function Page() { return null; }\n')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'base')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.root), *args], env=self.env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def test_the_run_carries_the_boundary_candidates(self):
        self.git('checkout', '--quiet', '-b', 'feature/wizard')
        self.write('src/app/_actions/actions.ts',
                   'import "server-only";\nimport { headers } from "next/headers";\n'
                   'export async function verifyLead() { return headers().get("x-real-ip"); }\n')
        self.write('src/app/_components/_wizards/FormStepPhoneValidator.tsx',
                   '"use client";\nimport { verifyLead } from "@/app/_actions/actions";\n'
                   'export const Step = () => { void verifyLead; return null; };\n')
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / 'prepare_review.py'), '--repo', str(self.root)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout)
        self.assertIn('nextjs-architecture-reviewer', run['expected_reviewers'])
        self.assertIn('modulos de servidor', run['reviewer_reasons']['nextjs-architecture-reviewer'])
        raw = json.loads((Path(run['run_dir']) / '_client-server-raw.json').read_text())
        self.assertEqual([c['module'] for c in raw['candidates']], ['src/app/_actions/actions.ts'])
        self.assertTrue(raw['candidates'][0]['en_delta'])


if __name__ == '__main__':
    unittest.main()
