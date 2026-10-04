// Der Tresor-Weg der App — Zugaenge lesen, Zugaenge aendern.
//
// Eine `extension ApprovalClient` und KEINE zweite `URLSession`. Das ist die
// Hausregel aus `Networking.swift`, und sie hat einen Grund: das gepinnte
// Zertifikat, die Geraetekennung und die Transportkennung haengen an dieser
// einen Sitzung. Eine zweite waere ein zweiter Vertrauensanker — und ein
// zweiter Anker ist einer, den irgendwann jemand vergisst nachzuschaerfen.
// Benutzt werden dieselben Durchreichen wie im Kontrollzentrum:
// `controlSession` und `controlRequest`.
//
// DER WERT REIST GENAU EINMAL. Beim ersten Versuch geht er als Base64 mit —
// der Mac versiegelt ihn sofort unter seinem Hauptschluessel und behaelt nur
// eine Einlagerungskennung. Danach verlangt die Matrix Face ID (jede
// Tresor-Aenderung ist VERY_CRITICAL), der Core antwortet mit
// `approval_required` und dieser Kennung, und der zweite Versuch nach der
// Freigabe schickt NUR noch die Kennung.
//
// Warum der Wert nicht in den Argumenten steht: der Freigabeweg schreibt die
// Argumente als Text in die Freigabe-Datenbank des Macs, hasht sie in den
// Autorisierungs-Digest und zeigt sie auf dem Bildschirm. Ein Wert darin waere
// dauerhaft im Klartext. Damit der Beweis ihn trotzdem abdeckt, geht sein
// SHA-256 in die Bindung ein.
import CryptoKit
import Foundation
import SolvioApprovalsKit

/// Ein Zugang, so wie ihn der Core beschreibt.
///
/// Es gibt kein Feld fuer den Wert — nicht als `nil`, nicht als leerer String.
/// Was es nicht gibt, kann auch nicht versehentlich angezeigt werden.
struct TresorEntry: Codable, Hashable, Identifiable {
    let secret_ref: String
    var kind: String = ""
    var status: String = "active"
    var version: Int = 1
    var display_name: String = ""
    var service_label: String = ""
    var account_label: String = ""
    var allowed_capabilities: [String] = []
    var allowed_targets: [String] = []
    var allowed_executors: [String] = []
    var allow_background: Bool = false
    var requires_user_presence: Bool = false
    var created_at: String = ""
    var rotated_at: String = ""
    var last_used_at: String = ""
    var note: String = ""

    var id: String { secret_ref }

    /// Der Name, den ein Mensch lesen will. Der Verweis ist der Rueckfall,
    /// nicht die Anzeige.
    var title: String {
        if !display_name.isEmpty { return display_name }
        if !service_label.isEmpty { return service_label }
        return secret_ref
    }
}

struct TresorOverview: Codable {
    var zustand: String = "unknown"
    var grund: String = ""
    var zugaenge: [TresorEntry] = []
    /// Die Faehigkeitsnamen, die der Core ueberhaupt kennt. Sie kommen vom
    /// Mac, damit die App keinen Namen raten und keinen tippen muss — ein
    /// Tippfehler ergaebe eine Berechtigung, die ins Leere geht, oder eine,
    /// die jemand anderem gehoert.
    var bekannte_faehigkeiten: [String] = []
}

struct TresorLedgerRow: Codable, Hashable, Identifiable {
    let id: Int
    var at: String = ""
    var secret_ref: String = ""
    var capability: String = ""
    var origin: String = ""
    var executor: String = ""
    var target: String = ""
    var outcome: String = ""
    var denied_reason: String = ""
}

struct TresorChallenge: Codable {
    let nonce: String
    let core_instance_id: String
}

