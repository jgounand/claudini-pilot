// What the widget can be set to, and what its buttons do.
//
// The buttons do not do the work themselves: a widget is sandboxed and cannot
// switch accounts or change settings. Each leaves a request in the shared
// folder for ClaudiniBar, which runs the engine exactly as a click in its own
// menu would.

import AppIntents
import WidgetKit

/// An account to follow, as the widget's settings list it.
struct AccountEntity: AppEntity {
    static let inUseID = "__in_use__"
    /// Not a real account: follow whichever one is in use, through every switch.
    static let inUse = AccountEntity(id: inUseID, title: "Account in use",
                                     subtitle: "follows every switch")

    let id: String
    let title: String
    let subtitle: String

    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Account"
    static var defaultQuery = AccountQuery()

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(title)", subtitle: "\(subtitle)")
    }
}

/// The accounts on offer are whatever the last snapshot listed.
struct AccountQuery: EntityQuery {
    private func all() -> [AccountEntity] {
        [.inUse] + (SharedStore.read()?.snapshot.profiles ?? []).map {
            AccountEntity(id: $0.name, title: $0.name, subtitle: planLine($0))
        }
    }

    func entities(for identifiers: [String]) async throws -> [AccountEntity] {
        let known = all()
        // A profile renamed or removed since keeps its place in the settings
        // under its old name; the widget follows the account in use meanwhile.
        return identifiers.map { id in
            known.first { $0.id == id }
                ?? AccountEntity(id: id, title: id, subtitle: "no longer found")
        }
    }

    func suggestedEntities() async throws -> [AccountEntity] { all() }

    func defaultResult() async -> AccountEntity? { .inUse }
}

/// The widget's settings: right-click it, Edit Widget.
struct FollowAccount: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Claude usage"
    static var description = IntentDescription("Choose the account this widget follows.")

    @Parameter(title: "Account")
    var account: AccountEntity?

    @Parameter(title: "Show switched-off accounts", default: true)
    var showOff: Bool

    /// The account name to follow, or nil for the one in use.
    var followed: String? {
        account.flatMap { $0.id == AccountEntity.inUseID ? nil : $0.id }
    }
}

/// Put an account in or out of the rotation, as the menu's switch does.
struct SetInRotation: AppIntent {
    static var title: LocalizedStringResource = "Switch a Claude account on or off"
    static var isDiscoverable = false

    @Parameter(title: "Account") var name: String
    @Parameter(title: "On") var on: Bool

    init() {}

    init(name: String, on: Bool) {
        self.name = name
        self.on = on
    }

    func perform() async throws -> some IntentResult {
        SharedStore.markDisabled(name, !on)
        try Request(kind: on ? .on : .off, name: name).send()
        return .result()
    }
}

/// Re-read every account now, skipping the waiting periods.
struct RefreshUsage: AppIntent {
    static var title: LocalizedStringResource = "Refresh Claude usage"
    static var isDiscoverable = false

    func perform() async throws -> some IntentResult {
        try Request(kind: .refresh, name: nil).send()
        return .result()
    }
}
