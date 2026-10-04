// Tresor — die Zugaenge, die SOLVIO benutzen darf, ohne sie zu zeigen.
//
// Das Modell haelt, was der Core beim letzten Abruf geliefert hat, und NICHTS
// Eigenes. Es gibt in diesem Modell keine Eigenschaft, die einen Wert traegt —
// nicht als Zwischenspeicher, nicht als Entwurf, nicht als `nil`. Der einzige
// Ort, an dem ein Wert ueberhaupt existiert, ist das lokale `@State` des
// Eingabeblattes, und es lebt genau so lange wie das Blatt.
//
// Wenn der Mac nicht antwortet, bleibt der letzte Stand stehen — als ALT
// gekennzeichnet, nie als frisch ausgegeben. Dieselbe Ehrlichkeit wie im
// Kontrollzentrum und im Wissen.
import Foundation
import SwiftUI

/// Der Zustand des Tresors in Produktsprache. Die Worte kommen WOERTLICH vom
/// Core; hier wird nur uebersetzt, nie abgeleitet.
func tresorZustandswort(_ zustand: String) -> String {
    switch zustand {
    case "healthy": return "Bereit"
    case "auth_required": return "Braucht deine Anmeldung"
    case "degraded": return "Eingeschränkt"
    case "unavailable": return "Nicht verfügbar"
    default: return "Nicht nachgesehen"
    }
}

func tresorStatuswort(_ status: String) -> String {
    switch status {
    case "active": return "Verfügbar"
    case "disabled": return "Gesperrt"
    case "revoked": return "Widerrufen"
    default: return "Unbekannt"
    }
}

func tresorArtwort(_ kind: String) -> String {
    switch kind {
    case "password": return "Passwort"
    case "api_token": return "Zugangstoken"
    case "api_key": return "Schlüssel"
    case "oauth_client_secret": return "Anwendungsgeheimnis"
    case "oauth_refresh_token": return "Anmeldung"
    case "service_credential": return "Dienstzugang"
    case "machine_credential": return "Gerätezugang"
    default: return "Zugang"
    }
}

/// „Nur amazon.de", nicht „nur https://www.amazon.de" — der Mensch liest den
/// Ort, nicht das Schema. Die BINDUNG bleibt trotzdem die vollstaendige
/// Herkunft; hier wird nur angezeigt.
func tresorZielwort(_ targets: [String]) -> String {
    let hosts = targets.compactMap { URL(string: $0)?.host }
    if hosts.isEmpty { return targets.joined(separator: ", ") }
    return "Nur " + hosts.joined(separator: ", ")
}

/// Was der Tresor vom Mac braucht — und sonst nichts.
///
/// Der einzige Grund für dieses Protokoll ist Prüfbarkeit. `ApprovalClient`
/// spricht über TLS mit gepinntem Zertifikat und App-Attest-Signatur; die vier
/// Ausgänge einer Freigaberunde lassen sich gegen ihn nicht beweisen, ohne
/// einen echten Mac und ein echtes Gesicht. **An der Sicherheit ändert das
/// nichts**: der echte Weg bleibt derselbe Client, und dieses Protokoll kann
/// nichts freigeben, nichts signieren und nichts herabstufen — es benennt nur
/// die vier Handgriffe, die das Modell überhaupt kennt.
protocol TresorTransport: Sendable {
    func tresorOverview() async throws -> TresorOverview
    func tresorLedger(ref: String) async throws -> [TresorLedgerRow]
    func tresorMutate(capability: String, arguments: [String: String],
                      secret: String?, secretSHA256: String,
                      stagingID: String) async throws -> TresorMutationResult
    func listPending() async throws -> [PendingApproval]
}

extension ApprovalClient: TresorTransport {}

