// Der Zahlungsweg der App — sehen, was hinterlegt ist; handeln, mit Beweis.
//
// Eine `extension ApprovalClient` und KEINE zweite `URLSession`. Das ist die
// Hausregel aus `Networking.swift`, und sie hat einen Grund: das gepinnte
// Zertifikat, die Geraetekennung und die Transportkennung haengen an dieser
// einen Sitzung. Eine zweite waere ein zweiter Vertrauensanker — und ein
// zweiter Anker ist einer, den irgendwann jemand vergisst nachzuschaerfen.
//
// DER ANBIETER-TOKEN REIST GENAU EINMAL. Beim ersten Versuch geht er als
// Base64 mit — der Mac versiegelt ihn sofort und behaelt nur eine
// Einlagerungskennung. Danach verlangt die Matrix Face ID (jede Geldbewegung
// und jede Erweiterung einer Zahlungsbefugnis ist VERY_CRITICAL), der Core
// antwortet mit `approval_required`, und der zweite Versuch nach der Freigabe
// schickt NUR noch die Kennung.
//
// Was es hier NICHT gibt: ein Feld fuer eine Kartennummer, eine Pruefziffer
// oder ein Ablaufdatum. Nicht als optionales Feld, nicht als leerer String.
// Was es nicht gibt, kann auch nicht versehentlich getippt werden.
import CryptoKit
import Foundation
import SolvioApprovalsKit

/// Ein Zahlungsmittel, so wie der Core es beschreibt.
///
/// Es gibt kein Feld fuer eine Kartennummer und keines fuer den Anbieter-Token
/// — der Core gibt beides gar nicht erst heraus.
struct PaymentMethod: Codable, Hashable, Identifiable {
    let verweis: String
    var name: String = ""
    var art: String = ""
    var anbieter: String = ""
    var status: String = "active"
    var verfuegbar: Bool = false
    var waehrungen: [String] = []
    var haendler: [String] = []
    var grenze_einzeln_minor: Int = 0
    var grenze_taeglich_minor: Int = 0
    var zuletzt_benutzt: String = ""
    var hinweis: String = ""
    var gesperrt_weil: String = ""

    var id: String { verweis }
    var title: String { name.isEmpty ? verweis : name }
}

/// Ein Vorgang in der sicheren Sicht des Cores.
struct PaymentIntentView: Codable, Hashable, Identifiable {
    let payment_intent_id: String
    var zustand: String = ""
    var haendler: String = ""
    var haendler_herkunft: String = ""
    var zweck: String = ""
    var waehrung: String = ""
    var zahlungsmittel: String = ""
    var betrag_minor: Int? = nil
    var betrag_lesbar: String = ""
    var pruefsumme: String = ""
    var lieferung: String = ""
    var offen: Bool = false

    var id: String { payment_intent_id }
}

struct PaymentLedgerRow: Codable, Hashable, Identifiable {
    let id: Int
    var at: String = ""
    var payment_intent_id: String = ""
    var event: String = ""
    var merchant_id: String = ""
    var description: String = ""
    var amount_minor: Int = 0
    var currency: String = ""
    var instrument_ref: String = ""
    var status: String = ""
    var failure_category: String = ""
    var refunded_minor: Int = 0
}

struct PaymentChallenge: Codable {
    let nonce: String
    let core_instance_id: String
}

/// Was der Core zu einer Handlung sagt. `human_message` ist SEIN Satz — die
/// App zeigt ihn woertlich und erfindet keinen eigenen.
struct PaymentMutationResult: Codable {
    var ok: Bool? = nil
    var outcome: String? = nil
    var reason: String? = nil
    var human_message: String? = nil
    var request_id: String? = nil
    var staging_id: String? = nil

    var succeeded: Bool { ok == true }
    var needsApproval: Bool { outcome == "approval_required" }
}

/// Eine Liste, bei der EINE unlesbare Zeile nicht die ganze Liste umbringt.
///
/// Swift entschluesselt ein Array alles-oder-nichts: fehlt in einem einzigen
/// Element ein Schluessel, wirft die ganze Umwandlung. Bei Zahlungen ist das
/// die falsche Haerte — der Vorgang, den ein Mensch aufloesen muss, wuerde mit
/// dem kaputten mitverschwinden, und zwar STILL. Der Mac schickt inzwischen
/// stabile Schluessel; das hier ist der zweite Zaun dahinter.
struct Lenient<Element: Decodable>: Decodable {
    let werte: [Element]
    /// Wie viele Zeilen unlesbar waren. Null heisst: die Liste ist vollständig.
    let verworfen: Int

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var out: [Element] = []
        var bad = 0
        while !container.isAtEnd {
            if let value = try? container.decode(Element.self) {
                out.append(value)
            } else {
                // Weiterschalten, damit die Schleife nicht steht.
                _ = try? container.decode(AnyIgnored.self)
                bad += 1
            }
        }
        werte = out
        verworfen = bad
    }

    private struct AnyIgnored: Decodable {}
}

