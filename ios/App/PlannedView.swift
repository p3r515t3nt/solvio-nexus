// Geplant — was SOLVIO von selbst tut.
//
// Menschensprache, kein Cron-Editor. Die vier Handlungen des Besitzers bleiben
// die freigegebenen: Pausieren, Fortsetzen, Jetzt pruefen, Loeschen — jede an
// die UNVERAENDERTE Kennung der angetippten Aufgabe gebunden.
//
// Die Kennung reist als KOPIE mit, nicht als Index. Waere es ein Index, koennte
// eine Aktualisierung zwischen Tippen und Ausfuehren die Handlung auf eine
// andere Aufgabe umlenken — dieselbe Lehre wie beim Freigabeweg.
import SwiftUI

struct PlannedView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                NavigationLink("Täglichen Überblick einrichten") { EverydaySetupView(model: model) }
                NavigationLink("An Mailantwort erinnern") { MailFollowupSetupView(model: model) }
                if !model.reachable { OfflineCard(since: model.snapshotAt) }
                if model.tasks.isEmpty {
                    EmptyState(icon: "clock.badge.questionmark",
                               title: "Noch nichts geplant.",
                               text: "Sag SOLVIO einfach, was es regelmäßig für dich prüfen soll.")
                } else {
                    ForEach(model.tasks) { task in
                        NavigationLink(value: task) { TaskCard(task: task) }
                            .buttonStyle(PressScale())
                    }
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Geplant")
        .refreshable { await model.refreshControl() }
        .navigationDestination(for: TaskRow.self) { TaskDetailView(model: model, task: $0) }
    }
}

struct TaskCard: View {
    let task: TaskRow

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: task.aktiv ? "calendar" : "pause.circle")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(task.aktiv ? Theme.blue : Theme.ink3)
                .frame(width: 40, height: 40)
                .background(task.aktiv ? Theme.tintBlue : Theme.line,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.chip,
                                                 style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(task.titel)
                        .font(.body.weight(.medium))
                        .foregroundStyle(Theme.ink)
                        .multilineTextAlignment(.leading)
                    if task.wartet_auf_freigabe {
                        Image(systemName: "faceid").font(.caption)
                            .foregroundStyle(Theme.warn)
                            .accessibilityLabel("Wartet auf Freigabe")
                    } else if task.fehlschlaege > 0 {
                        Image(systemName: "exclamationmark.triangle.fill").font(.caption)
                            .foregroundStyle(Theme.warn)
                            .accessibilityLabel("Zuletzt fehlgeschlagen")
                    }
                }
                Text("\(task.was) · \(task.wann)")
                    .font(.caption).foregroundStyle(Theme.ink2)
                if task.aktiv {
                    Text("Als Nächstes: \(clockTime(task.naechster_lauf))")
                        .font(.caption2).foregroundStyle(Theme.ink3)
                } else {
                    Text(task.zustand)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(Theme.ink3)
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(Theme.line, in: Capsule())
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.ink3).padding(.top, 12)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .opacity(task.aktiv ? 1 : 0.75)
    }
}

struct TaskDetailView: View {
    @ObservedObject var model: AppModel
    /// Eine KOPIE der angetippten Zeile — nicht aus der Liste nachgeschlagen.
    let task: TaskRow
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false
    /// Sperrt die Knoepfe, solange eine Handlung unterwegs ist. Im Live-Lauf
    /// kamen fuer einen Tipp fuenf Anfragen an: die Liste aktualisiert sich, der
    /// Knopf aenderte sich nicht sofort, und wer nichts passieren sieht, tippt
    /// noch einmal. Bei „Pausieren" folgenlos, bei „Loeschen" nicht.
    @State private var working = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(task.titel)
                        .font(.display(.title2, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                    Text("\(task.was) · \(task.wann)")
                        .font(.subheadline).foregroundStyle(Theme.ink2)
                }

                VStack(spacing: 0) {
                    row("Als Nächstes", task.aktiv ? clockTime(task.naechster_lauf) : "Pausiert")
                    Divider().padding(.leading, Theme.Space.card)
                    row("Zuletzt gelaufen", relativeTime(task.letzter_lauf))
                    if task.fehlschlaege > 0 {
                        Divider().padding(.leading, Theme.Space.card)
                        row("Fehlversuche", "\(task.fehlschlaege)", tone: Theme.warn)
                    }
                }
                .card()

                if !task.letzter_fehler.isEmpty {
                    Text(task.letzter_fehler)
                        .font(.callout).foregroundStyle(Theme.inkOnGold)
                        .padding(Theme.Space.card)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.tintGold,
                                    in: RoundedRectangle(cornerRadius: Theme.Radius.card,
                                                         style: .continuous))
                }

                if !task.auftrag.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Dein Auftrag")
                            .font(.display(.headline)).foregroundStyle(Theme.ink)
                        Text(task.auftrag)
                            .font(.callout).foregroundStyle(Theme.ink2)
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Theme.surface2,
                                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
                    }
                }

                VStack(spacing: 10) {
                    if working {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Einen Moment …").foregroundStyle(Theme.ink2)
                        }
                        .frame(maxWidth: .infinity).frame(height: 52)
                    } else if task.aktiv {
                        action("Jetzt prüfen", symbol: "play.circle.fill", prominent: true) {
                            try await model.client?.runNow(taskID: task.id)
                        }
                        action("Pausieren", symbol: "pause.circle") {
                            try await model.client?.pause(taskID: task.id)
                        }
                    } else {
                        action("Fortsetzen", symbol: "play.circle.fill", prominent: true) {
                            try await model.client?.resume(taskID: task.id)
                        }
                    }
                    if !working {
                        Button(role: .destructive) { confirmDelete = true } label: {
                            Label("Löschen", systemImage: "trash")
                                .font(.body.weight(.medium)).foregroundStyle(Theme.bad)
                                .frame(maxWidth: .infinity).frame(height: 52)
                        }
                    }
                }
                if !model.controlError.isEmpty {
                    Text(model.controlError).font(.footnote).foregroundStyle(Theme.bad)
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Aufgabe")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Aufgabe \u{201E}\(task.titel)\u{201C} löschen?",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Löschen", role: .destructive) {
                act(andLeave: true) { try await model.client?.deleteTask(taskID: task.id) }
            }
            Button("Abbrechen", role: .cancel) { }
        }
    }

    private func row(_ name: String, _ value: String, tone: Color = Theme.ink2) -> some View {
        HStack {
            Text(name).font(.body).foregroundStyle(Theme.ink)
            Spacer()
            Text(value).font(.body).foregroundStyle(tone)
        }
        .padding(Theme.Space.card)
    }

    private func action(_ title: String, symbol: String, prominent: Bool = false,
                        _ work: @escaping () async throws -> Void) -> some View {
        Button { act(work) } label: {
            Label(title, systemImage: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(prominent ? Theme.onBlue : Theme.blue)
                .frame(maxWidth: .infinity).frame(height: 52)
                .background(prominent ? AnyShapeStyle(Theme.blue) : AnyShapeStyle(Theme.tintBlue),
                            in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(PressScale())
    }

    private func act(andLeave: Bool = false, _ work: @escaping () async throws -> Void) {
        // Ein zweiter Tipp waehrend der ersten Handlung wird VERWORFEN, nicht
        // gepuffert. Zweimal „Loeschen" ist keine doppelte Absicht.
        guard !working else { return }
        Haptics.tap()
        working = true
        Task {
            defer { working = false }
            do {
                try await work()
                await model.refreshControl()
                if andLeave { dismiss() }
            } catch {
                model.controlError = "Das hat nicht geklappt."
            }
        }
    }
}
