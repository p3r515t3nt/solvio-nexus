// Wissen — die ruhige Liste dessen, was SOLVIO ueber deine Welt weiss.
//
// Drei Abschnitte, und nur die, die es gerade gibt: was deine Bestaetigung
// braucht (die EINZIGE Stelle mit Gold — Gold heisst Aufmerksamkeit, nie
// Dekoration), was neu gelernt wurde, und der Rest. Jede Zeile sagt in
// Menschenworten, woher sie stammt; Vertragsnamen des Cores erscheinen hier
// nirgends.
import SwiftUI

struct WissenView: View {
    let onOpenApprovals: () -> Void
    @StateObject private var model = WissenModel()
    @State private var search = ""
    @State private var filter: WissenFilter = .alle
    /// Der kurze Dank nach einer Bestaetigung. Er lebt HIER und nicht in der
    /// Karte, weil das Modell nach dem Bestaetigen sofort neu laedt und die
    /// Karte damit aus der Liste faellt — die Liste selbst traegt den Dank
    /// einen Atemzug weiter, sonst saehe niemand ihn je.
    @State private var thanked = false
    @State private var thanksTask: Task<Void, Never>?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if !model.reachable { OfflineCard(since: model.snapshotAt) }
                NavigationLink("Kontakte abgleichen und merken") {
                    ContactsView(onOpenApprovals: onOpenApprovals)
                }
                searchField
                filterChips
                if thanked {
                    Text("✓ Danke — ich merke es mir.")
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Theme.good)
                        .frame(maxWidth: .infinity)
                        .transition(.opacity)
                }
                content
                if !model.notice.isEmpty {
                    Text(model.notice)
                        .font(.footnote).foregroundStyle(Theme.ink2)
                        .frame(maxWidth: .infinity)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .background(Theme.bg)
        .navigationTitle("Wissen")
        .navigationBarTitleDisplayMode(.large)
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        .navigationDestination(for: MemoryItem.self) {
            WissenDetailView(model: model, entry: .memory($0))
        }
        .navigationDestination(for: MemoryCandidate.self) {
            WissenDetailView(model: model, entry: .candidate($0))
        }
    }

    // MARK: Inhalt

    @ViewBuilder
    private var content: some View {
        let candidates = filter == .alle ? model.visibleCandidates(search: search) : []
        let fresh = model.freshLearned(filter: filter, search: search)
        let rest = model.remaining(filter: filter, search: search)

        if candidates.isEmpty && fresh.isEmpty && rest.isEmpty {
            if !model.loaded && model.reachable {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 48)
            } else if search.isEmpty && filter == .alle {
                EmptyState(icon: "book.closed",
                           title: "Noch ist es still hier.",
                           text: "SOLVIO merkt sich, was dir wichtig ist — hier erscheint es.")
            } else {
                EmptyState(icon: "magnifyingglass",
                           title: "Nichts gefunden.",
                           text: "Unter dieser Auswahl gibt es gerade keinen Eintrag.")
            }
        } else {
            if !candidates.isEmpty {
                section("Braucht deine Bestätigung", count: candidates.count,
                        accent: true) {
                    ForEach(candidates) {
                        CandidateCard(model: model, candidate: $0,
                                      onConfirmed: showThanks)
                    }
                }
            }
            if !fresh.isEmpty {
                section("Neu gelernt", count: fresh.count) {
                    ForEach(fresh) { item in
                        NavigationLink(value: item) { MemoryRow(item: item) }
                            .buttonStyle(PressScale())
                    }
                }
            }
            if !rest.isEmpty {
                section("Über dich", count: rest.count) {
                    ForEach(rest) { item in
                        NavigationLink(value: item) { MemoryRow(item: item) }
                            .buttonStyle(PressScale())
                    }
                }
            }
        }
    }

    /// Abschnittskopf im Stil der Designakte: klein, versal, gesperrt — und
    /// dahinter die Zahl. Die Zahl ist IMMER die echte Laenge der Liste
    /// darunter, nie eine eigene Buchfuehrung; Gold bekommt sie nur dort, wo
    /// der Abschnitt Aufmerksamkeit verlangt.
    private func section(_ title: String, count: Int, accent: Bool = false,
                         @ViewBuilder rows: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(title.uppercased())
                    .foregroundStyle(Theme.ink3)
                Text("· \(count)")
                    .foregroundStyle(accent ? Theme.gold : Theme.ink3)
            }
            .font(.caption2.weight(.semibold))
            .kerning(1.2)
            .accessibilityElement(children: .combine)
            VStack(spacing: 10) { rows() }
        }
    }

    /// Zeigt den Dank kurz und raeumt ihn selbst wieder weg. Ein zweites
    /// Bestaetigen kurz hintereinander setzt die Uhr neu, statt doppelt zu
    /// blenden.
    private func showThanks() {
        thanksTask?.cancel()
        withAnimation(Motion.standard) { thanked = true }
        thanksTask = Task {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(Motion.standard) { thanked = false }
        }
    }

    // MARK: Suche und Auswahl

    private var searchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(Theme.ink3)
                .accessibilityHidden(true)
            TextField("Suchen", text: $search)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .foregroundStyle(Theme.ink)
            if !search.isEmpty {
                Button {
                    search = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Theme.ink3)
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Suche löschen")
            }
        }
        .padding(.leading, 14)
        .frame(height: 48)
        .background(Theme.surface,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous))
    }

    private var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(WissenFilter.allCases) { choice in
                    Button {
                        Haptics.state()
                        filter = choice
                    } label: {
                        Text(choice.rawValue)
                            .font(.footnote.weight(filter == choice ? .semibold : .regular))
                            .foregroundStyle(filter == choice ? Theme.blue : Theme.ink2)
                            .padding(.horizontal, 14)
                            .frame(height: 36)
                            .background(filter == choice ? Theme.tintBlue : Theme.surface,
                                        in: Capsule())
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .accessibilityAddTraits(filter == choice ? .isSelected : [])
                }
            }
        }
    }
}