/// Wie eine Änderung ausgegangen ist. Vier Ausgänge — nie ein stilles Nichts.
///
/// **Face ID ist kein Ausgang.** Der Daumen des Eigentümers sagt, dass er
/// zugestimmt hat; ob die Änderung wirklich griff, sagt allein der Mac. Diese
/// Unterscheidung ist der ganze Zweck dieses Typs: vorher schloss das Blatt
/// kommentarlos, und Erfolg, Absage und Fehler sahen gleich aus.
///
/// Der Satz kommt in jedem Fall WÖRTLICH vom Core. Die App wählt nur, welches
/// Gesicht er bekommt.
enum TresorErgebnis: Equatable {
    /// Der Mac hat die Änderung ausgeführt und bestätigt.
    case erledigt(String)
    /// Der Eigentümer hat abgelehnt. Dabei bleibt es.
    case abgelehnt(String)
    /// Niemand hat entschieden, solange die Anfrage galt.
    case abgelaufen(String)
    /// Etwas ging schief — Transport, Vertrag, oder der Umfang hat sich bewegt.
    case fehler(String)

    var satz: String {
        switch self {
        case .erledigt(let t), .abgelehnt(let t), .abgelaufen(let t), .fehler(let t):
            return t
        }
    }

    var gelungen: Bool { if case .erledigt = self { return true }; return false }

    /// Symbol und Farbe folgen dem Ausgang, nicht der Stimmung.
    var symbol: String {
        switch self {
        case .erledigt: return "checkmark.circle.fill"
        case .abgelehnt: return "hand.raised.fill"
        case .abgelaufen: return "clock.badge.xmark"
        case .fehler: return "exclamationmark.triangle.fill"
        }
    }
}

@MainActor
final class TresorModel: ObservableObject {
    @Published private(set) var entries: [TresorEntry] = []
    @Published private(set) var zustand: String = "unknown"
    @Published private(set) var grund: String = ""
    @Published private(set) var ledger: [TresorLedgerRow] = []
    @Published private(set) var reachable = true
    @Published private(set) var loaded = false
    @Published private(set) var snapshotAt: Double = 0
    /// Woraus eine Rechteliste ueberhaupt bestehen kann — vom Mac gelesen.
    @Published private(set) var bekannteFaehigkeiten: [String] = []
    @Published var working = false
    @Published var notice: String = ""

    let client: TresorTransport?

    private var invalidated = false

    init(client: TresorTransport? = nil, restoreEnrollment: Bool = true) {
        self.client = client ?? (restoreEnrollment ? TresorModel.restoredClient() : nil)
    }

    /// Dieselbe Registrierung wie ueberall — sie liegt genau EINMAL im
    /// Keychain. Es entsteht kein zweites Geheimnis und kein zweiter
    /// Vertrauensanker, nur ein zweiter Handgriff daran.
    private static func restoredClient() -> TresorTransport? {
        guard let data = Keychain.load(tag: "de.solvio.approvals.enrollment"),
              let e = try? JSONDecoder().decode(Enrollment.self, from: data)
        else { return nil }
        return ApprovalClient(pairing: e.pairing, deviceID: e.deviceID,
                              transportCred: e.transportCred)
    }

    func refresh() async {
        guard !invalidated, let client else { return }
        do {
            let overview = try await client.tresorOverview()
            guard !invalidated else { return }
            entries = overview.zugaenge
            zustand = overview.zustand
            grund = overview.grund
            bekannteFaehigkeiten = overview.bekannte_faehigkeiten
            let freshLedger = try? await client.tresorLedger(ref: "")
            guard !invalidated else { return }
            ledger = freshLedger ?? ledger
            snapshotAt = Date().timeIntervalSince1970
            reachable = true
            loaded = true
        } catch {
            // Der alte Stand bleibt stehen — als alt gekennzeichnet.
            reachable = false
        }
    }

    // MARK: - Aenderungen
    //
    // Jede laeuft ueber `tresorMutate`. Was zurueckkommt, ist der Satz des
    // Cores; die App formuliert keinen eigenen Erfolg und keine eigene Absage.

    /// Legt einen Zugang an. Der Wert geht genau einmal mit.
    ///
    /// Kommt `approval_required` zurueck, ist der Wert bereits versiegelt beim
    /// Core, und es fehlt nur noch Face ID. Die Einlagerungskennung wird
    /// zurueckgegeben, damit der zweite Versuch OHNE den Wert auskommt.
    func add(entry draft: TresorDraft, secret: String) async -> String {
        await mutate(capability: "secret_add", arguments: draft.arguments(),
                     secret: secret)
    }

