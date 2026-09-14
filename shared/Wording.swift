// How figures are worded, the same in the menu and in the widget — the way
// Claude's own usage menu words them.
//
// Every function takes the moment it is worded for. The widget draws entries
// for times that have not come yet, so "now" is not always now.

import Foundation

/// "88% · resets 1h 41m", or just "88%" when there is no reset to speak of.
func usage(_ percent: Int, resets epoch: Int?, label: String? = nil, now: Date = Date()) -> String {
    let head = [label, "\(percent)%"].compactMap { $0 }.joined(separator: " ")
    let tail = resetsIn(epoch, now: now)
    return tail.isEmpty ? head : head + " · " + tail
}

/// "resets 58m", "resets 4h 20m", "resets 5d" — Claude's wording.
func resetsIn(_ epoch: Int?, now: Date = Date()) -> String {
    guard let epoch else { return "" }
    let mins = max(0, epoch - Int(now.timeIntervalSince1970)) / 60
    if mins <= 0 { return "resets now" }
    if mins < 60 { return "resets \(mins)m" }
    if mins < 1440 {
        return mins % 60 == 0 ? "resets \(mins / 60)h" : "resets \(mins / 60)h \(mins % 60)m"
    }
    return "resets \(mins / 1440)d"
}

func updatedAgo(_ date: Date, now: Date = Date()) -> String {
    let seconds = Int(now.timeIntervalSince(date))
    if seconds < 60 { return "Updated just now" }
    if seconds < 3600 { return "Updated \(seconds / 60)m ago" }
    return "Updated \(seconds / 3600)h ago"
}

/// "team · max 20x": the workspace, then the plan.
func planLine(_ profile: Profile) -> String {
    [profile.space, profile.plan_label].compactMap { $0 }.filter { !$0.isEmpty }
        .joined(separator: " · ")
}
