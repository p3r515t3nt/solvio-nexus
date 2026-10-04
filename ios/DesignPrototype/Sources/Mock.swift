// Mock-Zustaende — der Prototyp redet mit NIEMANDEM.
//
// Kein Netz, keine Keychain, kein App Attest, keine Face ID, nichts wird
// gespeichert. Die Formen entsprechen dem, was der Core heute liefert
// (Overview, TaskRow, InboxItem, ComponentHealth aus SolvioApprovalsKit),
// damit die Implementierung spaeter nicht raten muss.
import Foundation
import SwiftUI

// MARK: - Szenario (fuer Screenshots per Startargument schaltbar)

enum Scenario: String {
    case normal          // alles gut, wenig zu tun
    case attention       // Freigaben + wichtiger Hinweis + Stoerung
    case offline         // Mac nicht erreichbar
}

// MARK: - Datenformen (Spiegel der echten Kit-Formen, nur Mock)

struct MockApproval: Identifiable {
    let id: String
    let summary: String        // human_summary — KI-Text, NICHT massgeblich
    let task: String           // Auftrag — massgeblich, woertlich
    let tool: String
    let toolLabel: String      // Produktsprache
    let mode: String
    let workspace: String
    let icon: String
    let consequence: String    // aus mode abgeleitet, Produktsprache
    let waitingSince: String
}

struct MockTask: Identifiable {
    let id: String
    let title: String
    let what: String
    let schedule: String
    let active: Bool
    let nextRun: String?
    let lastRun: String
    let failures: Int
    let order: String
    let waitsForApproval: Bool
}

enum InboxPriority { case important, normal, info }

struct MockNotice: Identifiable {
    let id: String
    let summary: String
    let source: String
    let time: String
    let read: Bool
    let priority: InboxPriority
    let findings: [String]
}

struct MockComponent: Identifiable {
    var id: String { name }
    let name: String           // Produktsprache: „Kalender & Mail", nicht calendar_list_events
    let state: String          // healthy / degraded / unavailable / auth_required / unknown
    let detail: String
}

// MARK: - Der Zustand

@MainActor
final class DesignModel: ObservableObject {
    @Published var scenario: Scenario
    @Published var voiceState: VoiceState = .listening

    init() {
        let args = ProcessInfo.processInfo.arguments
        if let index = args.firstIndex(of: "-scenario"), args.count > index + 1,
           let chosen = Scenario(rawValue: args[index + 1]) {
            scenario = chosen
        } else {
            scenario = .attention
        }
        if let index = args.firstIndex(of: "-voice"), args.count > index + 1,
           let chosen = VoiceState(rawValue: args[index + 1]) {
            voiceState = chosen
        }
    }

    var reachable: Bool { scenario != .offline }