    func replace(ref: String, secret: String) async -> String {
        await mutate(capability: "secret_replace", arguments: ["secret_ref": ref],
                     secret: secret)
    }

    func confirmPending(_ pending: TresorPending) async -> String {
        await mutate(capability: pending.capability, arguments: pending.arguments,
                     secret: nil, secretSHA256: pending.secretSHA256,
                     stagingID: pending.stagingID)
    }

    func disable(ref: String) async -> String {
        await mutate(capability: "secret_disable", arguments: ["secret_ref": ref])
    }

    func enable(ref: String) async -> String {
        await mutate(capability: "secret_enable", arguments: ["secret_ref": ref])
    }

    func delete(ref: String) async -> String {
        await mutate(capability: "secret_delete", arguments: ["secret_ref": ref])
    }

    /// Aendert, wofuer ein Zugang benutzt werden darf.
    ///
    /// `capabilities` ERSETZT die Liste beim Core — es ergaenzt sie nicht.
    /// Deshalb geht hier immer die VOLLSTAENDIGE Liste hinaus, nie eine
    /// Ergaenzung. Die Oberflaeche rechnet sie aus dem gelesenen Bestand
    /// aus; hardcodiert ist nichts.
    ///
    /// `expected_version` ist die Fassung, die der Mensch gesehen hat. Hat
    /// sich der Umfang zwischenzeitlich bewegt, weist der Core ab — die
    /// Zustimmung galt einem anderen Stand.
    /// Wie eine angestossene Änderung begonnen hat — nicht, wie sie ausging.
    enum Start: Equatable {
        /// Die Anfrage steht beim Mac. Es fehlt nur noch Face ID.
        case wartetAufFreigabe
        /// Ohne Freigabe durchgelaufen.
        case erledigt
        /// Der Mac hat sie gar nicht erst angenommen.
        case fehlgeschlagen
    }

    /// Läuft eine Änderung, auf die noch Face ID fehlt? Solange ja, erzeugt ein
    /// zweiter Tipp keine zweite Anfrage.
    private var wartetTask: Task<Void, Never>? = nil
    private var initialTransfer: Task<TresorMutationResult, Error>?

    /// Stösst die Rechteänderung an und kehrt zurück, SOBALD der Mac geantwortet
    /// hat — nicht erst, wenn die Freigabe entschieden ist.
    ///
    /// Das ist der ganze Unterschied zu `mutate`, und er ist keine Feinheit:
    /// wer auf die Face-ID-Runde wartet, hält das auslösende Blatt offen, und
    /// das Blatt verdeckt genau die Karte, über die freigegeben wird. Der
    /// Eigentümer sass dann vor einem stummen Bildschirm und musste
    /// „Abbrechen" erraten. Hier kehrt der Aufruf nach einem Rundlauf zurück,
    /// das Blatt schliesst, und das Warten übernimmt dieses Modell.
    ///
    /// **Das Blatt zu schliessen bricht nichts ab.** Die Wartearbeit gehört dem
    /// Modell, und das Modell gehört dem Bildschirm, nicht dem Blatt.
    ///
    /// `capabilities` ERSETZT die Liste beim Core — es ergänzt sie nicht.
    /// Deshalb geht hier immer die VOLLSTÄNDIGE Liste hinaus. `expected_version`
    /// ist die Fassung, die der Mensch gesehen hat; hat sich der Umfang
    /// zwischenzeitlich bewegt, weist der Core ab.
    func startRescope(ref: String, capabilities: [String],
                      expectedVersion: Int) async -> Start {
        await startMutation(capability: "secret_rescope", arguments: [
            "secret_ref": ref, "capabilities": capabilities.joined(separator: ", "),
            "expected_version": String(expectedVersion)], secret: nil)
    }