/// Was der Core zu einer Aenderung sagt. `human_message` ist SEIN Satz — die
/// App zeigt ihn woertlich und erfindet keinen eigenen.
struct TresorMutationResult: Codable {
    var ok: Bool? = nil
    var outcome: String? = nil
    var reason: String? = nil
    var human_message: String? = nil
    var request_id: String? = nil
    var staging_id: String? = nil

    var succeeded: Bool { ok == true }
    var needsApproval: Bool { outcome == "approval_required" }
}

extension ApprovalClient {
    private func tresorGET<T: Decodable>(_ type: T.Type, _ path: String,
                                         query: [URLQueryItem] = []) async throws -> T {
        var request = controlRequest(path)
        if !query.isEmpty, let url = request.url,
           var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            request.url = parts.url
        }
        let (data, resp) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(T.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    func googleConnectionSetup() async throws -> GoogleConnectionSetup {
        try await tresorGET(GoogleConnectionSetup.self, "v1/vault/google")
    }

    func tresorOverview() async throws -> TresorOverview {
        try await tresorGET(TresorOverview.self, "v1/vault/entries")
    }

    func tresorLedger(ref: String = "") async throws -> [TresorLedgerRow] {
        struct Envelope: Codable { let spur: [TresorLedgerRow] }
        let query = ref.isEmpty ? [] : [URLQueryItem(name: "verweis", value: ref)]
        return try await tresorGET(Envelope.self, "v1/vault/ledger", query: query).spur
    }

    /// EINE Tresor-Aenderung, mit Beweis.
    ///
    /// `secret` ist genau dann gesetzt, wenn der Wert zum ersten Mal mitgeht.
    /// `stagingID` ist genau dann gesetzt, wenn er schon beim Core liegt und
    /// nur noch die Freigabe fehlte. Beides gleichzeitig waere ein Fehler und
    /// wird vom Core abgewiesen.
    func tresorMutate(capability: String, arguments: [String: String],
                      secret: String? = nil, secretSHA256: String = "",
                      stagingID: String = "") async throws -> TresorMutationResult {
        let challenge = try await tresorGET(TresorChallenge.self,
                                            "v1/vault/mutation/challenge")
        try Task.checkCancellation()
        guard challenge.core_instance_id == pairedCoreInstanceID else {
            throw ClientError.badServer
        }
        let payload = VaultMutationPayload(capability: capability,
                                           arguments: arguments,
                                           secretSHA256: secretSHA256)
        let binding = VaultMutationBinding(coreInstanceID: challenge.core_instance_id,
                                           deviceID: deviceIdentifier,
                                           nonce: challenge.nonce,
                                           payloadSHA256: payload.sha256Hex())
        let keyId = try await AppAttestManager.ensureKeyId()
        try Task.checkCancellation()
        let assertion = try await AppAttestManager.assert(
            keyId: keyId, clientDataHash: binding.clientDataHash())
        try Task.checkCancellation()

        var body: [String: Any] = [
            "capability": capability, "arguments": arguments,
            "nonce": challenge.nonce,
            "assertion_b64": assertion.base64EncodedString()]
        if !secretSHA256.isEmpty { body["secret_sha256"] = secretSHA256 }
        if let secret, !secret.isEmpty {
            body["secret_b64"] = Data(secret.utf8).base64EncodedString()
        } else if !stagingID.isEmpty {
            body["staging_id"] = stagingID
        }

        var request = controlRequest("v1/vault/mutation", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let session = capability == "google_connect" ? googleMutationSession : controlSession
        if capability == "google_connect" { request.timeoutInterval = 50 }
        try Task.checkCancellation()
        let (data, resp) = try await session.data(for: request, delegate: ResultRedirectGuard())
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(TresorMutationResult.self,
                                                  from: data) else {
            throw ClientError.decode
        }
        return out
    }
}

enum TresorHash {
    /// Der SHA-256 eines Wertes in Hex. Der Wert selbst verlaesst diese
    /// Funktion nicht — und was sie zurueckgibt, ist nicht umkehrbar.
    static func hex(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
