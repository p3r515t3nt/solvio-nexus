"""Observation readback uses real temporary Router/Cost/Task/native ledgers.

Only native provider events are synthetic. The local command actually runs;
portal_list uses the registered Core handler and an absent temporary vault.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_sessions as T
import test_agent_portal_cost_dispatch as P
from solvio.agent_runtime import native_observations as O, native_tools as NT
from solvio.agent_runtime import native_tasks as TASK, artifact_creation as A, native_result_files as F


def block(text):
    return {'text': text, 'original_chars': len(text), 'complete': True, 'redacted': False}


@contextmanager
def world(*, local=True):
    with T.world() as w:
        w.router = P.R.CapabilityRouter()
        w.router._task_authority = w.authority
        P.P.register(w.router, P.P.PortalCapabilities(P.NoPortalClient(), P.PortalVault(str(w.folder / 'absent-vault'))))
        w.starts.router = w.router
        w.task, w.run = w.starts.create(objective='Lies die Portale, prüfe sie lokal und liefere eine Datei.',
            scope='task', origin='trusted_dashboard', principal='owner',
            receipt=T.VerifiedTaskReceipt('dashboard_session', 'observation:one', 'owner'), request_id='observation-one')
        actions = [{'id': 'f1', 'text': 'Liefere eine Datei.', 'effect': 'file'},
                   {'id': 'x1', 'text': 'Sende sie an ein Portal.', 'effect': 'external'}]
        if local:
            actions += [{'id': 'l1', 'text': 'Lies die lokalen Portale.', 'effect': 'local_execution'},
                        {'id': 'l2', 'text': 'Prüfe die gelesenen Daten lokal.', 'effect': 'local_execution'}]
        requirements = T.RQ.validate({'auskunft': [{'id': 'a1', 'text': 'Nenne die Portale.'}], 'handlungen': actions}, objective=w.task.objective)
        w.ledger.bind_requirements(w.task.task_id, json.dumps(requirements))
        w.workspace = w.folder.resolve() / 'task-workspace'
        w.workspace.mkdir(mode=0o700)
        w.session = w.manager.bind(task_id=w.task.task_id, run_id=w.run.run_id,
            provider='codex', profile=F.PROFILE, policy_digest=T.POLICY, workspace=str(w.workspace))
        w.ledger.transition(w.run.run_id, T.S.PLANNING)
        w.ledger.transition(w.run.run_id, T.S.RUNNING)
        w.step = w.ledger.create_step(run_id=w.run.run_id, seq=1, kind='specialist', specialist_profile=F.PROFILE)
        w.ledger.update_step(w.step.step_id, state='running', started=True)
        w.tools = NT.NativeCoreTools(w.ledger, w.manager, w.session.session_id, w.run.run_id, w.router)
        yield w


async def produce(w, *, portal=True, command=True, web=True, receipts=None, legacy=False,
                  extra_commands=(), initial_receipts=()):
    observed = list(initial_receipts)
    async def native():
        turn, _ = T.request(w)
        w.invocation = turn.invocation_id
        w.manager.bind_thread(w.invocation, 'native-thread-1')
        w.manager.started(w.invocation, native_thread_id='native-thread-1', native_turn_id='native-turn-1')
        if portal:
            response = await w.tools.call({'threadId': 'native-thread-1', 'turnId': 'native-turn-1',
                'callId': 'portal-read', 'tool': 'portal_list', 'arguments': {}})
            require(response['success'], response)
            w.response = response
            (w.workspace / 'portal.json').write_text(response['contentItems'][0]['text'])
            observed.append({'kind':'dynamicToolCall','item_id':'portal-read','status':'completed','tool':'portal_list','success':True})
        if command:
            code = "import json; from pathlib import Path; data=json.loads(Path('portal.json').read_text()); assert isinstance(data['data']['portale'],list); print('PORTAL_OK')" if portal else "print('LOCAL_OK')"
            argv = [sys.executable, '-c', code]
            process = await asyncio.create_subprocess_exec(*argv, cwd=w.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            stdout, _ = await process.communicate()
            require_equal(process.returncode, 0)
            text = shlex.join(argv)
            observed.append({'kind':'commandExecution','item_id':'python-check','status':'completed','exit_code':0,
                'command_sha256':hashlib.sha256(text.encode()).hexdigest(),'command':block(text),'output':block(stdout.decode())})
        for item_id, argv in extra_commands:
            process = await asyncio.create_subprocess_exec(*argv,cwd=w.workspace,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            stdout,_ = await process.communicate()
            require_equal(process.returncode,0)
            text=shlex.join(argv)
            observed.append({'kind':'commandExecution','item_id':item_id,'status':'completed','exit_code':0,
                'command_sha256':hashlib.sha256(text.encode()).hexdigest(),'command':block(text),'output':block(stdout.decode())})
        if web:
            base = dict(runtime='hermes-codex-app-server', invocation_id=w.invocation, operation_id=w.step.step_id,
                native_thread_id='native-thread-1', native_turn_id='native-turn-1')
            # A historical start marker is not itself a web proof.
            w.ledger.record_event(w.run.run_id, 'native_progress', 'Started', step_id=w.step.step_id,
                ref=json.dumps(dict(base, native_turn_id='earlier-start-marker', seq=1, event='started', status='', item_id='')))
            for index in range(int(web)):
                item_id='web-doc' if index==0 else 'web-doc-'+str(index+1)
                observed.append({'kind':'webSearch','item_id':item_id,'status':'completed',
                    'query':block('Python csv docs'),'action':'openPage','urls':['https://docs.python.org/3/library/csv.html'],'urls_complete':True})
                for seq,status in ((2+index*2,'started'),(3+index*2,'completed')):
                    w.ledger.record_event(w.run.run_id, 'native_progress', 'Web item observed', step_id=w.step.step_id,
                        ref=json.dumps(dict(base,seq=seq,event='web_search',status=status,item_id=item_id)))
        w.manager.terminal(w.invocation, native_thread_id='native-thread-1', native_turn_id='native-turn-1', status='completed')
    result = await T.dispatch(w, w.step.step_id, native)
    require_equal(result.cost_status, 'settled')
    outcome = SimpleNamespace(cost_invocation_id=w.invocation, native_thread_id='native-thread-1',
        native_turn_id='native-turn-1', native_tool_receipts=tuple(observed if receipts is None else receipts))
    if legacy:
        context = F._context(w.manager,w.session.session_id,w.run.run_id,w.step.step_id,w.invocation,
                             'native-thread-1','native-turn-1',(),None)
        body = dict(context, kind=O.KIND, receipts=list(outcome.native_tool_receipts), attestation='Historical native tool observations.')
        A._record(w.ledger,w.run.run_id,w.step.step_id,O.KIND,'native-turn-'+w.step.step_id+'.json',A._json(body))
    else:
        TASK._retain_observation(w.manager,w.session.session_id,w.run.run_id,w.step.step_id,outcome,None)
    w.ledger.update_step(w.step.step_id, state='succeeded', finished=True)
    w.artifact = next(a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind == O.KIND)
    return O.completion_evidence(w.ledger,w.run.run_id)


async def t_observation_material_is_redacted_line_wise_and_refused_only_for_a_key_shape():
    """Measured on the third real Durchstich (19.09.2026 12:01): the worker had
    printed the Python csv documentation ("a dict whose keys are given by …")
    into a command output; the whole-JSON statement heuristic (a credential WORD
    plus any colon — JSON always has one) refused the observation and the
    finished job vanished as native_result_not_retained. Observations are a
    Verlauf: a line that looks like a credential becomes the marker, the block
    says redacted=true, the record stays; a key-shaped VALUE still refuses."""
    from solvio.secret_vault.firewall import TRANSCRIPT_MARKER, CredentialRefused
    docs = ("L118: if the dictionary is missing a key in fieldnames\n"
            "L119: the optional fieldnames parameter is a sequence of keys that identify the order\n"
            "ASSERT header=['element', 'zweck', 'quelle']\n")
    text = 'python3 -c "print(1)"'
    reading = dict(kind='commandExecution', item_id='docs-read', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(text.encode()).hexdigest(), command=block(text), output=block(docs))
    leak_text = "cat config.ini"
    leaking = dict(kind='commandExecution', item_id='config-cat', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(leak_text.encode()).hexdigest(), command=block(leak_text),
        output=block("[db]\nhost = localhost\npassword: hunter2-4711\nport = 5432\n"))
    with world() as w:
        items = await produce(w, portal=False, command=False, web=False, receipts=[reading, leaking])
        proof = json.loads(Path(w.artifact.path).read_text())
        stored = {r['item_id']: r for r in proof['receipts']}
        # Documentation lines that NAME a credential term beside a colon are redacted one by one
        # (the accepted cost of the per-line statement heuristic); the verdict line stays.
        out = stored['docs-read']['output']
        require_equal(out['text'], TRANSCRIPT_MARKER + "\n" + TRANSCRIPT_MARKER + "\nASSERT header=['element', 'zweck', 'quelle']\n")
        require_equal((out['redacted'], out['original_chars']), (True, len(docs)))
        # The one credential line is the marker; the rest of the block survives, marked redacted.
        out = stored['config-cat']['output']
        require_equal(out['text'], "[db]\nhost = localhost\n" + TRANSCRIPT_MARKER + "\nport = 5432\n")
        require_equal((out['redacted'], out['original_chars'], out['complete']), (True, 57, True))
        require_equal(leaking['output']['text'].count('hunter2'), 1, "the worker's own receipt object was mutated")
        require('hunter2' not in Path(w.artifact.path).read_text())
        material = json.loads(items[0].finding.split('\n', 1)[1])
        require_equal([r['item_id'] for r in material['native_receipts']], ['docs-read', 'config-cat'])
    # Review Runde 9, F9-1: the common shapes of tool material — .env/shell with a prefix,
    # JSON, prose, and a label whose value sits on the next line — never reach the book.
    from solvio.secret_vault.firewall import redact_lines_if_credential
    leaky = ("DB_PASSWORD=hunter2xyz\nexport GITHUB_TOKEN=abcdefghijklmnop1234\n\"api_key\": \"abcd1234efgh\"\n"
             "Mein Passwort ist Hund1234\nPasswort:\nSommer2024!\nhost = localhost\n")
    cleaned, changed = redact_lines_if_credential(leaky, where='probe')
    require(changed and cleaned.count(TRANSCRIPT_MARKER) == 6 and cleaned.endswith("host = localhost\n"), cleaned)
    for secret in ("hunter2xyz", "abcdefghijklmnop1234", "abcd1234efgh", "Hund1234", "Sommer2024!"):
        require(secret not in cleaned, secret)
    kept, changed = redact_lines_if_credential("a dict whose keys are given by fieldnames\nASSERT header=[x]\nPATH=/usr/bin\n", where='probe')
    require(not changed, kept)
    # Review round 11, F11-1: a quoted value with spaces and a YAML block scalar carry the
    # label — the VALUE must fall with it, end to end (book and assessor material).
    env_text = "cat .env config.yaml"
    env_out = ('PASSWORD="my secret pass"\nexport TOKEN="abc def ghi"\nconfig:\n  password: |\n    hunter2xyz\n  host: db\n'
               'db:\n  passphrase: >\n    S3cretPass\nPATH=/usr/bin\n')
    envy = dict(kind='commandExecution', item_id='env-cat', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(env_text.encode()).hexdigest(), command=block(env_text), output=block(env_out))
    with world() as w:
        items = await produce(w, portal=False, command=False, web=False, receipts=[envy])
        book = Path(w.artifact.path).read_text()
        material = items[0].finding
        for secret in ('my secret pass', 'abc def ghi', 'hunter2xyz', 'S3cretPass'):
            require(secret not in book and secret not in material, secret + ' reached the book or the assessor')
        require('host: db' in book and 'PATH=/usr/bin' in book, 'the harmless lines vanished')
    # Review round 10, W10-1: helper SOURCE (prose=False) — syntax and identifiers that end in
    # or contain a credential term are code; an assigned literal or a quoted label still counts.
    from solvio.secret_vault.firewall import has_key_shape, looks_like_credential_line
    for line in ("    return token", "    yield token", "    for token in tokens:", "class Token:", "    secret = None",
                 "    tokens = line.split()", "    # parse the auth token", "    self.secret", '    """Author: Max Mustermann"""'):
        require(not looks_like_credential_line(line, prose=False), line)
    for line in ('    DB_PASSWORD = "hunter2-4711"', '    "password":', "AUTH_TOKEN: 'xyz12345'",
                 "    key = 'sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2'",
                 # review round 11, F11-1: literal forms of helper code
                 "SECRET = b'hunter2xyz'", "SECRET = f'hunter2xyz'", "token: str = 'hunter2xyz'", "TOKEN = ('hunter2xyz')",
                 'TOKEN = """hunter2xyz"""', 'password = "hun ter2"', "tokens = 'hunter2secret'", "secrets = 'hunter2secret'"):
        require(looks_like_credential_line(line, prose=False), line)
    for line in ("    tokens: list[str] = line.split()", "    token = None", "    secret=None",
                 # since review round 12 (H12-4) a non-literal value is code: reading the environment
                 # leaks nothing into the helper file, and `AUTH_TOKEN: xyz12345` is a bare word
                 "    api_key = os.environ['X']", "AUTH_TOKEN: xyz12345"):
        require(not looks_like_credential_line(line, prose=False), line)
    # Review round 11, H11-1: credentials without a term — URL userinfo and `-u user:pw`.
    for line in ("DATABASE_URL=postgres://app:pw12345@db.internal/app", "curl -u admin:hunter2xyz https://x", "wget --user admin:hunter2xyz"):
        require(looks_like_credential_line(line) and looks_like_credential_line(line, prose=False), line)
    for line in ("URL=https://docs.python.org/3/library/csv.html", "curl -u admin https://x", "git@github.com:org/repo.git"):
        require(not looks_like_credential_line(line, prose=False), line)
    require(looks_like_credential_line("Author: Max Mustermann") is False, "Author is not auth")
    # Review round 12, H12-4: in helper SOURCE only a string literal is a value — a tokenizer
    # helper (`self.token = token`, `token = get_token()`) is code, not a credential.
    for line in ("self.token = token", "token = tokens[0]", "token = get_token()", "tokenizer = Tokenizer()",
                 "tokenize = lambda s: s.split()", "cookie_jar = CookieJar()", "session_id = uuid4().hex",
                 "TOKEN_RE = re.compile(r'[A-Za-z]+')", "Authorization = header.get('Authorization')",
                 "password_hash = hash_password(pw)", "    tokens: list[str] = []"):
        require(not looks_like_credential_line(line, prose=False), line)
    for line in ("token: dict[str, int] = 'abcd1234'", 'headers = {"Authorization": "Bearer abcdefgh"}', 'AUTH_TOKEN="hunter2xyz"'):
        require(looks_like_credential_line(line, prose=False), line)
    # … but a shell, JSON or text helper knows no identifiers: a bare value counts there.
    for line in ("export TOKEN=hunter2xyz", "DB_PASSWORD=hunter2xyz", '"api_key": "abcd1234efgh"', "AUTH_TOKEN: xyz12345"):
        require(looks_like_credential_line(line, prose=False, bare_values=True), line)
    for line in ("TOKEN=$1", 'TOKEN=""', "token=null", '"token": null', "echo token", "secrets=()"):
        require(not looks_like_credential_line(line, prose=False, bare_values=True), line)
    # Review round 12, H12-2: a 16000-character line costs milliseconds, not seconds (the
    # assignment and URL shapes backtracked quadratically).
    import time as _time
    started = _time.monotonic()
    for long_line in ("token:" + " " * 16000, "A" * 16000, "token: str" + " " * 16000, "x:" * 8000, "-u " + "a" * 16000):
        looks_like_credential_line(long_line)
        looks_like_credential_line(long_line, prose=False)
    require(_time.monotonic() - started < 1.0, "long lines take seconds")
    # Review round 12, H12-1a: a YAML block scalar takes EVERY deeper-indented line, and the
    # block ends at the first line indented no deeper (blank lines do not end it).
    yaml_out, changed = redact_lines_if_credential(
        "db:\n  password: |\n    line1secret\n    line2secret\n\n    line3secret\n  host: db\nnext: x\n", where='probe')
    require(changed and yaml_out.count(TRANSCRIPT_MARKER) == 4 and "host: db" in yaml_out and "next: x" in yaml_out, yaml_out)
    for secret in ("line1secret", "line2secret", "line3secret"):
        require(secret not in yaml_out, secret)
    flat_out, _ = redact_lines_if_credential("Passwort:\n\nHund1234\nweiter\n", where='probe')
    require(flat_out.count(TRANSCRIPT_MARKER) == 2 and "Hund1234" not in flat_out and "weiter" in flat_out, flat_out)
    # H12-1b: a comment between label and value does not carry the value.
    comment_out, _ = redact_lines_if_credential("password:\n# comment\nhunter2xyz\nnext\n", where='probe')
    require(comment_out == TRANSCRIPT_MARKER + "\n# comment\n" + TRANSCRIPT_MARKER + "\nnext\n", comment_out)
    # H12-1c/d/e: typographic quotes, the short terms `pass`/`pw`/`Geheimnis`, and the Python
    # idioms around a term (tuple, subscript, union annotation, escaped quote).
    for line in ("PASSWORD=„my secret pass“", "PASS=hunter2xyz", "pw: hunter2xyz", "Geheimnis: Hund1234"):
        require(looks_like_credential_line(line), line)
    for line in ("auth = ('app', 'hunter2xyz')", "headers['Authorization'] = 'Basic YXBwOmh1bnRlcjI='", "os.environ['PASSWORD'] = 'hunter2'",
                 "config['password'] = 'hunter2'", "TOKEN: str | None = 'hunter2'", "PASSWORD = 'hu\\'nter2'",
                 "requests.get(url, auth=('app','hunter2xyz'))"):
        require(looks_like_credential_line(line) and looks_like_credential_line(line, prose=False), line)
    for line in ("tests pass: 5/5", "$ pwd", "pwd: /Users/x/work", "passed=true", "login: app", "compass=north", "PASSED=5364"):
        require(not looks_like_credential_line(line), line)
    # Review round 13, W13-2: `_` is a word character — the common prefix forms fell through.
    for line in ("DB_PASS=hunter2xyz", "db_pass=hunter2xyz", "SMTP_PASS: hunter2xyz", "ADMIN_PW=hunter2xyz",
                 "redis_pass = 'hunter2xyz'", "export PGPASS=hunter2xyz", "MYSQL_PWD=hunter2xyz"):
        require(looks_like_credential_line(line) and looks_like_credential_line(line, prose=False, bare_values=True), line)
    # Review round 14, R14-W2: a Bearer VALUE is token-like; documentation prose and the
    # standard curl placeholder are no key shape (a key shape refuses the whole record).
    for prose in ("Bearer authentication", "Authorization: Bearer $OPENAI_API_KEY", "Authorization: Bearer <YOUR_ACCESS_TOKEN>"):
        require(not has_key_shape(prose), prose)
    for value in ("Authorization: Bearer abcdefghijklmnop12345", "Bearer eyJhbGciOiJIUzI1NiJ9abc"):
        require(has_key_shape(value), value)
    # Review round 17, K17-1: the HOUSE heuristic (chat, voice, memory) keeps the production
    # breadth of the bearer shape — a bare bearer value without a digit is a key shape there,
    # while the worker-material fence keeps the token-like form above.
    from solvio.secret_vault.firewall import is_credential, redact_if_credential
    for line in ("Bearer abcdefghijklmnop", "Nutze Bearer AbCdEfGhIjKlMnOpQrSt fuer die API", "Bearer abcdefghijklmn0p"):
        require(is_credential(line), line)
        require(redact_if_credential(line, where="conversation.add_message") != line, line)
    # … and, as in production, the broad house form also takes the documentation prose
    # (a typed knowledge sentence about Bearer authentication is fenced: DEBT-0295, architect)
    require(is_credential("Bearer authentication ist ein Schema"))
    require(not has_key_shape("Bearer authentication ist ein Schema"), "the material fence must stay token-like")
    # ledger material (Review Runde 16): identifier glue is no secret symbol, numbers behind strong
    # terms are, a scheme word does not shield the value, plural verbs count
    for line in ("auth: api_key", "auth: logged_out", "auth: claude.ai", "reason: subscription_required", "Token-Limit: 128000",
                 "Passwort-Feld: leer", "Passwort geaendert am 2026-09-20", "Tokens: 1200",
                 # round 18, H18-2: a year behind a strong term and a digit-count are prose
                 "Passwort 2025 rotieren", "Kennwort 2026 erneuern", "Die PIN ist 4-stellig", "Das Passwort ist 12-stellig"):
        require(not looks_like_credential_line(line, prose=False, core_material=True), line)
    for line in ("password: 12345678", "Passwort: 12345678", "TAN: 482913", "Das Passwort ist 12345678", "PIN 4711",
                 "Authorization: Basic YWRtaW46aHVudGVyMg==", "Authorization: Bearer abcdefghijklmnopq123",
                 "die Zugangsdaten sind admin:Sommer2024", "Session-ID: 9f8e7d6c5b4a", "password: hunter2xyz"):
        require(looks_like_credential_line(line, prose=False, core_material=True), line)
    # chat mode (the line fence's part of the history fence; the memory heuristic runs beside it):
    # a question about credentials is no VALUE form, a statement WITH a value is
    for line in ("Was ist der Unterschied zwischen Basic Auth und Bearer Tokens?", "Wie viele Token hat der Text?", "Erklaer mir Bearer authentication bei REST-APIs"):
        require(not looks_like_credential_line(line, chat=True), line)
    for line in ("Mein Amazon-Passwort ist Hund1234", "Mein Passwort ist Sonnenblume.", "Meine PIN 4711", "Das Kennwort lautet Winterzeit99", 'PASSWORD="mein geheimes pass"'):
        require(looks_like_credential_line(line, chat=True), line)
    for line in ("    pass", "passed = True", "pwd = os.getcwd()", "auth = get_auth()", "headers['Authorization'] = header",
                 "config['password'] = value", "    if passwords:", "pw = None", "PASS = 1"):
        require(not looks_like_credential_line(line, prose=False), line)
    # Review round 12, H12-1f: further well-known provider forms are key shapes.
    for shaped in ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "glpat-" + "xxxxxxxxxxxxxxxxxxxx", "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
                   "hf_" + "abcdefghijklmnopqrstuvwxyz", "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789"):
        require(has_key_shape(shaped), shaped)
    require(not has_key_shape("sk_live_short") and not has_key_shape("npm_install_done"), "words are not shapes")
    # A key-shaped VALUE refuses the whole record BEFORE the line pass (review round 10,
    # B10-1): the line pass would remove exactly the signature the refusal needs — a PEM
    # header — while the base64 body lines, or a value split across two receipts, stayed.
    # ADR-0029: refuse, never sanitize. The record is never written.
    from solvio.secret_vault.firewall import refuse_if_key_shaped
    key = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    pem_body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUx\nOQAAACBQ"
    for item_id, text, output in (
            ("env-dump", "printenv", "PATH=/usr/bin\nTOKEN=" + key + "\nHOME=/Users/x\n"),
            ("key-cat", "cat id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\n" + pem_body + "\n-----END OPENSSH PRIVATE KEY-----\n"),
            ("key-tail", "tail -n +2 id_ed25519", pem_body + "\n-----END OPENSSH PRIVATE KEY-----\n")):
        keyed = dict(kind='commandExecution', item_id=item_id, status='completed', exit_code=0,
            command_sha256=hashlib.sha256(text.encode()).hexdigest(), command=block(text), output=block(output))
        header = dict(kind='commandExecution', item_id='key-head', status='completed', exit_code=0,
            command_sha256=hashlib.sha256(b"head -1 id_ed25519").hexdigest(), command=block("head -1 id_ed25519"),
            output=block("-----BEGIN OPENSSH PRIVATE KEY-----\n"))
        # key-tail alone carries no shape; beside the header receipt the record is a split key.
        receipts = [header, keyed] if item_id == "key-tail" else [keyed]
        with world() as w:
            try:
                await produce(w, portal=False, command=False, web=False, receipts=receipts)
            except CredentialRefused as exc:
                require_equal(exc.reason, 'key_shaped_value', item_id)
            else:
                raise AssertionError(item_id + ': a key-shaped observation was retained')
            require_equal([a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind == O.KIND], [],
                          item_id + ': the refused record was written')
            for path in Path(w.folder).rglob("native-turn-*.json"):
                require(key not in path.read_text() and pem_body.split("\n")[0] not in path.read_text(), str(path))
    # Review round 11, H11-2: `Authorization: Bearer` + newline + token is a key shape in
    # raw text but not in its JSON form — the write side checks the raw field texts.
    split_text = "curl -v https://api.example"
    split = dict(kind='commandExecution', item_id='bearer-split', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(split_text.encode()).hexdigest(), command=block(split_text),
        output=block("> Authorization: Bearer\nabcdefghijklmnopqrstuvwxyz0123\n< HTTP/1.1 200\n"))
    with world() as w:
        try:
            await produce(w, portal=False, command=False, web=False, receipts=[split])
        except CredentialRefused as exc:
            require_equal(exc.reason, 'key_shaped_value')
        else:
            raise AssertionError('a bearer token split over a newline was retained')
    try:
        refuse_if_key_shaped('{"x": "' + key + '"}', where='probe')
    except CredentialRefused as exc:
        require_equal(exc.reason, 'key_shaped_value')
    else:
        raise AssertionError('the whole-record guard let a key shape through')
    refuse_if_key_shaped('{"note": "a dict whose keys are given by: fieldnames"}', where='probe')  # a word is not a key


async def t_the_projection_never_exceeds_its_budget_whatever_the_receipt_sizes():
    """Measured 19.09.2026 (third real Durchstich, attempt 7): the trimmed item's id was
    counted AFTER the size check; a projection a few bytes over the budget made the
    whole observation `unconfirmed` and the finished job goal_unverified before any
    assessment. Sweep output sizes around the budget: the finding always fits."""
    # The measured shape first: two whole late checks fill the budget so that the earlier
    # big item is trimmed into the REMAINING room (not the share); its long item_id is what
    # the old code counted after the size check.
    long_id = 'exec-' + 'a1b2c3d4-' * 12 + 'end'
    require(len(long_id) <= 128)
    text = 'python3 csv_helper.py && python3 - <<PY\nassert True\nPY'
    big = dict(kind='commandExecution', item_id=long_id, status='completed', exit_code=0,
        command_sha256=hashlib.sha256(text.encode()).hexdigest(), command=block(text), output=block('z' * 9000))
    late = [dict(kind='commandExecution', item_id='late-' + str(i), status='completed', exit_code=0,
        command_sha256=hashlib.sha256(('python3 late%d.py' % i).encode()).hexdigest(),
        command=block('python3 late%d.py' % i), output=block('ok ' * 1100)) for i in (1, 2)]
    with world() as w:
        items = await produce(w, command=False, web=2, receipts=[big, *late])
        require(len(items[0].finding) <= O.MAX_MATERIAL_CHARS, len(items[0].finding))
        material = json.loads(items[0].finding.split('\n', 1)[1])
        projected = [r['item_id'] for r in material['native_receipts']]
        require('late-1' in projected and 'late-2' in projected, projected)
        require(long_id in material['trimmed_native_item_ids'] or long_id in material['omitted_native_item_ids'],
                'the big item is neither projected (trimmed) nor named as omitted')
    # Review round 10, W10-2: a line-dense check output (JSON doubles every newline) beside a
    # 6000-char receipt was OMITTED although budget was free — the raw-character cut overshot
    # the JSON fit test by its newline count. It must be projected trimmed, head and tail real.
    dense = 'noise line\n' * 350 + 'FAIL: expected 3 rows\n' + 'noise line\n' * 350 + 'ASSERT rows=3 ok\n'
    dense_text = 'python3 check_rows.py'
    dense_item = dict(kind='commandExecution', item_id='dense-check', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(dense_text.encode()).hexdigest(), command=block(dense_text), output=block(dense))
    other_text = 'python3 other.py'
    other = dict(kind='commandExecution', item_id='other', status='completed', exit_code=0,
        command_sha256=hashlib.sha256(other_text.encode()).hexdigest(), command=block(other_text), output=block('w' * 6000))
    with world() as w:
        items = await produce(w, command=False, web=False, receipts=[dense_item, other])
        require(len(items[0].finding) <= O.MAX_MATERIAL_CHARS, len(items[0].finding))
        material = json.loads(items[0].finding.split('\n', 1)[1])
        require_equal(material['omitted_native_item_ids'], [], 'the line-dense receipt was omitted although budget was free')
        require_equal(material['trimmed_native_item_ids'], ['dense-check'])
        shown = next(r for r in material['native_receipts'] if r['item_id'] == 'dense-check')['output']['text']
        require(shown.startswith('noise line\n') and shown.endswith('ASSERT rows=3 ok\n') and 'Zeichen ausgelassen' in shown, shown[-120:])
        require(len(items[0].finding) > O.MAX_MATERIAL_CHARS - 1500, 'the projection left most of the budget unused')
    sizes = [O.MAX_MATERIAL_CHARS + delta for delta in range(-900, 901, 150)] + [3000, 5000, 15000]
    for size in sizes:
        text = 'python3 check.py --size ' + str(size)
        big = dict(kind='commandExecution', item_id='big-' + str(size), status='completed', exit_code=0,
            command_sha256=hashlib.sha256(text.encode()).hexdigest(), command=block(text), output=block('y' * size))
        small_text = 'python3 small.py'
        small = dict(kind='commandExecution', item_id='small', status='completed', exit_code=0,
            command_sha256=hashlib.sha256(small_text.encode()).hexdigest(), command=block(small_text), output=block('ok\n' * 40))
        failed = dict(small, item_id='early-failure', status='failed', exit_code=1, output=block('boom\n' * 200))
        with world() as w:
            items = await produce(w, command=False, web=2, receipts=[failed, big, small])
            require(len(items[0].finding) <= O.MAX_MATERIAL_CHARS, (size, len(items[0].finding)))
            material = json.loads(items[0].finding.split('\n', 1)[1])
            named = set(material['omitted_native_item_ids']) | {r['item_id'] for r in material['native_receipts']}
            require_equal(named, {'early-failure', 'big-' + str(size), 'small'}, 'every receipt is either projected or named as omitted')
            require(set(material['trimmed_native_item_ids']) <= {r['item_id'] for r in material['native_receipts']})


def rejected(call):
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError('unconfirmed observation accepted')


async def t_actual_python_portal_and_web_readback_is_reopenable_local_candidate_only():
    with world() as w:
        items = await produce(w)
        require_equal([x.requirement for x in items], ['f1','l1','l2'])  # file criterion too (DEBT-0289 H5 pattern), never the external x1
        require_equal(items[0].finding,items[1].finding)
        require(items[0].evidence != items[1].evidence)
        material = json.loads(items[0].finding.split('\n',1)[1])
        require_equal(material['origin']['artifact_sha256'],w.artifact.sha256)
        require_equal(material['core_results'][0]['result']['data'],json.loads(w.response['contentItems'][0]['text'])['data'])
        require_equal(material['core_results'][0]['cost']['settlement_state'],'settled')
        require_equal(material['omitted_native_item_ids'],[])
        require_equal(material['omitted_core_call_ids'],[])
        require('PORTAL_OK' in items[0].finding)
        require('docs.python.org' in items[0].finding)
        require_equal(len(material['web_events']),1)
        require_equal(O.completion_evidence(T.S.AgentRunLedger(w.ledger.path),w.run.run_id),items)
        require_equal(w.costs.view(w.task.task_id)['counts'],{'settled':2})
        # Failed semantic judgement cannot erase historical observation or create success.
        w.ledger.transition(w.run.run_id,T.S.FAILED,failure_category='goal_unverified')
        w.authority.revoke(w.authority.for_run(w.run.run_id).reference,'test:expired-history')
        require_equal(O.completion_evidence(w.ledger,w.run.run_id),items)


async def t_hash_only_redacted_missing_output_and_legacy_tool_boolean_are_context():
    text = 'python helper.py'
    base = dict(kind='commandExecution',item_id='cmd',status='completed',exit_code=0,command_sha256=hashlib.sha256(text.encode()).hexdigest())
    cases = [base,dict(base,command=block(text),output={'text':'','original_chars':None,'complete':False,'redacted':False}),
             dict(base,command=block(text),output={'text':'<entfernt>','original_chars':50,'complete':False,'redacted':True}),
             dict(base,command=block(text),output=block('failure'),status='failed',exit_code=1)]
    for receipt in cases:
        with world() as w:
            items = await produce(w,portal=False,command=False,web=False,receipts=[receipt])
            require_equal([x.requirement for x in items],[''])
    with world() as w:
        items = await produce(w,command=False,web=False,legacy=True)
        context = json.loads(items[0].finding.split('\n',1)[1])
        require_equal([x.requirement for x in items],[''])
        require_equal(context['core_results'],[])
        require(context['legacy_core_payload_unattested'])
    with world(local=False) as w:
        items = await produce(w,web=False)
        require_equal([x.requirement for x in items],['f1'])  # the file criterion gets the observation token, x1 (external) never


async def t_mutated_task_run_revision_turn_producer_grant_or_settlement_fails_closed():
    mutations = [
        "UPDATE agent_native_turns SET revision_digest='foreign' WHERE invocation_id=?",
        "UPDATE agent_native_turns SET native_turn_id='foreign' WHERE invocation_id=?",
        "UPDATE agent_native_turns SET terminal_status='failed' WHERE invocation_id=?",
        "UPDATE agent_native_turns SET reservation_id='foreign' WHERE invocation_id=?",
        "UPDATE agent_provider_invocations SET request_digest='foreign' WHERE invocation_id=?",
        "UPDATE agent_provider_invocations SET operation_id='foreign' WHERE invocation_id=?",
        "UPDATE agent_cost_reservations SET state='unknown' WHERE invocation_id=?",
        "UPDATE agent_provider_invocations SET finished_at=NULL WHERE invocation_id=?",
    ]
    for sql in mutations:
        with world() as w:
            await produce(w,web=False)
            with w.ledger._open() as db:
                db.execute(sql,(w.invocation,))
            rejected(lambda: O.completion_evidence(w.ledger,w.run.run_id))
    for sql in ["UPDATE agent_steps SET state='failed' WHERE step_id=?",
                "UPDATE agent_steps SET specialist_profile='researcher/hermes' WHERE step_id=?"]:
        with world() as w:
            await produce(w,web=False)
            with w.ledger._open() as db:
                db.execute(sql,(w.step.step_id,))
            rejected(lambda: O.completion_evidence(w.ledger,w.run.run_id))


async def t_actual_core_receipt_must_remain_equal_to_immutable_artifact():
    for case in ('response','request','turn','grant','step','local_cost','local_route','artifact_bytes'):
        with world() as w:
            await produce(w,web=False)
            with w.ledger._open() as db:
                row = db.execute('SELECT * FROM agent_native_tool_calls WHERE run_id=?',(w.run.run_id,)).fetchone()
                if case=='response':
                    changed=json.loads(row['response_json'])
                    payload=json.loads(changed['contentItems'][0]['text'])
                    payload['data']['portale']=[]
                    changed['contentItems'][0]['text']=NT._json(payload)
                    # Rehashing mutable DB response must not bypass artifact anchoring.
                    db.execute('UPDATE agent_native_tool_calls SET response_json=?,response_digest=? WHERE call_id=?',
                        (NT._json(changed),NT._digest(changed),row['call_id']))
                elif case in {'request','turn','grant'}:
                    key={'request':'request_digest','turn':'native_turn_id','grant':'grant_reference'}[case]
                    db.execute('UPDATE agent_native_tool_calls SET '+key+"='foreign' WHERE call_id=?",(row['call_id'],))
                elif case=='step':
                    db.execute("UPDATE agent_steps SET dispatch_binding_digest='foreign' WHERE step_id=?",(row['step_id'],))
                elif case=='local_cost':
                    db.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE route='local.portal-catalog'")
                elif case=='local_route':
                    db.execute("UPDATE agent_provider_invocations SET provider='other-provider' WHERE operation_id=?",(row['step_id'],))
            if case=='artifact_bytes':
                Path(w.artifact.path).chmod(0o600)
                Path(w.artifact.path).write_text('{}')
            rejected(lambda: O.completion_evidence(w.ledger,w.run.run_id))


async def t_foreign_missing_web_events_and_unknown_receipt_schema_are_rejected():
    for case in ('missing','foreign_turn','foreign_invocation','foreign_step'):
        with world() as w:
            await produce(w,portal=False)
            with w.ledger._open() as db:
                row=db.execute("SELECT * FROM agent_events WHERE run_id=? AND kind='native_progress' ORDER BY id DESC",(w.run.run_id,)).fetchone()
                if case=='missing':
                    db.execute('DELETE FROM agent_events WHERE id=?',(row['id'],))
                else:
                    data=json.loads(row['ref'])
                    key={'foreign_turn':'native_turn_id','foreign_invocation':'invocation_id','foreign_step':'operation_id'}[case]
                    data[key]='foreign'
                    db.execute('UPDATE agent_events SET ref=? WHERE id=?',(json.dumps(data),row['id']))
            rejected(lambda: O.completion_evidence(w.ledger,w.run.run_id))
    for receipt in [dict(kind='madeUpAction',item_id='x',status='completed'),
                    dict(kind='fileChange',item_id='x',status='completed',changes=[{'path':block('../outside'),'kind':'add','move_path':None}],changes_complete=True,change_count=1)]:
        with world() as w:
            try:
                await produce(w,portal=False,command=False,web=False,receipts=[receipt])
            except ValueError:
                pass
            else:
                raise AssertionError('unsupported observation accepted')


async def t_full_receipts_or_explicit_omission_never_silent_material_slices():
    text='python helper.py'
    command=dict(kind='commandExecution',item_id='large-output',status='completed',exit_code=0,
        command_sha256=hashlib.sha256(text.encode()).hexdigest(),command=block(text),output=block('x'*15000))
    file=dict(kind='fileChange',item_id='file-change',status='completed',changes=[{'path':block('helper.py'),'kind':'add','move_path':None}],changes_complete=True,change_count=1)
    with world() as w:
        items=await produce(w,portal=False,command=False,web=False,receipts=[command,file])
        # A complete exit-zero command was OBSERVED whole; only its projection is trimmed —
        # it remains a candidate for the local-execution criteria (the assessor judges).
        require_equal([x.requirement for x in items],['f1','l1','l2'])
        material=json.loads(items[0].finding.split('\n',1)[1])
        # Measured 19.09.2026 (third real Durchstich): the decisive final exit-zero check
        # was the ONE receipt too large for the budget; dropping it left only the failed
        # attempts for the assessor. It is now projected TRIMMED and marked — never
        # silently sliced, never silently omitted.
        require_equal(material['omitted_native_item_ids'],[])
        require_equal(material['trimmed_native_item_ids'],['large-output'])
        projected={item['item_id']:item for item in material['native_receipts']}
        require_equal([item['item_id'] for item in material['native_receipts']],['large-output','file-change'])
        big=projected['large-output']
        require_equal((big['projection_trimmed'],big['status'],big['exit_code'],big['command_sha256']),(True,'completed',0,command['command_sha256']))
        require_equal(big['command'],dict(block(text),complete=True), 'a short command stays whole')
        require_equal((big['output']['complete'],big['output']['original_chars'],big['output']['redacted']),(False,15000,False))
        shown=big['output']['text']
        require(0<len(shown)<=int(O.MAX_MATERIAL_CHARS*O.TRIM_SHARE) and shown.startswith('x'*50) and shown.endswith('x'*50) and 'Zeichen ausgelassen' in shown,
                'head and tail around a marked gap')
        require_equal(projected['file-change'],file)
        require(len(items[0].finding)<=O.MAX_MATERIAL_CHARS)
        require('x'*15000 not in items[0].finding)
    # A large early failed experiment cannot evict the actual Core read and
    # the later complete reproducibility check from the bounded assessor view.
    failed = dict(command,item_id='early-failure',status='failed',exit_code=1)
    late_text='python helper.py --check'
    late = dict(command,item_id='late-helper-check',command_sha256=hashlib.sha256(late_text.encode()).hexdigest(),
        command=block(late_text),output=block('CSV_REPRODUCIBLE rows=4 sha256='+'a'*64))
    with world() as w:
        items=await produce(w,command=False,web=False,receipts=[failed,file,late])
        material=json.loads(items[0].finding.split('\n',1)[1])
        require_equal([x.requirement for x in items],['f1','l1','l2'])
        # The late complete check comes whole and first; the early failure is projected
        # trimmed (its tail — the traceback end — is what an assessor needs) if room remains.
        require_equal(material['omitted_native_item_ids'],[])
        require_equal(material['trimmed_native_item_ids'],['early-failure'])
        require_equal(material['omitted_core_call_ids'],[])
        require_equal([item['item_id'] for item in material['native_receipts']],['early-failure','file-change','late-helper-check'])
        require_equal(material['native_receipts'][1:],[file,late])
        require_equal((material['native_receipts'][0]['projection_trimmed'],material['native_receipts'][0]['exit_code']),(True,1))
        require(len(items[0].finding)<=O.MAX_MATERIAL_CHARS)
        require_equal(material['core_results'][0]['result']['data'],json.loads(w.response['contentItems'][0]['text'])['data'])
    with world() as w:
        check=('CSV_REPRODUCIBLE rows=4; actual local helper invocation; ').ljust(499,'a')
        require_equal(len(check)+1,500)
        suffix='print('+repr(check)+')\n'
        helper='#'+' reproducible local CSV helper '*(3000//31)
        helper=helper[:3000-len(suffix)-1]+'\n'+suffix
        require_equal(len(helper),3000)
        (w.workspace/'helper.py').write_text(helper)
        commands=(('helper-readback',[sys.executable,'-c',"from pathlib import Path; print(Path('helper.py').read_text(),end='')"]),
                  ('helper-check',[sys.executable,'helper.py']))
        items=await produce(w,command=False,web=2,extra_commands=commands,initial_receipts=[failed])
        material=json.loads(items[0].finding.split('\n',1)[1])
        # Two whole exit-zero commands (3000 + 500 chars of output) fill the budget; the early
        # failure is trimmed if >= TRIM_MIN_CHARS remain, otherwise named as omitted — never silent.
        require(material['omitted_native_item_ids']+material['trimmed_native_item_ids']==['early-failure'],
                (material['omitted_native_item_ids'],material['trimmed_native_item_ids']))
        require_equal(material['omitted_core_call_ids'],[])
        projected={item['item_id']:item for item in material['native_receipts']}
        require_equal(projected['helper-readback']['output'],block(helper))
        require_equal(projected['helper-check']['output'],block(check+'\n'))
        require_equal(len(material['web_events']),2)
        require(all(set(item)=={'event_id','item_id','status'} for item in material['web_events']))
        require_equal(material['core_results'][0]['result']['data'],json.loads(w.response['contentItems'][0]['text'])['data'])
        print('MEASURE native observation material:',len(items[0].finding),'characters; 3000+500 command output, 2 web items, whole Core result')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
