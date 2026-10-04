"""Durable worker CLI via installed Hermes and a local, persistent JSON-RPC peer.

No model, account, production state, or network request is used. The peer owns
the synthetic native transcript; assertions inspect the actual wire traffic.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
import test_hermes_native as B
from solvio.specialists import hermes_native as N, launcher as L

enforce_assertions()

_THREAD_BRANCH = '''    elif method=='thread/start':
        response={'thread':{'id':T},'model':'wrong-model' if mode=='wrong_model' else model,
            'modelProvider':'openai','cwd':params['cwd'],'sandbox':{'type':'readOnly','networkAccess':False},
            'approvalPolicy':'never','approvalsReviewer':'user','instructionSources':[]}
'''
_DURABLE_BRANCH = '''    elif method in ('thread/start','thread/read','thread/resume'):
        if method=='thread/start':
            thread={'id':T,'ephemeral':params['ephemeral'],'cwd':params['cwd'],
                'modelProvider':'openai','status':{'type':'idle'},'turns':[]}
            (root/'thread.json').write_text(json.dumps(thread))
        else:
            thread=json.loads((root/'thread.json').read_text())
        if mode.startswith('read_') and method=='thread/read' or mode.startswith('resume_') and method=='thread/resume':
            case=mode.split('_',1)[1]
            if case=='foreign': thread['id']='foreign-thread'
            elif case=='workspace': thread['cwd']=str(root/'foreign-workspace')
            elif case=='provider': thread['modelProvider']='foreign-provider'
            elif case=='ephemeral': thread['ephemeral']=True
            elif case=='active': thread['status']={'type':'active','activeFlags':[]}
            elif case=='last': thread['turns'][-1]['id']='foreign-turn'
            elif case=='unfinished': thread['turns'][-1]['status']='inProgress'
            elif case=='empty': thread['turns']=[]
        response={'thread':thread,'model':'wrong-model' if mode=='wrong_model' else model,
            'modelProvider':'openai','cwd':thread['cwd'],'sandbox':{'type':'readOnly','networkAccess':False},
            'approvalPolicy':'never','approvalsReviewer':'user','instructionSources':[]}
        if mode=='resume_policy' and method=='thread/resume': response['sandbox']['networkAccess']=True
        if mode=='start_ephemeral' and method=='thread/start': response['thread']['ephemeral']=True
'''


def peer_program():
    require_equal(B.RPC_PROGRAM.count(_THREAD_BRANCH), 1, "upstream peer contract changed")
    program = B.RPC_PROGRAM.replace(_THREAD_BRANCH, _DURABLE_BRANCH)
    program = program.replace("T='local-thread'; U='local-turn'; model=", "T='local-thread'; U='turn-'+str(int((root/'turn-counter').read_text())+1 if (root/'turn-counter').exists() else 1); model=")
    old = "    elif method=='turn/start':\n        response={'turn':{'id':U,'status':'inProgress'}}"
    require_equal(program.count(old), 1)
    program = program.replace(old, """    elif method=='turn/start':
        (root/'turn-counter').write_text(U.removeprefix('turn-'))
        response={'turn':{'id':U,'status':'inProgress'}}""")
    old = "        note('turn/completed',turn={'id':U,'status':'completed','error':None,'items':[]})"
    require_equal(program.count(old), 1)
    program = program.replace(old, """        thread=json.loads((root/'thread.json').read_text())
        thread['turns'].append({'id':U,'status':'completed','error':None,'items':[]})
        (root/'thread.json').write_text(json.dumps(thread))
