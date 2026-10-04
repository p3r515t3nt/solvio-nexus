import Foundation
import GoogleSignIn
import UIKit

/// Ephemeral hand-off to the pinned Core, never an app login or SOLVIO authority.
/// No Codable conformance: this value must not enter ordinary model/state storage.
@MainActor
final class GoogleAuthorizationCode: CustomStringConvertible, CustomDebugStringConvertible {
    private var code: String?
    let accountID: String
    let accountEmail: String

    init(code: String, accountID: String, accountEmail: String, scopes: [String]) throws {
        guard !code.isEmpty, code.utf8.count <= 8192,
              !accountID.isEmpty, accountID.utf8.count <= 256,
              !accountEmail.isEmpty, accountEmail.utf8.count <= 320,
              [code, accountID, accountEmail].allSatisfy({ value in
                  !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
              }), Set(GoogleSignInClient.scopes).isSubset(of: Set(scopes)) else {
            throw GoogleSignInFailure.incomplete
        }
        self.code = code
        self.accountID = accountID
        self.accountEmail = accountEmail
    }

    func takeCode() throws -> String {
        guard let value = code else { throw GoogleSignInFailure.expired }
        code = nil
        return value
    }

    func discard() { code = nil }
    nonisolated var description: String { "<GoogleAuthorizationCode redacted>" }
    nonisolated var debugDescription: String { description }
}

enum GoogleSignInFailure: Error, LocalizedError {
    case unavailable, busy, cancelled, incomplete, expired
    var errorDescription: String? {
        switch self {
        case .unavailable: return "Die Google-Anmeldung ist noch nicht eingerichtet."
        case .busy: return "Eine Google-Anmeldung ist bereits geöffnet."
        case .cancelled: return "Google-Anmeldung abgebrochen. Dein bisheriger Zugang bleibt erhalten."
        case .incomplete: return "Google hat keinen vollständigen Zugang für Gmail und Kalender bestätigt."
        case .expired: return "Diese Anmeldung ist nicht mehr verwendbar. Bitte melde dich erneut bei Google an."
        }
    }
}

/// Only invoked by a deliberate Connect action once the Core supports activation.
/// The official SDK owns browser/PKCE/state/callback validation. Face ID and
/// activation remain separate Core operations; obtaining a code grants neither.
@MainActor
final class GoogleSignInClient {
    static let clientID = ""
    static let serverClientID = ""
    static let callbackScheme = ""
    static let scopes = ["https://www.googleapis.com/auth/calendar.events",
                         "https://www.googleapis.com/auth/gmail.readonly",
                         "https://www.googleapis.com/auth/gmail.compose"]
    private var signingIn = false

    func authorize(presenting controller: UIViewController) async throws -> GoogleAuthorizationCode {
        guard !signingIn else { throw GoogleSignInFailure.busy }
        let types = Bundle.main.object(forInfoDictionaryKey: "CFBundleURLTypes") as? [[String: Any]] ?? []
        guard types.contains(where: { ($0["CFBundleURLSchemes"] as? [String])?.contains(Self.callbackScheme) == true }) else {
            throw GoogleSignInFailure.unavailable
        }
        signingIn = true
        let sdk = GIDSignIn.sharedInstance
        sdk.configuration = GIDConfiguration(clientID: Self.clientID, serverClientID: Self.serverClientID)
        // No automatic restoration or app-side use of provider access tokens.
        sdk.signOut()
        defer { sdk.signOut(); signingIn = false }
        let result: GIDSignInResult
        do {
            result = try await sdk.signIn(withPresenting: controller, hint: nil, additionalScopes: Self.scopes)
        } catch {
            // SDK error descriptions can contain account/provider details.
            let error = error as NSError
            if error.domain == kGIDSignInErrorDomain && error.code == -5 { throw GoogleSignInFailure.cancelled }
            throw GoogleSignInFailure.incomplete
        }
        try Task.checkCancellation()
        return try GoogleAuthorizationCode(code: result.serverAuthCode ?? "",
                                           accountID: result.user.userID ?? "",
                                           accountEmail: result.user.profile?.email ?? "",
                                           scopes: result.user.grantedScopes ?? [])
    }
}
