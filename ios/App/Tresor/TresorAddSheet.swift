// Einen Zugang eingeben — der einzige Ort in dieser App, an dem ein Wert
// überhaupt existiert.
//
// Sechs Vorkehrungen, und jede hat einen Grund:
//
// * `SecureField` statt `TextField`. Das ist nicht nur der Punkte-Effekt: iOS
//   behandelt ein sicheres Feld anders — es taucht nicht in Bildschirmaufnahmen
//   auf, und der Text wandert nicht in Vorschläge.
// * Autokorrektur, Autogroßschreibung und Smart-Quotes AUS. Ein Passwort, das
//   die Tastatur „verbessert", ist ein anderes Passwort.
// * Der Wert lebt in einem lokalen `@State` dieses Blattes und nirgends sonst.
//   Kein Modell hält ihn, kein `@AppStorage`, kein `UserDefaults`, keine
//   Zustandswiederherstellung. Wenn das Blatt geht, geht er mit.
// * Er wird beim Schließen ausdrücklich überschrieben. Swift gibt darüber
//   keine Garantie — eine `String`-Zuweisung löscht keinen Speicher — aber sie
//   verkürzt die Lebensdauer der Referenz, und mehr ist ehrlich nicht drin.
// * Er wird nie in eine Barrierefreiheits-Ansage gestellt: das Feld trägt ein
//   Label, keinen Wert.
// * Es gibt keine Kopieren-Schaltfläche und keinen Weg in die Zwischenablage.
//
// Was der Mensch hier tut, ist eine VERY_CRITICAL-Handlung. Sie geht deshalb
// nach dem Absenden noch durch Face ID — nicht durch eine Prüfung in dieser
// Datei, sondern weil die Matrix des Macs es so führt.
import SwiftUI

struct TresorAddSheet: View {
    @ObservedObject var model: TresorModel
    @Environment(\.dismiss) private var dismiss

    @State private var draft = TresorDraft()
    @State private var secret = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Wofür") {
                    TextField("Dienst, z. B. Amazon", text: $draft.service)
                        .textInputAutocapitalization(.words)
                    TextField("Konto, z. B. gregor", text: $draft.account)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Adresse, z. B. www.amazon.de", text: $draft.target)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section("Zugang") {
                    SecureField("Passwort oder Schlüssel", text: $secret)
                        .textContentType(.password)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityLabel("Zugang")
                    Picker("Art", selection: $draft.kind) {
                        Text("Passwort").tag("password")
                        Text("Zugangstoken").tag("api_token")
                        Text("Schlüssel").tag("api_key")
                    }
                }
                Section {
                    Toggle("Auch im Hintergrund benutzen", isOn: $draft.allowBackground)
                } footer: {
                    Text("Aus heißt: SOLVIO benutzt diesen Zugang nur, wenn du "
                         + "gerade etwas veranlasst hast — nicht in einer "
                         + "geplanten Aufgabe.")
                }
                Section {
                    Text("SOLVIO speichert diesen Zugang verschlüsselt und zeigt "
                         + "ihn nie wieder an — auch dir nicht. Er ist danach nur "
                         + "für \(hostWort) benutzbar.")
                        .font(.caption).foregroundStyle(Theme.ink2)
                }
            }
            .navigationTitle("Zugang hinzufügen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { finish() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Speichern") { save() }
                        .disabled(!draft.isComplete || secret.isEmpty || busy)
                }
            }
        }
        .interactiveDismissDisabled(busy)
        .onDisappear { secret = "" }
    }

    private var hostWort: String {
        URL(string: draft.targetOrigin)?.host ?? "die angegebene Adresse"
    }

    private func save() {
        busy = true
        let value = secret
        Task {
            _ = await model.add(entry: draft, secret: value)
            // Der Wert wird sofort nach dem Absenden aus dem Blatt genommen.
            // Der Mac hat ihn versiegelt; hier wird er nicht mehr gebraucht,
            // auch nicht für den zweiten Versuch nach Face ID.
            secret = ""
            busy = false
            dismiss()
        }
    }

    private func finish() {
        secret = ""
        dismiss()
    }
}

/// Einen bestehenden Zugang ersetzen. Dieselbe Sorgfalt, weniger Felder — die
/// Berechtigung bleibt, nur der Wert ist neu.
struct TresorReplaceSheet: View {
    @ObservedObject var model: TresorModel
    let entry: TresorEntry
    @Environment(\.dismiss) private var dismiss

    @State private var secret = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section(entry.title) {
                    SecureField("Neues Passwort oder Schlüssel", text: $secret)
                        .textContentType(.newPassword)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityLabel("Neuer Zugang")
                }
                Section {
                    Text("Der alte Wert wird dabei überschrieben und ist danach "
                         + "weg. \(tresorZielwort(entry.allowed_targets)) bleibt "
                         + "unverändert.")
                        .font(.caption).foregroundStyle(Theme.ink2)
                }
            }
            .navigationTitle("Zugang ersetzen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { secret = ""; dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Ersetzen") {
                        busy = true
                        let value = secret
                        Task {
                            _ = await model.replace(ref: entry.secret_ref,
                                                    secret: value)
                            secret = ""
                            busy = false
                            dismiss()
                        }
                    }
                    .disabled(secret.isEmpty || busy)
                }
            }
        }
        .interactiveDismissDisabled(busy)
        .onDisappear { secret = "" }
    }
}
