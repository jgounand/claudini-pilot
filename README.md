# claudini-console

See how much is left on each of your Claude subscriptions, and switch between
them from the menu bar, a terminal UI, or automatically.

[claudini](https://github.com/kimrgrey/claudini) switches the active Claude Code
account. It does not tell you *which* account you should switch to. This is the
missing half: live usage for every profile, and a policy that picks for you.

```
 claudini  actif: work          auto: ACTIF        → personal

  #  profil          compte                       session   semaine    Fable   reset
 ●1  work   you@example.com                 63%       72%      100%    3h47
  2  work            work@example.com               100%       42%       70%    1h57
  3  personal        me@example.com                 100%       16%        —    57min
```

## What you get

**`claudini-usage`** — one-shot table of every profile's limits, with bars and
reset countdowns. Good for a quick look or a shell prompt.

**`claudini-auto`** — a terminal console that refreshes itself, lets you switch
with a single keypress, and hosts the auto-switch toggle.

**ClaudiniBar** — a menu bar app showing the active profile and its remaining
headroom, with one-click switching and the same auto toggle.

All three read the same state, so a toggle in one shows up in the others.

## Auto-switching

Anthropic subscriptions have two kinds of limit: general ones (a 5-hour session
window and a weekly window) and per-model weekly quotas — the one for Fable runs
out well before the others if that's what you use.

The policy is *stay on a model, then fall back*:

1. Prefer an account that still has the target model available (Fable by default).
2. When every account has burned that model's quota, take the account with the
   most general headroom instead.
3. Never pick an account whose general limits are already maxed — a full Fable
   quota is worthless if the 5-hour window is at 100%.
4. Only switch when the current account is actually blocked, and never more than
   once per cooldown (10 min default). No churn.

Auto-switching is **off** by default. Turn it on with `a` in the console, from
the menu bar, or `claudini-usage --auto on`.

> **Already-running `claude` sessions keep the account they started with.**
> Switching profiles rewrites `~/.claude.json` and the keychain entry; a session
> that is already up has its credentials in memory. Auto-switching decides which
> account your *next* session gets. Relaunch `claude` for it to take effect.

## Install

Requires macOS, [claudini](https://github.com/kimrgrey/claudini), Python 3.9+,
and Xcode Command Line Tools for the menu bar app.

```sh
git clone https://github.com/jgounand/claudini-console.git
cd claudini-console
./install.sh
```

Everything is symlinked back to the clone, so `git pull` is enough to update
the commands. Re-run `./install.sh` to rebuild the menu bar app.

## Usage

```sh
claudini-usage                 # table of all profiles
claudini-usage --json          # machine-readable, used by the menu bar app
claudini-usage --switch NAME   # switch (wraps `claudini use`)
claudini-usage --auto on|off   # arm or disarm auto-switching
claudini-usage --tick          # run one auto-switch decision now
claudini-auto                  # the console: a auto · r refresh · 1-9 switch · q quit
```

Tuning lives in `~/.claudini/auto.json`:

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | auto-switching armed |
| `min_margin` | `5` | % of general headroom below which an account counts as spent |
| `cooldown_min` | `10` | minutes between two automatic switches |

## How it works

claudini stores each profile's OAuth credentials in the macOS keychain under the
service `claudini-profile-<name>`. This reads them, refreshes any expired access
token (writing the rotated token back), and asks
`GET https://api.anthropic.com/api/oauth/usage` what is left. Results are cached,
and an account that fails to refresh is left alone for 15 minutes rather than
being retried every tick.

Nothing is sent anywhere else. Credentials never leave the keychain and the
API call.

## Caveats

The usage endpoint is what Claude Code's own `/usage` command calls. It is not a
documented public API and may change without notice. This project is unofficial
and not affiliated with Anthropic.

## License

MIT
