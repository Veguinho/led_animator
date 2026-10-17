import AppKit
import SwiftUI

struct FanSettings: Codable, Equatable {
    var fan0 = 100.0
    var fan1 = 100.0
    var automatic = false
    var ledOnly = true

    static let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/LED Fan Control", isDirectory: true)
    static let url = directory.appendingPathComponent("settings.plist")
    static func load() -> FanSettings {
        guard let data = try? Data(contentsOf: url),
              let settings = try? PropertyListDecoder().decode(FanSettings.self, from: data),
              settings.fan0.isFinite, settings.fan1.isFinite,
              (40...100).contains(settings.fan0), (40...100).contains(settings.fan1) else { return FanSettings() }
        return settings
    }
    func save() throws {
        try FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        let data = try PropertyListEncoder().encode(self)
        try data.write(to: Self.url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: Self.url.path)
    }
}

struct FanReading: Decodable {
    let id: Int
    let rpm: Double?
    let min: Double?
    let max: Double?
    let target: Double?
    let manual: Bool?
    var utilization: Double {
        guard let rpm, let max, max > 0 else { return 0 }
        return Swift.min(1, Swift.max(0, rpm / max))
    }
}
struct FanSnapshot: Decodable {
    let ledProcesses: Int
    let settings: FanSettings
    let fans: [FanReading]
    let temperatures: TemperatureReadings?
}
struct TemperatureReadings: Decodable {
    let cpu: Double?
    let gpu: Double?
    let battery: Double?
}

final class FanModel: ObservableObject {
    @Published var settings = FanSettings.load()
    @Published var snapshot: FanSnapshot?
    @Published var error: String?
    @Published var backendAvailable = false
    @Published var installing = false
    private var loading = false
    private var timer: Timer?
    private var pendingSave: DispatchWorkItem?
    static let helper = "/Library/PrivilegedHelperTools/local.led-animator.fans"

    init(poll: Bool = true) {
        if poll {
            refresh()
            timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
                guard NSApp.windows.contains(where: { $0.isVisible }) else { return }
                self?.refresh()
            }
        }
    }
    static func readSnapshot() throws -> FanSnapshot {
        let process = Process()
        guard FileManager.default.isExecutableFile(atPath: helper) else {
            throw NSError(domain: "FanControl", code: 1, userInfo: [NSLocalizedDescriptionKey: "Fan control needs its helper installed or updated."])
        }
        // Sensor reads need no privilege. Use the bundled reader so new sensor
        // displays can be installed without restarting the running fan daemon.
        process.executableURL = Bundle.main.url(forResource: "led-fan-watch", withExtension: nil) ?? URL(fileURLWithPath: helper)
        process.arguments = ["--json"]
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        try process.run()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if process.isRunning { process.terminate() }
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw NSError(domain: "FanControl", code: 1, userInfo: [NSLocalizedDescriptionKey: "Fan control needs its helper installed or updated."])
        }
        return try JSONDecoder().decode(FanSnapshot.self, from: data)
    }
    func refresh() {
        guard !loading else { return }
        loading = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Result { try Self.readSnapshot() }
            DispatchQueue.main.async {
                guard let self else { return }
                self.loading = false
                switch result {
                case .success(let reading):
                    self.snapshot = reading; self.backendAvailable = true
                    if self.error == "Fan control needs its helper installed or updated." { self.error = nil }
                case .failure:
                    self.backendAvailable = false
                    self.error = "Fan control needs its helper installed or updated."
                }
            }
        }
    }
    func save() {
        pendingSave?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            do { try self.settings.save(); self.error = nil }
            catch { self.error = "Could not save fan settings: \(error.localizedDescription)" }
        }
        pendingSave = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }
    func fullSpeed() {
        settings.fan0 = 100; settings.fan1 = 100; settings.automatic = false; save()
    }
    func automatic() { settings.automatic = true; save() }
    func setSpeed(_ id: Int, _ value: Double) {
        if id == 0 { settings.fan0 = value } else { settings.fan1 = value }
        settings.automatic = false; save()
    }
    func installHelper() {
        guard !installing, let installer = Bundle.main.url(forResource: "InstallHelper", withExtension: "sh") else { return }
        installing = true
        // NSAppleScript objects must be created and executed on the main thread.
        DispatchQueue.main.async { [weak self] in
            let command = "/bin/sh '" + installer.path.replacingOccurrences(of: "'", with: "'\\''") + "'"
            let escaped = command.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
            var details: NSDictionary?
            let script = NSAppleScript(source: "do shell script \"\(escaped)\" with administrator privileges")
            _ = script?.executeAndReturnError(&details)
            let message = details?[NSAppleScript.errorMessage] as? String
            self?.installing = false; self?.error = message
            self?.refresh()
        }
    }
    var status: String {
        guard backendAvailable, let snapshot else { return "Connecting to your fans…" }
        let released = snapshot.fans.count == 2 && snapshot.fans.allSatisfy { $0.manual == false }
        if settings.automatic { return released ? "Automatic · macOS controls both fans" : "Returning to automatic control…" }
        if settings.ledOnly && snapshot.ledProcesses <= 0 { return released ? "Ready · waiting for the LED app" : "Returning to automatic control…" }
        let applied = snapshot.settings == settings && snapshot.fans.count == 2 && snapshot.fans.allSatisfy {
            guard let target = $0.target, let minimum = $0.min, let maximum = $0.max else { return false }
            let percent = $0.id == 0 ? settings.fan0 : settings.fan1
            let expected = Swift.max(minimum, maximum * percent / 100)
            return $0.manual == true && abs(target - expected) < 2
        }
        return applied ? "Live · your fan settings are active" : "Applying fan settings…"
    }
}

