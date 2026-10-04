// Das Kontrollzentrum — dieselbe Verbindung, dieselbe Geraetekennung, dieselbe
// Sicherheit wie der Freigabeweg.
//
// Es entsteht hier ausdruecklich KEIN zweiter Vertrauensanker: derselbe gepinnte
// TLS-Anschluss, dieselben Kopfzeilen `X-Device-Id` und `X-Transport-Cred`, die
// der Mac schon fuer die Freigabeliste prueft. Was dieses Modul NICHT kann und
// nicht koennen soll: eine Freigabe erteilen. Dafuer braucht es Face ID und eine
// frische App-Attest-Aussage, und beides liegt weiterhin allein im
// Freigabepfad.
//
// Die Handlungen hier — pausieren, fortsetzen, jetzt laufen, loeschen, gelesen —
// beruehren nur SOLVIOs eigene Buchhaltung. Nichts davon wirkt nach aussen.
import Foundation

// MARK: - Wire-Typen
//
// Bewusst schmal: nur was ein Bildschirm braucht. Der Mac schickt keine
// Protokolle, keine Rohtexte und keine Zugangsdaten, und diese Typen haetten
// auch gar kein Feld dafuer.

struct HealthSummary: Codable, Hashable {
    let zustand: String
    let satz: String
    let auffaellig: [String]
    let braucht_dich: [String]
}

struct Overview: Codable, Hashable {
    let stand: Double
    let gesundheit: HealthSummary
    let aufmerksamkeit: [String]
    let ungelesen: Int
    let aufgaben_aktiv: Int
    let aufgaben_gesamt: Int
    let naechster_lauf: Double?
    let freigaben_offen: Int
    let freigabe_id: String?
}

struct TaskRow: Codable, Identifiable, Hashable {
    let id: String
    let titel: String
    let was: String
    let wann: String
    let zustand: String
    let aktiv: Bool
    let naechster_lauf: Double?
    let letzter_lauf: Double?
    let fehlschlaege: Int
    let letzter_fehler: String
    let wartet_auf_freigabe: Bool
    let auftrag: String
}

struct InboxItem: Codable, Identifiable, Hashable {
    let id: String
    let zusammenfassung: String
    let dringlichkeit: String
    let zeit: Double?
    let aufgabe: String
    let gelesen: Bool
    let quelle: String
    var befunde: [String]? = nil
    var herkunft: String? = nil
    var lauf: String? = nil

    var agentRunID: String? {
        guard let lauf, lauf.range(of: "^ar-[0-9a-f]{16}$", options: .regularExpression) != nil else { return nil }
        return lauf
    }
}

struct ActivityEvent: Codable, Identifiable, Hashable {
    let zeit: Double
    let art: String
    let titel: String
    let ton: String
    var detail: String? = nil
    var aufgabe: String? = nil
    var meldung: String? = nil
    var freigabe: String? = nil

    // Die Chronik ist eine Projektion und hat keine eigene Kennung. Zeit plus
    // Titel identifiziert eine Zeile eindeutig genug fuer eine Liste — und
    // erfindet keine ID, die es serverseitig nicht gibt.
    var id: String { "\(Int(zeit))-\(titel)" }
}

struct ComponentHealth: Codable, Identifiable, Hashable {
    let komponente: String
    let name: String
    let zustand: String
    let grund: String
    let geprueft_um: Double?
    let zuletzt_gesund: Double?
    let braucht_dich: Bool
    /// Ob fuer diesen Zustand ein hinterlegtes Vorgehen existiert.
    ///
    /// Optional, damit ein aelterer Core die Ansicht nicht zerlegt — fehlt das
    /// Feld, gibt es eben keinen Knopf. Das ist die richtige Richtung fuer den
    /// Zweifelsfall: ein Knopf, hinter dem nichts liegt, ist schlimmer als
    /// keiner.
    let reparierbar: Bool?

    var id: String { komponente }
    var canRepair: Bool { reparierbar ?? false }
}

/// Ein Befund des Arztes. Die Felder heissen wie im Core — eine Uebersetzung
/// unterwegs waere eine Stelle, an der zwei Wahrheiten entstehen koennen.
struct Diagnosis: Codable, Hashable {
    let komponente: String
    let zustand: String
    let symptome: [String]
    let ursache: String
    let belege: [String]
    let zuversicht: String
    let dauerhaft: Bool
    let auswirkung: String
    let reparatur: String
    let reparierbar: Bool
    let braucht_dich: Bool
    let pruefung: String
    let zeit: Double
}

