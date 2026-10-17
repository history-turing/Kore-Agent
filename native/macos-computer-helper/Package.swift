// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KoreAgentNextComputerHelper",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "KoreAgentNextComputerCore", targets: ["KoreAgentNextComputerCore"]),
        .executable(name: "koreagent-next-computer-helper", targets: ["KoreAgentNextComputerHelper"]),
    ],
    targets: [
        .target(name: "KoreAgentNextComputerCore"),
        .executableTarget(name: "KoreAgentNextComputerHelper", dependencies: ["KoreAgentNextComputerCore"]),
        .testTarget(name: "KoreAgentNextComputerCoreTests", dependencies: ["KoreAgentNextComputerCore"]),
    ]
)