private let accent = Color(red: 0.30, green: 0.82, blue: 0.96)
private func rpmText(_ value: Double?) -> String {
    guard let value else { return "—" }
    return value.formatted(.number.precision(.fractionLength(0)))
}

struct FanCard: View {
    @ObservedObject var model: FanModel
    let id: Int
    var reading: FanReading? { model.snapshot?.fans.first(where: { $0.id == id }) }
    var speed: Double { id == 0 ? model.settings.fan0 : model.settings.fan1 }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Image(systemName: "fanblades.fill").foregroundStyle(accent)
                Text("Fan \(id + 1)").font(.headline)
                Spacer()
                Text(reading?.manual == true ? "MANUAL" : "AUTO")
                    .font(.system(size: 10, weight: .bold, design: .rounded)).foregroundStyle(.secondary)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(rpmText(reading?.rpm)).font(.system(size: 36, weight: .semibold, design: .rounded)).monospacedDigit()
                Text("RPM").font(.caption).foregroundStyle(.secondary)
            }
            ProgressView(value: reading?.utilization ?? 0).tint(accent)
            Text("Maximum \(rpmText(reading?.max)) RPM").font(.caption).foregroundStyle(.secondary)
            Divider().overlay(.white.opacity(0.08))
            HStack {
                Text("Set speed").font(.subheadline)
                Spacer()
                Text("\(Int(speed))%").font(.system(.headline, design: .rounded)).foregroundStyle(accent).monospacedDigit()
            }
            Slider(value: Binding(get: { speed }, set: { model.setSpeed(id, $0) }), in: 40...100, step: 5)
                .tint(accent).disabled(model.settings.automatic || !model.backendAvailable)
            HStack {
                Text("40%"); Spacer(); Text("100%")
            }.font(.caption2).foregroundStyle(.secondary)
            Text("Target \(rpmText(reading?.target)) RPM").font(.caption).foregroundStyle(.secondary)
        }
        .padding(20).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(.white.opacity(0.08)))
    }
}

