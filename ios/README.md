# SOLVIO Nexus for iOS

SwiftUI client for the SOLVIO Core. It provides chat, spoken conversations,
background-task results, connections, memory, and approvals.

`App/` contains the iPhone application. `Sources/SolvioApprovalsKit/` is a Swift
package for protocol parsing, request binding, and cryptography. `Tests/` includes
host, application, and navigation tests plus synthetic protocol vectors.
`DesignPrototype/` is a separate interface prototype with mock data.

## Build

Use macOS with Xcode supporting Swift 6, and install XcodeGen. The application
supports iOS 16 or later. From this directory:

```sh
xcodegen generate
open SolvioApprovals.xcodeproj
```

Choose your own signing team in Xcode before a physical-device build. The
repository does not include a signing identity or provisioning profile. If you
change the application bundle identifier, configure the matching App Attest
identity in your Core. Keep `project.yml` as the project configuration and
regenerate the project after changing it.

The Google Sign-In SDK is pinned to a remote Swift package in `project.yml`;
no vendored SDK binaries are included. Dependency resolution requires access
to those public repositories, or a previously populated local package cache.

Google connection is unconfigured by default. To enable it for your deployment,
set your own iOS and server OAuth client identifiers and callback scheme in
`App/GoogleSignInClient.swift`, and the same callback scheme in
`App/Info.plist` under `CFBundleURLSchemes`. Configure the corresponding server
client on the Core through its credential setup. Do not put client secrets,
tokens, or signing keys in this repository.

Pair with your own compatible Core using its enrollment QR code. Connection
settings and credentials are entered through the application; no paired device,
account, or gateway credentials are distributed here.

## Tests

The protocol package can be checked independently on a compatible host:

```sh
swift test
```

For application and navigation tests, use the `SolvioApprovalsApp` scheme in
Xcode with an iPhone Simulator. These tests do not establish a physical-device
biometric or voice acceptance result.

## Approval boundary

App Attest proves application/device integrity and binds authenticated requests.
It does not grant user authority. Risky decisions require a Secure Enclave key
whose use is gated by the current biometric set. The simulator signer is marked
non-attested and is not an approval substitute. Speech begins only through the
user's explicit conversation control.