    func startGoogleConnect(_ authorization: GoogleAuthorizationCode, binding: String) async -> Start {
        defer { authorization.discard() }
        guard !working, wartetTask == nil, pending == nil else { return .wartetAufFreigabe }
        guard let code = try? authorization.takeCode() else { return .fehlgeschlagen }
        return await startMutation(capability: "google_connect", arguments: [
            "expected_binding": binding, "account_id": authorization.accountID,
            "account_email": authorization.accountEmail], secret: code)
    }

    private func startMutation(capability: String, arguments: [String: String], secret: String?) async -> Start {
        guard !invalidated else { return .fehlgeschlagen }
        guard let client else {
            ergebnis = .fehler("Kein Zugang zum Mac.")
            return .fehlgeschlagen
        }
        guard !working, wartetTask == nil, pending == nil else { return .wartetAufFreigabe }
        let digest = secret.map(TresorHash.hex) ?? ""
        working = true
        defer { working = false }
        ergebnis = nil
        do {
            let transfer = Task {
                try Task.checkCancellation()
                return try await client.tresorMutate(capability: capability, arguments: arguments,
                    secret: secret, secretSHA256: digest, stagingID: "")
            }
            initialTransfer = transfer
            defer { initialTransfer = nil }
            let result = try await transfer.value
            guard !invalidated, !Task.isCancelled else { return .fehlgeschlagen }
            if result.needsApproval {
                let warten = TresorPending(capability: capability,
                                           arguments: arguments,
                                           secretSHA256: digest,
                                           stagingID: result.staging_id ?? "")
                pending = warten
                notice = "Bestätige das mit Face ID — ich mache dann von selbst weiter."
                Haptics.state()
                wartetTask = Task { [weak self] in
                    await self?.awaitApprovalAndFinish(warten)
                    self?.wartetTask = nil
                }
                return .wartetAufFreigabe
            }
            pending = nil
            await refresh()
            guard !invalidated else { return .fehlgeschlagen }
            if result.succeeded {
                Haptics.success()
                ergebnis = .erledigt(satzOderStandard(result, capability == "google_connect" ? "Google ist für SOLVIO verbunden." : "Die Rechte sind geändert."))
                notice = ergebnis?.satz ?? ""
                return .erledigt
            }
            Haptics.warning()
            ergebnis = deutung(result)
            notice = ergebnis?.satz ?? ""
            return .fehlgeschlagen
        } catch {
            guard !invalidated else { return .fehlgeschlagen }
            Haptics.warning()
            ergebnis = .fehler("Der Ausgang ist nicht bestätigt. Bitte prüfe den aktuellen Stand; der Auftrag wird nicht erneut gesendet.")
            notice = ergebnis?.satz ?? ""
            return .fehlgeschlagen
        }
    }

    private func satzOderStandard(_ result: TresorMutationResult,
                                  _ ersatz: String) -> String {
        let satz = (result.human_message ?? "").trimmingCharacters(in: .whitespaces)
        return satz.isEmpty ? ersatz : satz
    }

    /// Den Ausgang des Cores einem der vier Fälle zuordnen.
    ///
    /// Die Wörter bleiben seine — hier wird nur einsortiert. `denied` ist eine
    /// Antwort und keine Panne; `not_approved` heisst, dass niemand entschieden
    /// hat, solange die Anfrage galt.
    private func deutung(_ result: TresorMutationResult) -> TresorErgebnis {
        switch result.reason ?? "" {
        case "denied":
            return .abgelehnt(satzOderStandard(result, "Das hast du abgelehnt. Dabei bleibt es."))
        case "not_approved", "unknown_approval":
            return .abgelaufen(satzOderStandard(result, "Dafür kam keine Freigabe."))
        default:
            return .fehler(satzOderStandard(result, "Das hat nicht geklappt — es ist nichts passiert."))
        }
    }

    /// Der einzige Schreibweg. Setzt `pending`, wenn eine Freigabe fehlt.
    @Published private(set) var pending: TresorPending? = nil

    /// Wie die letzte Änderung ausging. Bleibt stehen, bis der Nächste
    /// beginnt — damit ein Ergebnis nicht mit dem Blatt verschwindet, das es
    /// ausgelöst hat.
    @Published private(set) var ergebnis: TresorErgebnis? = nil

