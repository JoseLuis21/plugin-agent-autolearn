"""Tests del cliente de sincronizacion (review_sync.py) contra una API falsa en un hilo local.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/agent-autolearn/tests -v
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
PLUGIN = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
import review_sync  # noqa: E402
from test_review_state import ReviewRepo, finding, fingerprint  # noqa: E402

TOKEN = 'alt_' + 'x' * 40


class FakeApi:
    """Lo minimo del contrato /v1: idempotencia por clave, ETag en el resultado y fallos inyectables."""

    def __init__(self):
        self.requests = []
        self.fail = []  # [(method, fragmento de ruta, status, veces)]
        self.idem = {}
        self.repos, self.versions, self.runs, self.usage, self.diagnostics = {}, {}, {}, {}, {}
        self.lock = threading.Lock()

    def created(self, kind):
        return len({'repos': self.repos, 'versions': self.versions, 'runs': self.runs}[kind])

    def handle(self, method, path, headers, body):
        with self.lock:
            self.requests.append((method, path, headers.get('idempotency-key'), headers.get('if-match')))
            for i, (m, frag, status, times) in enumerate(self.fail):
                if m == method and frag in path and times > 0:
                    self.fail[i] = (m, frag, status, times - 1)
                    return status, {'error': {'code': 'injected', 'message': f'fallo {status}', 'request_id': 'r'}}, {}
            if headers.get('authorization') != f'Bearer {TOKEN}':
                return 401, {'error': {'code': 'unauthenticated', 'message': 'token', 'request_id': 'r'}}, {}
            key = headers.get('idempotency-key')
            h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() if body is not None else ''
            if method == 'POST' and key:
                if key in self.idem:
                    prev_h, resp = self.idem[key]
                    return (resp if prev_h == h else (409, {'error': {'code': 'idempotency_conflict', 'message': 'x', 'request_id': 'r'}}, {}))
            resp = self.route(method, path, headers, body)
            if method == 'POST' and key and resp[0] < 300:
                self.idem[key] = (h, resp)
            return resp

    def route(self, method, path, headers, body):
        if path == '/v1/me':
            return 200, {'organization': {'name': 'Workspace Personal'}, 'actor': {'label': 'instalacion:test'},
                         'capabilities': {'ingest': True, 'read': True, 'approve': False}}, {}
        if path == '/v1/repositories' and method == 'POST':
            rid = self.repos.setdefault(body['remote'], f'repo_{len(self.repos) + 1}')
            return 201, {'id': rid}, {}
        if path == '/v1/component-versions' and method == 'POST':
            k = (body['type'], body['name'], body['commit'], body['content_hash'])
            vid = self.versions.setdefault(k, f'cv_{len(self.versions) + 1}')
            return 201, {'id': vid, 'component_id': 'cmp'}, {}
        if path == '/v1/review-runs' and method == 'POST':
            run = next((r for r in self.runs.values() if r['client_run_id'] == body['client_run_id']), None)
            if not run:
                run = {'id': f'run_{len(self.runs) + 1}', 'client_run_id': body['client_run_id'], 'body': body,
                       'version': 1, 'result_hash': None, 'result': None}
                self.runs[run['id']] = run
            return 201, {'id': run['id']}, {'etag': f'"{run["version"]}"'}
        parts = path.split('/')
        if len(parts) >= 4 and parts[2] == 'review-runs' and parts[3] in self.runs:
            run = self.runs[parts[3]]
            if len(parts) == 4 and method == 'GET':
                return 200, {'id': run['id']}, {'etag': f'"{run["version"]}"'}
            if parts[4:] == ['result'] and method == 'PUT':
                h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                if run['result_hash'] == h:
                    return 200, {'id': run['id'], 'replayed': True}, {'etag': f'"{run["version"]}"'}
                if (headers.get('if-match') or '').strip('"') != str(run['version']):
                    return 412, {'error': {'code': 'precondition_failed', 'message': 'etag', 'request_id': 'r'}}, {}
                run.update(result=body, result_hash=h, version=run['version'] + 1)
                return 200, {'id': run['id']}, {'etag': f'"{run["version"]}"'}
            if parts[4:] == ['diagnostics'] and method == 'PUT':
                self.diagnostics[run['id']] = body
                return 200, {'run_id': run['id']}, {}
            if parts[4] == 'usage' and method == 'PUT':
                self.usage[(run['id'], parts[5])] = body
                return 200, {'run_id': run['id']}, {}
        return 404, {'error': {'code': 'not_found', 'message': path, 'request_id': 'r'}}, {}


def serve(api):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            length = int(self.headers.get('content-length') or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, data, extra = api.handle(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(raw)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = _do

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ReviewSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-sync-tests-')
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.api = FakeApi()
        self.server = serve(self.api)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.env_backup = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env_backup)))
        for k in ('AGENT_AUTOLEARN_PROFILE', 'AGENT_AUTOLEARN_TOKEN', 'CLAUDE_PLUGIN_ROOT', 'AGENT_AUTOLEARN_PLUGIN_REPO'):
            os.environ.pop(k, None)
        os.environ['AGENT_AUTOLEARN_CONFIG'] = str(self.temp / 'cfg' / 'agent-autolearn' / 'config.json')
        os.environ['AGENT_AUTOLEARN_BACKOFF'] = '0'
        self.plugin_root = self.plugin_checkout()
        os.environ['CLAUDE_PLUGIN_ROOT'] = str(self.plugin_root)
        self.count = 0

    # ---------- utilidades ----------

    def plugin_checkout(self, with_git=True, name=None):
        """Copia del plugin en un repo propio y limpio, para que el commit y los hashes sean deterministas."""
        top = self.temp / (name or ('plugin-repo' if with_git else 'plugin-nogit'))
        dest = top / 'plugins' / 'agent-autolearn'
        shutil.copytree(PLUGIN, dest, ignore=shutil.ignore_patterns('__pycache__', 'tests', 'evals'))
        if with_git:
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                   'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
            for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '--no-gpg-sign', '-m', 'p']):
                subprocess.run(['git', '-C', str(top), *args], env=env, check=True, stdout=subprocess.DEVNULL)
        return dest

    def configure(self, name='personal', default=True):
        from contextlib import redirect_stdout
        from io import StringIO
        os.environ['AGENT_AUTOLEARN_TOKEN'] = TOKEN
        with redirect_stdout(StringIO()):
            review_sync.main(['configure', '--profile', name, '--url', self.url + '/v1', *(['--default'] if default else [])])
        os.environ.pop('AGENT_AUTOLEARN_TOKEN')

    def finalized_run(self, raw=None, remote='git@github.com:Acme/Shop.git'):
        self.count += 1
        repo = ReviewRepo(self, self.temp / f'repo-{self.count}')
        if remote:
            repo.git('remote', 'add', 'origin', remote)
        repo.write('app.py', 'value = 1\n')
        run = repo.prepare()
        repo.complete(run, raw=raw if raw is not None else {'code-reviewer': [finding()]})
        usage = Path(run['run_dir']) / 'usage'
        usage.mkdir(exist_ok=True)
        (usage / 'agent1.json').write_text(json.dumps({
            'schema_version': 1, 'run_dir': run['run_dir'], 'reviewer': 'code-reviewer', 'agent_key': 'agent1',
            'source': 'claude_transcript_assistant_usage', 'input_tokens': 100, 'cache_creation_input_tokens': 5,
            'cache_read_input_tokens': 0, 'output_tokens': 20, 'requests': 2, 'models': ['m'],
            'missing_fields': ['cache_read_input_tokens'], 'complete': False}))
        return repo, run

    def push(self, repo, *extra):
        review_sync.main(['push', '--repo', str(repo.root), '--timeout', '3', '--quiet', *extra])
        return review_sync.load_state(repo.root.resolve())

    def mutations(self):
        return [r for r in self.api.requests if r[0] != 'GET']

    # ---------- configuracion ----------

    def test_config_is_private_and_token_never_in_argv_or_output(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            self.configure()
            review_sync.main(['profiles'])
        path = review_sync.config_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn(TOKEN, buf.getvalue())
        self.assertIn(TOKEN[:8] + '…', buf.getvalue())
        self.assertEqual(json.loads(path.read_text())['profiles']['personal']['url'], self.url)
        with self.assertRaises(SystemExit):  # No existe forma de pasar el token por argv.
            review_sync.main(['configure', '--profile', 'x', '--url', self.url, '--token', TOKEN])

    def test_profile_resolution_order(self):
        repo = ReviewRepo(self, self.temp / 'repo-res')
        self.assertEqual(review_sync.resolve_profile(repo.root)[0], None)
        self.configure('personal')
        self.configure('empresa', default=False)
        self.assertEqual(review_sync.resolve_profile(repo.root)[0], 'personal')
        from contextlib import redirect_stdout
        from io import StringIO
        with redirect_stdout(StringIO()):
            review_sync.main(['use', 'empresa', '--repo', str(repo.root)])
        self.assertEqual(review_sync.resolve_profile(repo.root), ('empresa', '.agent-autolearn.json del repo'))
        os.environ['AGENT_AUTOLEARN_PROFILE'] = 'otro'
        self.assertEqual(review_sync.resolve_profile(repo.root)[0], 'otro')
        self.assertNotIn('token', (repo.root / '.agent-autolearn.json').read_text())

    def setup_cmd(self, *args, token=TOKEN, cwd_repo=None):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        os.environ.update(HOME=str(self.temp / 'home'), SHELL='/bin/zsh', AGENT_AUTOLEARN_TOKEN=token)
        os.environ.pop('ZDOTDIR', None)
        (self.temp / 'home').mkdir(exist_ok=True)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = review_sync.main(['setup', *args, '--url', self.url,
                                     '--repo', str(cwd_repo or self.temp / 'home')])
        os.environ.pop('AGENT_AUTOLEARN_TOKEN')
        return code, out.getvalue() + err.getvalue()

    def test_setup_validates_token_links_repo_and_adds_alias_once(self):
        repo = ReviewRepo(self, self.temp / 'repo-setup')
        code, out = self.setup_cmd(cwd_repo=repo.root)
        self.assertEqual(code, 0, out)
        self.assertIn("workspace 'Workspace Personal'", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(review_sync.resolve_profile(repo.root), ('personal', '.agent-autolearn.json del repo'))
        self.assertIsNone(review_sync.load_config().get('default'))  # Sin --default, otros repos no envian nada.
        rc = self.temp / 'home' / '.zshrc'
        self.assertIn('alias review-sync="python3 ', rc.read_text())
        self.assertEqual(self.setup_cmd('--no-repo')[0], 0)  # Repetirlo no duplica el alias.
        self.assertEqual(rc.read_text().count('alias review-sync='), 1)

    def test_setup_with_rejected_token_saves_nothing(self):
        repo = ReviewRepo(self, self.temp / 'repo-bad')
        code, out = self.setup_cmd('empresa', '--default', token='alt_' + 'z' * 40, cwd_repo=repo.root)
        self.assertEqual(code, 2)
        self.assertIn('No se guardo nada', out)
        self.assertEqual(review_sync.load_config()['profiles'], {})
        self.assertFalse((repo.root / '.agent-autolearn.json').exists())
        self.assertFalse((self.temp / 'home' / '.zshrc').exists())

    def test_configure_without_default_flag_never_becomes_default(self):
        self.configure('empresa', default=False)
        self.assertIsNone(review_sync.load_config().get('default'))

    def test_without_profile_sync_is_disabled_not_an_error(self):
        repo, run = self.finalized_run()
        self.assertEqual(review_sync.enqueue(run['run_dir'])['queued'], False)
        self.assertEqual(review_sync.main(['push', '--repo', str(repo.root), '--quiet']), 0)
        self.assertEqual(self.api.requests, [])

    # ---------- construccion del envio ----------

    def test_redaction_truncation_and_sensitive_files(self):
        secrets = {'aws': 'AKIA' + 'ABCDEFGHIJKLMNOP', 'gh': 'gh' + 'p_' + 'a' * 36, 'oa': 'sk-' + 'proj-' + 'b' * 40}
        red = {}
        text = f"k={secrets['aws']} t {secrets['gh']} o {secrets['oa']} DB_PASSWORD=hunter2hunter2 https://u:p4ss@db.local/x"
        out = review_sync.clean_text(text, red)
        for s in [*secrets.values(), 'hunter2hunter2', 'p4ss']:
            self.assertNotIn(s, out)
        self.assertEqual(review_sync.clean_text('x' * 5000, red)[-11:], '…[truncado]')
        self.assertEqual(review_sync.clean_text('API_KEY=abc', red, file='config/.env.local'), '[omitido: archivo sensible]')
        self.assertEqual(review_sync.clean_text('token_count() usa password_policy.md', {}), 'token_count() usa password_policy.md')

        self.configure()
        leaky = finding(evidence=f"const key = '{secrets['aws']}'")
        repo, run = self.finalized_run(raw={'code-reviewer': [leaky]})
        result = review_sync.enqueue(run['run_dir'])
        self.assertTrue(result['queued'])
        item = json.loads((review_sync.queue_dir(repo.root.resolve()) / f"{result['client_run_id']}.json").read_text())
        self.assertNotIn(secrets['aws'], json.dumps(item))
        self.assertNotIn(str(repo.root), json.dumps({k: v for k, v in item.items() if k != 'run_dir'}))
        self.assertEqual(item['remote'], 'github.com/acme/shop')
        f = item['result']['findings'][0]
        self.assertEqual((f['fingerprint'], f['sources'], f['decision']), (fingerprint(finding()), ['code-reviewer'], 'abierto'))
        self.assertEqual(item['result']['raw_observations'][0]['merged_into'], f['fingerprint'])
        self.assertEqual(item['result']['counts'], {'total': 1})
        self.assertTrue(item['result']['coverage']['complete'])
        self.assertEqual(item['usage'][0]['body']['cache_read_input_tokens'], None, 'sin medir es desconocido, no cero')
        self.assertEqual(item['usage'][0]['body']['input_tokens'], 100)
        names = {(c['type'], c['name']) for c in item['component_versions']}
        self.assertIn(('orchestrator', 'pre-pr-review'), names)
        self.assertIn(('reviewer', 'code-reviewer'), names)

    def test_multi_file_hash_matches_javascript_locale_order(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node no disponible')
        comps = review_sync.plugin_components(['security-reviewer'])
        paths = [f'plugins/agent-autolearn/{p}' for p in comps[0][2]] + ['a_b/x.md', 'a-b/x.md', 'A/Z.md', 'a/y.md']
        js = subprocess.run([node, '-e', 'const p=JSON.parse(process.argv[1]);p.sort((a,b)=>a.localeCompare(b));console.log(JSON.stringify(p))',
                             json.dumps(paths)], stdout=subprocess.PIPE, text=True, check=True)
        self.assertEqual(sorted(paths, key=review_sync.locale_key), json.loads(js.stdout))

    # ---------- envio ----------

    def test_push_is_idempotent_and_second_push_is_a_no_op(self):
        self.configure()
        repo, run = self.finalized_run()
        cid = review_sync.enqueue(run['run_dir'])['client_run_id']
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sent')
        sequence = [(m, p.split('/')[2]) for m, p, _, _ in self.mutations()]
        self.assertEqual(sequence[0], ('POST', 'repositories'))
        self.assertIn(('POST', 'review-runs'), sequence)
        self.assertEqual(self.api.created('runs'), 1)
        run_body = next(r for r in self.api.runs.values())['body']
        self.assertEqual(len(run_body['component_version_ids']), self.api.created('versions'))
        self.assertEqual(self.api.usage[('run_1', 'agent1')]['input_tokens'], 100)
        before = len(self.api.requests)
        self.push(repo)
        self.assertEqual(len(self.api.requests), before, 'un push sin cambios no llama a la API')
        # Aunque se pierda el estado local, las claves deterministas evitan duplicados.
        review_sync.state_path(repo.root.resolve()).unlink()
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sent')
        self.assertEqual((self.api.created('runs'), self.api.created('repos')), (1, 1))
        keys = [k for _, _, k, _ in self.mutations() if k]
        self.assertTrue(all(k.startswith(cid) for k in keys))

    def test_late_usage_is_sent_after_the_result(self):
        self.configure()
        repo, run = self.finalized_run()
        review_sync.enqueue(run['run_dir'])
        self.push(repo)
        (Path(run['run_dir']) / 'usage' / 'orchestrator-x.json').write_text(json.dumps({
            'reviewer': 'orchestrator', 'agent_key': 'orchestrator-x', 'input_tokens': 7, 'output_tokens': 1,
            'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'requests': 1, 'models': [],
            'missing_fields': [], 'complete': True, 'estimated': True}))
        self.push(repo)
        self.assertEqual(self.api.usage[('run_1', 'orchestrator-x')]['estimated'], True)
        self.assertEqual(self.api.runs['run_1']['version'], 2, 'la telemetria no reescribe el resultado')

    def test_diagnostics_describe_patch_and_reviewers_without_content(self):
        self.configure()
        repo, run = self.finalized_run()
        searches = Path(run['run_dir']) / 'searches'
        searches.mkdir(exist_ok=True)
        (searches / 'code-reviewer.json').write_text(json.dumps({'searches': [{'query': 'value', 'scope': 'app'}, {'query': ' '}]}))
        review_sync.enqueue(run['run_dir'])
        self.push(repo)
        diag = self.api.diagnostics['run_1']
        self.assertEqual(diag['schema_version'], 1)
        self.assertTrue(diag['mode_reason'])
        patch = diag['patch']
        self.assertEqual([f['path'] for f in patch['files']], ['app.py'])
        self.assertEqual((patch['files'][0]['added'], patch['files'][0]['kind']), (1, 'src'))
        self.assertEqual(patch['bytes'], len((Path(run['run_dir']) / 'new.patch').read_bytes()))
        self.assertNotIn('value = 1', json.dumps(diag), 'nunca se envia el contenido del patch')
        code = next(r for r in diag['reviewers'] if r['reviewer'] == 'code-reviewer')
        self.assertEqual(code['searches'], 1)
        self.assertEqual(diag['searches'], {'distinct': 1, 'repeated': 0})
        before = len(self.api.requests)
        self.push(repo)
        self.assertEqual(len(self.api.requests), before, 'un diagnostico sin cambios no se reenvia')

    def test_diagnostics_backfill_already_sent_runs_and_tolerate_old_servers(self):
        self.configure()
        repo, run = self.finalized_run()
        cid = review_sync.enqueue(run['run_dir'])['client_run_id']
        self.api.fail.append(('PUT', '/diagnostics', 404, 1))
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sent', 'un servidor sin el endpoint no hace fallar el envio')
        self.assertNotIn('run_1', self.api.diagnostics)
        state = self.push(repo)
        self.assertIn('run_1', self.api.diagnostics, 'la corrida ya enviada completa su diagnostico despues')
        self.assertEqual(self.api.runs['run_1']['version'], 2, 'el diagnostico no reescribe el resultado')

    def test_transient_failure_stays_pending_then_sends(self):
        self.configure()
        repo, run = self.finalized_run()
        cid = review_sync.enqueue(run['run_dir'])['client_run_id']
        self.api.fail.append(('POST', '/v1/review-runs', 503, 3))
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'pending')
        self.assertIn('503', state['runs'][cid]['error'])
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sent')
        self.assertEqual(self.api.created('runs'), 1)

    def test_schema_error_is_sync_failed_and_not_retried(self):
        self.configure()
        repo, run = self.finalized_run()
        cid = review_sync.enqueue(run['run_dir'])['client_run_id']
        self.api.fail.append(('PUT', '/result', 422, 1))
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sync_failed')
        self.assertIn('422', state['runs'][cid]['error'])
        before = len(self.api.requests)
        self.push(repo)
        self.assertEqual(len(self.api.requests), before)
        state = self.push(repo, '--retry-failed')
        self.assertEqual(state['runs'][cid]['state'], 'sent')

    def test_stale_etag_is_refreshed_once(self):
        self.configure()
        repo, run = self.finalized_run()
        cid = review_sync.enqueue(run['run_dir'])['client_run_id']
        self.api.fail.append(('PUT', '/result', 412, 1))
        state = self.push(repo)
        self.assertEqual(state['runs'][cid]['state'], 'sent')
        methods = [(m, p.rsplit('/', 1)[-1]) for m, p, _, _ in self.api.requests]
        self.assertIn(('GET', 'run_1'), methods)

    def test_plugin_without_commit_sends_run_without_versions(self):
        os.environ['CLAUDE_PLUGIN_ROOT'] = str(self.plugin_checkout(with_git=False))
        self.configure()
        repo, run = self.finalized_run()
        result = review_sync.enqueue(run['run_dir'])
        self.assertEqual(result['component_versions'], 0)
        self.assertIn('checkout de git', result['component_versions_note'])  # y sin clon del marketplace
        state = self.push(repo)
        self.assertEqual(state['runs'][result['client_run_id']]['state'], 'sent')
        self.assertEqual(next(iter(self.api.runs.values()))['body']['component_version_ids'], [])
        self.assertEqual(self.api.created('versions'), 0)

    def installed_from_marketplace(self, edit_head=None):
        """Reproduce la instalacion real de Claude Code: cache sin git + clon del marketplace con git."""
        plugins = self.temp / 'claude-plugins'
        clone_plugin = self.plugin_checkout(with_git=True, name='marketplace-clone')
        clone_top = clone_plugin.parent.parent
        shutil.copytree(PLUGIN.parent.parent / '.claude-plugin', clone_top / '.claude-plugin')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
               'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        g = lambda *a: subprocess.run(['git', '-C', str(clone_top), *a], env=env, check=True, stdout=subprocess.PIPE).stdout.decode().strip()
        g('add', '.'); g('commit', '-q', '--no-gpg-sign', '-m', 'marketplace')
        installed_commit = g('rev-parse', 'HEAD')
        market = plugins / 'marketplaces' / 'agent-autolearn'
        market.parent.mkdir(parents=True)
        shutil.move(str(clone_top), market)
        cache = plugins / 'cache' / 'agent-autolearn' / 'agent-autolearn' / '9.9.9'
        shutil.copytree(market / 'plugins' / 'agent-autolearn', cache)
        if edit_head:  # el marketplace avanzo despues de instalar: HEAD ya no coincide con la cache
            target = market / 'plugins' / 'agent-autolearn' / edit_head
            target.write_text(target.read_text() + '\ncambio posterior\n')
            subprocess.run(['git', '-C', str(market), 'commit', '-qam', 'posterior', '--no-gpg-sign'], env=env, check=True, stdout=subprocess.DEVNULL)
        return cache, installed_commit

    def test_installed_plugin_registers_versions_from_marketplace_clone(self):
        cache, installed_commit = self.installed_from_marketplace(edit_head='agents/code-reviewer.md')
        os.environ['CLAUDE_PLUGIN_ROOT'] = str(cache)
        self.configure()
        repo, run = self.finalized_run()
        result = review_sync.enqueue(run['run_dir'])
        self.assertGreater(result['component_versions'], 0, result['component_versions_note'])
        item = json.loads((repo.root / '.pre-pr-review' / 'sync' / 'queue' / f"{result['client_run_id']}.json").read_text())
        by_name = {c['name']: c for c in item['component_versions']}
        # El revisor cambio en HEAD despues de instalar: se registra un commit cuyo contenido es el que se ejecuto.
        market = cache.parent.parent.parent.parent / 'marketplaces' / 'agent-autolearn'
        head = subprocess.run(['git', '-C', str(market), 'rev-parse', 'HEAD'], stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        commit = by_name['code-reviewer']['commit']
        self.assertNotEqual(commit, head)
        shown = subprocess.run(['git', '-C', str(market), 'show', f'{commit}:plugins/agent-autolearn/agents/code-reviewer.md'], stdout=subprocess.PIPE, check=True).stdout
        self.assertEqual(shown, (cache / 'agents' / 'code-reviewer.md').read_bytes())
        for c in item['component_versions']:  # toda version registrada apunta a un commit con el contenido exacto usado
            for path in c['paths']:
                blob = subprocess.run(['git', '-C', str(market), 'show', f"{c['commit']}:{path}"], stdout=subprocess.PIPE, check=True).stdout
                self.assertEqual(blob, (cache / path.removeprefix('plugins/agent-autolearn/')).read_bytes(), path)
        self.assertTrue(installed_commit)
        self.assertTrue(all(p.startswith('plugins/agent-autolearn/') for c in item['component_versions'] for p in c['paths']))
        self.assertIsNone(result['component_versions_note'])

    def test_explicit_plugin_repo_env_and_local_edits_are_not_registered(self):
        # Copia instalada fuera de la estructura de cache: solo AGENT_AUTOLEARN_PLUGIN_REPO permite encontrar el clon.
        clone = self.plugin_checkout(with_git=True, name='clon-explicito').parent.parent
        shutil.copytree(PLUGIN.parent.parent / '.claude-plugin', clone / '.claude-plugin')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
               'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        for a in (['add', '.'], ['commit', '-q', '--no-gpg-sign', '-m', 'm']):
            subprocess.run(['git', '-C', str(clone), *a], env=env, check=True, stdout=subprocess.DEVNULL)
        cache = self.temp / 'otra-instalacion' / 'agent-autolearn'
        shutil.copytree(clone / 'plugins' / 'agent-autolearn', cache)
        os.environ['CLAUDE_PLUGIN_ROOT'] = str(cache)
        self.configure()
        repo, run = self.finalized_run()
        self.assertIn('AGENT_AUTOLEARN_PLUGIN_REPO', review_sync.enqueue(run['run_dir'])['component_versions_note'])
        os.environ['AGENT_AUTOLEARN_PLUGIN_REPO'] = str(clone)
        (cache / 'agents' / 'code-reviewer.md').write_text('editado a mano en la cache\n')
        repo, run = self.finalized_run()
        result = review_sync.enqueue(run['run_dir'])
        self.assertIn('reviewer:code-reviewer', result['component_versions_note'])
        self.assertGreater(result['component_versions'], 0)
        item = json.loads((repo.root / '.pre-pr-review' / 'sync' / 'queue' / f"{result['client_run_id']}.json").read_text())
        self.assertNotIn('code-reviewer', {c['name'] for c in item['component_versions']})

    def test_previous_pass_is_linked_by_local_uuid(self):
        self.configure()
        repo, first = self.finalized_run()
        repo.write('app.py', 'value = 2\n')
        second = repo.prepare()
        repo.complete(second, statuses={fingerprint(finding()): 'closed'})
        second_id = review_sync.enqueue(second['run_dir'])['client_run_id']
        # Encolada tarde: la primera pasada conserva su propio estado, no el del ledger actual.
        first_id = review_sync.enqueue(first['run_dir'])['client_run_id']
        first_item = json.loads((review_sync.queue_dir(repo.root.resolve()) / f'{first_id}.json').read_text())
        self.assertEqual([f['decision'] for f in first_item['result']['findings']], ['abierto'])
        state = self.push(repo)
        self.assertEqual({state['runs'][first_id]['state'], state['runs'][second_id]['state']}, {'sent'})
        order = [b['client_run_id'] for b in (r['body'] for r in self.api.runs.values())]
        self.assertEqual(order, [second_id, first_id], 'se envia en orden de encolado')
        item = json.loads((review_sync.queue_dir(repo.root.resolve()) / f'{second_id}.json').read_text())
        self.assertEqual(item['run']['previous_run_id'], first_id)
        closed = [f for f in item['result']['findings'] if f['fingerprint'] == fingerprint(finding())]
        self.assertEqual((closed[0]['decision'], closed[0]['verification']['status']), ('cerrado', 'closed'))

    def test_no_remote_never_sends_local_path(self):
        self.configure()
        repo, run = self.finalized_run(remote=None)
        result = review_sync.enqueue(run['run_dir'])
        self.assertFalse(result['queued'])
        self.assertIn('origin', result['reason'])

    def test_status_shows_workspace_and_queue(self):
        from io import StringIO
        from contextlib import redirect_stdout
        self.configure()
        repo, run = self.finalized_run()
        review_sync.enqueue(run['run_dir'])
        buf = StringIO()
        with redirect_stdout(buf):
            review_sync.main(['status', '--repo', str(repo.root), '--json'])
        out = json.loads(buf.getvalue())
        self.assertEqual((out['workspace'], out['queue']['pending']), ('Workspace Personal', 1))
        self.assertNotIn(TOKEN, buf.getvalue())


if __name__ == '__main__':
    unittest.main()
