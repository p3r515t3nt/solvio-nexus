import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

private final class CostWireState: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [(URLRequest, Data)] = []
    func reset() { lock.lock(); defer { lock.unlock() }; requests=[] }
    func record(_ request: URLRequest, _ body: Data) { lock.lock(); defer { lock.unlock() }; requests.append((request,body)) }
    func seen() -> [(URLRequest,Data)] { lock.lock(); defer { lock.unlock() }; return requests }
}
/// Offline HTTP contract; no real device, token, assertion verification or provider.
private final class CostWireProtocol: URLProtocol, @unchecked Sendable {
    static let state=CostWireState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes=request.httpBody ?? Data()
        if let stream=request.httpBodyStream {
            stream.open();defer { stream.close() };var buffer=[UInt8](repeating:0,count:1024)
            while stream.hasBytesAvailable { let count=stream.read(&buffer,maxLength:buffer.count);if count<=0 { break };bytes.append(contentsOf:buffer.prefix(count)) }
        }
        Self.state.record(request,bytes)
        do {
            let body=try JSONSerialization.jsonObject(with:bytes) as? [String:Any]
            let raw=try XCTUnwrap(body?["cost_approval"] as? [String:Any])
            let value=try JSONDecoder().decode(AppTaskCostApprovalBody.self,from:JSONSerialization.data(withJSONObject:raw))
            let challenge=request.url?.path.hasSuffix("/challenge")==true
            let authenticated=request.value(forHTTPHeaderField:"X-Device-Id")=="phone-fixture"
                && request.value(forHTTPHeaderField:"Content-Type")=="application/json"
                && (challenge ? request.value(forHTTPHeaderField:"X-Transport-Cred")=="fixture-reader"
                    : request.value(forHTTPHeaderField:"X-Transport-Cred")==nil)
            guard authenticated,request.httpMethod=="POST",Set(body?.keys.map{$0} ?? []) == (challenge ? ["cost_approval"] : ["cost_approval","proof"]) else {
                throw ClientError.http(401)
            }
            let result:[String:Any]
            if challenge {
                let nonce=String(repeating:"b",count:64)
                let binding:[String:Any]=["protocol_version":1,"type":"app_task_cost_approval_binding","core_instance_id":"core-fixture",
                    "principal_id":"owner","device_id":"phone-fixture","nonce":nonce,"request_digest":value.requestDigest,
                    "enrollment_id":"enrollment","app_attest_key_id":"key","approval_key_sha256":String(repeating:"c",count:64)]
                result=["nonce":nonce,"request_digest":value.requestDigest,"expires_at":Date().timeIntervalSince1970+30,
                    "binding_b64":try JSONSerialization.data(withJSONObject:binding,options:[.sortedKeys]).base64EncodedString()]
            } else {
                guard let proof=body?["proof"] as? [String:String],Set(proof.keys)==["nonce","assertion_b64"],
                      proof["nonce"]==String(repeating:"b",count:64),proof["assertion_b64"]=="AQ==" else { throw ClientError.http(401) }
                result=["task_id":value.task_id,"client_request_id":value.client_request_id,"max_total_cents":value.max_total_cents,
                    "costs":["configured":true,"task_id":value.task_id,"currency":"EUR","approved_ai_cap_cents":value.max_total_cents]]
            }
            respond(200,try JSONSerialization.data(withJSONObject:result))
        } catch { respond(401,Data(#"{"error":"unauthorized"}"#.utf8)) }
    }
    private func respond(_ status:Int,_ data:Data) {
        guard let url=request.url,let response=HTTPURLResponse(url:url,statusCode:status,httpVersion:"HTTP/1.1",headerFields:["Content-Type":"application/json"]) else { return }
        client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:data);client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@MainActor final class TaskCostApprovalNetworkingTests: XCTestCase {
    func testActualApprovalClientKeepsCanonicalBodyAndUsesSeparateChallengeAndSubmitProof() async throws {
        CostWireProtocol.state.reset()
        let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[CostWireProtocol.self]
        let pairing=PairingPayload(v:1,type:"fixture",core_instance_id:"core-fixture",endpoint:"https://127.0.0.1:1",
            tls_fingerprint:String(repeating:"0",count:64),mac_pubkey_fingerprint:"fixture",mac_pubkey_x963_b64:"",enrollment_token:"",expires_at:0)
        let client=ApprovalClient(testSession:URLSession(configuration:config),pairing:pairing,deviceID:"phone-fixture",transportCred:"fixture-reader")
        let body=try AppTaskCostApprovalBody(taskID:"at-1111111111111111",maxTotalCents:2001,requestID:"wire-cost-001")
        let challenge=try await client.costApprovalChallenge(body)
        _=try challenge.clientDataHash(body:body,coreID:"core-fixture",deviceID:"phone-fixture",appAttestKeyID:"key")
        let result=try await client.submitCostApproval(body,proof:.init(nonce:challenge.nonce,assertion:Data([1])))
        try result.validate(body:body)
        let seen=CostWireProtocol.state.seen();XCTAssertEqual(seen.count,2)
        XCTAssertEqual(seen[0].0.url?.path,"/v1/agent/tasks/\(body.task_id)/cost-approval/challenge")
        XCTAssertEqual(seen[1].0.url?.path,"/v1/agent/tasks/\(body.task_id)/cost-approval")
        XCTAssertEqual(seen[0].0.value(forHTTPHeaderField:"X-Transport-Cred"),"fixture-reader")
        XCTAssertNil(seen[1].0.value(forHTTPHeaderField:"X-Transport-Cred"))
        for (_,data) in seen {
            let wire=try XCTUnwrap(JSONSerialization.jsonObject(with:data) as? [String:Any])
            let request=try JSONDecoder().decode(AppTaskCostApprovalBody.self,from:JSONSerialization.data(withJSONObject:try XCTUnwrap(wire["cost_approval"])))
            XCTAssertEqual(request,body)
        }
        // No resume/cancel/start request occurs as part of cost approval.
        XCTAssertTrue(seen.allSatisfy{$0.0.url?.path.contains("/cost-approval")==true})
    }
}