    private func mutate(capability: String, arguments: [String: String],
                        secret: String? = nil, secretSHA256: String = "",
                        stagingID: String = "") async -> String {
        guard let client else { return "Kein Zugang zum Mac." }
        working = true
        defer { working = false }
        let digest = secret.map(TresorHash.hex) ?? secretSHA256
        // Ein neuer Vorgang löscht den alten Ausgang. Sonst stünde über einer
        // frischen Handlung noch das Ergebnis der vorigen.
        ergebnis = nil
        do {
            let result = try await client.tresorMutate(
                capability: capability, arguments: arguments, secret: secret,
                secretSHA256: digest, stagingID: stagingID)
            if result.needsApproval {
                let warten = TresorPending(capability: capability,
                                           arguments: arguments,
                                           secretSHA256: digest,
                                           stagingID: result.staging_id ?? "")
                pending = warten
                notice = "Bestätige das mit Face ID — ich mache dann von selbst weiter."
                Haptics.state()
                await awaitApprovalAndFinish(warten)
                return notice
            }
            pending = nil
            if result.succeeded {
                Haptics.success()
                ergebnis = .erledigt(satzOderStandard(result, "Erledigt."))
            } else {
                Haptics.warning()
                ergebnis = deutung(result)
            }
            await refresh()
            notice = ergebnis?.satz ?? ""
            return notice
        } catch {
            Haptics.warning()
            ergebnis = .fehler("Der Ausgang ist nicht bestätigt. Bitte prüfe den aktuellen Stand; der Auftrag wird nicht erneut gesendet.")
            notice = ergebnis?.satz ?? ""
            return notice
        }
    }

    /// Wartet auf die Face-ID-Entscheidung und setzt die Handlung SELBST fort.
    ///
    /// Vorher gab es hier einen Knopf „Ich habe freigegeben". Der war
    /// ueberfluessig und verwirrend: die App kann selbst sehen, ob die
    /// Anfrage noch offen steht — `GET /v1/approvals` fuehrt genau die
    /// wartenden. Verschwindet sie, ist entschieden, und der zweite Versuch
    /// laeuft ohne Zutun.
    ///
    /// **An der Sicherheit aendert das nichts.** Die Freigabe selbst faellt
    /// unveraendert im Freigabeweg mit Face ID; dieser Code wartet nur darauf
    /// und schickt danach dieselbe Handlung mit derselben Einlagerungskennung
    /// noch einmal. Er kann nichts freigeben, nichts herabstufen und nichts
    /// ueberspringen — der Mac wuerde eine unbestaetigte Wiederholung erneut
    /// mit `approval_required` beantworten.
    private func awaitApprovalAndFinish(_ warten: TresorPending) async {
        guard !invalidated, let client else { return }
        // Rund zwei Minuten Geduld, im Sekundentakt. Laenger braucht eine
        // Face-ID-Runde nicht, und kuerzer waere unhoeflich, wenn jemand die
        // Anfrage erst lesen will.
        // Die Frist wird NICHT geraten. Sie steht an der Anfrage selbst und wird
        // beim ersten Blick übernommen; bis dahin gilt eine grosszügige
        // Obergrenze, damit ein stiller Fehler nicht ewig wartet. Vorher standen
        // hier 120 feste Sekunden — kürzer als die Freigabe lebt. Wer sein
        // Telefon kurz weglegte, gab frei und bekam trotzdem nichts.
        var frist = Date().timeIntervalSince1970 + 600
        while !invalidated && !Task.isCancelled && Date().timeIntervalSince1970 < frist {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard !invalidated, !Task.isCancelled else { return }
            guard let offen = try? await client.listPending() else { continue }
            guard !invalidated, !Task.isCancelled else { return }
            if let meine = offen.first(where: { $0.tool == warten.capability }) {
                if meine.expires_at > 0 { frist = meine.expires_at }
                continue
            }
            // Entschieden. Ob ZUGESTIMMT oder ABGELEHNT, sagt der Mac — nicht
            // diese Schleife. Der zweite Versuch bringt es an den Tag.
            let result = try? await client.tresorMutate(
                capability: warten.capability, arguments: warten.arguments,
                secret: nil, secretSHA256: warten.secretSHA256,
                stagingID: warten.stagingID)
            guard !invalidated, !Task.isCancelled else { return }
            if let result, result.succeeded {
                pending = nil
                ergebnis = .erledigt(satzOderStandard(result, "Erledigt."))
                notice = ergebnis?.satz ?? ""
                Haptics.success()
                await refresh()
            } else if let result, result.needsApproval {
                // Noch nicht entschieden — weiter warten.
                continue
            } else {
                pending = nil
                // Face ID allein ist kein Erfolg. Erst dieser zweite Ausgang
                // sagt, ob die Änderung wirklich griff.
                ergebnis = result.map(deutung)
                    ?? .fehler("Der Ausgang ist nicht bestätigt. Bitte prüfe den aktuellen Stand; der Auftrag wird nicht erneut gesendet.")
                notice = ergebnis?.satz ?? ""
                Haptics.warning()
                await refresh()
            }
            return
        }
        guard !invalidated, !Task.isCancelled else { return }
        pending = nil
        ergebnis = .abgelaufen("Dafür kam keine Freigabe, solange die Anfrage galt.")
        notice = ergebnis?.satz ?? ""
        Haptics.warning()
    }

