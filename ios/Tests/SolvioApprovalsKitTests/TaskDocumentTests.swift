import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskDocumentTests: XCTestCase {
    // Real Core canonical_task_body + request_digest; Office fixture() creates
    // valid bounded containers. Original bytes, including CRLF/Unicode, stay exact.
    private let vectors: [(format: String, b64: String, digest: String)] = [
        ("rtf", "e1xydGYxXGFuc2kgR3J1ZXNzIGRpY2gufQ==", "eb67cfa752fdbc4692e0644bb28bfcaea8aea412530a23d8d5477133721d36a4"),
        ("txt", "R3LDvMOfZSwgRmFocnJhZCAmIENhZsOpLg0KWndlaXRlIFplaWxlLg==", "4d1e22dc68d54b98bd4b099f6849f8d87531b7714c05cec6533edc59040f4d93"),
        ("docx", "UEsDBBQAAAAIAAAAISh0JJxTuwAAAD4BAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbJWQuQ7CMAyGX6XKiqgRAwNquwArMPACVuq2EbkUuxxvT8o1sDHa//FZrk73SFzcnPVcq0EkrgFYD+SQyxDJZ6ULyaHkMfUQUZ+xJ1guFivQwQt5mcvUoZpqSx2OVordLa/ZBF+rRJZVsXkZJ1atMEZrNErW4eLbH8r8TShz8unhwUSeZYOCpjpcKCXTUnHEJHt0uQ6uIbXQBj26jCgn41+80HVG0zc/tcUUNDEb3ztbfhWHxn/ugOfbmgdQSwMEFAAAAAgAAAAhKGF7L0OJAAAA8gAAAAsAAABfcmVscy8ucmVsc43POw4CIRAG4KsQDrCzWlgYoLLZ1ngBAsMjLo8MGPX2UlisxsJy5p98f0accdU9ltxCrI090pqb5KH3egRoJmDSbSoV80hcoaT7GMlD1eaqPcJ+ng9AW4MrsTXZYiWnxe44uzwr/mMX56LBUzG3hLn/qPi6GLImj13yeyEL9r2eBstBCfh4Ub0AUEsDBBQAAAAIAAAAISgz6bTmqQAAANMAAAARAAAAd29yZC9kb2N1bWVudC54bWxFzz0OwjAMBeAdiTtEGZhQUzEwhJIFCe7QLSRuG6n5kRMInIaZO7D1YjRlYPks60nPcpO59upmwSXysKOLPB/pkFLgjEU1gJWx8gHcnHUerUzzij3LHnVAryBG43o7sl1d75mVxlHRZH71+llmKGBhaecxSAVHGhAi4B2ouOD0mV6wJWc5IEpNNtKGAznJbnpX61WbwSQgLZgRqobNNaKIi2Hxd4r93xBfUEsBAhQDFAAAAAgAAAAhKHQknFO7AAAAPgEAABMAAAAAAAAAAAAAAIABAAAAAFtDb250ZW50X1R5cGVzXS54bWxQSwECFAMUAAAACAAAACEoYXsvQ4kAAADyAAAACwAAAAAAAAAAAAAAgAHsAAAAX3JlbHMvLnJlbHNQSwECFAMUAAAACAAAACEoM+m05qkAAADTAAAAEQAAAAAAAAAAAAAAgAGeAQAAd29yZC9kb2N1bWVudC54bWxQSwUGAAAAAAMAAwC5AAAAdgIAAAAA", "d657797dbad5949bad3fa7d3116a18ef4a36ca31eb24159be8338b55a25cefc6"),
        ("odt", "UEsDBBQAAAAAAAAAIShexjIMJwAAACcAAAAIAAAAbWltZXR5cGVhcHBsaWNhdGlvbi92bmQub2FzaXMub3BlbmRvY3VtZW50LnRleHRQSwMEFAAAAAgAAAAhKILnBYKZAAAAQwEAABUAAABNRVRBLUlORi9tYW5pZmVzdC54bWyNUNsKgzAM/ZWRd9u5x2L9l1AjK7RpsVH071cHc44x2FtOknNJuojsRypiXsVljYHLAS3ME5uExRfDGKkYcSZl4iG5ORKL+dw3rbrC5UALTcUnttCqG/Td0R99oKayp+29O84hNBnlbkGfJCINHhvZMlnAnIN3KFVSLzyoZy51jqOEVgH9v5VLLDuvnvHDdFfU+7iq6q9/9Q9QSwMEFAAAAAgAAAAhKONkatqsAAAANwEAAAsAAABjb250ZW50LnhtbI3QvQ7CIBAA4N3EdyAMTgrWESmLib5DN6TXSCLQANX6NM6+g1tfTGvR2MHE6XI/311y3FWVVsBKpxoDNi6Us/EZUWuONrChm+PGW+Zk0IFZaSCwqJirwb4V+55mGVni5CO08V/dzw427TmBD9rZHGdkhQVP1b0rL5+kN4K/ZC12vrt3V5ijrTx4L0s0k6Zeo42suhuZTooz6AioAH0EwmlCnI5W0dEV+uM74gFQSwECFAMUAAAAAAAAACEoXsYyDCcAAAAnAAAACAAAAAAAAAAAAAAAgAEAAAAAbWltZXR5cGVQSwECFAMUAAAACAAAACEogucFgpkAAABDAQAAFQAAAAAAAAAAAAAAgAFNAAAATUVUQS1JTkYvbWFuaWZlc3QueG1sUEsBAhQDFAAAAAgAAAAhKONkatqsAAAANwEAAAsAAAAAAAAAAAAAAIABGQEAAGNvbnRlbnQueG1sUEsFBgAAAAADAAMAsgAAAO4BAAAAAA==", "da72c83936a2784fa3c8a3aec2188684f89e702ea7b3d176669d1ee660b00bb8")
    ]
    private let objective = "Lies das Dokument für München vollständig."
    private func body(_ document: AppTaskDocumentRequest, id: String = "document-ios-001") throws -> AppTaskBody {
        try AppTaskBody(scope: "research", objective: objective, targetRepo: "", requestID: id, documentRequest: document)
    }
    private func document(_ index: Int) throws -> AppTaskDocumentRequest {
        let row = vectors[index]
        return try AppTaskDocumentRequest(format: row.format, content: XCTUnwrap(Data(base64Encoded: row.b64)))
    }

    func testEveryFormatKeepsExactBytesAndMatchesRealCoreHash() throws {
        for (index, vector) in vectors.enumerated() {
            let document = try document(index), body = try body(document)
            XCTAssertEqual(document.content_b64, vector.b64)
            XCTAssertEqual(body.requestDigest, vector.digest)
            XCTAssertEqual(try JSONDecoder().decode(AppTaskBody.self, from: JSONEncoder().encode(body)), body)
            let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
            XCTAssertEqual(Set(wire.keys), ["scope", "objective", "target_repo", "client_request_id", "document_request"])
            XCTAssertEqual(Set(try XCTUnwrap(wire["document_request"] as? [String: Any]).keys), ["operation", "format", "content_b64"])
        }
    }

    func testFormatSizeBoundariesAndUnsupportedInputs() throws {
        for format in AppTaskDocumentRequest.formats {
            let limit = try AppTaskDocumentRequest.byteLimit(format: format)
            let prefix = format == "rtf" ? Data("{\\rtf1 ".utf8) : Data("x".utf8)
            let atLimit = prefix + Data(repeating: 120, count: limit - prefix.count)
            XCTAssertEqual(try AppTaskDocumentRequest(format: format, content: atLimit).byteCount, limit)
            XCTAssertThrowsError(try AppTaskDocumentRequest(format: format, content: atLimit + Data([120])))
            XCTAssertThrowsError(try AppTaskDocumentRequest(format: format, content: Data()))
        }
        for format in ["pdf", "doc", "html", "RTF", "docm", "zip"] {
            XCTAssertThrowsError(try AppTaskDocumentRequest(format: format, content: Data([1])))
        }
    }

    func testUTF8ValidationNeverNormalizesOriginalBytes() throws {
        let content = Data([0xef, 0xbb, 0xbf]) + Data("Grüße e\u{301}\r\n\tZwei".utf8)
        XCTAssertEqual(try AppTaskDocumentRequest(format: "txt", content: content).content, content)
        for bad in [Data([0xff]), Data([0xc0, 0xaf]), Data([0xed, 0xa0, 0x80]),
                    Data(" \n\t".utf8), Data([0xef, 0xbb, 0xbf]), Data("Text\0".utf8), Data([120, 1]), Data([120, 31])] {
            XCTAssertThrowsError(try AppTaskDocumentRequest(format: "txt", content: bad))
        }
        for bad in ["{\\rtf1}", "{\\rtf10 Text}", "Plain text"] {
            XCTAssertThrowsError(try AppTaskDocumentRequest(format: "rtf", content: Data(bad.utf8)))
        }
    }

    func testWireIsClosedCanonicalAndCannotMixActionOrBuild() throws {
        let request = try document(1), body = try body(request)
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        for (key, value) in [("operation", "write" as Any), ("format", "pdf"), ("path", "/tmp/private"),
                             ("content_b64", request.content_b64 + "\n"), ("content_b64", "eB=="),
                             ("content_b64", true)] {
            var changed = wire; changed[key] = value
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskDocumentRequest.self, from: JSONSerialization.data(withJSONObject: changed)))
        }
        for scope in ["build", "action"] {
            XCTAssertThrowsError(try AppTaskBody(scope: scope, objective: objective, targetRepo: "", requestID: body.client_request_id, documentRequest: request))
        }
        var task = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
        task["document_request"] = NSNull()
        XCTAssertThrowsError(try JSONDecoder().decode(AppTaskBody.self, from: JSONSerialization.data(withJSONObject: task)))
    }

    func testFileSelectionIsBoundedFrozenAndDoesNotSendFilenameOrPath() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        for (index, vector) in vectors.enumerated() {
            let url = folder.appendingPathComponent("PRIVATE_FILENAME_72." + vector.format.uppercased())
            let bytes = try XCTUnwrap(Data(base64Encoded: vector.b64))
            try bytes.write(to: url)
            let selected = try TaskDocumentSelection.read(url)
            XCTAssertEqual(selected.request, try document(index))
            try Data("changed on disk".utf8).write(to: url)
            XCTAssertEqual(selected.request.content, bytes)
            let wire = String(decoding: try body(selected.request).canonicalBytes(), as: UTF8.self)
            XCTAssertFalse(wire.contains("PRIVATE_FILENAME_72")); XCTAssertFalse(wire.contains(folder.path))
            let oversized = Data(repeating: 120, count: try AppTaskDocumentRequest.byteLimit(format: vector.format) + 1)
            try oversized.write(to: url)
            XCTAssertThrowsError(try TaskDocumentSelection.read(url))
        }
        let directory = folder.appendingPathComponent("directory.txt")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        XCTAssertThrowsError(try TaskDocumentSelection.read(directory))
        let source = folder.appendingPathComponent("source.txt"), link = folder.appendingPathComponent("link.txt")
        try Data("Private source".utf8).write(to: source)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: source)
        XCTAssertThrowsError(try TaskDocumentSelection.read(link))
        XCTAssertThrowsError(try TaskDocumentSelection.read(XCTUnwrap(URL(string: "https://example.invalid/file.txt"))))
    }

    func testRetryAcrossProcessLossBindsFormatAndEveryByte() throws {
        let source = try document(1)
        var draft = TaskStartDraft()
        let original = try draft.prepare(scope: "research", objective: objective, targetRepo: "", documentRequest: source)
        let retry = TaskStartRetryBinding(body: original, coreID: "core-one", deviceID: "device-one")
        let metadata = String(decoding: try JSONEncoder().encode(retry), as: UTF8.self)
        XCTAssertFalse(metadata.contains(source.content_b64)); XCTAssertFalse(metadata.contains(objective))
        var fresh = TaskStartDraft()
        let restored = try fresh.prepare(scope: "research", objective: objective, targetRepo: "", retry: retry,
            coreID: "core-one", deviceID: "device-one", documentRequest: source)
        XCTAssertEqual(restored, original)
        let changed = try AppTaskDocumentRequest(format: "txt", content: source.content + Data([33]))
        for candidate in [changed, try document(0), nil] {
            XCTAssertNil(retry.restore(scope: "research", objective: objective, targetRepo: "",
                coreID: "core-one", deviceID: "device-one", documentRequest: candidate))
            XCTAssertNotEqual(try fresh.prepare(scope: "research", objective: objective, targetRepo: "", documentRequest: candidate).client_request_id,
                              original.client_request_id)
        }
        XCTAssertNil(retry.restore(scope: "research", objective: objective, targetRepo: "",
            coreID: "other", deviceID: "device-one", documentRequest: source))
    }
    @MainActor
    func testLostDocumentResponseRetriesExactBodyWithFreshProofAndRejectsDifferentBytes() async throws {
        let original = try body(document(2))
        let changed = try body(document(3))
        let attempt = TaskStartAttempt()
        var sent = [AppTaskBody](), nonces = [String](), signed = [Data]()
        for index in 0...1 {
            let nonce = String(repeating: index == 0 ? "ab" : "cd", count: 32)
            let binding: [String: Any] = ["protocol_version": 1, "type": "app_task_start_binding",
                "core_instance_id": "core-one", "principal_id": "owner", "device_id": "device-one",
                "nonce": nonce, "request_digest": original.requestDigest, "enrollment_id": "enrolled",
                "app_attest_key_id": "test-key", "approval_key_sha256": String(repeating: "1", count: 64)]
            let raw = try JSONSerialization.data(withJSONObject: binding, options: [.sortedKeys, .withoutEscapingSlashes])
            let challenge = AppTaskChallenge(nonce: nonce, requestDigest: original.requestDigest,
                expiresAt: 2000000000, bindingB64: raw.base64EncodedString())
            XCTAssertThrowsError(try challenge.clientDataHash(body: changed, coreID: "core-one",
                deviceID: "device-one", appAttestKeyID: "test-key", now: 1999999990))
            do {
                _ = try await attempt.send(body: original, coreID: "core-one", deviceID: "device-one",
                    appAttestKeyID: "test-key", challenge: { submitted in
                        XCTAssertEqual(submitted, original); return challenge
                    }, sign: { data in signed.append(data); return Data([1]) }, submit: { submitted, proof in
                        sent.append(submitted); nonces.append(proof.nonce)
                        if index == 0 { throw URLError(.networkConnectionLost) }
                        return AppTaskAccepted(taskID: "at-test", runID: "ar-test", state: "angenommen")
                    }, now: { 1999999990 })
                XCTAssertEqual(index, 1)
            } catch { XCTAssertEqual(index, 0) }
        }
        XCTAssertEqual(sent, [original, original])
        XCTAssertEqual(sent.first?.document_request?.content, try document(2).content)
        XCTAssertNotEqual(nonces[0], nonces[1]); XCTAssertNotEqual(signed[0], signed[1])
    }

}
