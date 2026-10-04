// Local approval gateway client. LOCAL NETWORK ONLY, HTTPS with
// certificate pinning to the fingerprint carried in the pairing QR (global TLS validation is
// NOT disabled). Transport auth (device credential) can only READ; approval authority is the
// Face-ID Secure-Enclave signature PLUS a fresh Apple App Attest assertion, both verified by
// the Mac control-plane.
import CryptoKit
import Foundation
import SolvioApprovalsKit

struct PairingPayload: Codable {
    let v: Int
    let type: String
    let core_instance_id: String
    let endpoint: String
    let tls_fingerprint: String
    let mac_pubkey_fingerprint: String
    let mac_pubkey_x963_b64: String
    let enrollment_token: String
    let expires_at: Double
}

struct BeginEnrollResponse: Codable {
    let enrollment_id: String
    let binding_b64: String
    let attestation_nonce: String
    let expires_at: Double
    let device_id: String
    let principal: String
}

struct PendingApproval: Codable, Identifiable, Hashable {
    let approval_id: String
    let tool: String
    let mode: String
    let task: String
    let workspace: String
    let human_summary: String
    let action_digest: String
    let expires_at: Double
    var id: String { approval_id }
}

struct ChallengeWire: Codable { let payload_b64: String; let signature_b64: String; let key_id: String }
struct DecisionResult: Codable { let approval_id: String; let decision: String }

/// A Mac-signed challenge whose signature was verified over `payload` (the EXACT received
/// bytes). `payloadSHA256` is bound into the decision so consent covers this display context.
struct VerifiedChallenge {
    let challenge: ApprovalChallenge
    let payload: Data
    let payloadSHA256: String
}

enum ClientError: Error, LocalizedError {
    case http(Int), decode, badPinning, badChallengeSignature, badServer
    case challengeMismatch, challengeExpired, displayMismatch
    var errorDescription: String? {
        switch self {
        case let .http(c): return "Server \(c)"
        case .decode: return "Antwort nicht lesbar"
        case .badPinning: return "TLS-Zertifikat nicht vertrauenswürdig"
        case .badChallengeSignature: return "Mac-Signatur ungültig"
        case .badServer: return "Falscher Server"
        case .challengeMismatch: return "Signierte Anfrage passt nicht zur Auswahl"
        case .challengeExpired: return "Anzeige abgelaufen – bitte neu laden"
        case .displayMismatch: return "SICHERHEITSWARNUNG: Liste und signierte Anfrage widersprechen sich"
        }
    }
}

final class PinningDelegate: NSObject, URLSessionDelegate {
    private let pinned: String
    init(pinned: String) { self.pinned = pinned }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge) async
        -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else {
            return (.cancelAuthenticationChallenge, nil)
        }
        let der = SecCertificateCopyData(leaf) as Data
        let fp = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        return fp == pinned ? (.useCredential, URLCredential(trust: trust))
                            : (.cancelAuthenticationChallenge, nil)
    }
}

final class ApprovalClient: Sendable {
    private let endpoint: URL
    private let deviceID: String
    private let transportCred: String
    private let macPubKeyX963: Data
    private let coreInstanceID: String
    private let session: URLSession
    let googleMutationSession: URLSession

    init(pairing: PairingPayload, deviceID: String, transportCred: String) {
        self.endpoint = URL(string: pairing.endpoint)!
        self.deviceID = deviceID
        self.transportCred = transportCred
        self.macPubKeyX963 = Data(base64Encoded: pairing.mac_pubkey_x963_b64) ?? Data()
        self.coreInstanceID = pairing.core_instance_id
        self.session = ApprovalClient.pinnedSession(pairing)
        self.googleMutationSession = ApprovalClient.pinnedSession(pairing, requestLimit: 50, resourceLimit: 55)
    }

    #if DEBUG
    /// Offline URLProtocol fixtures only. Release has only the pinned initializer.
    init(testSession: URLSession, pairing: PairingPayload, deviceID: String, transportCred: String) {
        self.endpoint = URL(string: pairing.endpoint)!
        self.deviceID = deviceID
        self.transportCred = transportCred
        self.macPubKeyX963 = Data(base64Encoded: pairing.mac_pubkey_x963_b64) ?? Data()
        self.coreInstanceID = pairing.core_instance_id
        self.session = testSession
        self.googleMutationSession = testSession
    }
    #endif

