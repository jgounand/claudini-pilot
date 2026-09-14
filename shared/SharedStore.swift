// The folder the menu bar app and its widget share.
//
// A widget is sandboxed: it cannot run the engine, read the keychain or touch
// ~/.claudini. So the app does all of that, as it always has, and leaves two
// kinds of file in an App Group container the widget is allowed into:
//
//   snapshot.json   the engine's latest answer, verbatim — the widget draws it
//   requests/       what a widget button asked for — the app carries it out
//
// Both sides are signed by the same team, which is what lets them share it.

import Foundation

enum SharedStore {
    /// "<team>.com.github.claudini-pilot", filled into Info.plist at build
    /// time. An ad-hoc build has no team, no widget and nothing to share.
    static let group: String? = {
        guard let id = Bundle.main.object(forInfoDictionaryKey: "ClaudiniAppGroup") as? String,
              !id.isEmpty, !id.hasPrefix("."), !id.contains("$(") else { return nil }
        return id
    }()

    static var folder: URL? {
        group.flatMap { FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) }
    }

    static var snapshotFile: URL? { folder?.appendingPathComponent("snapshot.json") }
    static var requestsFolder: URL? {
        folder?.appendingPathComponent("requests", isDirectory: true)
    }

    /// The engine's answer exactly as it came, so the widget decodes the same
    /// contract the menu does and cannot drift from it.
    static func write(_ data: Data) {
        guard let url = snapshotFile else { return }
        try? data.write(to: url, options: .atomic)
    }

    /// The latest snapshot, and when the app wrote it — the widget says so
    /// once that is old enough to mean the app is not running.
    static func read() -> (snapshot: Snapshot, writtenAt: Date)? {
        guard let url = snapshotFile, let data = try? Data(contentsOf: url),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
        let writtenAt = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
            .contentModificationDate ?? .distantPast
        return (snapshot, writtenAt)
    }

    /// Flip one account's switch in the stored snapshot, ahead of the app.
    ///
    /// WidgetKit redraws a widget the moment its button's intent returns, from
    /// whatever the snapshot says then. Without this the switch you just
    /// flipped would spring back until the app's answer arrived.
    static func markDisabled(_ name: String, _ disabled: Bool) {
        guard let url = snapshotFile, let data = try? Data(contentsOf: url),
              var root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              var profiles = root["profiles"] as? [[String: Any]] else { return }
        for index in profiles.indices where profiles[index]["name"] as? String == name {
            profiles[index]["disabled"] = disabled
        }
        root["profiles"] = profiles
        if let out = try? JSONSerialization.data(withJSONObject: root) {
            try? out.write(to: url, options: .atomic)
        }
    }
}

/// Something a widget button asked for. A closed list rather than engine
/// arguments: anything able to write into the folder could otherwise make the
/// app run whatever it liked.
struct Request: Codable {
    enum Kind: String, Codable { case on, off, refresh }
    let kind: Kind
    let name: String?

    /// One file per request, named so they sort in the order they were made.
    func send() throws {
        guard let folder = SharedStore.requestsFolder else { return }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let stamp = String(format: "%.6f", Date().timeIntervalSince1970)
        try JSONEncoder().encode(self)
            .write(to: folder.appendingPathComponent("\(stamp)-\(UUID().uuidString).json"),
                   options: .atomic)
    }

    /// Every pending request, oldest first, each removed as it is taken — so
    /// one left while the app was not running is carried out when it starts.
    static func drain() -> [Request] {
        guard let folder = SharedStore.requestsFolder,
              let names = try? FileManager.default.contentsOfDirectory(atPath: folder.path)
        else { return [] }
        return names.filter { $0.hasSuffix(".json") }.sorted().compactMap { name in
            let url = folder.appendingPathComponent(name)
            defer { try? FileManager.default.removeItem(at: url) }
            return (try? Data(contentsOf: url)).flatMap { try? JSONDecoder().decode(Request.self, from: $0) }
        }
    }
}
