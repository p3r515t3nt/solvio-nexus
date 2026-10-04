// The existing voice socket confirms when the shared Core history is durable.
// Local microphone shutdown and a network close are not that confirmation.
import Foundation

struct VoiceHandoff {
    enum State: Equatable { case idle, starting, ready, ending, complete, unknown, acceptedIncomplete }
    private(set) var state: State = .idle
    private(set) var conversationID: String?
    private(set) var sessionID: String?
    // Transient recovery truth, retained across reconnects and selection changes.
    private(set) var uncertainConversations: Set<String> = []

    var blocksText: Bool { state != .idle && state != .complete && state != .acceptedIncomplete }

    mutating func start(conversationID: String?) {
        if blocksText, let previous = self.conversationID { uncertainConversations.insert(previous) }
        self.conversationID = conversationID; sessionID = nil; state = .starting
    }
    mutating func bind(sessionID: String, conversationID: String, version: Int) {
        guard state == .starting, version == 1, !sessionID.isEmpty, sessionID.count <= 128,
              ConversationSummary.validID(conversationID), self.conversationID == conversationID else { return }
        self.sessionID = sessionID; state = .ready
    }
    mutating func ending() {
        if state == .ready { state = .ending }
        else if state == .starting { fail() }
    }
    @discardableResult
    mutating func accept(sessionID: String, conversationID: String, status: String, providerClosed: Bool) -> Bool {
        guard [.ready, .ending].contains(state), self.sessionID == sessionID,
              self.conversationID == conversationID else { return false }
        if status == "complete", providerClosed, !uncertainConversations.contains(conversationID) { state = .complete }
        else { fail() }
        return true
    }
    @discardableResult
    mutating func acceptStoredHistory(conversationID: String) -> Bool {
        guard hasUncertainty(in: conversationID) else { return false }
        uncertainConversations.remove(conversationID)
        if self.conversationID == conversationID { state = .acceptedIncomplete }
        return true
    }
    func hasUncertainty(in conversationID: String) -> Bool {
        uncertainConversations.contains(conversationID) || (self.conversationID == conversationID && state == .unknown)
    }
    func blocksText(in conversationID: String) -> Bool {
        uncertainConversations.contains(conversationID) || (self.conversationID == conversationID && blocksText)
    }
    mutating func fail() {
        if blocksText {
            if let conversationID { uncertainConversations.insert(conversationID) }
            state = .unknown
        }
    }
}

struct VoiceHandoffError: LocalizedError {
    var errorDescription: String? {
        "Mikrofon aus. Der Abschluss des Sprachverlaufs ist nicht bestätigt. Deine Nachricht bleibt als Entwurf erhalten."
    }
}