    /// Ohne Frist stapeln sich im Drei-Sekunden-Takt haengende Abrufe, bis der
    /// Stau die App lahmlegt. Die Voreinstellung von `URLSession` sind 60
    /// Sekunden je Anfrage und sieben Tage je Ressource — beides ist fuer eine
    /// Liste im lokalen Netz absurd lang.
    static let requestTimeout: TimeInterval = 8
    static let resourceTimeout: TimeInterval = 15

    private static func pinnedSession(_ pairing: PairingPayload, requestLimit: TimeInterval = requestTimeout,
                                      resourceLimit: TimeInterval = resourceTimeout) -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = requestLimit
        config.timeoutIntervalForResource = resourceLimit
        config.waitsForConnectivity = false      // lieber ehrlich scheitern als warten
        return URLSession(configuration: config,
                          delegate: PinningDelegate(pinned: pairing.tls_fingerprint),
                          delegateQueue: nil)
    }

    // MARK: - enrollment (two-step: begin -> App Attest -> complete)
    static func beginEnroll(pairing: PairingPayload, deviceID: String, approvalPublicKeyX963: Data,
                            appAttestKeyId: String, transportCred: String) async throws -> BeginEnrollResponse {
        let session = pinnedSession(pairing)
        var req = URLRequest(url: URL(string: pairing.endpoint)!.appendingPathComponent("v1/enroll/begin"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "enrollment_token": pairing.enrollment_token, "device_id": deviceID,
            "approval_public_key_x963_b64": approvalPublicKeyX963.base64EncodedString(),
            "app_attest_key_id": appAttestKeyId, "transport_cred": transportCred])
        let (data, resp) = try await session.data(for: req)
        guard let h = resp as? HTTPURLResponse else { throw ClientError.decode }
        guard h.statusCode == 200 else { throw ClientError.http(h.statusCode) }
        guard let out = try? JSONDecoder().decode(BeginEnrollResponse.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    static func completeEnroll(pairing: PairingPayload, enrollmentId: String,
                               attestationB64: String) async throws {
        let session = pinnedSession(pairing)
        var req = URLRequest(url: URL(string: pairing.endpoint)!.appendingPathComponent("v1/enroll/complete"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "enrollment_id": enrollmentId, "attestation_b64": attestationB64])
        let (_, resp) = try await session.data(for: req)
        guard let h = resp as? HTTPURLResponse else { throw ClientError.decode }
        guard h.statusCode == 200 else { throw ClientError.http(h.statusCode) }
    }

    private func authed(_ path: String, _ method: String = "GET") -> URLRequest {
        var r = URLRequest(url: endpoint.appendingPathComponent(path))
        r.httpMethod = method
        r.setValue(deviceID, forHTTPHeaderField: "X-Device-Id")
        r.setValue(transportCred, forHTTPHeaderField: "X-Transport-Cred")
        return r
    }

    // MARK: - Zugang fuer das Kontrollzentrum
    //
    // Absichtlich ein Durchreichen und keine zweite Sitzung: das Kontrollzentrum
    // soll denselben gepinnten Anschluss und dieselben Kopfzeilen benutzen wie
    // die Freigabeliste. Eine eigene Sitzung waere ein zweiter Vertrauensanker,
    // und ein zweiter Anker ist einer, den irgendwann jemand vergisst
    // nachzuschaerfen.
    var controlSession: URLSession { session }

    func controlRequest(_ path: String) -> URLRequest { authed(path) }

    func controlRequest(_ path: String, _ method: String) -> URLRequest {
        authed(path, method)
    }

    /// Die Kennung dieses Geraets. Nur lesbar; sie geht in jede Bindung ein.
    var deviceIdentifier: String { deviceID }

    /// Die Kennung der Core-Instanz, gegen die dieses Geraet gepaart ist.
    ///
    /// Nur lesbar, und nur zu einem Zweck: eine Challenge, die eine ANDERE
    /// Core-Kennung nennt, kommt nicht von dem Mac, mit dem dieses Geraet
    /// gepaart wurde — und wird verworfen, bevor irgendetwas signiert wird.
    var pairedCoreInstanceID: String { coreInstanceID }

    func listPending() async throws -> [PendingApproval] {
        let (data, resp) = try await session.data(for: authed("v1/approvals"))
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        struct Envelope: Codable { let approvals: [PendingApproval] }
        return (try? JSONDecoder().decode(Envelope.self, from: data))?.approvals ?? []
    }

    /// Fetch a Mac-signed challenge, VERIFY the Mac signature over the EXACT received bytes,
    /// and return those bytes together with their SHA-256. The result is the ONLY authoritative
    /// basis for the approval screen — never the unsigned pending list.
    func challenge(approvalID: String) async throws -> VerifiedChallenge {
        let (data, resp) = try await session.data(for: authed("v1/approvals/\(approvalID)/challenge", "POST"))
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let wire = try? JSONDecoder().decode(ChallengeWire.self, from: data),
              let payload = Data(base64Encoded: wire.payload_b64),
              let sig = Data(base64Encoded: wire.signature_b64) else { throw ClientError.decode }
        guard ApprovalCrypto.verify(publicKeyX963: macPubKeyX963, signatureDER: sig, data: payload) else {
            throw ClientError.badChallengeSignature
        }
        let obj = try ApprovalProtocol.strictParse(payload)
        let ch = try ApprovalChallenge(from: obj)
        guard ch.coreInstanceID == coreInstanceID else { throw ClientError.badServer }
        // The signed challenge must be about the approval we asked for, and be issued to
        // THIS device — otherwise a substituted challenge could be signed and displayed.
        guard ch.approvalID == approvalID else { throw ClientError.challengeMismatch }
        guard ch.deviceID == deviceID else { throw ClientError.challengeMismatch }
        guard Date().timeIntervalSince1970 < ch.expiresAt else { throw ClientError.challengeExpired }
        return VerifiedChallenge(challenge: ch, payload: payload,
                                 payloadSHA256: ApprovalCrypto.sha256Hex(payload))
    }

    /// Submit the signed decision. For APPROVE, `assertionB64` carries the App Attest assertion.
    func submitDecision(approvalID: String, payloadB64: String, signatureB64: String,
                        keyID: String, assertionB64: String?) async throws -> DecisionResult {
        var req = authed("v1/approvals/\(approvalID)/decision", "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["payload_b64": payloadB64, "signature_b64": signatureB64,
                                   "key_id": keyID]
        if let assertionB64 { body["assertion_b64"] = assertionB64 }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await session.data(for: req)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let r = try? JSONDecoder().decode(DecisionResult.self, from: data) else { throw ClientError.decode }
        return r
    }
}

// MARK: - Wissen — das Gedaechtnis, wie die App es sieht (SOLVIO PRESENCE V2)
//
// Dieselbe Verbindung, dieselben Kopfzeilen, derselbe gepinnte Anschluss wie
// die Freigabeliste — es entsteht KEIN zweiter Vertrauensanker. Lesen laeuft
// wie im Kontrollzentrum ueber die Transportkennung. AENDERN dagegen braucht
// je Aufruf eine frische, vom Core ausgegebene Nonce und eine App-Attest-
// Assertion ueber den exakten Auftrag (MemoryMutationBinding) — die statische
// Transportkennung allein kann kein Gedaechtnis umschreiben.

/// Beweislage eines Eintrags — Zahlen und Zeitpunkte, nie Wortlaut. Der
/// Gespraechskoerper liegt nirgends, auch nicht hinter diesem Typ.
struct MemoryEvidence: Codable, Hashable {
    var observations: Int? = nil
    var kinds: [String: Int]? = nil
    var first_at: String? = nil
    var last_at: String? = nil
}

/// Ein Eintrag, wie der Core ihn liefert. `lifecycle` und `explanation` sind
/// ABGELEITETE Wahrheiten des Cores — die App leitet nie selbst ab und
/// formuliert keine eigene Herkunftsauskunft.
struct MemoryItem: Codable, Identifiable, Hashable {
    let id: String
    let content: String
    let memory_type: String
    var subject: String? = nil
    let lifecycle: String
    var sensitivity: String? = nil
    var trust_level: String? = nil
    var confidence: Double? = nil
    var created_at: String? = nil
    var updated_at: String? = nil
    var valid_until: String? = nil
    var evidence: MemoryEvidence? = nil
    var explanation: String? = nil
}

/// Ein wartender Vorschlag. Vorschlaege sind KEIN Gedaechtnis — sie kommen
/// ausschliesslich aus `/candidates` und werden nie mit Eintraegen vermischt.
struct MemoryCandidate: Codable, Identifiable, Hashable {
    let candidate_id: String
    let statement: String
    var state: String? = nil
    var ask_reason: String? = nil
    var memory_type: String? = nil
    var sensitivity: String? = nil
    var observations: Int? = nil
    var independent_conversations: Int? = nil
    var first_seen: String? = nil
    var last_seen: String? = nil
    var contradicts: MemoryContradiction? = nil
    var id: String { candidate_id }
}

/// Der bestehende Eintrag, dem ein Vorschlag widerspricht.
struct MemoryContradiction: Codable, Hashable {
    let id: String
    let content: String
    var lifecycle: String? = nil
}

struct MemoryProvenanceEntry: Codable, Hashable {
    var source_type: String? = nil
    var trust_level: String? = nil
    var at: String? = nil
    var source: String? = nil
    var note: String? = nil
}

struct MemoryProvenance: Codable, Hashable {
    let id: String
    var lifecycle: String? = nil
    var explanation: String? = nil
    var evidence: MemoryEvidence? = nil
    var chain: [MemoryProvenanceEntry] = []
}

struct MemoryMutationChallenge: Codable {
    let nonce: String
    let core_instance_id: String
}

/// Was der Core zu einer Aenderung sagt. `human_message` ist SEIN Satz — die
/// App zeigt ihn woertlich und erfindet keinen eigenen.
struct MemoryMutationResult: Codable {
    var ok: Bool? = nil
    var outcome: String? = nil
    var reason: String? = nil
    var human_message: String? = nil

    var succeeded: Bool { ok == true }
}

extension ApprovalClient {
    private func memoryGET<T: Decodable>(_ type: T.Type, _ path: String,
                                         query: [URLQueryItem] = []) async throws -> T {
        var request = authed(path)
        if !query.isEmpty, let url = request.url,
           var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            request.url = parts.url
        }
        let (data, resp) = try await session.data(for: request)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(T.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    func memories(limit: Int = 100) async throws -> [MemoryItem] {
        struct Envelope: Codable { let memories: [MemoryItem] }
        return try await memoryGET(Envelope.self, "v1/memory/memories",
                                   query: [URLQueryItem(name: "limit", value: String(limit))])
            .memories
    }

    func memoryCandidates() async throws -> [MemoryCandidate] {
        struct Envelope: Codable { let candidates: [MemoryCandidate] }
        return try await memoryGET(Envelope.self, "v1/memory/candidates").candidates
    }

    func memoryProvenance(id: String) async throws -> MemoryProvenance {
        try await memoryGET(MemoryProvenance.self, "v1/memory/memories/\(id)/provenance")
    }

    /// EINE Aenderung am Gedaechtnis, mit Beweis.
    ///
    /// Ablauf: frische Nonce vom Core holen, den Auftrag kanonisieren, die
    /// App-Attest-Assertion ueber das MemoryMutationBinding erzeugen, senden.
    /// Die Nonce ist je Aufruf frisch — eine mitgeschnittene Aenderung laesst
    /// sich nicht wiederholen. Ob die Aenderung ERLAUBT ist, entscheidet allein
    /// der Core; die App kann hier nichts herabstufen und nichts umgehen.
    func memoryMutate(capability: String, arguments: [String: String]) async throws
        -> MemoryMutationResult {
        let ch = try await memoryGET(MemoryMutationChallenge.self, "v1/memory/mutation/challenge")
        guard ch.core_instance_id == coreInstanceID else { throw ClientError.badServer }
        let payload = MemoryMutationPayload(capability: capability, arguments: arguments)
        let binding = MemoryMutationBinding(coreInstanceID: coreInstanceID, deviceID: deviceID,
                                            nonce: ch.nonce, payloadSHA256: payload.sha256Hex())
        let keyId = try await AppAttestManager.ensureKeyId()
        let assertion = try await AppAttestManager.assert(
            keyId: keyId, clientDataHash: binding.clientDataHash())
        var req = authed("v1/memory/mutation", "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "capability": capability, "arguments": arguments,
            "nonce": ch.nonce, "assertion_b64": assertion.base64EncodedString()])
        let (data, resp) = try await session.data(for: req)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(MemoryMutationResult.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }
}

// MARK: - Chats (C3) — dauerhafte Gespraeche in derselben gepinnten Verbindung
//
// Lesen und Anlegen laufen ueber die Transportkennung — wie die Auftragsliste:
// keine Kosten, keine Autoritaet. Eine NACHRICHT dagegen braucht je Zustellung
// eine frische Nonce und eine App-Attest-Assertion ueber den exakten Body
// (ConversationMessageBinding); die statische Kennung allein stellt keine
// Nachricht zu, genau wie sie keinen Auftrag startet.

extension ApprovalClient {
    private func conversationGET<T: Decodable>(_ type: T.Type, _ path: String, query: [URLQueryItem] = []) async throws -> T {
        var request = controlRequest(path)
        if !query.isEmpty, let url = request.url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            request.url = parts.url
        }
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, http.statusCode == 200, response.url == request.url else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(T.self, from: data) else { throw ClientError.decode }
        return out
    }

    func conversations(limit: Int = 30) async throws -> [ConversationSummary] {
        struct Envelope: Decodable { let conversations: [ConversationSummary] }
        let rows = try await conversationGET(Envelope.self, "v1/conversations",
                                             query: [URLQueryItem(name: "limit", value: String(max(1, min(limit, 100))))]).conversations
        guard rows.allSatisfy({ ConversationSummary.validID($0.conversation_id) }) else { throw ClientError.decode }
        return rows
    }

    func conversation(_ conversationID: String, afterSequence: Int? = nil) async throws -> ConversationDetail {
        guard ConversationSummary.validID(conversationID) else { throw ClientError.decode }
        let query = afterSequence.map { [URLQueryItem(name: "after_sequence", value: String($0))] } ?? []
        let detail = try await conversationGET(ConversationDetail.self, "v1/conversations/\(conversationID)", query: query)
        guard detail.conversation.conversation_id == conversationID else { throw ClientError.decode }
        return detail
    }

    func createConversation(clientRequestID: String, title: String = "") async throws -> ConversationCreated {
        var request = controlRequest("v1/conversations", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["client_request_id": clientRequestID]
        if !title.isEmpty { body["title"] = title }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        let status = (response as? HTTPURLResponse)?.statusCode ?? -1
        guard [200, 201].contains(status), response.url == request.url else { throw ClientError.http(status) }
        guard let created = try? JSONDecoder().decode(ConversationCreated.self, from: data),
              ConversationSummary.validID(created.conversation_id) else { throw ClientError.decode }
        return created
    }

    func messageChallenge(_ conversationID: String, body: AppConversationMessageBody) async throws -> AppConversationMessageChallenge {
        struct Body: Encodable { let message: AppConversationMessageBody }
        guard body.conversation_id == conversationID else { throw ClientError.decode }
        var request = controlRequest("v1/conversations/\(conversationID)/messages/challenge", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(message: body))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard (response as? HTTPURLResponse)?.statusCode == 200, response.url == request.url else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return try JSONDecoder().decode(AppConversationMessageChallenge.self, from: data)
    }

    func submitMessage(_ conversationID: String, body: AppConversationMessageBody, proof: AppTaskProof) async throws -> AppConversationMessageAccepted {
        struct Body: Encodable { let message: AppConversationMessageBody; let proof: AppTaskProof }
        guard body.conversation_id == conversationID else { throw ClientError.decode }
        var request = controlRequest("v1/conversations/\(conversationID)/messages", "POST")
        // Der Chat-Endpunkt authentifiziert zuerst das gekoppelte Geraet mit
        // der Transportkennung. Zusaetzlich autorisiert nur die frische,
        // nachrichtengebundene App-Attest-Assertion die Zustellung.
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(message: body, proof: proof))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        let status = (response as? HTTPURLResponse)?.statusCode ?? -1
        guard status == 202, response.url == request.url else { throw ClientError.http(status) }
        return try AppConversationMessageAccepted.response(data: data, status: status)
    }

    func delivery(_ conversationID: String, deliveryID: String) async throws -> ConversationDeliveryStatus {
        guard ConversationSummary.validID(conversationID), ConversationDelivery.validID(deliveryID) else { throw ClientError.decode }
        return try await conversationGET(ConversationDeliveryStatus.self, "v1/conversations/\(conversationID)/deliveries/\(deliveryID)")
    }
}

extension ApprovalClient {
    func contactLookup(_ query: String) async throws -> ContactLookup {
        var request = authed("v1/contacts/search", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["q": query])
        return try await contactResponse(request, as: ContactLookup.self)
    }

    func contactMutate(_ operation: String, arguments: [String: String]) async throws -> ContactMutationResult {
        let challenge = try await contactResponse(authed("v1/contacts/challenge"), as: MemoryMutationChallenge.self)
        guard challenge.core_instance_id == coreInstanceID else { throw ClientError.badServer }
        let payload = MemoryMutationPayload(capability: operation, arguments: arguments)
        let binding = MemoryMutationBinding(coreInstanceID: coreInstanceID, deviceID: deviceID,
            nonce: challenge.nonce, payloadSHA256: payload.sha256Hex())
        let key = try await AppAttestManager.ensureKeyId()
        let assertion = try await AppAttestManager.assert(keyId: key, clientDataHash: binding.clientDataHash())
        var request = authed("v1/contacts", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "capability": operation, "arguments": arguments,
            "nonce": challenge.nonce, "assertion_b64": assertion.base64EncodedString()])
        return try await contactResponse(request, as: ContactMutationResult.self)
    }

    private func contactResponse<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return try JSONDecoder().decode(type, from: data)
    }
}

