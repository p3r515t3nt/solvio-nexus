// Ein Zahlungsmittel hinterlegen und seine Grenzen ändern.
//
// WAS HIER NICHT STEHT, IST DER PUNKT: es gibt kein Feld für eine
// Kartennummer, keines für eine Prüfziffer, keines für ein Ablaufdatum und
// keines für einen Bankzugang. Nicht deaktiviert, nicht ausgegraut — es gibt
// sie nicht. Wer sie sucht, findet sie nicht, weil SOLVIO sie nie haben soll.
//
// Was der Mensch hier einträgt, ist die Kennung, unter der SEIN ANBIETER das
// Zahlungsmittel führt (`pm_…`). Sie ist kein Geheimnis: allein bewegt sie
// keinen Cent — dafür braucht es zusätzlich den Anbieterzugang, und der liegt
// im Tresor. Sie steht trotzdem in einem `SecureField` und reist eingelagert
// statt als Argument: was in den Argumenten steht, landet im Freigabetext und
// dauerhaft in der Freigabe-Datenbank des Macs, und dorthin gehört sie nicht.
//
// Die Karte selbst hinterlegt der Eigentümer beim Anbieter, in dessen eigener
// Oberfläche. SOLVIO bekommt danach die Kennung — nie die Karte.
//
// Beide Handlungen sind VERY_CRITICAL und gehen nach dem Absenden durch
// Face ID — nicht durch eine Prüfung in dieser Datei, sondern weil die Matrix
// des Macs sie so führt.
import SwiftUI

struct PaymentAddSheet: View {
    @ObservedObject var model: PaymentModel
    @Environment(\.dismiss) private var dismiss

    @State private var draft = PaymentDraft()
    @State private var token = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Wofür") {
                    TextField("Verweis, z. B. payment://shopping/default",
                              text: $draft.verweis)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Name, z. B. SOLVIO Shopping", text: $draft.name)
                        .textInputAutocapitalization(.words)
                    Picker("Art", selection: $draft.art) {
                        Text("Virtuelle Karte").tag("virtual_card")
                        Text("Beim Anbieter hinterlegt").tag("provider_token")
                        Text("Beim Händler hinterlegt").tag("merchant_saved")
                        Text("Wallet").tag("wallet")
                    }
                }
                Section {
                    TextField("Anbieter, z. B. sandbox", text: $draft.anbieter)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Kennung beim Anbieter (pm_…)", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityLabel("Kennung beim Anbieter")
                    TextField("Hinweis, z. B. •••• 4242", text: $draft.hinweis)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Beim Anbieter")
                } footer: {
                    Text("Die Karte selbst hinterlegst du beim Anbieter. SOLVIO "
                         + "bekommt nur diese Kennung — und sie bewegt allein "
                         + "keinen Cent.")
                }
                Section {
                    TextField("Höchstens je Zahlung, in Cent",
                              text: $draft.grenzeEinzelnMinor)
                        .keyboardType(.numberPad)
                    TextField("Höchstens je Tag, in Cent (0 = keine)",
                              text: $draft.grenzeTaeglichMinor)
                        .keyboardType(.numberPad)
                    TextField("Währungen, z. B. EUR", text: $draft.waehrungen)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                    TextField("Nur bei diesen Händlern", text: $draft.haendler)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Grenzen")
                } footer: {
                    Text("Leer heißt NEIN: ohne Händler kauft dieses "
                         + "Zahlungsmittel nirgends, ohne Währung zahlt es nichts.")
                }
                Section {
                    TextField("Belastung, z. B. secret://…/…", text: $draft.zugang)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Nur lesen, z. B. secret://…/…",
                              text: $draft.lesezugang)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Zugang im Tresor")
                } footer: {
                    Text("Zwei getrennte Zugänge: einer darf belasten und "
                         + "verlangt dafür deine Anwesenheit, einer darf nur "
                         + "nachsehen.")
                }
                Section {
                    Button {
                        busy = true
                        Task {
                            // Erst das Blatt schliessen, DANN handeln: die
                            // Handlung wartet auf eine Face-ID-Runde, und ein
                            // Blatt, das dabei stehen bleibt, verdeckt genau
                            // die Karte, die den Fortschritt zeigt. Das
                            // Ergebnis steht danach auf dem Bildschirm
                            // darunter — der Satz kommt vom Core.
                            let entwurf = draft
                            let wert = token
                            token = ""
                            busy = false
                            dismiss()
                            _ = await model.add(draft: entwurf, token: wert)
                        }
                    } label: {
                        HStack {
                            Spacer()
                            if busy { ProgressView().controlSize(.small) }
                            Text("Hinterlegen")
                            Spacer()
                        }
                    }
                    .disabled(busy || token.isEmpty || draft.anbieter.isEmpty
                              || draft.haendler.isEmpty)
                }
            }
            .navigationTitle("Zahlungsmittel")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { token = ""; dismiss() }
                }
            }
            .onDisappear { token = "" }
        }
    }
}

