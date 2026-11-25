// The panel that opens from the menu bar: the fleet at a glance, the account
// in use, and every other account as a compact table grouped by workspace.
//
// It replaced a native menu. With twenty-odd accounts the menu ran off the
// screen, a row kept its highlight after its switch was clicked, and each row
// showed only one figure — the 5-hour window on one line, the weekly one on the
// next — so nothing could be compared. Here every account has the same columns,
// the list scrolls inside a bounded panel, and it can be filtered.
//
// The controls are drawn (capsules and buttons) rather than AppKit-backed, so
// `ClaudiniBar --menu-preview` can render the panel to a picture exactly as it
// looks, which is how its layout is checked.

import AppKit
import SwiftUI

/// What the panel shows, and what its controls ask the app to do. The app owns
/// the engine; the panel only calls these.
final class PanelModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var loadedAt = Date.distantPast
    @Published var busy = false
    @Published var banner: Banner?
    /// Drawn into a picture: no scroll view, no menus — nothing AppKit-backed.
    var preview = false

    var onSwitch: (String) -> Void = { _ in }
    var onRotation: (String, Bool) -> Void = { _, _ in }
    var onReconnect: (String) -> Void = { _ in }
    var onRefresh: () -> Void = {}
    var onAuto: (Bool) -> Void = { _ in }
    var onMode: (String) -> Void = { _ in }
    var onDrain: (Bool) -> Void = { _ in }
    var onSet: (String, Int) -> Void = { _, _ in }
    var onRename: (String, String) -> Void = { _, _ in }
    var onRemove: (String) -> Void = { _ in }
    var onAdd: () -> Void = {}
    var onHistory: () -> Void = {}
    var onSettings: () -> Void = {}
    var onQuit: () -> Void = {}
    var onMenuBarStyle: () -> Void = {}

    /// The line an account counts as spent at.
    var line: Int { snapshot?.settings?.max_usage ?? 95 }

    func show(_ text: String, error: Bool = false) {
        let banner = Banner(text: text, error: error)
        self.banner = banner
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in
            if self?.banner == banner { self?.banner = nil }
        }
    }
}

struct Banner: Equatable {
    let id = UUID()
    let text: String
    let error: Bool
}

// MARK: - reading an account

enum AccountState {
    case ready, spent, off, attention

    var tint: Color {
        switch self {
        case .ready: return .green
        case .spent: return .red
        case .off: return .secondary
        case .attention: return .orange
        }
    }
}

extension Profile {
    /// A general window by its short label: "5h" or "7d".
    func window(_ short: String) -> Limit? {
        limits.first { $0.general && $0.short_label == short }
    }

    var modelLimit: Limit? { limits.first { $0.preferred } }

    func state(line: Int) -> AccountState {
        if disabled { return .off }
        guard let binding else { return .attention }
        return binding.percent >= line ? .spent : .ready
    }

    /// When a spent account comes back: the binding window's reset.
    func backIn(line: Int, now: Date) -> String {
        guard state(line: line) == .spent, let epoch = binding?.resets_at_epoch else { return "" }
        let mins = max(0, epoch - Int(now.timeIntervalSince1970)) / 60
        if mins < 60 { return "\(max(1, mins))m" }
        if mins < 1440 { return "\(mins / 60)h" + (mins % 60 >= 10 ? String(format: "%02d", mins % 60) : "") }
        return "\(mins / 1440)d"
    }
}

/// Accounts that share a workspace, in the engine's order (best first).
struct AccountGroup: Identifiable {
    let id: String
    let title: String
    let profiles: [Profile]
    /// How much of every name in the group is the same, so the part that tells
    /// them apart can stand out: "agent-" and "-work" fade, "009" does not.
    let sharedPrefix: Int
    let sharedSuffix: Int
}