// MARK: - Zeilen

struct MemoryRow: View {
    let item: MemoryItem

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(item.content)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(3)
                Text("\(herkunftswort(item.lifecycle)) · \(relativeTime(wissenEpoch(item.updated_at) ?? wissenEpoch(item.created_at)))")
                    .font(.caption)
                    .foregroundStyle(Theme.ink2)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.ink3)
                .padding(.top, 4)
                .accessibilityHidden(true)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
    }
}

/// Ein Vorschlag: der EINE Ort, an dem Gold erscheint — als getoente Karte
/// mit goldenem Rand, wie in der Designakte. Bestaetigen und Ablehnen wirken
/// sofort aus der Liste; Details liegen hinter der Zeile.
struct CandidateCard: View {
    @ObservedObject var model: WissenModel
    let candidate: MemoryCandidate
    /// Meldet den Erfolg nach oben: die Liste zeigt den Dank, denn diese
    /// Karte ist nach dem Neuladen des Modells bereits verschwunden.
    var onConfirmed: () -> Void = {}

    // Der Gold-Ton der Designakte, bewusst als Alpha-Wert: derselbe Hauch
    // traegt auf heller wie dunkler Grundflaeche.
    private let cardGold = Color(hex: "#D6A545")

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            NavigationLink(value: candidate) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(candidate.statement)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.ink)
                        .multilineTextAlignment(.leading)
                    Text("Möchte SOLVIO sich merken · \(relativeTime(wissenEpoch(candidate.last_seen)))")
                        .font(.caption)
                        .foregroundStyle(Theme.ink2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint("Zeigt Einzelheiten zum Vorschlag.")

            HStack(spacing: 10) {
                Button {
                    Haptics.tap()
                    Task {
                        if await model.confirmCandidate(candidate.candidate_id) {
                            onConfirmed()
                        }
                    }
                } label: {
                    Text("Stimmt")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.onBlue)
                        .frame(maxWidth: .infinity)
                        .frame(height: 44)
                        .background(Theme.blue,
                                    in: RoundedRectangle(cornerRadius: Theme.Radius.row,
                                                         style: .continuous))
                }
                .buttonStyle(PressScale())
                Button {
                    Haptics.tap()
                    Task { await model.declineCandidate(candidate.candidate_id) }
                } label: {
                    // Geisterknopf: nur Wort und Linie — der Goldgrund der
                    // Karte scheint durch, damit „Eher nicht" leiser bleibt
                    // als „Stimmt".
                    Text("Eher nicht")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.ink2)
                        .frame(maxWidth: .infinity)
                        .frame(height: 44)
                        .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row,
                                                  style: .continuous)
                            .stroke(Theme.line))
                        .contentShape(Rectangle())
                }
                .buttonStyle(PressScale())
            }
            .disabled(model.working)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(cardGold.opacity(0.07),
                    in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .stroke(cardGold.opacity(0.28)))
    }
}