    var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<11: return "Guten Morgen."
        case 11..<18: return "Guten Tag."
        default: return "Guten Abend."
        }
    }

    /// Der eine Satz oben. Produktsprache, keine Komponentenliste.
    var statusLine: (text: String, color: Color) {
        switch scenario {
        case .normal: return ("SOLVIO läuft.", Theme.good)
        case .attention: return ("SOLVIO läuft — eine Sache braucht dich.", Theme.warn)
        case .offline: return ("SOLVIO ist gerade nicht erreichbar.", Theme.warn)
        }
    }

    var approvals: [MockApproval] {
        guard scenario != .normal else { return [] }
        return [
            MockApproval(
                id: "a1",
                summary: "Antwort an Frau Berger senden: Terminvorschlag Donnerstag 14 Uhr bestätigt.",
                task: "Antworte auf die E-Mail von anna.berger@example.de vom 24.08. "
                    + "(Betreff: Projektbesprechung) und bestätige Donnerstag, 28.08., "
                    + "14:00 Uhr als Termin. Freundlich, kurz, auf Deutsch.",
                tool: "gmail_send_draft", toolLabel: "E-Mail senden",
                mode: "NON_IDEMPOTENT_WRITE", workspace: "Gmail",
                icon: "envelope.fill",
                consequence: "Die E-Mail geht wirklich raus und lässt sich nicht zurückholen.",
                waitingSince: "seit 4 Minuten"),
            MockApproval(
                id: "a2",
                summary: "Heizung im Bad morgens auf 22 °C stellen.",
                task: "Setze das Thermostat im Bad werktags um 06:30 auf 22 °C.",
                tool: "ha_set_temperature", toolLabel: "Zuhause steuern",
                mode: "RECONCILABLE_WRITE", workspace: "Zuhause",
                icon: "house.fill",
                consequence: "Ändert eine Einstellung im Haus. Lässt sich jederzeit zurückstellen.",
                waitingSince: "seit 18 Minuten"),
        ]
    }

    var tasks: [MockTask] {
        [
            MockTask(id: "t1", title: "Morgenlage",
                     what: "Kalender und Postfach prüfen", schedule: "Täglich um 07:30",
                     active: true, nextRun: "Morgen, 07:30", lastRun: "Heute, 07:30",
                     failures: 0,
                     order: "Sieh morgens in Kalender und Postfach und sag mir, was heute wichtig ist.",
                     waitsForApproval: false),
            MockTask(id: "t2", title: "Wochenrückblick",
                     what: "Woche zusammenfassen", schedule: "Sonntags um 18:00",
                     active: true, nextRun: "So, 18:00", lastRun: "vor 6 Tagen",
                     failures: 0,
                     order: "Fass mir sonntags zusammen, was diese Woche passiert ist und was ansteht.",
                     waitsForApproval: false),
            MockTask(id: "t3", title: "Rechnungs-Erinnerung",
                     what: "Posteingang nach Rechnungen durchsehen", schedule: "Freitags um 09:00",
                     active: false, nextRun: nil, lastRun: "vor 2 Wochen",
                     failures: scenario == .attention ? 2 : 0,
                     order: "Erinnere mich freitags an offene Rechnungen im Postfach.",
                     waitsForApproval: false),
        ]
    }

    var notices: [MockNotice] {
        let base = [
            MockNotice(id: "n1",
                       summary: "Dein Termin morgen um 9 Uhr wurde auf 10 Uhr verschoben — der Kalender ist aktualisiert.",
                       source: "Kalender", time: "vor 25 Minuten", read: false,
                       priority: scenario == .attention ? .important : .normal,
                       findings: ["Einladung von M. Weber, 24.08. 17:41",
                                  "Alter Termin: Di 09:00 · Neuer Termin: Di 10:00"]),
            MockNotice(id: "n2",
                       summary: "Zwei neue E-Mails sehen nach Antwort aus: Frau Berger (Termin) und die KFZ-Versicherung.",
                       source: "Morgenlage", time: "Heute, 07:31", read: false,
                       priority: .normal,
                       findings: []),
            MockNotice(id: "n3",
                       summary: "Die Recherche zu Balkonkraftwerken ist fertig — drei Modelle passen zu deinem Balkon.",
                       source: "Recherche", time: "Gestern, 21:12", read: true,
                       priority: .info,
                       findings: []),
        ]
        return base
    }

    var unreadCount: Int { notices.filter { !$0.read }.count }

    var components: [MockComponent] {
        let degraded = scenario == .attention
        return [
            MockComponent(name: "Sprechen & Zuhören", state: "healthy",
                          detail: "Das Wohnzimmer hört auf „Hey Solvio“."),
            MockComponent(name: "Freigaben", state: "healthy",
                          detail: "Dein iPhone ist gekoppelt."),
            MockComponent(name: "Kalender & Mail", state: degraded ? "auth_required" : "healthy",
                          detail: degraded ? "Die Google-Anmeldung ist abgelaufen."
                                           : "Verbunden."),
            MockComponent(name: "Zuhause", state: "healthy",
                          detail: "Home Assistant antwortet."),
            MockComponent(name: "Recherche", state: "healthy",
                          detail: "Nachschlagen im Netz funktioniert."),
            MockComponent(name: "Erinnern", state: "healthy",
                          detail: "Das Gedächtnis ist da."),
        ]
    }

    var systemHeadline: (text: String, sub: String, tone: Color) {
        if scenario == .offline {
            return ("SOLVIO ist gerade nicht erreichbar.",
                    "Angezeigt wird der Stand von vor 12 Minuten.", Theme.warn)
        }
        if components.contains(where: { $0.state != "healthy" }) {
            return ("Eine Sache braucht dich.",
                    "Die Google-Anmeldung ist abgelaufen.", Theme.warn)
        }
        return ("SOLVIO läuft.", "Alles in Ordnung — zuletzt geprüft gerade eben.", Theme.good)
    }
}

// MARK: - Sprachzustaende

enum VoiceState: String, CaseIterable {
    case ready, listening, thinking, speaking, interrupted, deepwork,
         reconnecting, offline, ended
}