extension ApprovalClient {
    private func paymentGET<T: Decodable>(_ type: T.Type, _ path: String,
                                          query: [URLQueryItem] = []) async throws -> T {
        var request = controlRequest(path)
        if !query.isEmpty, let url = request.url,
           var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            request.url = parts.url
        }
        let (data, resp) = try await controlSession.data(for: request)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(T.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    func paymentMethods() async throws -> [PaymentMethod] {
        struct Envelope: Decodable { let zahlungsmittel: Lenient<PaymentMethod> }
        return try await paymentGET(Envelope.self,
                                    "v1/payment/methods").zahlungsmittel.werte
    }

    func paymentIntents() async throws -> [PaymentIntentView] {
        struct Envelope: Decodable { let vorgaenge: Lenient<PaymentIntentView> }
        return try await paymentGET(Envelope.self,
                                    "v1/payment/intents").vorgaenge.werte
    }

    func paymentLedger() async throws -> [PaymentLedgerRow] {
        struct Envelope: Decodable { let eintraege: Lenient<PaymentLedgerRow> }
        return try await paymentGET(Envelope.self,
                                    "v1/payment/ledger").eintraege.werte
    }

    /// EINE Zahlungshandlung, mit Beweis.
    ///
    /// `token` ist genau dann gesetzt, wenn der Anbieter-Token zum ersten Mal
    /// mitgeht. `stagingID` ist genau dann gesetzt, wenn er schon beim Core
    /// liegt und nur noch die Freigabe fehlte. Beides gleichzeitig waere ein
    /// Fehler und wird vom Core abgewiesen.
    func paymentMutate(capability: String, arguments: [String: String],
                       token: String? = nil, tokenSHA256: String = "",
                       stagingID: String = "") async throws -> PaymentMutationResult {
        let challenge = try await paymentGET(PaymentChallenge.self,
                                             "v1/payment/mutation/challenge")
        guard challenge.core_instance_id == pairedCoreInstanceID else {
            throw ClientError.badServer
        }
        let payload = PaymentMutationPayload(capability: capability,
                                             arguments: arguments,
                                             tokenSHA256: tokenSHA256)
        let binding = PaymentMutationBinding(coreInstanceID: challenge.core_instance_id,
                                             deviceID: deviceIdentifier,
                                             nonce: challenge.nonce,
                                             payloadSHA256: payload.sha256Hex())
        let keyId = try await AppAttestManager.ensureKeyId()
        let assertion = try await AppAttestManager.assert(
            keyId: keyId, clientDataHash: binding.clientDataHash())

        var body: [String: Any] = [
            "capability": capability, "arguments": arguments,
            "nonce": challenge.nonce,
            "assertion_b64": assertion.base64EncodedString()]
        if !tokenSHA256.isEmpty { body["token_sha256"] = tokenSHA256 }
        if let token, !token.isEmpty {
            body["token_b64"] = Data(token.utf8).base64EncodedString()
        } else if !stagingID.isEmpty {
            body["staging_id"] = stagingID
        }

        var request = controlRequest("v1/payment/mutation", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await controlSession.data(for: request)
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(PaymentMutationResult.self,
                                                  from: data) else {
            throw ClientError.decode
        }
        return out
    }
}

enum PaymentHash {
    /// Der SHA-256 eines Tokens in Hex. Der Token selbst verlaesst diese
    /// Funktion nicht — und was sie zurueckgibt, ist nicht umkehrbar.
    static func hex(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// Betraege in Menschensprache. Bewusst ohne `NumberFormatter`-Locale-Rat:
/// eine Systemeinstellung darf nicht bestimmen, welcher Betrag auf einer
/// Kaufbestaetigung steht. Dieselbe Rechnung wie `format_amount` im Core.
enum PaymentAmount {
    static func text(_ minor: Int, _ currency: String) -> String {
        let sign = minor < 0 ? "-" : ""
        let value = abs(minor)
        let major = value / 100
        let rest = value % 100
        var grouped = String(major)
        var out = ""
        while grouped.count > 3 {
            out = "." + String(grouped.suffix(3)) + out
            grouped = String(grouped.dropLast(3))
        }
        out = grouped + out
        return "\(sign)\(out),\(String(format: "%02d", rest)) \(currency)"
    }
}