func accountGroups(_ profiles: [Profile]) -> [AccountGroup] {
    var order: [String] = []
    var buckets: [String: [Profile]] = [:]
    for profile in profiles {
        // An account that cannot be read has no workspace worth grouping on.
        let key = profile.binding == nil && !profile.disabled ? "~attention"
                : (profile.space ?? "~attention")
        if buckets[key] == nil { order.append(key) }
        buckets[key, default: []].append(profile)
    }
    // Accounts that need a login go last, whatever the engine's order.
    order = order.filter { $0 != "~attention" } + order.filter { $0 == "~attention" }
    return order.map { key in
        let members = buckets[key] ?? []
        let names = members.map(\.name)
        let title = key == "~attention" ? "Needs a login"
                  : key == "personal" ? "Personal" : key
        return AccountGroup(id: key, title: title, profiles: members,
                            sharedPrefix: sharedEdge(names, fromEnd: false),
                            sharedSuffix: sharedEdge(names, fromEnd: true))
    }
}

/// The length of the start (or end) every name shares, cut back to a
/// separator so a fade never splits "009" into "00" and "9".
func sharedEdge(_ names: [String], fromEnd: Bool) -> Int {
    guard names.count > 1 else { return 0 }
    let words = names.map { fromEnd ? Array($0.reversed()) : Array($0) }
    let shortest = words.map(\.count).min() ?? 0
    var length = 0
    while length < shortest, words.allSatisfy({ $0[length] == words[0][length] }) { length += 1 }
    let separators: Set<Character> = ["-", "_", ".", " "]
    // Keep the separator on the faded side.
    while length > 0, !separators.contains(words[0][length - 1]) { length -= 1 }
    // Never fade a whole name.
    return length < shortest ? length : 0
}

// MARK: - drawn controls

/// A switch drawn from shapes, so it renders in previews as it looks live.
struct DrawnSwitch: View {
    let on: Bool
    var tint: Color = .accentColor
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Capsule()
                .fill(on ? tint : Color.primary.opacity(0.18))
                .frame(width: 26, height: 15)
                .overlay(alignment: on ? .trailing : .leading) {
                    Circle().fill(.white).padding(1.5)
                        .shadow(color: .black.opacity(0.18), radius: 0.6, y: 0.5)
                }
                .animation(.easeOut(duration: 0.15), value: on)
        }
        .buttonStyle(.plain)
    }
}

