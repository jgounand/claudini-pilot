# claudini-pilot

See how much is left on each of your Claude subscriptions, and switch between
them from the menu bar, a terminal UI, or automatically.

It grew out of [claudini](https://github.com/kimrgrey/claudini), which switches
the active Claude Code account but does not tell you *which* account to switch
to. It now does both: live usage for every profile, a policy that picks for
you, and the switching and profile management itself — so claudini is optional.
The two read the same layout, so they coexist if you already use it.

```
 claudini   auto: ON   mode: endurance   next: side — resets in 22h, spend it before it is lost

  #  profile         account                workspace   plan      session week   Fable  reset   ⟲
 ●1  personal        you@example.com        personal    max 20x      18%   64%   100%   4h36   ⟲
  2  work            you@example.com        Acme Inc    team std    100%   29%          4h46
  3  side            other@example.com      personal    max 20x      63%   72%    70%   1h57   ⟲
  4  old             stale@example.com                              login required
```

Rows 1 and 2 are the *same account* in two different workspaces — different
subscriptions, different limits, and only one of them has `/limit-reset`. The
email alone can't tell them apart, so the workspace and plan are shown next to
it, read from the API rather than from local config (which another session can
overwrite).

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

There are two ways to be optimal, because there are two different goals, so
there are two modes. Switch with `m` in the console, from the menu bar, or
`claudini-usage --mode model|endurance`.

### `model` — keep the preferred model available

The default. The policy is *stay on a model, then fall back*:

1. Prefer an account that still has the target model available (Fable by default)
   — but only among accounts on the largest plan you have. A team seat with
   untouched Fable is worth less than a Max 20x with real headroom left, because
   percentages aren't comparable across plans: 20% of a team seat is not 20% of
   a Max 20x.
2. When no account on that plan has the model, take the one with the most
   general headroom instead.
3. Never pick an account whose general limits are already maxed — a full Fable
   quota is worthless if the 5-hour window is at 100%.
4. Only switch when the current account is actually blocked, and never more than
   once per cooldown (10 min default). No churn.

On a team plan the *seat* decides which models you get. A standard seat has no
premium model, and reports no quota line for it — which is not the same as an
untouched quota, so those accounts don't win the model preference. Upgrade the
seat to premium and it counts again, with no configuration on your side.

The console and the menu bar both name the account the next session will use,
the reason it's better, and — when nothing is moving — what is holding the
switch back ("auto-switching is off", "cooldown, 4 min left", "current account
is not blocked yet"). That decision is made once, in the engine; the interfaces
only spell it out.

### `endurance` — never stop working

For when you don't need Fable and just want to keep going. A weekly allowance
that resets tonight is worth nothing kept; one that resets in five days is a
reserve. So this mode spends the account whose weekly window **resets soonest**,
and among accounts resetting together takes the one with the most room so the
next wall is furthest away.

The difference is not only which account wins but *when* it switches. `model`
moves only once the current account is blocked. `endurance` moves as soon as
another account's allowance is closer to expiring, because waiting is exactly
how that allowance gets wasted — still bounded by the cooldown, so it doesn't
churn.

Both modes ignore accounts whose general limits are already spent, and both say
what they are doing: *"license resets in 22h34, spend it before it is lost"*.

Auto-switching is **off** by default. Turn it on with `a` in the console, from
the menu bar, or `claudini-usage --auto on`.

> **Already-running `claude` sessions keep the account they started with.**
> Switching profiles rewrites `~/.claude.json` and the keychain entry; a session
> that is already up has its credentials in memory. Auto-switching decides which
> account your *next* session gets. Relaunch `claude` for it to take effect.

## `/limit-reset`

Claude Code has a hidden `/limit-reset` command: it clears your 5-hour session
window immediately so you can keep working, once a week, and the cost comes out
of your weekly limit. It is rolled out account by account, so you have it on
some subscriptions and not others — and there is no way to tell from inside a
session which of your other accounts has it.

Accounts where it is available are marked `⟲` in the console and the menu bar,
and called out in `claudini-usage`. When your session window is full, that tells
you at a glance whether switching to another account buys you a reset or just
another wall.

Availability is decided server-side; this only reports what your client was
told. It cannot grant the command on an account that does not have it.

## When an account cannot be read

Not every failure is a credentials problem, and the tool says which is which
rather than offering a login for all of them:

| what you see | what it means |
|---|---|
| `login required` | the refresh token really is expired or revoked — click to log in |
| `oauth blocked` | the *organisation* has OAuth turned off (`oauth_not_allowed_for_organization`). The token is fine; no login will help. Someone with admin rights on that workspace has to allow it |
| `rate limited` | a 429, nothing to do but wait |
| `unreachable` | a network or server-side failure, retried on its own |

Only the first offers a login. The detail line carries the API's own wording.

## When an account needs a new login

Refresh tokens expire after a few days, and an account whose token is dead shows
`login required`. Click it in the menu bar, or press its number in the console,
and you get an OAuth login for **that** account.

Your active account never moves. The login runs in a throwaway
`CLAUDE_CONFIG_DIR`, which gets its own keychain slot and its own `claude.json`,
so neither the active profile nor any running `claude` session is touched — the
thing that would otherwise corrupt a live session's config. The fresh
credentials are then filed into the profile you meant to fix and the temporary
slot is deleted.

That temporary slot's keychain service name carries an unguessable suffix, so it
is found by diffing the keychain entries around the login rather than computed.

## Rate limits

Reading usage for several accounts at once will get you a 429 if you sweep them
too eagerly, so the cache is deliberately tuned against the poll period rather
than against a round number.

**The cache TTL sits just below the poll period.** A poller waking at `P` always
finds its own row too old and refetches, while a second interface polling out of
phase lands inside the window and pays nothing. Set the TTL *above* `P` and
nothing ever refetches; set it far below and the cache stops mattering at all.
With the console and the menu bar both open that is one sweep per period instead
of two.

**A broken account backs off.** Each retry of an account that needs a login
costs a token refresh that is bound to fail, against the endpoint that
rate-limits fastest. So the delay doubles with each consecutive failure, from 15
minutes up to 6 hours — roughly 34 retries a week instead of 670. Logging the
account back in clears the entry, so a real fix is picked up on the very next
pass however deep the backoff went.

The rest: at most two requests in flight, and a 429 mutes the API entirely for
three minutes while everything is served from cache. All three interfaces say
when that is happening and for how long.

## Install

Requires macOS, Python 3.9+, and Xcode Command Line Tools for the menu bar app.
claudini is optional; if you have no profiles yet, log in with Claude Code as
usual and run `claudini-usage --add NAME` to turn that login into the first one.

```sh
git clone https://github.com/jgounand/claudini-pilot.git
cd claudini-pilot
./install.sh
```

Everything is symlinked back to the clone, so `git pull` is enough to update
the commands. Re-run `./install.sh` to rebuild the menu bar app.

## Usage

```sh
claudini-usage                 # table of all profiles
claudini-usage --json          # machine-readable, used by the menu bar app
claudini-usage --switch NAME   # switch (wraps `claudini use`)
claudini-usage --reconnect NAME  # OAuth login for one profile, active account untouched
claudini-usage --add NAME      # save the credentials in use now as a new profile
claudini-usage --rename OLD NEW
claudini-usage --remove NAME   # refuses while the profile is in use
claudini-usage --force         # re-read now, skipping the per-account waiting periods
claudini-usage --auto on|off   # arm or disarm auto-switching
claudini-usage --mode model|endurance   # which goal the policy optimises for
claudini-usage --tick          # run one auto-switch decision now
claudini-auto                  # the console: a auto · m mode · r refresh · 1-9 switch · q quit
```

Tuning lives in `~/.claudini/auto.json`:

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | auto-switching armed |
| `mode` | `model` | `model` keeps Fable available, `endurance` spends what resets soonest |
| `min_margin` | `5` | % of general headroom below which an account counts as spent |
| `cooldown_min` | `10` | minutes between two automatic switches |

## Switching, without claudini

A profile is three things: a directory holding a `claude.json`, a keychain entry
`claudini-profile-<name>` holding its OAuth credentials, and a name in
`~/.claudini/config.json`. A profile is *active* when `~/.claude.json` is a
symlink to its config file — that one path is what Claude Code reads.

Switching therefore means: save the live credentials back into the outgoing
profile (Claude Code refreshes them as it runs, so the profile's copy is behind
by however long it was active), load the target's into the live slot, and
repoint the link. The link is replaced rather than written through, so a session
reading mid-switch sees one file or the other, never a half-written one.

## How it works

Each profile's OAuth credentials live in the macOS keychain under the service
`claudini-profile-<name>`. This reads them, refreshes any expired access
token (writing the rotated token back), and asks two endpoints what it needs:

- `GET /api/oauth/usage` — the limits, the same numbers Claude Code's own
  `/usage` shows. **For the account you are actively working on this is often
  not requested at all**: Claude Code caches the response it gets into that
  profile's `claude.json` while it runs, so the numbers are already on disk and
  fresher than anything we would poll for. That copy is used when it is under
  ten minutes old and its account uuid matches the profile.

  Only for the *active* profile, deliberately. Switching profiles copies the
  running session's `claude.json` into the outgoing profile, so an idle
  profile's cached usage usually belongs to whichever account was live at the
  time — on this machine four profiles out of five carried a fifth one's
  numbers. The uuid is checked regardless.
- `POST https://platform.claude.com/v1/oauth/token` — refreshing an expired
  access token. Note the host: it is not the API host, and it sits behind
  Cloudflare, which answers a request with no `User-Agent` with `error code:
  1010` — indistinguishable from a rejected token if you don't read the body.
- `GET /api/claude_cli/bootstrap` — the workspace, plan and seat tier, which is
  what makes two profiles on one email distinguishable and what decides whether
  a missing model quota means "untouched" or "no access". This one is cached for
  a day, since an account's plan doesn't move.

Everything derived from those — headroom, spent model quotas, reset countdowns,
whether an account needs a login, and the switch decision itself — is computed
once in the Python engine. The menu bar app decodes that and draws it; it holds
no policy of its own.

Nothing is sent anywhere else. Credentials never leave the keychain and those
two API calls.

## Caveats

The usage endpoint is what Claude Code's own `/usage` command calls. It is not a
documented public API and may change without notice. This project is unofficial
and not affiliated with Anthropic.

## License

MIT