struct FanContent: View {
    @ObservedObject var model: FanModel
    var body: some View {
        VStack(alignment: .leading, spacing: 23) {
            HStack(spacing: 14) {
                Image(systemName: "fanblades.fill").font(.system(size: 32)).foregroundStyle(accent)
                    .frame(width: 62, height: 62).background(accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
                VStack(alignment: .leading, spacing: 5) {
                    Text("LED Fan Control").font(.system(size: 25, weight: .semibold, design: .rounded))
                    Text("Cooler mixing. Live fan speeds.").font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
            }
            HStack(spacing: 8) {
                Circle().fill(model.backendAvailable ? accent : Color.orange).frame(width: 7, height: 7)
                Text(model.status).font(.subheadline).foregroundStyle(.secondary)
                Spacer()
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("SYSTEM TEMPERATURES").font(.system(size: 10, weight: .semibold)).tracking(1).foregroundStyle(.secondary)
                HStack(spacing: 12) {
                    TemperatureCard(title: "CPU · PECI", value: model.snapshot?.temperatures?.cpu)
                    TemperatureCard(title: "GPU proximity", value: model.snapshot?.temperatures?.gpu)
                    TemperatureCard(title: "Battery", value: model.snapshot?.temperatures?.battery)
                }
            }
            HStack(spacing: 16) { FanCard(model: model, id: 0); FanCard(model: model, id: 1) }
            HStack(spacing: 12) {
                Button { model.fullSpeed() } label: {
                    Label("100% · Full speed", systemImage: "wind").frame(maxWidth: .infinity)
                }.buttonStyle(.borderedProminent).tint(accent).foregroundStyle(.black)
                Button { model.automatic() } label: {
                    Label("Automatic", systemImage: "sparkles").frame(maxWidth: .infinity)
                }.buttonStyle(.bordered)
            }.controlSize(.large).disabled(!model.backendAvailable)
            VStack(alignment: .leading, spacing: 8) {
                Toggle("Only while LED app runs", isOn: Binding(get: { model.settings.ledOnly }, set: { model.settings.ledOnly = $0; model.save() }))
                    .toggleStyle(.switch).tint(accent).disabled(!model.backendAvailable)
                Text(model.settings.ledOnly ? "When the LED app closes, both fans return to automatic control." : "Your fan settings stay active even when the LED app closes.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error = model.error {
                HStack(alignment: .top) {
                    Text(error).font(.caption).foregroundStyle(.orange)
                    Spacer()
                    if !model.backendAvailable {
                        Button(model.installing ? "Enabling…" : "Enable control…") { model.installHelper() }.disabled(model.installing)
                    }
                }
            } else {
                Text("Changes apply within a few seconds. Settings are saved automatically.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(28).frame(width: 640)
        .background(Color(red: 0.055, green: 0.068, blue: 0.09))
        .preferredColorScheme(.dark)
    }
}

struct TemperatureCard: View {
    let title: String
    let value: Double?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            if let value {
                Text(value.formatted(.number.precision(.fractionLength(1))) + " °C")
                    .font(.system(size: 22, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(accent)
            } else {
                Text("Unavailable").font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(14)
        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
        .help("Live sensor temperature in degrees Celsius")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { sender.windows.first?.makeKeyAndOrderFront(nil) }
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
struct FanControlApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = FanModel()
    var body: some Scene {
        WindowGroup("LED Fan Control") { FanContent(model: model) }
            .windowResizability(.contentSize)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}

func makeIcon(_ path: String) throws {
    let image = NSImage(size: NSSize(width: 1024, height: 1024))
    image.lockFocus()
    let background = NSBezierPath(roundedRect: NSRect(x: 40, y: 40, width: 944, height: 944), xRadius: 208, yRadius: 208)
    NSGradient(starting: NSColor(calibratedRed: 0.11, green: 0.17, blue: 0.23, alpha: 1), ending: NSColor(calibratedRed: 0.035, green: 0.06, blue: 0.09, alpha: 1))?.draw(in: background, angle: 270)
    let symbol = NSImage(systemSymbolName: "fanblades.fill", accessibilityDescription: nil)!
        .withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [NSColor(calibratedRed: 0.30, green: 0.82, blue: 0.96, alpha: 1)]))!
    symbol.draw(in: NSRect(x: 230, y: 230, width: 564, height: 564))
    image.unlockFocus()
    guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
    try png.write(to: URL(fileURLWithPath: path))
}

func verifyControls() throws {
    let original = FanSettings.load()
    defer { try? original.save() }
    func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw NSError(domain: "FanControlTest", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }
    let before = try FanModel.readSnapshot()
    try require(before.ledProcesses > 0, "Run the LED app before verifying live fan control.")
    var settings = original
    settings.fan0 = 85; settings.fan1 = 90; settings.automatic = false; settings.ledOnly = true
    try settings.save()
    Thread.sleep(forTimeInterval: 4)
    let changed = try FanModel.readSnapshot()
    for fan in changed.fans {
        let expected = (fan.max ?? 0) * (fan.id == 0 ? 0.85 : 0.90)
        try require(fan.manual == true && abs((fan.target ?? 0) - expected) < 2, "Fan \(fan.id + 1) did not apply its requested speed.")
    }
    settings.automatic = true; try settings.save()
    Thread.sleep(forTimeInterval: 4)
    let automatic = try FanModel.readSnapshot()
    try require(automatic.fans.count == 2 && automatic.fans.allSatisfy { $0.manual == false }, "Automatic control did not release both fans.")
    settings = FanSettings(); try settings.save()
    Thread.sleep(forTimeInterval: 4)
    let full = try FanModel.readSnapshot()
    try require(full.fans.count == 2 && full.fans.allSatisfy { $0.manual == true && abs(($0.target ?? 0) - ($0.max ?? 0)) < 2 }, "100% did not apply to both fans.")
    print("Live controls passed: independent 85% / 90% targets, automatic release, and both fans restored to 100%.")
}

func renderPreview(_ path: String) throws {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let model = FanModel(poll: false)
    model.snapshot = try FanModel.readSnapshot(); model.backendAvailable = true
    let host = NSHostingView(rootView: FanContent(model: model))
    host.setFrameSize(NSSize(width: 640, height: 760))
    let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 640, height: 760), styleMask: [.borderless], backing: .buffered, defer: false)
    window.contentView = host
    host.layoutSubtreeIfNeeded()
    RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw CocoaError(.fileWriteUnknown) }
    host.cacheDisplay(in: host.bounds, to: bitmap)
    guard let data = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
    try data.write(to: URL(fileURLWithPath: path))
}

if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--render-preview" {
    try renderPreview(CommandLine.arguments[2])
} else if CommandLine.arguments.contains("--verify-controls") {
    try verifyControls()
} else if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--icon" {
    _ = NSApplication.shared
    try makeIcon(CommandLine.arguments[2])
} else if CommandLine.arguments.contains("--self-test") {
    let settings = FanSettings()
    precondition(settings.fan0 == 100 && settings.fan1 == 100 && !settings.automatic && settings.ledOnly)
    let encoded = try PropertyListEncoder().encode(settings)
    let decoded = try PropertyListDecoder().decode(FanSettings.self, from: encoded)
    precondition(decoded == settings)
    let sample = Data("{\"ledProcesses\":1,\"settings\":{\"fan0\":100,\"fan1\":100,\"automatic\":false,\"ledOnly\":true},\"fans\":[{\"id\":0,\"rpm\":5940,\"min\":2160,\"max\":5927,\"target\":5927,\"manual\":true}]}".utf8)
    let snapshot = try JSONDecoder().decode(FanSnapshot.self, from: sample)
    precondition(snapshot.fans[0].utilization == 1 && snapshot.settings == settings)
    precondition(snapshot.temperatures == nil)
    let thermalSample = Data("{\"ledProcesses\":1,\"settings\":{\"fan0\":100,\"fan1\":100,\"automatic\":false,\"ledOnly\":true},\"fans\":[],\"temperatures\":{\"cpu\":67.3,\"gpu\":61.0,\"battery\":null}}".utf8)
    let thermal = try JSONDecoder().decode(FanSnapshot.self, from: thermalSample)
    precondition(thermal.temperatures?.cpu == 67.3 && thermal.temperatures?.gpu == 61 && thermal.temperatures?.battery == nil)
    print("App tests passed: 100% defaults, settings round-trip, fan and temperature decoding, and unavailable sensors.")
} else {
    FanControlApp.main()
}