/// A small capsule button: icon and/or title.
struct Chip: View {
    var icon: String? = nil
    var title: String? = nil
    var selected = false
    var tint: Color = .accentColor
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if let icon { Image(systemName: icon).font(.system(size: 10, weight: .semibold)) }
                if let title { Text(title).font(.system(size: 11, weight: .medium)) }
            }
            .padding(.horizontal, title == nil ? 6 : 9)
            .frame(height: 22)
            .foregroundStyle(selected ? Color.white : Color.primary.opacity(0.8))
            .background(Capsule().fill(selected ? tint : Color.primary.opacity(0.07)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// A bar and its figure, small enough for a table cell.
struct MiniMeter: View {
    let limit: Limit?
    var muted = false

    var body: some View {
        HStack(spacing: 5) {
            if let limit {
                Gauge(percent: limit.percent, level: limit.level, height: 4, muted: muted)
                    .frame(width: 20)
                Text("\(limit.percent)%")
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(muted ? .tertiary : .secondary)
                    .fixedSize()
                    .frame(width: 35, alignment: .trailing)
            } else {
                Spacer(minLength: 0)
                Text("—").font(.system(size: 11)).foregroundStyle(.quaternary)
            }
        }
    }
}

// MARK: - the panel

enum Column {
    static let window: CGFloat = 60
    static let model: CGFloat = 34
    static let back: CGFloat = 40
    static let toggle: CGFloat = 26
}

enum Filter: String, CaseIterable {
    case all, ready, spent, off, attention

    var title: String {
        switch self {
        case .all: return "All"
        case .ready: return "Ready"
        case .spent: return "Spent"
        case .off: return "Off"
        case .attention: return "Login"
        }
    }

    func admits(_ state: AccountState) -> Bool {
        switch self {
        case .all: return true
        case .ready: return state == .ready
        case .spent: return state == .spent
        case .off: return state == .off
        case .attention: return state == .attention
        }
    }
}

let panelWidth: CGFloat = 432
let listMaxHeight: CGFloat = 318

struct PanelView: View {
    @ObservedObject var model: PanelModel
    @AppStorage("panel.filter") private var filterName = Filter.all.rawValue
    @AppStorage("panel.collapsed") private var collapsedList = ""

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            VStack(spacing: 0) {
                header
                if let snap = model.snapshot {
                    content(snap, now: context.date)
                } else {
                    Text("Reading your accounts…")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 120)
                }
                Divider()
                footer(now: context.date)
            }
            .frame(width: panelWidth)
            .overlay(alignment: .top) { bannerView }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "gauge.with.dots.needle.67percent")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.accentColor)
            Text("claudini-pilot").font(.system(size: 14, weight: .semibold))
            Spacer()
            if let snap = model.snapshot {
                modeControl(snap)
                HStack(spacing: 6) {
                    Text("Auto").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                    DrawnSwitch(on: snap.auto, tint: .green) { model.onAuto(!snap.auto) }
                }
                .help(snap.auto ? "Auto-switching is on" : "Auto-switching is off")
            }
            Chip(icon: "arrow.clockwise") { model.onRefresh() }
                .rotationEffect(.degrees(model.busy ? 180 : 0))
                .animation(model.busy ? .linear(duration: 0.6).repeatForever(autoreverses: false)
                                      : .default, value: model.busy)
                .help("Re-read every account now")
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 10)
    }

    @ViewBuilder
    private func modeControl(_ snap: Snapshot) -> some View {
        let label = snap.mode.prefix(1).uppercased() + snap.mode.dropFirst()
        if model.preview {
            Chip(icon: "scope", title: label) {}
        } else {
            Menu {
                ForEach(snap.modes, id: \.name) { mode in
                    Button { model.onMode(mode.name) } label: {
                        if mode.name == snap.mode { Label(mode.title, systemImage: "checkmark") }
                        else { Text(mode.title) }
                    }
                }
            } label: {
                Label(label, systemImage: "scope").font(.system(size: 11, weight: .medium))
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("What auto-switching optimises for")
        }
    }

    // MARK: content

    @ViewBuilder
    private func content(_ snap: Snapshot, now: Date) -> some View {
        tiles(snap, now: now)
            .padding(.horizontal, 14)
            .padding(.bottom, 12)
        if let active = snap.profiles.first(where: { $0.active }) {
            activeCard(active, next: snap.next, now: now)
                .padding(.horizontal, 14)
                .padding(.bottom, 12)
        }
        table(snap, now: now)
    }

    private func tiles(_ snap: Snapshot, now: Date) -> some View {
        let summary = snap.summary
        let readable = snap.profiles.filter { $0.binding != nil && !$0.disabled }
        let attention = snap.profiles.filter { $0.binding == nil && !$0.disabled }.count
        let usable = summary?.usable ?? readable.filter { $0.state(line: model.line) == .ready }.count
        let total = summary.map { $0.total - $0.off } ?? readable.count
        let reserve = Double(summary?.weekly_reserve ?? 0) / 100
        let freeAt = summary?.next_free?.at_epoch
        return HStack(spacing: 8) {
            Tile(title: "Ready", value: "\(usable)", unit: "/\(total)",
                 detail: summary?.draining == true ? "draining the rest" : "accounts",
                 tint: usable == 0 ? (summary?.draining == true ? .orange : .red) : .green)
            Tile(title: "Weekly left", value: String(format: "%.1f", reserve), unit: "",
                 detail: "accounts' worth", tint: reserve < 1 ? .orange : .accentColor)
            Tile(title: "Next back", value: freeAt.map { countdown(epoch: $0, now: now) } ?? "—",
                 unit: "", detail: summary?.next_free?.name ?? "none waiting", tint: .primary)
            Tile(title: "Login", value: "\(attention)", unit: "",
                 detail: attention == 0 ? "all signed in" : attention == 1 ? "needs it" : "need it",
                 tint: attention > 0 ? .orange : .secondary)
        }
    }

    private func activeCard(_ profile: Profile, next: NextUp, now: Date) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle().fill(profile.state(line: model.line).tint).frame(width: 8, height: 8)
                Text(profile.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Text(planLine(profile)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                Text("IN USE").font(.system(size: 9, weight: .bold)).kerning(0.5)
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, 6).frame(height: 16)
                    .background(Capsule().fill(Color.accentColor.opacity(0.14)))
            }
            if profile.limits.isEmpty {
                Text(profile.needs_login ? "\(profile.detail) — log in again" : profile.detail)
                    .font(.system(size: 11)).foregroundStyle(.orange)
            } else {
                ForEach(Array(profile.limits.enumerated()), id: \.offset) { _, limit in
                    LimitRow(limit: limit, now: now, muted: profile.dimmed)
                }
            }
            Divider().opacity(0.5)
            HStack(spacing: 6) {
                Image(systemName: next.staying ? "pin.fill" : "arrow.right.circle.fill")
                    .font(.system(size: 10))
                    .foregroundStyle(next.staying ? Color.secondary : Color.green)
                Group {
                    if next.staying {
                        Text("Staying") + Text(next.after.map { " · then \($0)" } ?? "")
                            .foregroundColor(.secondary)
                    } else {
                        Text("Next session: \(next.name ?? "—")") + Text(" · \(next.reason)")
                            .foregroundColor(.secondary)
                    }
                }
                .font(.system(size: 11)).lineLimit(1)
                Spacer(minLength: 0)
            }
            if let blocked = next.blocked_by {
                Text("Not switching: \(blocked)").font(.system(size: 10)).foregroundStyle(.tertiary)
                    .lineLimit(2)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .fill(Color.primary.opacity(0.045)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(Color.primary.opacity(0.07)))
    }

    // MARK: table

    private var filter: Filter { Filter(rawValue: filterName) ?? .all }
    private var collapsed: Set<String> { Set(collapsedList.split(separator: "|").map(String.init)) }

    private func toggleGroup(_ id: String) {
        var set = collapsed
        if set.contains(id) { set.remove(id) } else { set.insert(id) }
        collapsedList = set.sorted().joined(separator: "|")
    }

    @ViewBuilder
    private func table(_ snap: Snapshot, now: Date) -> some View {
        let others = snap.profiles.filter { !$0.active }
        let line = model.line
        let counts = Dictionary(grouping: others) { $0.state(line: line) }.mapValues(\.count)
        let shown = others.filter { filter.admits($0.state(line: line)) }
        let groups = accountGroups(shown)

        HStack(spacing: 5) {
            ForEach(Filter.allCases, id: \.self) { option in
                let count = option == .all ? others.count : counts[stateFor(option)] ?? 0
                if option == .all || count > 0 || option == filter {
                    Chip(title: "\(option.title) \(count)", selected: option == filter) {
                        filterName = option.rawValue
                    }
                }
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 8)

        columnHeader(snap)
            .padding(.horizontal, 14)

        let rows = rowsView(groups, now: now)
        let height = estimatedHeight(groups)
        Group {
            if model.preview {
                rows.frame(height: min(height, listMaxHeight), alignment: .top).clipped()
            } else {
                ScrollView(.vertical) { rows }
                    .frame(height: min(height, listMaxHeight))
                    .overlay(alignment: .bottom) {
                        if height > listMaxHeight {
                            LinearGradient(colors: [.clear, Color(nsColor: .windowBackgroundColor).opacity(0.9)],
                                           startPoint: .top, endPoint: .bottom)
                                .frame(height: 18).allowsHitTesting(false)
                        }
                    }
            }
        }
        .padding(.bottom, 6)
    }

    private func stateFor(_ filter: Filter) -> AccountState {
        switch filter {
        case .all, .ready: return .ready
        case .spent: return .spent
        case .off: return .off
        case .attention: return .attention
        }
    }

    private func columnHeader(_ snap: Snapshot) -> some View {
        HStack(spacing: 8) {
            Text("ACCOUNT").frame(maxWidth: .infinity, alignment: .leading).padding(.leading, 16)
            Text("5H").frame(width: Column.window, alignment: .trailing)
            Text("WEEK").frame(width: Column.window, alignment: .trailing)
            Text((snap.settings?.preferred_model ?? "Model").uppercased())
                .frame(width: Column.model, alignment: .trailing)
            Text("BACK").frame(width: Column.back, alignment: .trailing)
            Color.clear.frame(width: Column.toggle, height: 1)
        }
        .font(.system(size: 9, weight: .semibold))
        .kerning(0.4)
        .foregroundStyle(.tertiary)
        .padding(.horizontal, 8)
        .padding(.bottom, 4)
    }

    private func rowsView(_ groups: [AccountGroup], now: Date) -> some View {
        VStack(spacing: 2) {
            if groups.isEmpty {
                Text("No account here").font(.system(size: 11)).foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, minHeight: 40)
            }
            ForEach(groups) { group in
                GroupHeader(group: group, line: model.line, collapsed: collapsed.contains(group.id)) {
                    toggleGroup(group.id)
                }
                if !collapsed.contains(group.id) {
                    ForEach(group.profiles, id: \.name) { profile in
                        AccountRow(model: model, profile: profile, group: group, now: now)
                    }
                }
            }
        }
        .padding(.horizontal, 6)
    }

    private func estimatedHeight(_ groups: [AccountGroup]) -> CGFloat {
        if groups.isEmpty { return 44 }
        let rows = groups.filter { !collapsed.contains($0.id) }.map(\.profiles.count).reduce(0, +)
        return CGFloat(groups.count) * 26 + CGFloat(rows) * 28 + 4
    }

    // MARK: footer and banner

    private func footer(now: Date) -> some View {
        VStack(spacing: 6) {
            if let notice = model.snapshot?.throttle_notice {
                Text(notice).font(.system(size: 10)).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 6) {
                Chip(icon: "plus", title: "Add account") { model.onAdd() }
                Chip(icon: "chart.xyaxis.line", title: "Dashboard") { model.onHistory() }
                Spacer()
                Text(updatedAgo(model.loadedAt, now: now))
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
                Chip(icon: "gearshape") { model.onSettings() }.help("Settings")
                Chip(icon: "power") { model.onQuit() }.help("Quit claudini-pilot")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var bannerView: some View {
        if let banner = model.banner {
            HStack(spacing: 6) {
                Image(systemName: banner.error ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                Text(banner.text).lineLimit(2)
            }
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(.white)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(Capsule().fill(banner.error ? Color.red : Color.green).shadow(radius: 3, y: 1))
            .padding(.top, 8)
            .transition(.move(edge: .top).combined(with: .opacity))
            .onTapGesture { model.banner = nil }
        }
    }
}

/// "4h50", "35m", "2d": time left until an epoch.
func countdown(epoch: Int, now: Date) -> String {
    let mins = max(0, epoch - Int(now.timeIntervalSince1970)) / 60
    if mins < 60 { return "\(mins)m" }
    if mins < 1440 { return String(format: "%dh%02d", mins / 60, mins % 60) }
    return "\(mins / 1440)d"
}

struct Tile: View {
    let title: String
    let value: String
    let unit: String
    let detail: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title.uppercased()).font(.system(size: 8.5, weight: .semibold)).kerning(0.4)
                .foregroundStyle(.tertiary)
            (Text(value).font(.system(size: 17, weight: .semibold, design: .rounded)).foregroundColor(tint)
             + Text(unit).font(.system(size: 11, weight: .medium)).foregroundColor(.secondary))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(detail).font(.system(size: 9.5)).foregroundStyle(.secondary)
                .lineLimit(1).truncationMode(.tail)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 9).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(Color.primary.opacity(0.045)))
    }
}

