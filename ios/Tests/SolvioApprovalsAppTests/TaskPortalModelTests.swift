import Foundation
import XCTest
import SwiftUI
import SolvioApprovalsKit
@testable import SolvioApprovals

private final class PortalReadState: @unchecked Sendable {
    private let lock = NSLock()
    private var status = 200, data = Data()
    private var requests: [URLRequest] = []
    func reset(status: Int = 200, data: Data) { lock.lock(); defer { lock.unlock() }; self.status = status; self.data = data; requests = [] }
    func reply(_ request: URLRequest) -> (Int, Data) { lock.lock(); defer { lock.unlock() }; requests.append(request); return (status, data) }
    func seen() -> [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests }
}
private final class PortalReadProtocol: URLProtocol, @unchecked Sendable {
    static let state = PortalReadState()
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

@MainActor final class TaskPortalModelTests: XCTestCase {
    private let account = "portal-" + String(repeating: "a", count: 32)
    private func data(empty: Bool = false, multiple: Bool = false) throws -> Data {
        let row: [String: Any] = ["account": account, "target": ["portal_id": "studio", "session_id": "ps-123-1"],
            "label": "Studio", "origin": "https://portal.example.invalid", "authenticated": true, "expires_in_s": 300]
        var second = row; second["account"] = "portal-" + String(repeating: "b", count: 32)
        second["target"] = ["portal_id": "studio", "session_id": "ps-123-2"]
        return try JSONSerialization.data(withJSONObject: ["service": "portal", "observed_at": Date().timeIntervalSince1970,
            "truncated": false, "items": empty ? [] : (multiple ? [row, second] : [row])])
    }
    private func client() -> ApprovalClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [PortalReadProtocol.self]
        let session = URLSession(configuration: config)
        let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture", endpoint: "https://127.0.0.1:1",
            tls_fingerprint: String(repeating: "0", count: 64), mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)
        return ApprovalClient(testSession: session, pairing: pairing, deviceID: "phone-fixture", transportCred: "fixture-reader")
    }
    func testActualEnrolledClientOnlyReadsFixedCatalogueBeforeExplicitChoice() async throws {
        PortalReadProtocol.state.reset(data: try data())
        let client = client(), model = TaskStartModel(); model.scope = "action"; model.exactAction = true; model.actionForm.kind = .portal
        await model.loadPortalSessions(client: client)
        XCTAssertNil(model.actionForm.portal.selectedSession); XCTAssertFalse(model.valid); XCTAssertNil(model.accepted)
        let seen = PortalReadProtocol.state.seen(); XCTAssertEqual(seen.count, 1)
        let request = try XCTUnwrap(seen.first)
        XCTAssertEqual(request.httpMethod, "GET"); XCTAssertEqual(request.url?.path, "/v1/agent/action-portal-sessions")
        XCTAssertEqual(request.url?.query, "limit=50"); XCTAssertNil(request.httpBody)
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Id"), "phone-fixture")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Transport-Cred"), "fixture-reader")
        model.selectPortalSession(account)
        XCTAssertTrue(model.valid); XCTAssertNil(model.accepted); XCTAssertEqual(PortalReadProtocol.state.seen().count, 1)
    }
    func testHTTPFailuresNeverKeepActionablePreviousCatalogue() async throws {
        let client = client(), model = TaskStartModel(); model.scope = "action"; model.exactAction = true; model.actionForm.kind = .portal
        PortalReadProtocol.state.reset(data: try data()); await model.loadPortalSessions(client: client); model.selectPortalSession(account)
        XCTAssertTrue(model.valid)
        for status in [401, 403, 503] {
            PortalReadProtocol.state.reset(status: status, data: Data("{}".utf8)); await model.loadPortalSessions(client: client)
            XCTAssertNil(model.actionForm.portal.catalogue); XCTAssertFalse(model.valid)
        }
    }
    func testMissingSessionNeverClaimsSuccessfulConnection() async throws {
        PortalReadProtocol.state.reset(data: try data(empty: true))
        let model = TaskStartModel(); model.scope = "action"; model.exactAction = true; model.actionForm.kind = .portal
        await model.loadPortalSessions(client: client())
        XCTAssertFalse(model.valid); XCTAssertNil(model.accepted); XCTAssertTrue(model.portalMessage.contains("nicht bestätigt"))
    }

    func testNativePortalViewsWithTemporaryUnpairedApp() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let app = AppModel(); XCTAssertNil(app.client)
        let cases: [(String, Double, DynamicTypeSize, Bool, Bool)] = [
            ("empty", 320, .large, true, false), ("selected", 390, .large, false, true),
            ("choose-large", 320, .accessibility5, false, false), ("selected-large", 320, .accessibility5, false, true)]
        for (name, width, size, empty, selected) in cases {
            let model = TaskStartModel(); model.scope = "action"; model.exactAction = true; model.actionForm.kind = .portal
            let rows = try TaskPortalSessions.decode(data(empty: empty, multiple: true))
            await model.loadPortalSessions(coreID: "fixture", deviceID: "fixture", fetch: { rows })
            if selected { model.selectPortalSession(account) }
            let controller = UIHostingController(rootView: NavigationStack {
                TaskStartView(app: app, model: model)
            }.environment(\.dynamicTypeSize, size))
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: width, height: 844)
            window.rootViewController = controller; window.makeKeyAndVisible()
            try await Task.sleep(nanoseconds: 500_000_000)
            controller.view.frame = window.bounds; controller.view.layoutIfNeeded()
            func capture(_ suffix: String) {
                let image = UIGraphicsImageRenderer(size: window.bounds.size).image { _ in
                    controller.view.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
                }
                let attachment = XCTAttachment(image: image)
                attachment.name = "portal-\(name)-\(Int(width))-\(suffix)"; attachment.lifetime = .keepAlways; add(attachment)
            }
            capture("top")
            func descendants(_ view: UIView) -> [UIView] { [view] + view.subviews.flatMap { descendants($0) } }
            if let scroll = descendants(controller.view).compactMap({ $0 as? UIScrollView }).first(where: { $0.contentSize.height > $0.bounds.height + 20 }) {
                scroll.setContentOffset(CGPoint(x: 0, y: max(0, scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)), animated: false)
                try await Task.sleep(nanoseconds: 100_000_000); capture("bottom")
            }
            window.isHidden = true; window.rootViewController = nil
            if size.isAccessibilitySize {
                // The full Form lazily estimates its off-screen rows. Capture the
                // actual new component separately so its complete large-type
                // contents, rather than only the preceding form, are reviewed.
                let fields = UIHostingController(rootView: ScrollView {
                    TaskPortalFields(model: model, client: nil).padding(20)
                }.environment(\.dynamicTypeSize, size))
                let detail = UIWindow(windowScene: scene)
                detail.frame = CGRect(x: 0, y: 0, width: width, height: 844)
                detail.rootViewController = fields; detail.makeKeyAndVisible()
                try await Task.sleep(nanoseconds: 300_000_000)
                fields.view.frame = detail.bounds; fields.view.layoutIfNeeded()
                let scroll = try XCTUnwrap(descendants(fields.view).compactMap { $0 as? UIScrollView }.first)
                let last = max(0, scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)
                let offsets = stride(from: 0.0, through: last, by: 650.0).map { $0 } + (last > 0 ? [last] : [])
                for (index, offset) in offsets.enumerated() {
                    scroll.setContentOffset(CGPoint(x: 0, y: offset), animated: false)
                    try await Task.sleep(nanoseconds: 100_000_000)
                    let picture = UIGraphicsImageRenderer(size: detail.bounds.size).image { _ in
                        fields.view.drawHierarchy(in: detail.bounds, afterScreenUpdates: true)
                    }
                    let attachment = XCTAttachment(image: picture)
                    attachment.name = "portal-fields-\(name)-\(Int(width))-\(index)"; attachment.lifetime = .keepAlways; add(attachment)
                }
                detail.isHidden = true; detail.rootViewController = nil
            }
        }
        XCTAssertNil(app.client)
    }
}
