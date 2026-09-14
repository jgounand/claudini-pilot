// The engine's contract: what `claudini-usage --json` prints, decoded.
//
// Shared by the menu bar app and the widget, which read the very same JSON —
// the app from the engine, the widget from the copy the app leaves for it.
// No AppKit here: the widget is SwiftUI only.

import Foundation

struct Limit: Decodable {
    let short_label: String
    /// "5-hour limit", "Weekly · all models" — worded like Claude's own menu.
    let title: String
    let percent: Int
    let level: String
    /// Absolute, so the countdown stays right even drawn from a stale snapshot.
    let resets_at_epoch: Int?
    /// Which limits are the general windows, and which one is the preferred
    /// model, is the engine's call — the app only lays them out.
    let general: Bool
    let preferred: Bool
}

struct Profile: Decodable {
    let name: String
    let email: String?
    let active: Bool
    /// Switched off by you: still read and shown, never chosen by the policy.
    let disabled: Bool
    let status: String
    let detail: String
    let limits: [Limit]
    let limit_reset: Bool?
    let needs_login: Bool
    let space: String?
    let plan_label: String?
    let binding: Binding?
    let saturated: [String]
}

/// The general window that will stop you first — which one it is changes
/// through the day, so the engine names it rather than the app assuming.
struct Binding: Decodable {
    let label: String
    let percent: Int
    let level: String
    let resets_at_epoch: Int?
}

/// What the next `claude` session gets, and what stops us moving there.
struct NextUp: Decodable {
    let name: String?
    let reason: String
    let blocked_by: String?
    let staying: Bool
    /// Where you would go if this account ran out — so the line says something
    /// even when nothing is about to change.
    let after: String?
}

struct Mode: Decodable {
    let name: String
    let title: String
}

/// The command line, described by the engine rather than retyped here — the
/// list would otherwise drift the first time a command changed.
struct Action: Decodable {
    let command: String
    let about: String
}

struct Snapshot: Decodable {
    let profiles: [Profile]
    let auto: Bool
    /// One line on whether the fleet as a whole is heading for an outage.
    let fleet: String
    let actions: [Action]
    let mode: String
    let modes: [Mode]
    /// A spent model quota only means something while the policy protects it.
    let show_saturated: Bool
    let throttle_notice: String?
    let poll_after_sec: Int
    let next: NextUp
}