extension ApprovalClient {
    func pushChallenge() async throws -> PushSetupResponse {
        try await contactResponse(authed("v1/push/challenge"), as: PushSetupResponse.self)
    }
    func pushRegister(token: String, environment: String) async throws -> PushSetupResponse {
        try await setupMutation(path: "v1/push", operation: "push_register",
                                arguments: ["token": token, "environment": environment], as: PushSetupResponse.self)
    }
    func followupMailSearch(_ query: String) async throws -> FollowupMailResults {
        var request = authed("v1/everyday/mail/search", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["query": query])
        return try await contactResponse(request, as: FollowupMailResults.self)
    }
    func everydaySchedule(_ schedule: [String: Any]) async throws -> ContactMutationResult {
        let raw = try JSONSerialization.data(withJSONObject: schedule, options: [.sortedKeys])
        guard let json = String(data: raw, encoding: .utf8) else { throw ClientError.decode }
        return try await setupMutation(path: "v1/everyday", operation: "everyday_schedule",
                                       arguments: ["schedule_json": json], as: ContactMutationResult.self)
    }
    private func setupMutation<T: Decodable>(path: String, operation: String,
                                             arguments: [String: String], as type: T.Type) async throws -> T {
        let challenge = try await contactResponse(authed(path + "/challenge"), as: MemoryMutationChallenge.self)
        guard challenge.core_instance_id == coreInstanceID else { throw ClientError.badServer }
        let payload = MemoryMutationPayload(capability: operation, arguments: arguments)
        let binding = MemoryMutationBinding(coreInstanceID: coreInstanceID, deviceID: deviceID,
            nonce: challenge.nonce, payloadSHA256: payload.sha256Hex())
        let key = try await AppAttestManager.ensureKeyId()
        let assertion = try await AppAttestManager.assert(keyId: key, clientDataHash: binding.clientDataHash())
        var request = authed(path, "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "capability": operation, "arguments": arguments,
            "nonce": challenge.nonce, "assertion_b64": assertion.base64EncodedString()])
        return try await contactResponse(request, as: type)
    }
}
