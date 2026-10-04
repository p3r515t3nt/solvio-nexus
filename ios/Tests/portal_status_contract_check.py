#!/usr/bin/env python3
"""Swift status selection -> real temporary Core AppAttest admission -> Unix READ.

No real device, portal login, credentials, model or provider. Existing Core
fixtures substitute CDP and assessment with local protocol-only counterparts.
"""
import argparse
import asyncio
import base64
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SWIFT = r'''
import Foundation
import SolvioApprovalsKit
@main struct Selection {
    static func main() throws {
        let rows = try TaskPortalSessions.decode(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var form = TaskPortalForm(); try form.accept(rows)
        guard let selected = rows.items.first else { throw TaskActionError.portalSelectionRequired }
        try form.select(selected.account)
        let action = try AppTaskActionRequest(actions: [form.action()])
        let body = try AppTaskBody(scope: "action", objective: form.objective, targetRepo: "", requestID: "native-portal-status-contract-001", actionRequest: action)
        let value: [String: Any] = ["body": try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)),
                                  "canonical_b64": body.canonicalBytes().base64EncodedString(), "digest": body.requestDigest]
        print(String(decoding: try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), as: UTF8.self))
    }
}
'''

def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--core', type=Path, required=True); parser.add_argument('--kit-build', type=Path, required=True)
    args = parser.parse_args(); core = args.core.resolve()
    sys.path[:0] = [str(core/'src'), str(core/'tests')]
    os.environ['SOLVIO_CORE_ROOT'] = str(core); os.environ['SOLVIO_CORE_SRC'] = str(core/'src')
    from test_agent_action_portal import portal_fixture
    from test_agent_action_execution import world
    from test_agent_task_entry import H, AA, T
    from solvio.agent_runtime import action_contract as AC
    async def check(binary, folder):
        async with portal_fixture(owner='local-owner') as p, world(expected_native='42.00') as w:
            w.orch.action_service.portals = p.portals
            device = await H.enroll_attested(w.cp, transport_cred='fixture-native-portal-status')
            app = await w.new_client()
            headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'fixture-native-portal-status'}
            path = '/v1/agent/action-portal-sessions?limit=50'
            assert (await app.get(path)).status == 401
            assert (await app.get(path, headers={**headers, 'X-Transport-Cred': 'wrong'})).status == 401
            response = await app.get(path, headers=headers); assert response.status == 200
            body = await response.json(); file = folder/'catalogue.json'; file.write_text(json.dumps(body))
            completed = subprocess.run([str(binary), str(file)], check=True, capture_output=True, text=True, timeout=10)
            swift = json.loads(completed.stdout); task = swift['body']
            assert swift['digest'] == T.request_digest(task)
            assert json.loads(base64.b64decode(swift['canonical_b64'])) == T.canonical_task_body(task)
            assert task['action_request']['actions'][0]['operation'] == 'status'
            assert (await app.post('/v1/agent/tasks', json={'task':task}, headers=headers)).status == 401
            response = await app.post('/v1/agent/tasks/challenge', json={'task':task}, headers=headers)
            assert response.status == 200; challenge = await response.json()
            assertion = AA.fake_assertion(device.aakey, T.client_data_hash(base64.b64decode(challenge['binding_b64'])), 1)
            proof = {'nonce':challenge['nonce'], 'assertion_b64':base64.b64encode(assertion).decode()}
            response = await app.post('/v1/agent/tasks', json={'task':task,'proof':proof}, headers={'X-Device-Id': device.device_id})
            accepted = await response.json(); assert response.status == 201, accepted
            run = accepted['run_id']; assert w.orch.task_authority.for_run(run).receipt_method == 'app_session'
            await w.finish(run)
            receipts = AC.read_receipts(w.ledger, run)
            assert len(receipts) == 1 and receipts[0]['native']['observed']['confirmed'] is True
            assert receipts[0]['operation'] == 'status' and receipts[0]['target'] == body['items'][0]['target']
            assert len(w.ledger.recent_runs()) == 1
            assert set(p.operations) <= {'ping','list_sessions','read'}
            assert w.native.transport.calls == []
            await w.cp.revoke_device(device.device_id)
            assert (await app.get(path, headers=headers)).status == 401
            return {'passed': 1, 'failed':0, 'checks': ['owner_read_auth', 'swift_python_canonical_binding',
                'fresh_app_attest_admission', 'actual_status_receipt', 'only_read_worker_operations', 'revocation'],
                'native_operation': 'portal.status', 'task_count':1,
                'receipt_confirmed':True, 'swift_core_digest':swift['digest'], 'worker_operations':p.operations,
                'device':'synthetic_attestation', 'page':'synthetic_cdp', 'assessment':'local_fixture',
                'real_login':False,'real_provider_calls':0}
    with tempfile.TemporaryDirectory(prefix='solvio-portal-swift-contract-') as tmp:
        folder=Path(tmp); source=folder/'Select.swift'; source.write_text(SWIFT); binary=folder/'select'
        build=args.kit_build/'arm64-apple-macosx/debug'; objects=sorted((build/'SolvioApprovalsKit.build').glob('*.swift.o'))
        if not objects: raise SystemExit('Missing existing Kit build')
        subprocess.run(['xcrun','swiftc','-swift-version','6','-parse-as-library','-I',str(build/'Modules'),str(source),*map(str,objects),'-o',str(binary)],check=True)
        with redirect_stdout(sys.stderr):
            result=asyncio.run(check(binary,folder))
        result['core_commit']=subprocess.check_output(['git','-C',str(core),'rev-parse','HEAD'],text=True).strip()
        result['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
