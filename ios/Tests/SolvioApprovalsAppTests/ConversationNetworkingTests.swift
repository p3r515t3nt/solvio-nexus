import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

private final class ChatWireState: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [(URLRequest, Data)] = []
    private var reject = false
    func reset(reject: Bool = false) {
        lock.lock(); defer { lock.unlock() }; requests = []; self.reject = reject
    }
    func record(_ request: URLRequest, _ data: Data) -> Bool {
        lock.lock(); defer { lock.unlock() }; requests.append((request, data)); return reject
    }
    func seen() -> [(URLRequest, Data)] {
        lock.lock(); defer { lock.unlock() }; return requests
    }
}

/// Offline counterpart of the Core's transport gate. No real credentials or network.
private final class ChatWireProtocol: URLProtocol, @unchecked Sendable {
    static let state = ChatWireState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                bytes.append(contentsOf: buffer.prefix(count))
            }
        }
        let rejected = Self.state.record(request, bytes)
        let authenticated = request.value(forHTTPHeaderField: "X-Device-Id") == "phone-fixture"
            && request.value(forHTTPHeaderField: "X-Transport-Cred") == "fixture-reader"
        let list = request.url?.path == "/v1/agent/runs" && request.httpMethod == "GET"
        let status = authenticated && !rejected ? (list ? 200 : 202) : 401
        let responseBody = status == 200 ? #"{"laeufe":[],"next_before":null}"# : status == 202
            ? #"{"delivery_id":"cd-0123456789abcdef","message_id":"m-0123456789abcdef","status":"accepted"}"#
            : #"{"error":"unauthorized"}"#
        guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(responseBody.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor final class ConversationNetworkingTests: XCTestCase {
    private let chat = "c-0123456789abcdef"
    private func client() -> ApprovalClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ChatWireProtocol.self]
        let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture",
            endpoint: "https://127.0.0.1:1", tls_fingerprint: String(repeating: "0", count: 64),
            mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)
        return ApprovalClient(testSession: URLSession(configuration: config), pairing: pairing,
            deviceID: "phone-fixture", transportCred: "fixture-reader")
    }
    private func message() throws -> AppConversationMessageBody {
        try .init(conversationID: chat, clientMessageID: "message-fixture-0001", text: "Drei Ideen für ein Abendessen?")
    }
    func testActualMessageRequestRetainsTransportAndExactFreshProof() async throws {
        ChatWireProtocol.state.reset()
        let body = try message(), proof = AppTaskProof(nonce: String(repeating: "b", count: 64), assertion: Data([1, 2, 3]))
        let accepted = try await client().submitMessage(chat, body: body, proof: proof)
        XCTAssertEqual(accepted.delivery_id, "cd-0123456789abcdef")
        let seen = ChatWireProtocol.state.seen(); XCTAssertEqual(seen.count, 1)
        let (request, bytes) = try XCTUnwrap(seen.first)
        XCTAssertEqual(request.url?.path, "/v1/conversations/\(chat)/messages")
        XCTAssertNil(request.url?.query); XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Id"), "phone-fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Transport-Cred"), "fixture-reader")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: bytes) as? [String: Any])
        XCTAssertEqual(Set(payload.keys), ["message", "proof"])
        XCTAssertEqual(payload["message"] as? [String: String], ["conversation_id": chat,
            "client_message_id": body.client_message_id, "text": body.text])
        XCTAssertEqual(payload["proof"] as? [String: String], ["nonce": proof.nonce, "assertion_b64": "AQID"])
    }
    func testRejectedAuthenticationNeverClaimsAcceptanceOrAutomaticallyRetries() async throws {
        ChatWireProtocol.state.reset(reject: true)
        do {
            _ = try await client().submitMessage(chat, body: message(),
                proof: AppTaskProof(nonce: String(repeating: "b", count: 64), assertion: Data([1])))
            XCTFail("Unauthorized delivery was accepted")
        } catch ClientError.http(let status) { XCTAssertEqual(status, 401) }
        XCTAssertEqual(ChatWireProtocol.state.seen().count, 1)
    }
    func testForeignConversationNeverSendsTheBoundMessage() async throws {
        ChatWireProtocol.state.reset()
        do {
            _ = try await client().submitMessage("c-aaaaaaaaaaaaaaaa", body: message(),
                proof: AppTaskProof(nonce: String(repeating: "b", count: 64), assertion: Data([1])))
            XCTFail("Foreign conversation was sent")
        } catch ClientError.decode { }
        XCTAssertTrue(ChatWireProtocol.state.seen().isEmpty)
    }

    func testLibraryCursorIsAQueryAndRetainsTransportAuthentication() async throws {
        ChatWireProtocol.state.reset()
        let cursor = "ar-0123456789abcdef"
        let page = try await client().agentRunPage(before: cursor)
        XCTAssertTrue(page.laeufe.isEmpty); XCTAssertNil(page.next_before)
        let (request, _) = try XCTUnwrap(ChatWireProtocol.state.seen().first)
        XCTAssertEqual(request.url?.path, "/v1/agent/runs")
        XCTAssertEqual(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems,
                       [URLQueryItem(name: "before", value: cursor)])
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Id"), "phone-fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Transport-Cred"), "fixture-reader")
        ChatWireProtocol.state.reset()
        do { _ = try await client().agentRunPage(before: "../foreign?x=y"); XCTFail("Invalid cursor sent") }
        catch ClientError.decode { }
        XCTAssertTrue(ChatWireProtocol.state.seen().isEmpty)
    }
}