/// Grenzen ändern — und die Richtung ist die eigentliche Aussage.
///
/// SENKEN ist die sichere Richtung und läuft über die billigere Fähigkeit.
/// ANHEBEN ist eine Erweiterung der Befugnis und bleibt aus jeder Herkunft
/// biometrisch. Der Core rechnet nach, was die neue Zeile gegenüber der alten
/// bedeutet — diese Ansicht kann sich die Richtung nicht aussuchen.
struct PaymentLimitSheet: View {
    @ObservedObject var model: PaymentModel
    let method: PaymentMethod
    @Environment(\.dismiss) private var dismiss

    @State private var single: String = ""
    @State private var daily: String = ""
    @State private var busy = false

    private var currency: String { method.waehrungen.first ?? "EUR" }
    private var newSingle: Int? { Int(single) }
    private var newDaily: Int? { Int(daily) }

    /// Wäre das eine ANHEBUNG? Dieselbe Rechnung wie im Core — hier nur, um
    /// dem Menschen VORHER zu sagen, was ihn erwartet.
    private var raises: Bool {
        if let value = newSingle, value > method.grenze_einzeln_minor { return true }
        if let value = newDaily {
            if method.grenze_taeglich_minor > 0 && value == 0 { return true }
            if value > method.grenze_taeglich_minor { return true }
        }
        return false
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Bisher") {
                    LabeledContent("Je Zahlung",
                                   value: PaymentAmount.text(method.grenze_einzeln_minor,
                                                             currency))
                    LabeledContent("Je Tag",
                                   value: method.grenze_taeglich_minor > 0
                                   ? PaymentAmount.text(method.grenze_taeglich_minor,
                                                        currency)
                                   : "keine")
                }
                Section {
                    TextField("Je Zahlung, in Cent", text: $single)
                        .keyboardType(.numberPad)
                    TextField("Je Tag, in Cent (0 = keine)", text: $daily)
                        .keyboardType(.numberPad)
                } header: {
                    Text("Neu")
                } footer: {
                    Text(raises
                         ? "Das ist eine ANHEBUNG. Sie kostet dein Gesicht."
                         : "Senken ist die sichere Richtung und geht direkt.")
                        .foregroundStyle(raises ? Theme.warn : Theme.ink3)
                }
                Section {
                    Button {
                        busy = true
                        Task {
                            let hoeher = raises
                            let einzeln = newSingle
                            let taeglich = newDaily
                            let ref = method.verweis
                            busy = false
                            dismiss()
                            _ = hoeher
                                ? await model.raiseLimit(ref: ref, single: einzeln,
                                                         daily: taeglich)
                                : await model.lowerLimit(ref: ref, single: einzeln,
                                                         daily: taeglich)
                        }
                    } label: {
                        HStack {
                            Spacer()
                            if busy { ProgressView().controlSize(.small) }
                            Text(raises ? "Anheben — mit \(Biometrics.faceIDName)"
                                        : "Senken")
                            Spacer()
                        }
                    }
                    .disabled(busy || (newSingle == nil && newDaily == nil))
                }
            }
            .navigationTitle(method.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                }
            }
        }
    }
}