struct GroupHeader: View {
    let group: AccountGroup
    let line: Int
    let collapsed: Bool
    let action: () -> Void

    var body: some View {
        let ready = group.profiles.filter { $0.state(line: line) == .ready }.count
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .bold))
                    .rotationEffect(.degrees(collapsed ? 0 : 90))
                    .foregroundStyle(.tertiary)
                    .frame(width: 10)
                Text(group.title).font(.system(size: 11, weight: .semibold))
                Text("\(group.profiles.count)").font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5).frame(height: 15)
                    .background(Capsule().fill(Color.primary.opacity(0.07)))
                Spacer()
                if group.id != "~attention" {
                    Text("\(ready) ready").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 8)
            .frame(height: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct AccountRow: View {
    @ObservedObject var model: PanelModel
    let profile: Profile
    let group: AccountGroup
    let now: Date
    @State private var hovering = false

    var body: some View {
        let line = model.line
        let state = profile.state(line: line)
        let muted = profile.dimmed
        HStack(spacing: 8) {
            Circle()
                .fill(state == .off ? Color.clear : state.tint)
                .overlay(Circle().strokeBorder(state == .off ? Color.secondary : .clear, lineWidth: 1))
                .frame(width: 7, height: 7)
                .frame(width: 8)
            name
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(alignment: .trailing) {
                    if hovering && state != .off && !model.preview { useButton(state) }
                }
            if profile.binding != nil {
                MiniMeter(limit: profile.window("5h"), muted: muted).frame(width: Column.window)
                MiniMeter(limit: profile.window("7d"), muted: muted).frame(width: Column.window)
                Text(profile.modelLimit.map { "\($0.percent)%" } ?? "—")
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(profile.modelLimit.map {
                        muted || $0.level == "ok" || $0.level == "normal" ? Color.secondary : Color.level($0.level)
                    } ?? Color.secondary.opacity(0.5))
                    .frame(width: Column.model, alignment: .trailing)
                Text(state == .off ? "off" : profile.backIn(line: line, now: now))
                    .font(.system(size: 11, weight: .medium).monospacedDigit())
                    .foregroundStyle(state == .spent ? Color.red.opacity(0.85) : Color.secondary)
                    .frame(width: Column.back, alignment: .trailing)
            } else {
                Text(profile.needs_login ? "refresh token expired — log in again" : profile.detail)
                    .font(.system(size: 11)).foregroundStyle(.orange).lineLimit(1)
                    .frame(width: 2 * Column.window + Column.model + Column.back + 24, alignment: .trailing)
            }
            DrawnSwitch(on: !profile.disabled) { model.onRotation(profile.name, profile.disabled) }
                .frame(width: Column.toggle)
                .opacity(hovering || profile.disabled ? 1 : 0)
                .help(profile.disabled ? "Off: never chosen. Click to put it back in the rotation."
                                       : "In the rotation. Click to switch it off.")
        }
        .padding(.horizontal, 8)
        .frame(height: 26)
        .background(RoundedRectangle(cornerRadius: 6, style: .continuous)
            .fill(hovering ? Color.accentColor.opacity(0.10) : Color.clear))
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .contextMenu { menu(state) }
        .help(tooltip)
    }

    private var name: some View {
        let chars = Array(profile.name)
        let prefix = min(group.sharedPrefix, chars.count)
        let suffix = min(group.sharedSuffix, chars.count - prefix)
        let head = String(chars[..<prefix])
        let body = String(chars[prefix..<(chars.count - suffix)])
        let tail = String(chars[(chars.count - suffix)...])
        let faded = Color.secondary.opacity(profile.disabled ? 0.6 : 0.85)
        return (Text(head).foregroundColor(faded)
                + Text(body).foregroundColor(profile.disabled ? .secondary : .primary)
                + Text(tail).foregroundColor(faded))
            .font(.system(size: 12, weight: .medium))
            .lineLimit(1)
            .truncationMode(.middle)
    }

    private func useButton(_ state: AccountState) -> some View {
        Button {
            if profile.needs_login { model.onReconnect(profile.name) } else { model.onSwitch(profile.name) }
        } label: {
            Text(profile.needs_login ? "Log in" : "Use")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8).frame(height: 18)
                .background(Capsule().fill(Color.accentColor))
        }
        .buttonStyle(.plain)
        .help(profile.needs_login ? "Log this account in again" : "Make this the account in use")
    }

    @ViewBuilder
    private func menu(_ state: AccountState) -> some View {
        if profile.needs_login {
            Button("Log in again") { model.onReconnect(profile.name) }
        } else if state != .off {
            Button("Use this account") { model.onSwitch(profile.name) }
        }
        Button(profile.disabled ? "Put back in the rotation" : "Switch off (never chosen)") {
            model.onRotation(profile.name, profile.disabled)
        }
        Divider()
        if let email = profile.email {
            Button("Copy \(email)") { copy(email) }
        }
        Button("Copy profile name") { copy(profile.name) }
    }

    private var tooltip: String {
        var lines = [profile.name, planLine(profile)]
        if let email = profile.email { lines.append(email) }
        for limit in profile.limits {
            lines.append("\(limit.title): \(usage(limit.percent, resets: limit.resets_at_epoch, now: now))")
        }
        if let note = staleNote(profile) { lines.append(note) }
        return lines.filter { !$0.isEmpty }.joined(separator: "\n")
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
