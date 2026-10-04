// System — verstaendlich fuer den Besitzer, nicht fuer den Betreiber.
//
// Der Normalfall ist EIN Satz. Elf gruene Dienstpunkte sind keine Auskunft,
// sondern Laerm. Komplexitaet erscheint erst, wenn etwas dich braucht — und
// dann in Produktsprache, nicht als Komponentenkennung. Die volle Liste bleibt
// eine Ebene tiefer erreichbar, fuer den neugierigen Blick; der Arzt liegt
// dahinter und ist unveraendert.
//
// Grau heisst „nicht nachgesehen", nie „gesund": `toneColor` faerbt alles
// Unbekannte sekundaer, und daneben steht immer ein Wort.
import SwiftUI

struct SystemView: View {
    @ObservedObject var model: AppModel
    @State private var showAll = false

    private var troubled: [ComponentHealth] {
        model.system.filter { $0.zustand != "healthy" && $0.zustand != "unknown" }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                headline

                if !troubled.isEmpty && model.reachable {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Braucht dich")
                            .font(.display(.headline)).foregroundStyle(Theme.ink)
                        ForEach(troubled) { component in
                            NavigationLink { DoctorView(model: model, component: component) } label: {
                                TroubleCard(component: component)
                            }
                            .buttonStyle(PressScale())
                        }
                    }
                }

                areas

                // Der Tresor liegt hier und nicht als fuenfter Reiter: die
                // Gestaltungsakte deckelt die App bei vier Reitern, und ein
                // Zugangsverwalter ist etwas, das man selten oeffnet und nie
                // sucht — er gehoert zu System, wie der Verlauf darunter.
                NavigationLink(value: HomeRoute.tresor) {
                    HStack {
                        Label("Tresor", systemImage: "lock.rectangle.stack")
                            .font(.body).foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
                    }
                    .padding(Theme.Space.card)
                    .card()
                }
                .buttonStyle(PressScale())

                NavigationLink(value: HomeRoute.activity) {
                    HStack {
                        Label("Verlauf", systemImage: "list.bullet.rectangle")
                            .font(.body).foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
                    }
                    .padding(Theme.Space.card)
                    .card()
                }
                .buttonStyle(PressScale())
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
            .padding(.bottom, 24)
        }
        .background(Theme.bg)
        .navigationTitle("System")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await model.loadSystem() }
        .task { await model.loadSystem() }
    }

    private var headline: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                StatusDot(color: model.statusTone)
                Text(model.reachable ? model.statusSentence
                                     : "SOLVIO ist gerade nicht erreichbar.")
                    .font(.display(.title2, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(freshness)
                .font(.subheadline).foregroundStyle(Theme.ink2)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
    }

    private var freshness: String {
        guard model.reachable else {
            guard let at = model.snapshotAt else { return "Kein aktueller Stand." }
            return "Angezeigt wird der Stand von \(relativeTime(at))."
        }
        if let at = model.snapshotAt { return "Zuletzt geprüft \(relativeTime(at))." }
        return "Ich sehe nach …"
    }

    private var areas: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button { withAnimation(Motion.standard) { showAll.toggle() } } label: {
                HStack {
                    Text("Alle Bereiche")
                        .font(.display(.headline)).foregroundStyle(Theme.ink)
                    Spacer()
                    Image(systemName: showAll ? "chevron.up" : "chevron.down")
                        .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showAll {
                VStack(spacing: 0) {
                    ForEach(Array(model.system.enumerated()), id: \.element.id) { index, component in
                        if index > 0 { Divider().padding(.leading, Theme.Space.card) }
                        areaRow(component)
                    }
                }
                .card()
            }
        }
    }

    @ViewBuilder
    private func areaRow(_ component: ComponentHealth) -> some View {
        let row = HStack(spacing: 12) {
            StatusDot(color: toneColor(component.zustand))
            VStack(alignment: .leading, spacing: 1) {
                Text(component.name).font(.body).foregroundStyle(Theme.ink)
                if !component.grund.isEmpty {
                    Text(component.grund).font(.caption).foregroundStyle(Theme.ink2)
                        .lineLimit(2)
                }
            }
            Spacer()
            Text(stateWord(component.zustand))
                .font(.caption).foregroundStyle(Theme.ink2)
        }
        .padding(Theme.Space.card)
        .accessibilityElement(children: .combine)

        // Nur was gestoert ist, laesst sich oeffnen. Ein Befund ueber etwas
        // Gesundes hat nichts zu sagen, und eine Zeile, die sich antippen laesst
        // und dann nichts zeigt, ist ein gebrochenes Versprechen.
        if component.zustand == "healthy" || component.zustand == "unknown" {
            row
        } else {
            NavigationLink { DoctorView(model: model, component: component) } label: { row }
                .buttonStyle(.plain)
        }
    }
}

struct TroubleCard: View {
    let component: ComponentHealth

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: component.zustand == "auth_required"
                  ? "person.crop.circle.badge.exclamationmark"
                  : "exclamationmark.triangle.fill")
                .font(.title3).foregroundStyle(Theme.inkOnGold)
            VStack(alignment: .leading, spacing: 2) {
                Text(component.name).font(.headline).foregroundStyle(Theme.inkOnGold)
                Text(component.grund.isEmpty ? stateWord(component.zustand) : component.grund)
                    .font(.footnote).foregroundStyle(Theme.inkOnGold.opacity(0.85))
                    .multilineTextAlignment(.leading)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.inkOnGold.opacity(0.6))
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }
}

// MARK: - Verlauf

struct ActivityView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if !model.reachable { OfflineCard(since: model.snapshotAt) }
                if model.activity.isEmpty {
                    EmptyState(icon: "clock", title: "Noch nichts passiert.",
                               text: "Was SOLVIO für dich getan hat, steht hier.")
                }
                VStack(spacing: 0) {
                    ForEach(Array(model.activity.enumerated()), id: \.element.id) { index, event in
                        if index > 0 { Divider().padding(.leading, Theme.Space.card) }
                        HStack(alignment: .top, spacing: 12) {
                            StatusDot(color: toneColor(event.ton)).padding(.top, 6)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(event.titel).font(.callout).foregroundStyle(Theme.ink)
                                Text(clockTime(event.zeit))
                                    .font(.caption2).foregroundStyle(Theme.ink2)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(Theme.Space.card)
                        .accessibilityElement(children: .combine)
                    }
                }
                .card()
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Verlauf")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.loadActivity() }
        .refreshable { await model.loadActivity() }
    }
}
