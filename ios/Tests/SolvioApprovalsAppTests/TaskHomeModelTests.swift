import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

private final class HomeReadState: @unchecked Sendable {
    private let lock = NSLock()
    private var rows: [String: (Int, Data)] = [:]
    private var requests: [URLRequest] = []
    func reset(_ rows: [String: (Int, Data)]) { lock.lock(); defer { lock.unlock() }; self.rows = rows; requests = [] }
    func reply(_ request: URLRequest) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }; requests.append(request)
        return rows[request.url?.path ?? ""] ?? (404, Data())
    }
    func seen() -> [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests }
}
private final class HomeReadProtocol: URLProtocol, @unchecked Sendable {
    static let state = HomeReadState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, data) = Self.state.reply(request)
        guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor
final class TaskHomeModelTests: XCTestCase {
    private let account = ActionServiceAccount(service: "ha", account: "ha-" + String(repeating: "a", count: 32), resource: "configured_home")
    private func resourceData() throws -> Data {
        try JSONSerialization.data(withJSONObject: ["service": "ha", "account": account.account, "truncated": false,
            "items": [["target": ["entity_id": "light.fixture"], "name": "Lampe", "area": "Zimmer",
                       "domain": "light", "state": "off", "operations": ["set_state", "set_brightness"]]]])
    }
    private func setup() async throws -> (TaskStartModel, ApprovalClient) {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [HomeReadProtocol.self]
        let session = URLSession(configuration: config)
        let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture",
            endpoint: "https://127.0.0.1:1", tls_fingerprint: String(repeating: "0", count: 64),
            mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)
        let client = ApprovalClient(testSession: session, pairing: pairing, deviceID: "phone-fixture", transportCred: "fixture-reader")
        let services = try JSONEncoder().encode(["services": [account]])
        HomeReadProtocol.state.reset(["/v1/agent/action-services": (200, services),
                                    "/v1/agent/action-resources": (200, try resourceData())])
        let model = TaskStartModel(); model.scope = "action"; model.exactAction = true; model.actionForm.kind = .ha
        await model.loadActionServices(client: client)
        return (model, client)
    }

    func testActualClientReadsFixedOwnerEndpointsAndCannotSelectOrStartAutomatically() async throws {
        let (model, client) = try await setup()
        await model.loadHomeResources(client: client)
        XCTAssertNotNil(model.actionForm.home.catalogue); XCTAssertNil(model.actionForm.home.selectedDevice)
        XCTAssertFalse(model.valid)
        let seen = HomeReadProtocol.state.seen()
        XCTAssertEqual(seen.map(\.httpMethod), ["GET", "GET"])
        let request = try XCTUnwrap(seen.last)
        let query = try XCTUnwrap(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems)
        XCTAssertEqual(Dictionary(uniqueKeysWithValues: query.map { ($0.name, $0.value ?? "") }),
                       ["service": "ha", "account": account.account, "limit": "100"])
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Id"), "phone-fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Transport-Cred"), "fixture-reader")
        model.selectHomeDevice("light.fixture"); model.actionForm.home.desired = .on
        XCTAssertTrue(model.valid); XCTAssertNil(model.accepted)
        XCTAssertEqual(HomeReadProtocol.state.seen().count, 2)
    }

    func testReadFailureInvalidatesActionableCatalogueInsteadOfUsingOldView() async throws {
        let (model, client) = try await setup()
        await model.loadHomeResources(client: client)
        model.selectHomeDevice("light.fixture"); model.actionForm.home.desired = .brightness
        XCTAssertTrue(model.valid)
        await model.loadHomeResources(coreID: "core-fixture", deviceID: "phone-fixture", fetch: { _ in throw URLError(.notConnectedToInternet) })
        XCTAssertNil(model.actionForm.home.catalogue); XCTAssertFalse(model.valid)
        XCTAssertFalse(model.homeMessage.isEmpty)
    }

    func testLateReadAfterScopeChangeCannotRepopulateHomeControls() async throws {
        let (model, _) = try await setup(), data = try resourceData()
        let value = try TaskHomeResources.decode(data, account: account)
        var reply: CheckedContinuation<TaskHomeResources, Never>?
        let pending = Task { await model.loadHomeResources(coreID: "core", deviceID: "phone", fetch: { _ in
            await withCheckedContinuation { reply = $0 }
        }) }
        while reply == nil { await Task.yield() }
        model.scope = "research"
        reply?.resume(returning: value); await pending.value
        XCTAssertNil(model.actionForm.home.catalogue); XCTAssertFalse(model.homeLoading)
    }

    func testOlderParallelReadCannotReplaceFreshDeviceCatalogue() async throws {
        let (model, _) = try await setup()
        let value = try TaskHomeResources.decode(resourceData(), account: account)
        var first: CheckedContinuation<TaskHomeResources, Never>?
        let pending = Task { await model.loadHomeResources(coreID: "core", deviceID: "phone", fetch: { _ in
            await withCheckedContinuation { first = $0 }
        }) }
        while first == nil { await Task.yield() }
        await model.loadHomeResources(coreID: "core", deviceID: "phone", fetch: { _ in throw URLError(.timedOut) })
        first?.resume(returning: value); await pending.value
        XCTAssertNil(model.actionForm.home.catalogue); XCTAssertFalse(model.homeMessage.isEmpty)
    }

    func testDisconnectedDeviceReplyIsDiscardedWithoutBecomingAValidAction() async throws {
        let (model, _) = try await setup()
        let value = try TaskHomeResources.decode(resourceData(), account: account)
        await model.loadHomeResources(coreID: "core", deviceID: "phone", fetch: { _ in value }, stillConnected: { false })
        XCTAssertNil(model.actionForm.home.catalogue); XCTAssertFalse(model.valid)
    }
}
