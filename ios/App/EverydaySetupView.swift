import SwiftUI

struct EverydaySetupView: View {
    @ObservedObject var model: AppModel
    @State private var time = Calendar.current.date(from: DateComponents(hour: 8)) ?? Date()
    @State private var working = false
    @State private var notice = ""
    var body: some View {
        Form {
            Section("Täglicher Überblick") {
                Text("Termine heute, bis zu zehn ungelesene Gmail-Mails und offene eigene SOLVIO-Aufträge. Fehlende Abfragen werden ausdrücklich genannt.")
                DatePicker("Jeden Tag um", selection: $time, displayedComponents: .hourAndMinute)
                    .environment(\.timeZone, TimeZone(identifier: "Europe/Berlin")!)
                Text("Uhrzeit in Deutschland. Das Ergebnis erscheint unter Hinweise. Mitteilungen bei geschlossener App benötigen die separate Apple-Einrichtung.")
                    .font(.footnote)
                Button("Mit Face ID einrichten") { Task { await submit() } }
                    .disabled(working || model.client == nil)
            }
            if !notice.isEmpty { Section { Text(notice) } }
            Section { NavigationLink("Freigaben öffnen") { ApprovalsView(model: model) } }
        }.navigationTitle("Tagesüberblick")
    }
    private func submit() async {
        guard let client = model.client, !working else { return }
        working = true; defer { working = false }
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(identifier: "Europe/Berlin")!
        let parts = calendar.dateComponents([.hour, .minute], from: time)
        let clock = String(format: "%02d:%02d", parts.hour ?? 8, parts.minute ?? 0)
        do {
            let result = try await client.everydaySchedule([
                "titel": "Täglicher Überblick", "wann": "täglich " + clock,
                "aktion": "tagesueberblick", "argumente": [String: String]()])
            notice = result.request_id != nil ? "Bitte öffne die Freigaben und bestätige den Umfang mit Face ID. Erst danach wird der Überblick eingerichtet." :
                result.ok ? "Der Überblick ist eingerichtet." : "Die Einrichtung wurde nicht bestätigt."
            await model.refreshAll()
        } catch { notice = "Die Einrichtung ist noch nicht bestätigt. Bitte sieh bei den Freigaben nach, bevor du es erneut versuchst." }
    }
}

struct FollowupMail: Decodable, Identifiable, Equatable {
    let id: String
    let thread_id: String
    let subject: String
    let date: String
}
struct FollowupMailResults: Decodable { let messages: [FollowupMail] }

struct MailFollowupSetupView: View {
    @ObservedObject var model: AppModel
    @State private var query = ""
    @State private var messages: [FollowupMail] = []
    @State private var selected: FollowupMail?
    @State private var deadline = Date().addingTimeInterval(86400)
    @State private var working = false
    @State private var notice = ""
    var body: some View {
        Form {
            Section("Gesendete Gmail-Mail auswählen") {
                TextField("Empfänger oder Betreff", text: $query)
                Button("Gesendete Mails suchen") { Task { await search() } }
                    .disabled(working || query.trimmingCharacters(in: .whitespacesAndNewlines).count < 2 || model.client == nil)
                ForEach(messages) { message in
                    Button {
                        selected = message; notice = ""
                    } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(message.subject.isEmpty ? "Ohne Betreff" : message.subject)
                                Text(message.date).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if selected?.id == message.id { Image(systemName: "checkmark.circle.fill") }
                        }
                    }.disabled(working)
                }
            }
            if let selected {
                Section("Einmalig nachsehen") {
                    Text(selected.subject.isEmpty ? "Ausgewählte Mail" : selected.subject)
                    DatePicker("Prüfen am", selection: $deadline, in: Date()..., displayedComponents: [.date, .hourAndMinute])
                        .environment(\.timeZone, TimeZone(identifier: "Europe/Berlin")!)
                    Text("Uhrzeit in Deutschland. Gibt es im ausgewählten Mailverlauf keine spätere eingegangene Nachricht, erinnert SOLVIO dich unter Hinweise. Antworten in anderen Verläufen sind nicht erfasst. Es wird nichts versendet.")
                        .font(.footnote)
                    Button("Mit Face ID einrichten") { Task { await submit(selected) } }
                        .disabled(working || model.client == nil)
                }
            }
            if !notice.isEmpty { Section { Text(notice) } }
            Section { NavigationLink("Freigaben öffnen") { ApprovalsView(model: model) } }
        }.navigationTitle("An Antwort erinnern")
    }
    private func search() async {
        guard let client = model.client, !working else { return }
        working = true; defer { working = false }
        selected = nil; messages = []; notice = ""
        do {
            messages = try await client.followupMailSearch(query).messages
            if messages.isEmpty { notice = "Keine passende gesendete Mail gefunden. Bitte präzisiere die Suche." }
        } catch { notice = "Die Suche ist nicht bestätigt. Bitte prüfe die Verbindung." }
    }
    private func submit(_ mail: FollowupMail) async {
        guard let client = model.client, !working else { return }
        working = true; defer { working = false }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Europe/Berlin"); formatter.dateFormat = "yyyy-MM-dd HH:mm"
        do {
            let result = try await client.everydaySchedule([
                "titel": "Antwort prüfen: " + String(mail.subject.prefix(90)), "wann": formatter.string(from: deadline),
                "aktion": "mail_antwort_pruefen", "argumente": ["thread_id": mail.thread_id, "message_id": mail.id]])
            notice = result.request_id != nil ? "Bitte bestätige den ausgewählten Verlauf und Prüfzeitpunkt unter Freigaben mit Face ID." :
                result.ok ? "Die Prüfung ist eingerichtet." : "Die Einrichtung wurde nicht bestätigt."
            await model.refreshAll()
        } catch { notice = "Die Einrichtung ist noch nicht bestätigt. Bitte sieh zuerst bei den Freigaben nach, bevor du es erneut versuchst." }
    }
}