    /// Enrollment changed: never resume old work or publish its late response.
    func invalidate() {
        invalidated = true
        initialTransfer?.cancel(); initialTransfer = nil
        wartetTask?.cancel(); wartetTask = nil
        pending = nil; ergebnis = nil; notice = ""
        entries = []; ledger = []; loaded = false; reachable = false
    }

    func clearPending() { pending = nil }

    /// Der Eigentümer hat das Ergebnis gelesen. Es verschwindet erst dann —
    /// nicht schon, weil ein Blatt sich geschlossen hat.
    func clearErgebnis() { ergebnis = nil }
}

/// Eine Aenderung, die auf Face ID wartet.
///
/// Sie traegt KEINEN Wert — nur seinen SHA-256 und die Einlagerungskennung.
/// Der Wert selbst liegt versiegelt beim Core und wird von dieser App nach dem
/// ersten Versuch nicht mehr gehalten.
struct TresorPending: Equatable {
    let capability: String
    let arguments: [String: String]
    let secretSHA256: String
    let stagingID: String
}

/// Die Angaben eines neuen Zugangs. Alles daran ist Metadatum.
struct TresorDraft: Equatable {
    var service = ""
    var account = ""
    var displayName = ""
    var kind = "password"
    var target = ""
    var capability = "portal_login"
    var executor = "browser"
    var allowBackground = false

    /// `secret://<dienst>/<konto>` — kleingeschrieben, ohne Sonderzeichen.
    var reference: String {
        "secret://\(TresorDraft.slug(service))/\(TresorDraft.slug(account))"
    }

    static func slug(_ text: String) -> String {
        let lowered = text.lowercased()
        let kept = lowered.map { character -> Character in
            if character.isLetter || character.isNumber { return character }
            if character == "." || character == "-" || character == "_" { return character }
            return "-"
        }
        return String(kept).trimmingCharacters(in: CharacterSet(charactersIn: "-._"))
    }

    var targetOrigin: String {
        let trimmed = target.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return "" }
        if trimmed.contains("://") { return trimmed.lowercased() }
        return "https://" + trimmed.lowercased()
    }

    var isComplete: Bool {
        !TresorDraft.slug(service).isEmpty && !TresorDraft.slug(account).isEmpty
            && !targetOrigin.isEmpty
    }

    func arguments() -> [String: String] {
        [
            "secret_ref": reference,
            "kind": kind,
            "display_name": displayName.isEmpty ? service : displayName,
            "service_label": service,
            "account_label": account,
            "capabilities": capability,
            "targets": targetOrigin,
            "executors": executor,
            "allow_background": allowBackground ? "true" : "false",
        ]
    }
}
