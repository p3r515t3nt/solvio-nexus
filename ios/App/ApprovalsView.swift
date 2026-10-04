// Freigaben — die Stelle, an der Autoritaet ausgeuebt wird.
//
// Hier aendert sich die PRAESENTATION und sonst nichts. Die Sicherheitsstruktur
// ist die freigegebene und steht Zeile fuer Zeile so, wie sie stand:
//
// * Die ungesignte Liste ist NUR Entdeckung. Massgeblich ist allein die
//   signierte Anfrage vom Mac.
// * Widerspricht die Liste der signierten Anfrage, wird gesperrt — nicht
//   stillschweigend eine der beiden bevorzugt.
// * Der Auftrag steht woertlich und monospaced. Nie Markdown, nie HTML, nie
//   WebView. Nichts wird stillschweigend gekuerzt.
// * Die KI-Zusammenfassung ist sichtbar als unverbindlich gekennzeichnet.
// * Ohne verifizierte Anfrage gibt es keinen Entscheidungsknopf.
// * Freigeben heisst Face ID + frische App-Attest-Aussage. Ein Simulator ist
//   nie ein Beweis.
//
// Was gesprochen wurde, aendert daran nichts: eine Sprachanfrage landet als
// GENAU DIESE Freigabe hier und braucht denselben Beweis wie jede andere.
import SwiftUI
import SolvioApprovalsKit

struct ApprovalsView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                GoogleConnectionNotice(model: model.googleConnection)
                if !model.attested { simulatorNote }
                if model.pending.isEmpty {
                    EmptyState(icon: "checkmark.shield",
                               title: "Nichts wartet auf dich.",
                               text: "Wenn SOLVIO etwas tun will, das deine Freigabe braucht, erscheint es hier.")
                } else {
                    ForEach(model.pending) { approval in
                        NavigationLink(value: approval) { ApprovalCard(approval: approval) }
                            .buttonStyle(PressScale())
                    }
                }
                if !model.status.isEmpty {
                    Text(model.status).font(.footnote).foregroundStyle(Theme.ink2)
                        .multilineTextAlignment(.center).padding(.top, 4)
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Freigaben")
        .navigationDestination(for: PendingApproval.self) {
            ApprovalDetailView(model: model, approval: $0)
        }
        .refreshable { await model.refresh() }
        .task {
            await model.refresh()
            model.startAutoRefresh()
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Entkoppeln") { Task { await model.unpair() } }
                    .font(.footnote).foregroundStyle(Theme.ink2)
            }
        }
    }

    private var simulatorNote: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.warn)
            Text("Kein Secure Enclave / App Attest — auf diesem Gerät ist keine echte Freigabe möglich.")
                .font(.footnote).foregroundStyle(Theme.inkOnGold)
            Spacer(minLength: 0)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }
}

/// Die Kategorie eines Werkzeugs — nur fuer Symbol und Produktnamen.
///
/// Rein kosmetisch: sie leitet KEINE Semantik ab und beeinflusst nichts an der
/// Entscheidung. Was wirklich passiert, steht im Auftrag, und der kommt
/// signiert vom Mac.
struct ToolLook {
    let symbol: String
    let label: String

    static func of(_ tool: String) -> ToolLook {
        switch true {
        case tool == "google_connect": return .init(symbol: "link", label: "Google verbinden")
        case tool.hasPrefix("gmail"):    return .init(symbol: "envelope.fill", label: "E-Mail")
        case tool.hasPrefix("calendar"): return .init(symbol: "calendar", label: "Kalender")
        case tool.hasPrefix("ha_"):      return .init(symbol: "house.fill", label: "Zuhause")
        case tool.hasPrefix("browser"):  return .init(symbol: "globe", label: "Browser")
        case tool.hasPrefix("portal"):   return .init(symbol: "lock.shield", label: "Portal")
        case tool.hasPrefix("memory"):   return .init(symbol: "brain", label: "Erinnern")
        // Geld sieht anders aus als „Aktion". Ohne diese Zeile stuende eine
        // Kaufbestaetigung mit demselben grauen Funken da wie eine Erinnerung.
        case tool.hasPrefix("purchase"): return .init(symbol: "creditcard.fill", label: "Kauf")
        case tool.hasPrefix("payment"):  return .init(symbol: "creditcard.fill", label: "Zahlung")
        case tool.hasPrefix("refund"):   return .init(symbol: "arrow.uturn.backward.circle.fill", label: "Erstattung")
        case tool.hasPrefix("secret"):   return .init(symbol: "lock.fill", label: "Tresor")
        case tool.hasPrefix("deep"):     return .init(symbol: "magnifyingglass", label: "Recherche")
        case tool.hasPrefix("system"):   return .init(symbol: "stethoscope", label: "System")
        default:                         return .init(symbol: "sparkles", label: "Aktion")
        }
    }
}

struct ApprovalCard: View {
    let approval: PendingApproval

    var body: some View {
        let look = ToolLook.of(approval.tool)
        return HStack(alignment: .top, spacing: 14) {
            Image(systemName: look.symbol)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(Theme.blue)
                .frame(width: 40, height: 40)
                .background(Theme.tintBlue,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.chip,
                                                 style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                Text(approval.human_summary)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(2)
                Text(look.label)
                    .font(.caption).foregroundStyle(Theme.ink2)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.ink3).padding(.top, 12)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }
}

/// Was nach der Freigabe passiert — aus der Ausfuehrungssemantik des Cores.
///
/// Der Modus kommt aus der signierten Anfrage; die App erfindet hier keine
/// Semantik, sie uebersetzt nur den vom Core gelieferten Wert in einen Satz.
/// Ein unbekannter Modus bekommt bewusst die VORSICHTIGE Formulierung.
func consequenceSentence(mode: String) -> String {
    switch mode.uppercased() {
    case "READ_ONLY":
        return "Das liest nur — es verändert nichts."
    case "IDEMPOTENT_WRITE":
        return "Das lässt sich gefahrlos wiederholen."
    case "RECONCILABLE_WRITE":
        return "Das verändert etwas und lässt sich zurückstellen."
    case "NON_IDEMPOTENT_WRITE":
        return "Das wirkt wirklich nach außen und lässt sich nicht zurückholen."
    default:
        return "Das wirkt nach außen. Prüfe den Auftrag genau."
    }
}


/// Activation feedback stays visible where the owner just gave Face ID.
private struct GoogleConnectionNotice: View {
    @ObservedObject var model: TresorModel
    var body: some View {
        if !model.notice.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Label("Google-Verbindung", systemImage: model.ergebnis?.symbol ?? "link")
                    .font(.headline)
                Text(model.notice).font(.subheadline)
                if model.ergebnis?.gelungen == true {
                    Text("Für SOLVIO verbunden. Eine echte Mail- oder Kalenderabfrage wurde dabei nicht ausgeführt.")
                        .font(.footnote).foregroundStyle(Theme.ink2)
                }
            }.padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
                .accessibilityIdentifier("google.approval-result")
        }
    }
}
