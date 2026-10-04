// System — verstaendlich fuer den Besitzer, nicht fuer den Betreiber.
//
// Der Normalfall ist EIN Satz: „SOLVIO läuft." Elf gruene Dienstpunkte sind
// keine Auskunft, sondern Laerm. Komplexitaet erscheint erst, wenn etwas dich
// braucht — und dann in Produktsprache („Die Google-Anmeldung ist abgelaufen"),
// nicht als Komponentenkennung. Die volle Liste bleibt eine Ebene tiefer
// erreichbar, fuer den neugierigen Blick.
import SwiftUI

struct SystemView: View {
    @ObservedObject var model: DesignModel
    @State private var showAll = false

    private var troubled: [MockComponent] {
        model.components.filter { $0.state != "healthy" }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                headline

                if !troubled.isEmpty && model.reachable {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Braucht dich")
                            .font(.display(.headline))
                            .foregroundStyle(Theme.ink)
                        ForEach(troubled) { component in
                            TroubleCard(component: component)
                        }
                    }
                }

                allAreas

                NavigationLink { ActivityPlaceholder() } label: {
                    HStack {
                        Label("Verlauf", systemImage: "list.bullet.rectangle")
                            .font(.body)
                            .foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Theme.ink3)
                    }
                    .padding(Theme.Space.card)
                    .card()
                }
                .buttonStyle(PressScale())
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("System")
        .navigationBarTitleDisplayMode(.inline)
    }

    // Der eine Satz — gross und ruhig.
    private var headline: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                StatusDot(color: model.systemHeadline.tone)
                Text(model.systemHeadline.text)
                    .font(.display(.title2, weight: .semibold))
                    .foregroundStyle(Theme.ink)
            }
            Text(model.systemHeadline.sub)
                .font(.subheadline)
                .foregroundStyle(Theme.ink2)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
    }

    // Die Bereiche — Produktsprache, eingeklappt bis man sie will.
    private var allAreas: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(Motion.standard) { showAll.toggle() }
            } label: {
                HStack {
                    Text("Alle Bereiche")
                        .font(.display(.headline))
                        .foregroundStyle(Theme.ink)
                    Spacer()
                    Image(systemName: showAll ? "chevron.up" : "chevron.down")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Theme.ink3)
                }
            }
            if showAll {
                VStack(spacing: 0) {
                    ForEach(Array(model.components.enumerated()), id: \.element.id) { index, component in
                        if index > 0 { Divider().padding(.leading, Theme.Space.card) }
                        HStack(spacing: 12) {
                            StatusDot(color: stateColor(component.state))
                            VStack(alignment: .leading, spacing: 1) {
                                Text(component.name).font(.body).foregroundStyle(Theme.ink)
                                Text(component.detail)
                                    .font(.caption).foregroundStyle(Theme.ink2)
                            }
                            Spacer()
                            Text(stateWord(component.state))
                                .font(.caption)
                                .foregroundStyle(Theme.ink2)
                        }
                        .padding(Theme.Space.card)
                        .accessibilityElement(children: .combine)
                    }
                }
                .card()
            }
        }
    }
}

struct TroubleCard: View {
    let component: MockComponent

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "person.crop.circle.badge.exclamationmark")
                    .font(.title3)
                    .foregroundStyle(Theme.inkOnGold)
                VStack(alignment: .leading, spacing: 2) {
                    Text(component.name)
                        .font(.headline)
                        .foregroundStyle(Theme.inkOnGold)
                    Text(component.detail)
                        .font(.footnote)
                        .foregroundStyle(Theme.inkOnGold.opacity(0.8))
                }
            }
            Button { Haptics.tap() } label: {
                Text("Neu anmelden")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.onBlue)
                    .padding(.horizontal, 18).padding(.vertical, 9)
                    .background(Theme.blue, in: Capsule())
            }
            .buttonStyle(PressScale())
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }
}

func stateColor(_ state: String) -> Color {
    switch state {
    case "healthy": return Theme.good
    case "degraded", "auth_required", "quota_limited": return Theme.warn
    case "unavailable": return Theme.bad
    default: return Theme.ink3      // grau = nicht nachgesehen, nie „gesund"
    }
}

func stateWord(_ state: String) -> String {
    switch state {
    case "healthy": return "in Ordnung"
    case "degraded": return "eingeschränkt"
    case "unavailable": return "nicht erreichbar"
    case "auth_required": return "Anmeldung nötig"
    case "quota_limited": return "Kontingent erschöpft"
    default: return "unbekannt"
    }
}

/// Platzhalter im Prototyp — der echte Verlauf existiert im Produkt bereits.
struct ActivityPlaceholder: View {
    var body: some View {
        VStack(spacing: 12) {
            EmptyState(icon: "clock",
                       title: "Verlauf",
                       text: "Hier steht, was SOLVIO getan hat — wie heute, nur im neuen Gewand.")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .navigationTitle("Verlauf")
        .navigationBarTitleDisplayMode(.inline)
    }
}
