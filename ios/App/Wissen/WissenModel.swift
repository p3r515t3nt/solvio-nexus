// Wissen — was SOLVIO ueber deine Welt weiss.
//
// Das Modell haelt, was der Core beim letzten Abruf geliefert hat, und NICHTS
// Eigenes: Lebenszyklus, Herkunftssatz und Beweislage rechnet der Core. Wenn
// der Mac nicht antwortet, bleibt der letzte Stand stehen — als ALT
// gekennzeichnet, nie als frisch ausgegeben (dieselbe Ehrlichkeit wie im
// Kontrollzentrum).
import Foundation
import SwiftUI

// MARK: - Zeit und Sprache

/// Die Zeitstempel des Gedaechtnisses sind ISO-8601-Text (Python `isoformat()`),
/// mal mit, mal ohne Bruchteile oder Zeitzone — Altbestand kennt beides.
/// Deshalb mehrere Versuche statt eines strengen Formats.
func wissenEpoch(_ iso: String?) -> Double? {
    guard let iso, !iso.isEmpty else { return nil }
    let withFraction = ISO8601DateFormatter()
    withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let plain = ISO8601DateFormatter()
    plain.formatOptions = [.withInternetDateTime]
    if let d = withFraction.date(from: iso) ?? plain.date(from: iso) {
        return d.timeIntervalSince1970
    }
    // Naiver Stempel ohne Zeitzone: als UTC lesen — so schreibt ihn der Core.
    for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss"] {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = format
        if let d = f.date(from: iso) { return d.timeIntervalSince1970 }
    }
    return nil
}

/// Herkunft in Produktsprache. Die Stufe kommt WOERTLICH vom Core
/// (`lifecycle`); hier wird nur uebersetzt, nie abgeleitet. Eine unbekannte
/// Stufe bekommt bewusst die vorsichtige Formulierung.
func herkunftswort(_ lifecycle: String) -> String {
    switch lifecycle {
    case "explicit": return "Von dir gemerkt"
    case "confirmed": return "Von dir bestätigt"
    case "learned": return "Von SOLVIO erkannt"
    default: return "Aus einem früheren Gespräch"
    }
}

// MARK: - Filter

enum WissenFilter: String, CaseIterable, Identifiable {
    case alle = "Alle"
    case gemerkt = "Von dir gemerkt"
    case erkannt = "Von SOLVIO erkannt"

    var id: String { rawValue }

    func matches(_ item: MemoryItem) -> Bool {
        switch self {
        case .alle: return true
        case .gemerkt: return item.lifecycle == "explicit" || item.lifecycle == "confirmed"
        case .erkannt: return item.lifecycle == "learned"
        }
    }
}

// MARK: - Modell

@MainActor
final class WissenModel: ObservableObject {
    @Published var memories: [MemoryItem] = []
    @Published var candidates: [MemoryCandidate] = []
    /// Ob der Mac beim letzten Versuch geantwortet hat.
    @Published var reachable = true
    /// Wann der angezeigte Stand geholt wurde — damit Altes nie wie Frisches aussieht.
    @Published var snapshotAt: Double?
    @Published var loaded = false
    @Published var working = false
    /// Der Satz des Cores zu einer nicht ausgefuehrten Aenderung — woertlich.
    @Published var notice = ""

    private(set) var client: ApprovalClient?
    private var refreshing = false

    init(client: ApprovalClient? = nil) {
        self.client = client ?? WissenModel.restoredClient()
    }

    /// Die Wissen-Zeile auf Start baut die Ansicht ohne das App-Modell. Die
    /// Registrierung liegt genau EINMAL im Keychain — hier wird derselbe
    /// Eintrag gelesen, den auch der Freigabeweg liest: derselbe gepinnte
    /// Anschluss, dieselbe Kennung. Es entsteht kein zweites Geheimnis und
    /// kein zweiter Vertrauensanker, nur ein zweiter Handgriff daran.
    private static func restoredClient() -> ApprovalClient? {
        guard let data = Keychain.load(tag: "de.solvio.approvals.enrollment"),
              let e = try? JSONDecoder().decode(Enrollment.self, from: data) else { return nil }
        return ApprovalClient(pairing: e.pairing, deviceID: e.deviceID,
                              transportCred: e.transportCred)
    }

    func refresh() async {
        guard let client, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            async let mems = client.memories()
            async let cands = client.memoryCandidates()
            memories = try await mems
            // Vorschlaege sind nachrangig: laeuft die Adaptive-Schicht nicht,
            // soll das Wissen selbst trotzdem erscheinen.
            candidates = (try? await cands) ?? candidates
            snapshotAt = Date().timeIntervalSince1970
            reachable = true
            loaded = true
        } catch {
            // Der alte Stand bleibt stehen — als alt gekennzeichnet.
            reachable = false
        }
    }

    // MARK: Ableitungen fuer den Bildschirm

    /// Von SOLVIO erkannt und juenger als sieben Tage — die Zeile „Neu gelernt".
    func freshLearned(filter: WissenFilter, search: String) -> [MemoryItem] {
        let cutoff = Date().timeIntervalSince1970 - 7 * 24 * 3600
        return visible(filter: filter, search: search).filter {
            $0.lifecycle == "learned" && (wissenEpoch($0.updated_at) ?? 0) >= cutoff
        }
    }

    func remaining(filter: WissenFilter, search: String) -> [MemoryItem] {
        let fresh = Set(freshLearned(filter: filter, search: search).map(\.id))
        return visible(filter: filter, search: search).filter { !fresh.contains($0.id) }
    }

    func visibleCandidates(search: String) -> [MemoryCandidate] {
        guard !search.isEmpty else { return candidates }
        return candidates.filter { $0.statement.localizedCaseInsensitiveContains(search) }
    }

    private func visible(filter: WissenFilter, search: String) -> [MemoryItem] {
        var items = memories.filter { filter.matches($0) }
        if !search.isEmpty {
            items = items.filter {
                $0.content.localizedCaseInsensitiveContains(search)
                    || ($0.subject ?? "").localizedCaseInsensitiveContains(search)
            }
        }
        return items
    }

    // MARK: Aenderungen
    //
    // Jede laeuft ueber den Mutationsweg mit frischer Nonce und App-Attest-
    // Assertion. Ob sie ERLAUBT ist, entscheidet allein der Core — sagt er
    // wider Erwarten „braucht Freigabe", steht sein Satz ehrlich im Hinweis.

    func forget(memoryID: String) async -> Bool {
        await mutate("memory_forget", ["memory_id": memoryID])
    }

    func correct(memoryID: String, statement: String) async -> Bool {
        await mutate("memory_correct", ["memory_id": memoryID, "statement": statement])
    }

    func confirmCandidate(_ candidateID: String) async -> Bool {
        await mutate("memory_confirm_candidate", ["candidate_id": candidateID])
    }

    func declineCandidate(_ candidateID: String) async -> Bool {
        await mutate("memory_decline_candidate", ["candidate_id": candidateID])
    }

    private func mutate(_ capability: String, _ arguments: [String: String]) async -> Bool {
        guard let client, !working else { return false }
        working = true
        defer { working = false }
        do {
            let result = try await client.memoryMutate(capability: capability,
                                                       arguments: arguments)
            if result.succeeded {
                notice = ""
                Haptics.success()
                await refresh()
                return true
            }
            notice = result.human_message ?? "Das hat gerade nicht geklappt."
            Haptics.warning()
            return false
        } catch {
            notice = error.localizedDescription
            Haptics.warning()
            return false
        }
    }
}
