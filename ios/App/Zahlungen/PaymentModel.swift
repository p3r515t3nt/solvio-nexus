// Zahlungen — was SOLVIO bezahlen darf, und was gerade offen ist.
//
// Das Modell haelt, was der Core beim letzten Abruf geliefert hat, und NICHTS
// Eigenes. Es gibt hier keine Eigenschaft, die eine Kartennummer traegt — nicht
// als Zwischenspeicher, nicht als Entwurf, nicht als `nil`. Der einzige Ort, an
// dem ueberhaupt ein Anbieter-Token existiert, ist das lokale `@State` des
// Eingabeblattes, und es lebt genau so lange wie das Blatt.
//
// Wenn der Mac nicht antwortet, bleibt der letzte Stand stehen — als ALT
// gekennzeichnet, nie als frisch. Dieselbe Ehrlichkeit wie im Tresor, im
// Wissen und im Kontrollzentrum.
import Foundation
import SwiftUI

/// Der Zustand eines Vorgangs in Produktsprache. Die Worte kommen WOERTLICH vom
/// Core; hier wird nur uebersetzt, nie abgeleitet.
func zahlungZustandswort(_ zustand: String) -> String {
    switch zustand {
    case "draft": return "Entwurf"
    case "quoted": return "Betrag steht"
    case "ready_for_approval": return "Wartet auf dich"
    case "approved": return "Freigegeben"
    case "executing": return "Läuft"
    case "awaiting_sca": return "Deine Bank fragt nach"
    case "succeeded": return "Bezahlt"
    case "failed": return "Nicht bezahlt"
    case "reconciliation_required": return "Ausgang unklar"
    case "cancelled": return "Verworfen"
    case "expired": return "Abgelaufen"
    case "partially_refunded": return "Teilweise zurück"
    case "refunded": return "Zurückerstattet"
    default: return "Unbekannt"
    }
}

func zahlungStatuswort(_ status: String) -> String {
    switch status {
    case "active": return "Verfügbar"
    case "disabled": return "Gesperrt"
    case "revoked": return "Widerrufen"
    case "revalidation_required": return "Muss geprüft werden"
    default: return "Unbekannt"
    }
}

func zahlungArtwort(_ art: String) -> String {
    switch art {
    case "virtual_card": return "Virtuelle Karte"
    case "provider_token": return "Beim Anbieter hinterlegt"
    case "merchant_saved": return "Beim Händler hinterlegt"
    case "wallet": return "Wallet"
    default: return "Zahlungsmittel"
    }
}

/// Eine wartende Handlung, die nur noch Face ID braucht.
struct PaymentPending: Equatable {
    let capability: String
    let arguments: [String: String]
    var tokenSHA256: String = ""
    var stagingID: String = ""
}

@MainActor
final class PaymentModel: ObservableObject {
    @Published private(set) var methods: [PaymentMethod] = []
    @Published private(set) var intents: [PaymentIntentView] = []
    @Published private(set) var ledger: [PaymentLedgerRow] = []
    @Published private(set) var reachable = true
    @Published private(set) var loaded = false
    @Published private(set) var snapshotAt: Double = 0
    @Published private(set) var pending: PaymentPending? = nil
    @Published var working = false
    @Published var notice: String = ""

    let client: ApprovalClient?

    init(client: ApprovalClient? = nil) {
        self.client = client ?? PaymentModel.restoredClient()
    }

    /// Dieselbe Registrierung wie ueberall — sie liegt genau EINMAL im
    /// Keychain. Es entsteht kein zweites Geheimnis und kein zweiter
    /// Vertrauensanker, nur ein zweiter Handgriff daran.
    private static func restoredClient() -> ApprovalClient? {
        guard let data = Keychain.load(tag: "de.solvio.approvals.enrollment"),
              let e = try? JSONDecoder().decode(Enrollment.self, from: data)
        else { return nil }
        return ApprovalClient(pairing: e.pairing, deviceID: e.deviceID,
                              transportCred: e.transportCred)
    }

    /// Die offenen Vorgaenge — das, was gerade wirklich auf jemanden wartet.
    var offen: [PaymentIntentView] { intents.filter { $0.offen } }

    func refresh() async {
        guard let client else { return }
        // KEIN `try?` auf den Teillisten.
        //
        // Vorher verschluckte es einen Lesefehler und liess den Bildschirm
        // gesund und leer dastehen — beim Zahlungsbuch also: „du hast nie
        // etwas bezahlt". Ein Fehler, den man nicht sieht, ist schlimmer als
        // ein alter Stand, der sich als alt zu erkennen gibt.
        do {
            methods = try await client.paymentMethods()
            intents = try await client.paymentIntents()
            ledger = try await client.paymentLedger()
            snapshotAt = Date().timeIntervalSince1970
            reachable = true
            loaded = true
        } catch {
            // Der alte Stand bleibt stehen — als alt gekennzeichnet.
            reachable = false
        }
    }

    // MARK: - Handlungen
    //
    // Jede laeuft ueber `paymentMutate`. Was zurueckkommt, ist der Satz des
    // Cores; die App formuliert keinen eigenen Erfolg und keine eigene Absage.

    /// Bezahlen. Der teuerste Knopf der App — und der einzige, der Geld bewegt.
    func pay(intent: PaymentIntentView) async -> String {
        await mutate(capability: "purchase_place",
                     arguments: ["vorgang": intent.payment_intent_id,
                                 "pruefsumme": intent.pruefsumme])
    }

    func disable(ref: String, reason: String = "") async -> String {
        await mutate(capability: "payment_method_disable",
                     arguments: ["verweis": ref, "grund": reason])
    }

