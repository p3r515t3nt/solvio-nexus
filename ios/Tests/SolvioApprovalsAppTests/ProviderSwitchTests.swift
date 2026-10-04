import Foundation
import XCTest
import SwiftUI
import SolvioApprovalsKit
@testable import SolvioApprovals

private final class ProviderReplyState: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [(URLRequest, Data)] = []
    private var status = 200
    func reset(_ status: Int = 200) { lock.lock(); defer { lock.unlock() }; requests = []; self.status = status }
    func reply(_ request: URLRequest, body: Data) -> Int {
        lock.lock(); defer { lock.unlock() }; requests.append((request, body)); return status
    }
    func seen() -> [(URLRequest, Data)] { lock.lock(); defer { lock.unlock() }; return requests }
}
private final class ProviderReplyProtocol: URLProtocol, @unchecked Sendable {
    static let state = ProviderReplyState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }; body.append(contentsOf: buffer.prefix(count))
            }
        }
        let status = Self.state.reply(request, body: body)
        guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor final class ProviderSwitchTests: XCTestCase {
    private let id = "ar-0123456789abcdef", ref = String(repeating: "a", count: 64)
    private func client() -> ApprovalClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [ProviderReplyProtocol.self]
        let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture", endpoint: "https://127.0.0.1:1",
            tls_fingerprint: String(repeating: "0", count: 64), mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)
        return ApprovalClient(testSession: URLSession(configuration: config), pairing: pairing,
                              deviceID: "phone-fixture", transportCred: "fixture-reader")
    }
    private func fixtureRun() throws -> AgentRun {
        let raw: [String: Any] = ["id": id, "aufgabe": "at-0123456789abcdef", "auftrag": "Vergleiche drei geeignete Angebote.",
            "zustand": "Wartet auf dich", "zustand_code": "WAITING_USER", "offen": true,
            "grund": "Das Kontingent von OpenAI ist erreicht.",
            "anbietergrenze": ["fortsetzbar": true, "boundary_ref": ref, "wechseloptionen": [[
                "provider": "claude-code", "label": "Claude Code", "werkzeuge": ["WebSearch", "WebFetch"],
                "hinweis": "Diese Wahl gilt für die Recherche. Auftrag, Ergebnisse und Kostenrahmen bleiben erhalten."]]]]
        return try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    func testEnrolledClientSendsExactlyTheExplicitProviderAndBoundaryToExistingRun() async throws {
        ProviderReplyProtocol.state.reset()
        try await client().switchAgentRun(id, provider: "claude-code", boundaryRef: ref)
        let seen = ProviderReplyProtocol.state.seen(); XCTAssertEqual(seen.count, 1)
        let (request, bytes) = try XCTUnwrap(seen.first)
        XCTAssertEqual(request.url?.path, "/v1/agent/runs/\(id)/resume"); XCTAssertNil(request.url?.query)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Id"), "phone-fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Transport-Cred"), "fixture-reader")
        XCTAssertEqual(try JSONSerialization.jsonObject(with: bytes) as? [String: String], ["provider": "claude-code", "boundary_ref": ref])
    }
    func testInvalidChoiceNeverRequestsAndStaleCoreBoundaryNeverClaimsResume() async throws {
        let client = client(); ProviderReplyProtocol.state.reset()
        for (runID, provider, boundary) in [(id, "api", ref), (id, "claude-code", "old"), ("../" + id, "codex", ref)] {
            do { try await client.switchAgentRun(runID, provider: provider, boundaryRef: boundary); XCTFail("Invalid choice accepted") }
            catch { }
        }
        XCTAssertTrue(ProviderReplyProtocol.state.seen().isEmpty)
        ProviderReplyProtocol.state.reset(409)
        let model = AgentResultsModel(); await model.refreshDetail(runID: id) { try self.fixtureRun() }
        await model.act(runID: id, action: "resume") { try await client.switchAgentRun(self.id, provider: "claude-code", boundaryRef: self.ref) }
        XCTAssertEqual(ProviderReplyProtocol.state.seen().count, 1)
        XCTAssertTrue(model.actionMessage.contains("nicht bestätigt")); XCTAssertEqual(model.detail?.zustand_code, "WAITING_USER")
    }
    func testProviderChoiceRendersInExistingConversationWithoutStartingConnection() async throws {
        let app = AppModel.visualPreview(); XCTAssertNil(app.client)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        for (width, size) in [(390.0, DynamicTypeSize.large), (320.0, .accessibility3)] {
            let model = AgentResultsModel(), value = try fixtureRun()
            await model.refreshDetail(runID: id) { value }
            let controller = UIHostingController(rootView: NavigationStack {
                AgentResultView(app: app, runID: id, model: model)
            }.preferredColorScheme(.dark).environment(\.dynamicTypeSize, size))
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: width, height: 1000)
            window.rootViewController = controller; window.makeKeyAndVisible()
            try await Task.sleep(nanoseconds: 100_000_000)
            controller.view.frame = window.bounds; controller.view.layoutIfNeeded()
            let image = UIGraphicsImageRenderer(size: window.bounds.size).image { _ in
                controller.view.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
            }
            let attachment = XCTAttachment(image: image); attachment.name = "provider-choice-\(Int(width))"
            attachment.lifetime = .keepAlways; add(attachment)
            if width == 320 {
                func scrollView(_ view: UIView) -> UIScrollView? {
                    if let scroll = view as? UIScrollView { return scroll }
                    return view.subviews.compactMap { scrollView($0) }.first
                }
                let scroll = try XCTUnwrap(scrollView(controller.view))
                XCTAssertGreaterThan(scroll.contentSize.height, scroll.bounds.height)
                scroll.setContentOffset(CGPoint(x: 0, y: max(0, scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)), animated: false)
                try await Task.sleep(nanoseconds: 100_000_000)
                let bottom = UIGraphicsImageRenderer(size: window.bounds.size).image { _ in
                    controller.view.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
                }
                let bottomAttachment = XCTAttachment(image: bottom)
                bottomAttachment.name = "provider-choice-320-scrolled"; bottomAttachment.lifetime = .keepAlways
                add(bottomAttachment)
            }
            XCTAssertEqual(model.detail?.providerOptions.map(\.provider), ["claude-code"])
            XCTAssertNil(app.client); XCTAssertFalse(model.working)
            window.isHidden = true; window.rootViewController = nil
        }
    }
}
