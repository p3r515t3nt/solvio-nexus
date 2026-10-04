// Die Ansichten des Kontrollzentrums.
//
// Leitgedanke: das hier ist kein Entwicklerwerkzeug. Wer morgens draufschaut,
// soll in zwei Sekunden wissen, ob alles gut ist — und erst danach, wenn er will,
// warum nicht. Deshalb steht oben ein Satz und keine Tabelle, und deshalb heissen
// die Dinge „Kalender" und nicht `calendar_list_events`.
//
// Was hier bewusst NICHT vorkommt: Prozesskennungen, Dateipfade, Zustandsnamen
// aus dem Vertrag, Rohfehler. Das steht alles im Journal des Mac, wo es
// hingehoert.
import SwiftUI

// MARK: - Gemeinsames

/// Die Farben der Zustaende. Grau heisst ausdruecklich „ich habe nicht
/// nachgesehen" und nicht „alles gut" — ein gruener Punkt, der nur Nichtwissen
/// bedeutet, waere schlimmer als gar keiner.
func toneColor(_ state: String) -> Color {
    switch state {
    case "healthy", "gut": return .green
    case "degraded", "quota_limited", "warnung": return .orange
    case "unavailable", "schlecht": return .red
    case "auth_required": return .orange
    default: return .secondary
    }
}

func stateWord(_ state: String) -> String {
    switch state {
    case "healthy": return "in Ordnung"
    case "degraded": return "eingeschraenkt"
    case "unavailable": return "nicht erreichbar"
    case "auth_required": return "Anmeldung noetig"
    case "quota_limited": return "Kontingent erschoepft"
    default: return "unbekannt"
    }
}

func relativeTime(_ stamp: Double?) -> String {
    guard let stamp, stamp > 0 else { return "—" }
    let date = Date(timeIntervalSince1970: stamp)
    let formatter = RelativeDateTimeFormatter()
    formatter.locale = Locale(identifier: "de_DE")
    formatter.unitsStyle = .full
    return formatter.localizedString(for: date, relativeTo: Date())
}

func clockTime(_ stamp: Double?) -> String {
    guard let stamp, stamp > 0 else { return "—" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "de_DE")
    formatter.dateFormat = Calendar.current.isDateInToday(
        Date(timeIntervalSince1970: stamp)) ? "HH:mm" : "EEE HH:mm"
    return formatter.string(from: Date(timeIntervalSince1970: stamp))
}

/// Wird gezeigt, wenn der Mac nicht antwortet.
///
/// Wichtig ist der zweite Satz: ein alter Stand darf nicht wie der aktuelle
/// aussehen. Lieber steht dort „Stand von vor zehn Minuten" als eine Zahl, der
/// man ansieht, dass sie stimmt, obwohl sie es nicht tut.
struct OfflineNote: View {
    let since: Double?

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "wifi.slash").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("SOLVIO ist gerade nicht erreichbar.")
                    .font(.subheadline.weight(.medium))
                if let since {
                    Text("Angezeigt wird der Stand von \(relativeTime(since)).")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
        .padding(12)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct EmptyNote: View {
    let icon: String
    let text: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.largeTitle).foregroundStyle(.secondary)
            Text(text).font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 44)
    }
}

// MARK: - Was hier NICHT mehr steht
//
// Die Listenansichten des Kontrollzentrums (Uebersicht, Aufgaben, Posteingang,
// Verlauf, System) sind mit iPhone Experience V1 durch die entworfenen
// Ansichten ersetzt worden: HomeView, PlannedView, InboxView, SystemView,
// ActivityView. Was blieb, sind die Helfer, die dort weiterbenutzt werden, und
// der Arzt — er ist eine Detailebene und hat im Entwurf keine neue Gestalt
// bekommen.

struct DoctorView: View {
    @ObservedObject var model: AppModel
    let component: ComponentHealth