""" + old)
    return program


@contextmanager
def fixture(mode="ok"):
    original = N.worker_invocation
    with B.native_fixture(mode, timeout=2.0) as (root, config, ledger, task, run, request):
        codex = Path(config.codex_bin)
        codex.write_text("#!" + str(Path(sys.executable).resolve()) + " -ISB\n" + peer_program())
        workspace = root.resolve() / "workspace"
        workspace.mkdir(mode=0o700)
        source = Path(N.__file__).with_name("hermes_native_worker.py")
        copied = root / "hermes_native_worker.py"
        require_equal(copied.read_bytes(), source.read_bytes())

        def invoke(native_config, workdir, **kwargs):
            result = original(native_config, workdir, **kwargs)
            return replace(result, argv=(*result.argv[:2], str(copied), *result.argv[3:]))

        with patch.object(N, "worker_invocation", invoke):
            yield root, config, task, workspace


async def call(config, task, workspace, *, resume=False, ordinary=False):
    invocation = (N.worker_invocation(config, str(workspace)) if ordinary else
        N.continuation_invocation(config, str(workspace), task_id=task,
            native_thread_id="local-thread" if resume else "",
            previous_turn_id="turn-1" if resume else ""))
    outcome = await L.run(invocation, "Lies die lokalen synthetischen Protokollbelege.")
    body, _events = N._decode(outcome.text, B.SP.redact_specialist_output)
    return body


def methods(root):
    return [row["method"] for row in B.observed(root) if "method" in row]


async def t_durable_start_and_fresh_worker_resume_use_same_thread_and_workspace():
    with fixture() as (root, config, task, workspace):
        first = await call(config, task, workspace)
        require_equal(first["status"], "completed", first)
        require_equal(first["thread_id"], "local-thread")
        require_equal(first["turn_id"], "turn-1")
        second = await call(config, task, workspace, resume=True)
        require_equal(second["status"], "completed", second)
        require_equal(second["thread_id"], first["thread_id"])
        require_equal(second["turn_id"], "turn-2")
        require_equal(len(B.method_rows(root, "thread/start")), 1)
        require_equal(len(B.method_rows(root, "thread/read")), 1)
        require_equal(len(B.method_rows(root, "thread/resume")), 1)
        require_equal(len(B.method_rows(root, "turn/start")), 2)
        start = B.method_rows(root, "thread/start")[0]["params"]
        resume = B.method_rows(root, "thread/resume")[0]["params"]
        require(start["ephemeral"] is False)
        require("ephemeral" not in resume)
        require_equal(start["cwd"], resume["cwd"])
        require_equal(resume["threadId"], "local-thread")
        require_equal(B.method_rows(root, "thread/read")[0]["params"],
                      {"threadId": "local-thread", "includeTurns": True})
        require_equal(methods(root).count("turn/interrupt"), 0)
        for turn in B.method_rows(root, "turn/start"):
            require_equal(turn["params"]["sandboxPolicy"], {"type":"readOnly", "networkAccess":False})
        require_equal(len({r["pid"] for r in B.observed(root) if "argv" in r}), 2)


async def t_thread_read_rejects_foreign_active_missing_or_unfinished_history_before_resume():
    for mode in ("read_foreign", "read_workspace", "read_provider", "read_ephemeral", "read_active",
                 "read_last", "read_unfinished", "read_empty"):
        with fixture() as (root, config, task, workspace):
            require_equal((await call(config, task, workspace))["status"], "completed")
            (root / "mode").write_text(mode)
            result = await call(config, task, workspace, resume=True)
            require_equal(result["status"], "failed", mode)
            require(result["reason"] in {"native_session_binding_invalid", "native_previous_turn_unconfirmed"}, (mode, result))
            require_equal(len(B.method_rows(root, "thread/resume")), 0, mode)
            require_equal(len(B.method_rows(root, "turn/start")), 1, mode)


async def t_resume_response_rechecked_before_any_new_turn():
    for mode in ("resume_foreign", "resume_workspace", "resume_provider", "resume_ephemeral", "resume_active",
                 "resume_last", "resume_unfinished", "resume_empty", "resume_policy", "wrong_model"):
        with fixture() as (root, config, task, workspace):
            require_equal((await call(config, task, workspace))["status"], "completed")
            (root / "mode").write_text(mode)
            result = await call(config, task, workspace, resume=True)
            require_equal(result["status"], "failed", mode)
            require_equal(len(B.method_rows(root, "thread/resume")), 1, mode)
            require_equal(len(B.method_rows(root, "turn/start")), 1, mode)


async def t_ephemeral_start_reply_cannot_satisfy_durable_start():
    with fixture("start_ephemeral") as (root, config, task, workspace):
        result = await call(config, task, workspace)
        require_equal(result["reason"], "native_session_binding_invalid")
        require_equal(len(B.method_rows(root, "turn/start")), 0)


async def t_normal_research_stays_ephemeral_and_never_reads_or_resumes():
    with fixture() as (root, config, task, workspace):
        result = await call(config, task, workspace, ordinary=True)
        require_equal(result["status"], "completed", result)
        require(B.method_rows(root, "thread/start")[0]["params"]["ephemeral"] is True)
        require_equal(len(B.method_rows(root, "turn/start")), 1)
        require_equal(len(B.method_rows(root, "thread/read")), 0)
        require_equal(len(B.method_rows(root, "thread/resume")), 0)


async def t_native_quota_never_restarts_the_turn_or_thread():
    with fixture("quota_turn") as (root, config, task, workspace):
        result = await call(config, task, workspace)
        require_equal(result["reason"], "quota")
        require_equal(len(B.method_rows(root, "turn/start")), 1)
        require_equal(len(B.method_rows(root, "thread/start")), 1)
        require_equal(len(B.method_rows(root, "thread/resume")), 0)


async def t_invalid_cli_task_or_incomplete_resume_ids_rejected_before_native_process():
    for option, value in (("--task-id", "foreign-task"), ("--resume-thread", "local-thread"),
                          ("--previous-turn", "turn-1"), ("--session-mode", "unknown")):
        with fixture() as (root, config, task, workspace):
            invocation = N.continuation_invocation(config, str(workspace), task_id=task)
            argv = list(invocation.argv)
            argv[argv.index(option) + 1] = value
            outcome = await L.run(replace(invocation, argv=tuple(argv)), "Lokale Protokollprobe.")
            body, _events = N._decode(outcome.text, B.SP.redact_specialist_output)
            require_equal(body["reason"], "native_session_binding_invalid", (option, body))
            require_equal(B.observed(root), [], option)


async def t_unprivate_workspace_rejected_before_native_process():
    with fixture() as (root, config, task, workspace):
        workspace.chmod(0o755)
        result = await call(config, task, workspace)
        require_equal(result["reason"], "native_workspace_invalid")
        require_equal(B.observed(root), [])


async def t_workspace_parent_alias_is_rejected_before_creating_unresumable_thread():
    with fixture() as (root, config, task, workspace):
        alias = root / "parent-alias"
        alias.symlink_to(workspace.parent, target_is_directory=True)
        aliased_workspace = alias / workspace.name
        require(not aliased_workspace.is_symlink())
        require_equal(aliased_workspace.resolve(), workspace)
        result = await call(config, task, aliased_workspace)
        require_equal(result["reason"], "native_workspace_invalid")
        require_equal(B.observed(root), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
