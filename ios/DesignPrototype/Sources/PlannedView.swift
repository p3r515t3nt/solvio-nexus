// Geplant — was SOLVIO von selbst tut.
//
// Menschensprache, kein Cron-Editor: „Täglich um 07:30", nicht „30 7 * * *".
// Die vier Besitzerhandlungen bleiben die freigegebenen: Pausieren,
// Fortsetzen, Jetzt prüfen, Löschen.
import SwiftUI

struct PlannedView: View {
    @ObservedObject var model: DesignModel

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if !model.reachable { OfflineCard() }
                if model.tasks.isEmpty {
                    EmptyState(icon: "clock.badge.questionmark",
                               title: "Noch nichts geplant.",
                               text: "Sag SOLVIO einfach, was es regelmäßig für dich prüfen soll.")
                } else {
                    ForEach(model.tasks) { task in
                        NavigationLink(value: task.id) { TaskCard(task: task) }
                            .buttonStyle(PressScale())
                    }
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Geplant")
        .navigationDestination(for: String.self) { id in
            if let task = model.tasks.first(where: { $0.id == id }) {
                TaskDetail(task: task)
            }
        }
    }
}

struct TaskCard: View {
    let task: MockTask

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: task.active ? "calendar" : "pause.circle")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(task.active ? Theme.blue : Theme.ink3)
                .frame(width: 40, height: 40)
                .background(task.active ? Theme.tintBlue : Theme.line,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(task.title)
                        .font(.body.weight(.medium))
                        .foregroundStyle(Theme.ink)
                    if task.failures > 0 {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundStyle(Theme.warn)
                            .accessibilityLabel("Zuletzt fehlgeschlagen")
                    }
                }
                Text("\(task.what) · \(task.schedule)")
                    .font(.caption)
                    .foregroundStyle(Theme.ink2)
                if task.active, let next = task.nextRun {
                    Text("Als Nächstes: \(next)")
                        .font(.caption2)
                        .foregroundStyle(Theme.ink3)
                } else if !task.active {
                    Text("Pausiert")
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(Theme.ink3)
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(Theme.line, in: Capsule())
                }
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.ink3)
                .padding(.top, 12)
        }
        .padding(Theme.Space.card)
        .card()
        .opacity(task.active ? 1 : 0.75)
    }
}

struct TaskDetail: View {
    let task: MockTask

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(task.title)
                        .font(.display(.title2, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                    Text("\(task.what) · \(task.schedule)")
                        .font(.subheadline)
                        .foregroundStyle(Theme.ink2)
                }

                VStack(spacing: 0) {
                    detailRow("Als Nächstes", task.active ? (task.nextRun ?? "—") : "Pausiert")
                    Divider().padding(.leading, Theme.Space.card)
                    detailRow("Zuletzt gelaufen", task.lastRun)
                    if task.failures > 0 {
                        Divider().padding(.leading, Theme.Space.card)
                        detailRow("Fehlversuche", "\(task.failures)", tone: Theme.warn)
                    }
                }
                .card()

                VStack(alignment: .leading, spacing: 8) {
                    Text("Dein Auftrag")
                        .font(.display(.headline))
                        .foregroundStyle(Theme.ink)
                    Text(task.order)
                        .font(.callout)
                        .foregroundStyle(Theme.ink2)
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.surface2,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
                }

                VStack(spacing: 10) {
                    if task.active {
                        actionButton("Jetzt prüfen", symbol: "play.circle.fill", prominent: true)
                        actionButton("Pausieren", symbol: "pause.circle")
                    } else {
                        actionButton("Fortsetzen", symbol: "play.circle.fill", prominent: true)
                    }
                    Button(role: .destructive) { Haptics.warning() } label: {
                        Label("Löschen", systemImage: "trash")
                            .font(.body.weight(.medium))
                            .foregroundStyle(Theme.bad)
                            .frame(maxWidth: .infinity)
                            .frame(height: 50)
                    }
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Aufgabe")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func detailRow(_ name: String, _ value: String, tone: Color = Theme.ink2) -> some View {
        HStack {
            Text(name).font(.body).foregroundStyle(Theme.ink)
            Spacer()
            Text(value).font(.body).foregroundStyle(tone)
        }
        .padding(Theme.Space.card)
    }

    private func actionButton(_ title: String, symbol: String, prominent: Bool = false) -> some View {
        Button { Haptics.tap() } label: {
            Label(title, systemImage: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(prominent ? Theme.onBlue : Theme.blue)
                .frame(maxWidth: .infinity)
                .frame(height: 52)
                .background(prominent ? AnyShapeStyle(Theme.blue) : AnyShapeStyle(Theme.tintBlue),
                            in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(PressScale())
    }
}