    @State private var diagnosis: Diagnosis?
    @State private var loadError = ""
    /// Sperrt den Knopf, solange die Reparatur laeuft. Derselbe Grund wie bei
    /// den Aufgaben: die Liste aktualisiert sich im Sekundentakt, und wer
    /// nichts passieren sieht, tippt noch einmal. Ein zweiter Tipp wird
    /// verworfen, nicht aufgehoben.
    @State private var working = false
    @State private var result: RepairResult?

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Circle().fill(toneColor(component.zustand))
                            .frame(width: 10, height: 10)
                        Text(component.name).font(.title3.weight(.semibold))
                    }
                    Text(headline).font(.subheadline)
                        .foregroundStyle(headlineTone)
                }.padding(.vertical, 4)
            }

            if let found = diagnosis {
                if !found.auswirkung.isEmpty {
                    Section("Was das für dich heißt") {
                        Text(found.auswirkung).font(.callout)
                    }
                }
                if !found.ursache.isEmpty {
                    Section("Vermutete Ursache") {
                        Text(found.ursache).font(.callout)
                        // Die Zuversicht steht dabei, weil eine Vermutung im
                        // Gewand einer Feststellung schlimmer ist als ein
                        // ehrliches „unklar".
                        Text("Zuversicht: \(confidenceWord(found.zuversicht))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if !found.belege.isEmpty {
                    Section("Woran ich das sehe") {
                        ForEach(found.belege, id: \.self) { line in
                            Text(line).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } else if loadError.isEmpty {
                Section { HStack(spacing: 8) { ProgressView(); Text("Sehe nach …") } }
            } else {
                Section { Text(loadError).font(.callout).foregroundStyle(.orange) }
            }

            actionSection
        }
        .navigationTitle("Befund")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    @ViewBuilder
    private var actionSection: some View {
        if let outcome = result {
            Section {
                if outcome.ok {
                    Label("Wiederhergestellt", systemImage: "checkmark.circle")
                        .foregroundStyle(.green)
                    if let attempt = outcome.versuch, !attempt.detail.isEmpty {
                        Text(attempt.detail).font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Label(failureWord(outcome), systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    if let attempt = outcome.versuch, !attempt.detail.isEmpty {
                        Text(attempt.detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        } else if working {
            Section {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("Reparatur läuft …").foregroundStyle(.secondary)
                }
            }
        } else if let found = diagnosis, found.braucht_dich {
            Section {
                Label("Aktion von dir nötig", systemImage: "person.crop.circle.badge.exclamationmark")
                    .foregroundStyle(.orange)
                if !found.pruefung.isEmpty {
                    Text(found.pruefung).font(.caption).foregroundStyle(.secondary)
                }
            }
        } else if let found = diagnosis, found.reparierbar {
            Section {
                Button { Task { await repair() } } label: {
                    Label("Versuchen zu reparieren", systemImage: "wrench.and.screwdriver")
                }
                if !found.pruefung.isEmpty {
                    Text("Danach prüfe ich: \(found.pruefung)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        } else if diagnosis != nil {
            Section {
                Text("Dafür habe ich kein hinterlegtes Vorgehen.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private var headline: String {
        if result?.ok == true { return "Wiederhergestellt" }
        if working { return "Reparatur läuft" }
        if let found = diagnosis, found.braucht_dich { return "Aktion von dir nötig" }
        if diagnosis != nil { return "Problem erkannt" }
        return stateWord(component.zustand)
    }

    private var headlineTone: Color {
        if result?.ok == true { return .green }
        if let found = diagnosis, found.braucht_dich { return .orange }
        return .secondary
    }

    private func failureWord(_ outcome: RepairResult) -> String {
        switch outcome.grund ?? "" {
        case "braucht_dich": return "Das kann nur jemand von Hand"
        case "kein_vorgehen": return "Kein hinterlegtes Vorgehen"
        case "kein_arzt": return "Gerade nicht verfügbar"
        default: return "Hat nicht geholfen"
        }
    }

    private func load() async {
        guard let client = model.client else { return }
        do { diagnosis = try await client.diagnose(component: component.komponente) }
        catch { loadError = "Der Befund ließ sich nicht laden." }
    }

    private func repair() async {
        guard !working, let client = model.client else { return }
        working = true
        defer { working = false }
        do {
            result = try await client.repair(component: component.komponente)
        } catch {
            result = RepairResult(ok: false, grund: "unerreichbar")
        }
        // Nach einem Eingriff nicht die alte Anzeige stehen lassen.
        await model.refreshControl()
    }
}

private func confidenceWord(_ raw: String) -> String {
    switch raw {
    case "hoch": return "hoch"
    case "mittel": return "mittel"
    default: return "gering"
    }
}