/// Was ein Reparaturversuch ergeben hat.
struct RepairResult: Codable {
    let ok: Bool
    var grund: String? = nil
    var befund: Diagnosis? = nil
    var versuch: RepairAttempt? = nil
}

struct RepairAttempt: Codable, Hashable {
    let vorgehen: String
    let komponente: String
    let ausgefuehrt: Bool
    let wiederhergestellt: Bool
    let ergebnis: String
    let detail: String
    let dauer_s: Double
}

struct SystemHealth: Codable, Hashable {
    let komponenten: [ComponentHealth]
    let zusammenfassung: HealthSummary
    let stand: Double
}

struct ActionResult: Codable {
    let ok: Bool?
    var id: String? = nil
    var zustand: String? = nil
    var aktiv: Bool? = nil
    var ungelesen: Int? = nil
    var geaendert: Bool? = nil
    var titel: String? = nil
}

// MARK: - Client

extension ApprovalClient {
    private func decode<T: Decodable>(_ type: T.Type, _ path: String) async throws -> T {
        let (data, resp) = try await controlSession.data(for: controlRequest(path))
        guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else {
            throw ClientError.http((resp as? HTTPURLResponse)?.statusCode ?? -1)
        }
        guard let out = try? JSONDecoder().decode(T.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    func overview() async throws -> Overview {
        try await decode(Overview.self, "v1/control/overview")
    }

    func tasks() async throws -> [TaskRow] {
        struct Envelope: Codable { let aufgaben: [TaskRow] }
        return try await decode(Envelope.self, "v1/control/tasks").aufgaben
    }

    func inbox() async throws -> [InboxItem] {
        struct Envelope: Codable { let meldungen: [InboxItem] }
        return try await decode(Envelope.self, "v1/control/inbox").meldungen
    }

    func inboxItem(id: String) async throws -> InboxItem {
        try await decode(InboxItem.self, "v1/control/inbox/\(id)")
    }

    func activity() async throws -> [ActivityEvent] {
        struct Envelope: Codable { let ereignisse: [ActivityEvent] }
        return try await decode(Envelope.self, "v1/control/activity").ereignisse
    }

    func systemHealth() async throws -> SystemHealth {
        try await decode(SystemHealth.self, "v1/control/system")
    }

    /// Eine Handlung auf EINER Kennung.
    ///
    /// Die Kennung steht im Pfad — sie wird beim Tippen kopiert und danach nicht
    /// mehr nachgeschlagen. Aktualisiert sich die Liste zwischen Tippen und
    /// Senden, trifft die Handlung trotzdem genau das, was auf dem Schirm stand.
    /// Ein Index in eine Liste haette hier genau das Gegenteil bewirkt.
    @discardableResult
    func act(_ path: String) async throws -> ActionResult {
        var request = controlRequest(path)
        request.httpMethod = "POST"
        let (data, resp) = try await controlSession.data(for: request)
        guard let h = resp as? HTTPURLResponse else { throw ClientError.decode }
        guard h.statusCode == 200 else { throw ClientError.http(h.statusCode) }
        return (try? JSONDecoder().decode(ActionResult.self, from: data))
            ?? ActionResult(ok: true)
    }

    func diagnose(component: String) async throws -> Diagnosis {
        try await decode(Diagnosis.self, "v1/control/system/\(component)/diagnose")
    }

    /// Der ausdrueckliche Tipp des Besitzers auf EINE Komponente.
    ///
    /// Der Core stellt dabei einen frischen Befund — der angezeigte kann Minuten
    /// alt sein, und ein Vorgehen soll zu dem passen, was jetzt ist. Deshalb
    /// wird hier bewusst kein Befund mitgeschickt.
    func repair(component: String) async throws -> RepairResult {
        var request = controlRequest("v1/control/system/\(component)/repair")
        request.httpMethod = "POST"
        let (data, resp) = try await controlSession.data(for: request)
        guard let h = resp as? HTTPURLResponse else { throw ClientError.decode }
        guard h.statusCode == 200 else { throw ClientError.http(h.statusCode) }
        guard let out = try? JSONDecoder().decode(RepairResult.self, from: data) else {
            throw ClientError.decode
        }
        return out
    }

    func pause(taskID: String) async throws { try await act("v1/control/tasks/\(taskID)/pause") }
    func resume(taskID: String) async throws { try await act("v1/control/tasks/\(taskID)/resume") }
    func runNow(taskID: String) async throws { try await act("v1/control/tasks/\(taskID)/run_now") }
    func deleteTask(taskID: String) async throws { try await act("v1/control/tasks/\(taskID)/delete") }
    func markRead(itemID: String) async throws { try await act("v1/control/inbox/\(itemID)/read") }
}
