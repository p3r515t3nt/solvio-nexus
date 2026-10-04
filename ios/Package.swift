// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "SolvioApprovalsKit",
    platforms: [.macOS(.v13), .iOS(.v16)],
    products: [
        .library(name: "SolvioApprovalsKit", targets: ["SolvioApprovalsKit"]),
    ],
    targets: [
        .target(name: "SolvioApprovalsKit", path: "Sources/SolvioApprovalsKit"),
        .testTarget(
            name: "SolvioApprovalsKitTests",
            dependencies: ["SolvioApprovalsKit"],
            path: "Tests/SolvioApprovalsKitTests",
            resources: [.copy("Vectors/mobile_approval_v2.json"), .copy("Vectors/app_attest_binding_v1.json"),
                        .copy("Vectors/app_task_start_v1.json"), .copy("Vectors/app_conversation_message_v1.json")]
        ),
    ]
)
