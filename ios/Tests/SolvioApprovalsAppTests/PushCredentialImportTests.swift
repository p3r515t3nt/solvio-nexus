import CryptoKit
import XCTest
@testable import SolvioApprovals

private final class PushImportMac: TresorTransport, @unchecked Sendable {
    var calls = 0
    var valuesSent = 0
    var scopes: [[String: String]] = []
    func tresorOverview() async throws -> TresorOverview { TresorOverview(zustand: "healthy") }
    func tresorLedger(ref: String) async throws -> [TresorLedgerRow] { [] }
    func listPending() async throws -> [PendingApproval] { [] }
    func tresorMutate(capability: String, arguments: [String: String], secret: String?,
                      secretSHA256: String, stagingID: String) async throws -> TresorMutationResult {
        calls += 1; valuesSent += secret == nil ? 0 : 1; scopes.append(arguments)
        XCTAssertEqual(capability, "secret_add")
        if calls == 1 {
            return TresorMutationResult(outcome: "approval_required", staging_id: "synthetic-stage")
        }
        XCTAssertNil(secret)
        XCTAssertEqual(stagingID, "synthetic-stage")
        return TresorMutationResult(ok: false, outcome: "rejected_by_policy", reason: "denied")
    }
}

final class PushCredentialImportTests: XCTestCase {
    private func fixture(_ edits: [String: String] = [:]) throws -> Data {
        // Ephemeral synthetic material, never an Apple credential.
        var object = ["bundle_id": "de.solvio.approvals", "environment": "development",
                      "team_id": "WQ8CG7R53R", "key_id": "SYNTHETIC1",
                      "private_key": P256.Signing.PrivateKey().pemRepresentation]
        object.merge(edits) { _, new in new }
        return try JSONSerialization.data(withJSONObject: object)
    }
    private func folder() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
    func testOnlyExactNexusDevelopmentKeyAndClosedEnvelopeAccepted() throws {
        let payload = try PushCredentialImport.validate(fixture())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: String])
        XCTAssertEqual(Set(object.keys), ["team_id", "key_id", "private_key"])
        for changes in [["bundle_id": "de.solvioforms.app"], ["environment": "production"],
                        ["team_id": "OTHERTEAM1"], ["key_id": "bad"],
                        ["key_id": "SYNTHETIC1\n"], ["key_id": "SYNTHETIC1\r"],
                        ["key_id": "SYNTHETICÄ"],
                        ["private_key": "not a key"], ["extra": "not allowed"]] {
            XCTAssertThrowsError(try PushCredentialImport.validate(fixture(changes)))
        }
        XCTAssertThrowsError(try PushCredentialImport.validate(Data(repeating: 32, count: 16_385)))
    }
    func testTemporaryHandoffConsumedExactlyOnceBeforeTransport() throws {
        let file = try folder().appendingPathComponent("setup.json")
        try fixture().write(to: file)
        XCTAssertTrue(PushCredentialImport.isPrepared(at: file))
        _ = try PushCredentialImport.take(at: file)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertThrowsError(try PushCredentialImport.take(at: file))
    }
    func testSymlinkAndNonRegularInputCannotReadOrConsumeAnotherFile() throws {
        let root = try folder(); let real = root.appendingPathComponent("original.json")
        let link = root.appendingPathComponent("setup.json")
        try fixture().write(to: real)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        XCTAssertFalse(PushCredentialImport.isPrepared(at: link))
        XCTAssertThrowsError(try PushCredentialImport.take(at: link))
        XCTAssertTrue(FileManager.default.fileExists(atPath: real.path))
        XCTAssertThrowsError(try PushCredentialImport.take(at: root))
    }
    func testFixedScopeCannotBecomeMailBrowserOrProductionAuthority() {
        let args = PushCredentialImport.draft.arguments()
        XCTAssertEqual(args["secret_ref"], "secret://apple-push/nexus")
        XCTAssertEqual(args["targets"], "https://api.sandbox.push.apple.com")
        XCTAssertEqual(args["capabilities"], "push_notify")
        XCTAssertEqual(args["executors"], "http")
        XCTAssertEqual(args["allow_background"], "true")
        XCTAssertEqual(args["kind"], "api_key")
        XCTAssertNil(args["private_key"])
    }
    @MainActor
    func testExistingVaultApprovalPathKeepsRejectionFinalAndSendsValueOnlyOnce() async throws {
        let file = try folder().appendingPathComponent("setup.json")
        try fixture().write(to: file)
        let mac = PushImportMac(); let model = TresorModel(client: mac)
        let value = try PushCredentialImport.take(at: file)
        _ = await model.add(entry: PushCredentialImport.draft, secret: value)
        XCTAssertEqual(mac.calls, 2); XCTAssertEqual(mac.valuesSent, 1)
        XCTAssertEqual(mac.scopes[0], mac.scopes[1])
        XCTAssertEqual(model.ergebnis, .abgelehnt("Das hast du abgelehnt. Dabei bleibt es."))
        XCTAssertNil(model.pending)
        XCTAssertThrowsError(try PushCredentialImport.take(at: file))
    }
}