    func enable(ref: String) async -> String {
        await mutate(capability: "payment_method_enable",
                     arguments: ["verweis": ref])
    }

    func remove(ref: String) async -> String {
        await mutate(capability: "payment_method_remove",
                     arguments: ["verweis": ref])
    }

    /// Eine Grenze SENKEN. Die sichere Richtung — und deshalb der billigere Weg.
    func lowerLimit(ref: String, single: Int?, daily: Int?) async -> String {
        var args = ["verweis": ref]
        if let single { args["grenze_einzeln"] = String(single) }
        if let daily { args["grenze_taeglich"] = String(daily) }
        return await mutate(capability: "payment_limit_lower", arguments: args)
    }

    /// Eine Grenze ANHEBEN. Aus jeder Herkunft biometrisch.
    func raiseLimit(ref: String, single: Int?, daily: Int?) async -> String {
        var args = ["verweis": ref]
        if let single { args["grenze_einzeln"] = String(single) }
        if let daily { args["grenze_taeglich"] = String(daily) }
        return await mutate(capability: "payment_limit_raise", arguments: args)
    }

    /// Ein Zahlungsmittel hinterlegen. Der Anbieter-Token geht genau einmal mit.
    func add(draft: PaymentDraft, token: String) async -> String {
        await mutate(capability: "payment_method_add",
                     arguments: draft.arguments(), token: token)
    }

    func confirmPending(_ warten: PaymentPending) async -> String {
        await mutate(capability: warten.capability, arguments: warten.arguments,
                     token: nil, tokenSHA256: warten.tokenSHA256,
                     stagingID: warten.stagingID)
    }

    /// Der einzige Schreibweg. Setzt `pending`, wenn eine Freigabe fehlt.
    private func mutate(capability: String, arguments: [String: String],
                        token: String? = nil, tokenSHA256: String = "",
                        stagingID: String = "") async -> String {
        guard let client else { return "Kein Zugang zum Mac." }
        working = true
        defer { working = false }
        let digest = token.map(PaymentHash.hex) ?? tokenSHA256
        do {
            let result = try await client.paymentMutate(
                capability: capability, arguments: arguments, token: token,
                tokenSHA256: digest, stagingID: stagingID)
            if result.needsApproval {
                let warten = PaymentPending(capability: capability,
                                            arguments: arguments,
                                            tokenSHA256: digest,
                                            stagingID: result.staging_id ?? "")
                pending = warten
                notice = "Bestätige das mit Face ID — ich mache dann von selbst weiter."
                Haptics.state()
                await awaitApprovalAndFinish(warten)
                return notice
            }
            pending = nil
            if result.succeeded { Haptics.success() } else { Haptics.warning() }
            await refresh()
            notice = result.human_message ?? ""
            return notice
        } catch {
            Haptics.warning()
            notice = "Das hat der Mac nicht angenommen."
            return notice
        }
    }

    /// Wartet auf die Face-ID-Entscheidung und setzt die Handlung SELBST fort.
    ///
    /// **An der Sicherheit aendert das nichts.** Die Freigabe faellt unveraendert
    /// im Freigabeweg mit Face ID; dieser Code wartet nur darauf und schickt
    /// danach dieselbe Handlung noch einmal. Er kann nichts freigeben, nichts
    /// herabstufen und nichts ueberspringen — der Mac beantwortet eine
    /// unbestaetigte Wiederholung erneut mit `approval_required`, und eine
    /// ABGELEHNTE Freigabe bleibt abgelehnt: sie ist verbraucht, und der
    /// zweite Versuch trifft auf nichts.
    private func awaitApprovalAndFinish(_ warten: PaymentPending) async {
        guard let client else { return }
        for _ in 0..<120 {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard let offen = try? await client.listPending() else { continue }
            if offen.contains(where: { $0.tool == warten.capability }) { continue }
            let result = try? await client.paymentMutate(
                capability: warten.capability, arguments: warten.arguments,
                token: nil, tokenSHA256: warten.tokenSHA256,
                stagingID: warten.stagingID)
            if let result, result.succeeded {
                pending = nil
                notice = result.human_message ?? ""
                Haptics.success()
                await refresh()
            } else if let result, result.needsApproval {
                continue
            } else {
                pending = nil
                notice = result?.human_message ?? "Das wurde nicht freigegeben."
                Haptics.warning()
                await refresh()
            }
            return
        }
        notice = "Ich habe keine Entscheidung gesehen. Versuch es noch einmal."
        pending = nil
    }

    func clearPending() { pending = nil }
}

/// Der Entwurf fuer ein neues Zahlungsmittel. Traegt KEINEN Token — der lebt
/// im `@State` des Eingabeblattes und geht direkt in `add(draft:token:)`.
struct PaymentDraft: Equatable {
    var verweis: String = "payment://shopping/default"
    var art: String = "virtual_card"
    var anbieter: String = ""
    var name: String = ""
    var hinweis: String = ""
    var waehrungen: String = "EUR"
    var haendler: String = ""
    var grenzeEinzelnMinor: String = "5000"
    var grenzeTaeglichMinor: String = "10000"
    var zugang: String = ""
    var lesezugang: String = ""

    func arguments() -> [String: String] {
        [
            "verweis": verweis, "art": art, "anbieter": anbieter,
            "name": name, "hinweis": hinweis,
            "waehrungen": waehrungen, "haendler": haendler,
            "grenze_einzeln": grenzeEinzelnMinor,
            "grenze_taeglich": grenzeTaeglichMinor,
            "zugang": zugang, "lesezugang": lesezugang,
        ]
    }
}
