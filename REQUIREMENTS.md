# Requirements Document: Club Callsign Onboarding Bot

**Version:** 2.0 (draft)
**Status:** Decisions §6–§9 resolved; remaining open questions in §16

---

## 1. Purpose & Scope

A custom Discord bot that onboards new members by collecting their amateur radio callsign, preferred name, and rules agreement; validating the callsign; matching it against the club roster; assigning roles accordingly; and normalizing each member's display name to a consistent format.

**In scope (v1):** US callsigns with live lookup; worldwide callsigns via shape-check only; role assignment; display-name normalization; rules acceptance; duplicate/impersonation protection; exception-based moderator review queue; roster sync from an external source with local snapshot.

**Out of scope (v1):** Non-US live lookup; the actual text of the server rules (authored separately before launch); ongoing periodic re-validation of licenses; license-class roles (see §16).

---

## 2. Goals

**Primary purpose (stated first, because its absence from v2 skewed every tradeoff in this document toward gatekeeping):**

| # | Goal |
|---|---|
| **G0** | **Grow the club.** The server is a public front door — for the curious, the unlicensed, the young, and hams from elsewhere. Onboarding must feel like a welcome, not a checkpoint. |
| **G0a** | **Guests are the funnel, not an edge case.** They may be most of the server. Guest → licensed → member is the conversion this whole thing exists to produce, and the metric it should be judged by. |

**Supporting goals:**

| # | Goal |
|---|---|
| G1 | Reduce manual moderator work by auto-assigning club member / club officer roles |
| G2 | Enforce a consistent display-name convention **for callsign holders**. Guests keep their own Discord name (§7.0) — the format is a signal, not a uniform |
| G3 | Prevent one person from claiming another's callsign (impersonation guard) |
| G4 | Confirm every member has read and agreed to the rules |
| G5 | Keep the door open for international expansion without a rewrite |
| G6 | **The club owns its data.** No runtime dependency on a vendor; roster and ledger exportable and restorable at any time |
| G7 | **Fail open, not closed.** An external outage degrades the experience; it never blocks onboarding |

### 2.1 The governing principle: friction scales with privilege

G0 and G3 pull against each other. This is how the tension resolves, everywhere in this document:

| Who | Friction budget |
|---|---|
| **Guest** (no callsign) | **Zero.** Name + rules, ~30 seconds, instant role. Nothing is being granted that anyone would want to steal. Any friction here is pure conversion loss. |
| **Member** (roster callsign) | Low. One email code (§6.5), or one officer click (§6.1). |
| **Officer** | Real review, unconditionally. Small N, high privilege, rare. |

Where G0 and G3 genuinely conflict, **G3 yields for guests and wins for officers.** The middle is where the design work goes.

**Corollary — no security control may be justified by "it's only a little friction."** For a guest, a little friction is the entire cost of the feature and none of the benefit.

**Explicit non-goal:** the bot is not a moderation system. It constrains format and flags anomalies. Judgment calls stay with humans.

---

## 3. Onboarding Flow (User-Facing)

**Framing rule (G0):** one button serves two audiences. A member is *verifying*; a guest is *introducing themselves*. **Write every user-facing string for the guest** — the member will figure it out regardless, and the guest is the one who leaves. "Start Verification" is the correct word for a member and an intimidating one for a curious sixteen-year-old who wandered in. Prefer **"Introduce Yourself"** or **"Get Started."** The word "verification" should not appear in anything a guest sees.

1. **Member joins** → auto-assign `@unverified`. Bot posts a welcome message with a **Get Started** button in a dedicated `#welcome` channel.
   - Button + modal, because Discord modals are the only reliable way to collect typed input and they don't depend on the user having DMs open.
   - The same button must be **persistent** (survives bot restarts, `custom_id`-based) and re-runnable — used for backfill (§15) and for guest→member upgrades.
   - `@unverified` exists for raid/spam control and rules acceptance, nothing more. It is a 30-second speed bump, not a gate. See §15.4 on how little should sit behind it.
2. **Modal collects:**
   - Preferred first name — required, text, sanitized per §7. For guests this is used only to greet them; it is never written to their nickname (§7.0)
   - Last name — optional, self-reported, sanitized per §7
   - Callsign — **optional**, text. The field label must make "I don't have one yet" feel like a normal answer, not a failure. Something like *"Callsign (leave blank if you're not licensed — welcome!)"*
3. **Rules agreement** — presented as its own step, because Discord modals can't contain a true checkbox. Implement as a second view with **I Agree** / **I Do Not Agree** buttons.
4. **Bot processes** the submission through the validation pipeline (§5).
5. **Bot responds** with an ephemeral message. One of four outcomes:
   - **Approved** → roles applied, nickname set, done.
   - **Pending review** → `@pending-verification` role, message explains a human is looking and no further action is needed. Case lands in the review queue (§6).
   - **Guest** → `@guest` role, **nickname left alone** (§7.0), warm welcome, and a concrete pointer to how the club helps people get licensed. This message is a **conversion surface** (G0a), not a receipt — it's the first thing a prospect reads. Write it accordingly.
   - **Declined rules** → no roles, stays `@unverified`, invited to restart anytime.

> **Requirement:** no path leaves the user in silence. Every terminal state produces a message that tells them what happened and what, if anything, they should do next.

---

## 4. Data Model

| Store | Contents | Notes |
|---|---|---|
| `roster` | `callsign → {full_name, is_officer, notes, source_row_id, synced_at}` | Local SQLite snapshot, synced from external source (§8). Bot **never** reads the external source at request time. |
| `claimed` | `callsign → {discord_user_id, claimed_at, released_at, released_by}` | The dedup ledger. Must persist. Soft-delete on release so history survives. |
| `verifications` | `{discord_user_id, callsign, first_name, last_name, rules_version, accepted_at, outcome, reviewed_by}` | Current state per user |
| `review_queue` | `{id, discord_user_id, reason_code, payload, status, opened_at, resolved_at, resolved_by, resolution_note}` | §6 |
| `audit_log` | Append-only event records | Who verified, when, roles granted, collisions flagged, approvals, overrides, roster syncs, releases |
| `lookup_cache` | `callsign → {response, fetched_at}` | TTL per §13 |

**Normalization:** all callsigns uppercase and whitespace-stripped on both roster load and user input, so `ke4car` and `KE4CAR` match. Apply at the storage boundary so it can't be forgotten at a call site.

**Backend:** SQLite. Single file, trivially backed up, no server to maintain. Non-negotiable that it persists across restarts — the dedup guard is worthless if it resets.

---

## 5. Callsign Validation Pipeline

Executed in order, fail fast. Each terminal state maps to an outcome in §9.

1. **Normalize** input (uppercase, trim).
2. **Sanitize names** per §7. Hard-fail → re-prompt the user with a specific reason. Soft-flag → continue, but the case is queued for review.
3. **Empty callsign check** → if blank, take the guest path (name + rules still collected; no member/officer role).
4. **Tier 1 — Shape validation (regex).** Runs for every callsign, US or foreign.
   - US-focused regex for the common case.
   - Adopt a worldwide regex generated from the ITU callsign series (e.g. the `ge0rg/callsign-regex` approach) so foreign-issued calls and future international members pass the shape gate.
   - Fail → reject with an explanation, no queue entry. A malformed callsign is almost always a typo.
5. **Duplicate / impersonation guard.**
   - Claimed by **same** Discord user → rejoin, re-grant roles silently.
   - Claimed by **different** user → **queue for review** (`reason_code: collision`), no roles, notify moderators. Do not tell the claimant who holds it.
6. **Tier 2 — Live lookup (US only).**
   - Query `callook.info` or HamDB for US callsigns.
   - Foreign callsigns skip this step in v1 and rely on Tier 1 + roster match.
   - Lookup **fails to find a valid license** → warn but continue (`reason_code: lookup_miss` if also a roster hit). Roster membership is the real gate.
   - Lookup **times out / service down** → skip silently, proceed on Tier 1 + roster (G7). Never blocks.
7. **Name cross-check** (see §6.2). Compares self-reported name against the FCC record and/or the roster entry. Mismatch → queue, don't reject.
8. **Roster match** → role assignment (§9). A roster hit on an **unclaimed** callsign raises `first_claim` and routes to review (§6.1) — it does not grant. Optionally short-circuited by an email code (§6.5).

---

## 6. Review & Approval (Human-in-the-Loop)

### 6.1 Design principle

Approving every join would defeat G1. **Approve by exception** — but the exception is drawn around *first claims*, not around anomalies.

**Any first claim on a roster callsign is queued.** Auto-approval is limited to cases where nothing new is being granted:

**Auto-approve (no human):**
- **Rejoin / re-verify** — `claimed` already maps this callsign to *this* Discord user. Re-grant silently.
- **Guest** — no callsign, nothing to steal, nothing to review.

**Everything else queues.** Reason codes:

| `reason_code` | Trigger | Why a human |
|---|---|---|
| `first_claim` | Valid callsign, roster hit, unclaimed | **The impersonation gate (§6.2).** An officer confirms this is the person the club knows. |
| `officer_grant` | Roster entry flagged officer | Small N, high privilege, low frequency. Cheapest possible safeguard. |
| `collision` | Callsign already claimed by a different user | Requires knowing which human is which (§6.4) |
| `roster_miss` | Valid callsign, not on roster | New member, lapsed member, or a vanity call change (§12) |
| `lookup_miss` | US callsign, no valid license found | Could be expired, could be FCC data lag |
| `name_mismatch` | Self-reported name conflicts with FCC/roster record | Weak signal, but free (§6.3) |
| `manual` | Officer command | Escape hatch |

Multiple codes can attach to one case; the embed shows all of them.

### 6.1.1 Why this still satisfies G1

The volume is small, because *first claim on an unclaimed roster callsign* is a rare event in steady state. It happens for: a member slow to verify, a member who joins Discord months after the launch, a member whose claim an officer released, or a newly-added dues-paying member. For a club of ~100, that's on the order of 10–20 reviews per year — after the one-time backfill spike (§15).

And the counterfactual for G1 is not "zero work." It's a moderator manually checking the roster, granting two roles, and renaming by hand, every time. Under this design they click one button and the bot does the ledger write, both role grants, the nickname, and the audit entry. That is the reduction G1 asked for. The 10% retained is precisely the part that requires knowing Jim — which is the part a bot cannot do.

**If even that is too much:** §6.5 (roster email verification) automates the recognition step. Do not instead relax `first_claim` to auto-approve — that reopens the hole this section exists to close.

### 6.2 The first-come problem, and what actually closes it

**Roster matching on callsign alone is first-come-first-served, and callsigns are public.** They're on QRZ, on the club roster, and spoken aloud on the air. The `claimed` ledger only stops the *second* person to claim a callsign. If an impersonator claims KE4CAR before the real Jim does, the impersonator gets the role — and Jim later gets the collision warning and looks like the liar.

**Bounding the problem.** The attack needs a callsign that is (a) on the roster, (b) unclaimed, and (c) held by someone not yet in the Discord. That set is large exactly once — at launch — and monotonically shrinks. It is also **self-detecting**: when the real Jim verifies, he collides, and the case surfaces. So this is primarily a *rollout* problem (§15) and only secondarily a steady-state one.

**Defenses, in order of value:**

1. **Pre-seed the ledger before launch (§15).** Nothing beats a callsign that was never claimable. Tractable in a ham Discord: many members already carry their callsign in their nickname (parse, officer spot-check, seed); officers personally know many more; and a pre-launch public thread — "post your callsign before the bot goes live" — is visible to the entire club, which is exactly the property you want.
2. **Roster email code (§6.5).** The primary path. Works regardless of whether anyone has met the member — which is why it, not officer recognition, is the backbone (§14.1.2).
3. **Queue every `first_claim` (§6.1).** The fallback, and the only defense that works against a motivated impersonator *when it works at all*. A roster hit means the club already knows this person, so an officer may read the entry and think "that's not Jim — I've known Jim for fifteen years." Not cryptography: **social recognition**. But note the limit — it fails silently for any member no officer has met, which is a growing share of the roster (§14.1.1). Never rely on it alone.
4. **Resolve collisions in the right direction (§6.4)** so that losing the race isn't fatal.
5. **Public verification log (§6.6)** — free crowdsourced detection, and a growth surface in its own right.

**Accepted residual risk:** a determined impersonator, launching during the backfill window, against a member who never shows up, whose name they read off QRZ, with a Discord account aged in advance, may succeed until an officer or another member notices. The realistic threat model here is a previously-banned member returning under a friend's callsign — not a sophisticated adversary. The response is proportionate: social recognition, an audit trail, and easy revocation.

### 6.3 Name cross-check — a flag, never a gate

Compare the self-reported name against the FCC record (US, when lookup succeeded) and against the roster's stored full name (works for foreign members too). Allow common diminutives (Robert→Bob/Rob/Bobby, William→Bill, Richard→Rick/Dick). Fuzzy and forgiving.

**Be clear about its weakness:** Jim's name sits next to his callsign on QRZ. Anyone who can look up the call can look up the name. This check catches careless impersonation and nothing else. It is nearly free, so keep it — but **never gate a grant on it passing**, and never treat a pass as evidence. Skip it entirely when neither source is available; queuing on absence of evidence would make every foreign member a review case.

### 6.4 Collisions must not privilege the incumbent

By default, first-come-first-served means the *impersonator* is the incumbent and the real member is the one filing a complaint. Design against that:

- The collision queue embed shows **both claims side by side**: submitted names, claim timestamps, and — critically — **Discord account creation date and server join date** for each. An impersonator's account is frequently new, or joined the server the week the bot launched. Jim's has been sitting in the server for three years. Free signal, and a strong one.
- The embed must **not** present the existing claim as presumptively valid. Neutral framing, both sides equal.
- The second claimant's ephemeral message must not read as an accusation: *"This callsign is already registered. An officer is reviewing it — no action needed from you."* Not "someone already claimed this, contact an officer."
- Never disclose to either party who the other is.
- **Revocation is as easy as granting:** `/claim release <callsign>` then `/verify user <@user>`. Two commands. If the wrong person won the race, fixing it must be trivial or it won't happen.

### 6.5 Roster email verification — the primary member path

**Build this. It is not optional, and it is not a fallback.** Per §14.1.2, it is the mechanism that works for members whose link 2 exists only in records form — which is the membership G0 is trying to grow. Officer recognition (§6.1) is the path for members who *predate* the records.

It uses data the club already owns: your roster almost certainly carries member email addresses, because you send a newsletter and you collect dues.

**Flow:** callsign matches the roster → bot emails a 6-digit, single-use, 15-minute code **to the address already on file** → user enters it in a follow-up modal → auto-approve `@club-member`. Officer grants still queue, unconditionally.

An impersonator does not control Jim's inbox. This binds the Discord account to the club's own membership record with no vendor, no new source of truth, and no officer in the loop — it serves G0 and G1 *simultaneously*, which almost nothing else in this document does.

| Consideration | Handling |
|---|---|
| Bounce / stale address / spam folder | Fall back to `first_claim` queue. Never a dead end. Tell the user plainly that a human will handle it. |
| No email on roster for that member | Fall back to queue. Prompt the roster owner to backfill it. |
| Privacy | Email lives in the roster snapshot only. Never displayed, never written to the audit log, never echoed to the user — **not even partially masked**, since a mask leaks. |
| Sending infra | Club domain SMTP, or any transactional free tier. One integration. |
| Rate limiting | Max 3 code requests per user per hour; codes single-use; invalidate on new request. |

**Measuring coverage before you commit — free, today.** "We have an address for every member" is not the same as "every member still controls that address." Dead Yahoo accounts and addresses from a job someone left in 2022 are the norm on an aging roster. **The club already sends a newsletter, so the bounce rate on the last send is the §6.5 coverage number.** Go read it:

- ~3% bounce → email is the path; officer review is a genuine rarity; build with confidence.
- ~30% bounce → the roster owner has an email-backfill project, and it is on the critical path, not a follow-up.

Shared household addresses (two hams, one inbox) are fine — both are members, and either proving control of it proves club membership for both.

**Sequencing note:** §6.5 is *most* valuable during backfill (§15), which is the one moment it can't be retrofitted into. If it ships after launch, the entire existing membership routes through officer recognition at the exact moment the queue is most swamped — and the members who fail that path are the newest ones.

### 6.6 Public verification log (recommended, free)

A public `#verification-log` or `#new-members` channel where the bot posts each successful verification: *"Welcome KE4CAR — Jim F!"* and, louder, each guest→member upgrade: *"Jim just got licensed — welcome to the airwaves, KE4CAR!"*

Two jobs at once:
- **Growth (G0a):** new-licensee announcements are the club's best recurring engagement moment, and they make the funnel visible to everyone. A prospect lurking in `#general` sees people like them crossing the line regularly. That's worth more than any recruiting post.
- **Security (§6.2):** crowdsources impersonation detection to the hundred people who actually know Jim. His friends will notice if "Jim" isn't Jim, faster than an officer reading a queue entry in isolation.

Privacy cost is near zero: the nickname and role are already visible in the member list to everyone in the server. Frame the channel as a welcome feed, not an audit log — the security benefit is a side effect and should read that way.

### 6.7 Rejected: verification that gates on operating

**Net check-in verification is rejected outright** — not as disproportionate, but as **mission-hostile**.

The idea: bot issues a code, the member reads it on the air during the weekly net, net control confirms the voice and approves. It is the most ham-native option available and genuinely hard to fake.

It is also **disqualifying for licensed hams who don't transmit** — an ordinary and growing group, and precisely the membership G0 targets. Gating club access on operating erects the exact barrier the server exists to lower, and it does so invisibly: the people it excludes never complain, they just leave. Do not build it. Do not build a softer version of it.

The general rule this instantiates: **no verification path may require a capability the club is trying to help people acquire.** Never gate membership on operating, on equipment, on attending in person, or on knowing someone.

**Postcard to the FCC address of record** is listed only to mark the ceiling: authoritative, one-week latency, someone licks stamps. Wildly disproportionate to a Discord role. Out of scope.

**§6.7 is out of scope for v1 and should stay out.**

### 6.8 Review queue UX

- Cases post to a private `#verification-review` channel as an embed: user mention, submitted values, all reason codes, what the lookup and roster say, a diff when there's a mismatch, and **Discord account age + server join date** (§6.4).
- **`roster_miss` embeds lead with `NOT ON ROSTER`** and render the license lookup as muted secondary context — never a green check at the top. The officer's question is "is this a member of our club?", not "is this a real license?" The bot already answered the second one, and it's the wrong question (§14.3).
- Buttons: **Approve** / **Deny** / **Approve as Guest**. Deny prompts for an optional reason.
- Resolution DMs the user (falling back to an ephemeral follow-up if DMs are closed) and writes to the audit log with the resolving officer's ID.
- Anyone with the officer role can resolve; no assignment or claiming mechanics. This is a club, not a helpdesk.
- **Staleness alert:** anything unresolved past 48h re-pings the channel. The failure mode of an approval queue in a volunteer org is silence, not bad decisions. With `first_claim` now queued, a stalled queue means real members sitting in limbo — this alert is load-bearing, not a nicety.

### 6.9 Officer commands

| Command | Purpose |
|---|---|
| `/roster sync` | Force a roster re-sync now; report added/changed/removed |
| `/roster status` | Last sync time, row count, last error |
| `/claim release <callsign>` | Release a claimed callsign (typo, departed member, correction) |
| `/claim transfer <old> <new>` | Migrate a claim to a new callsign — vanity call changes (§12) |
| `/claim who <callsign>` | Show who holds a callsign and when they claimed it |
| `/verify user <@user>` | Re-run verification for a member; also the manual-override path |
| `/rules bump <version>` | Publish a new rules version and trigger re-acceptance (§8) |

All privileged commands write to the audit log with the invoking officer's ID.

---

## 7. Name Handling, Display-Name Normalization & Sanitization

### 7.0 Scope: the bot renames callsign holders and nobody else

**Guests keep their own Discord name. The bot does not touch it.**

G2's purpose is to make a Discord person mappable to a callsign to a real human. **A guest has no callsign, so there is nothing to map** — imposing `Jim F` on them achieves nothing while overwriting an identity they chose. Worse, it renders them as *a member with a missing callsign*: ambiguous in precisely the wrong direction.

Leaving guests alone makes the nickname format **a signal**, readable at a glance in the member list:

| Nickname shape | Means |
|---|---|
| `Jim F - KE4CAR` | Verified club member |
| Anything else | Guest |

This doesn't compromise G2 — it sharpens it. It also satisfies §2.1 (guests get zero friction) and removes a small act of ownership over someone who hasn't joined anything yet.

**Consequences, all simplifying:**
- No `Manage Nicknames` failures on guests, and no role-hierarchy problem for the largest and least privileged group.
- No 32-character truncation case for guests.
- The "two `Jim F` guests collide" case ceases to exist.
- **The sanitization problem shrinks to near-nothing.** See §7.3.
- The rename becomes a *moment*: it happens exactly once, when a guest earns a callsign (§9). That's a promotion, not a processing step — announce it (§6.6).

The bot still **collects** a preferred first name from guests, purely so it can greet them by it and so officers know who's who. It just never writes it to their nickname. Warn a converting guest before the rename — *"your nickname will become Jim F - KE4CAR"* — so it isn't a surprise.

### 7.1 Target format (callsign holders only)

`Jim F - KE4CAR`

- `Jim` — user's preferred first name (self-reported)
- `F` — first initial of last name
- `KE4CAR` — validated callsign

**Last-name source:** ask in the modal, but pre-fill/suggest from the Tier 2 lookup when available (US). Display only the initial, so full legal names are never published. Works for foreign hams, avoids parsing FCC legal names into display form, keeps the privacy optics clean.

### 7.2 Fallbacks and edge cases

| Case | Result |
|---|---|
| First + last + callsign | `Jim F - KE4CAR` |
| First + callsign, no last name and no lookup | `Jim - KE4CAR` |
| **No callsign (guest)** | **Nickname untouched. Bot takes no action.** |
| Over Discord's 32-char cap | Truncate the **first name** first, then drop the last initial; never truncate the callsign — it's the identity anchor |
| Re-running verification | **Overwrite** cleanly. Never append |
| Guest → member | Set the nickname for the first time. Warn first; celebrate publicly (§6.6) |
| Server owner | Cannot be renamed by anyone. Detect, skip silently, still grant roles |
| Bot role position | Bot needs **Manage Nicknames** and its role must sit above every member it renames |

### 7.3 Sanitization — and why it's now nearly a non-issue

**Who the bot renames is now exactly: people who supplied a valid callsign, matched the club roster, and passed §6.5 or officer review.** That is a population of licensed adults whose legal name sits in a federal database and whose dues record the club holds. The scope of "someone might set a vulgar name" has collapsed to "a verified, accountable, known club member might."

That is a moderation problem with a name attached to it. It is not a bot problem.

**Keep (structural, cheap, no judgment):**
- Allowed characters only: Unicode letters, space, hyphen, apostrophe, period
- Length: 1–20 characters per field
- No emoji, no zero-width characters, no combining-character stacks (Zalgo), no RTL/LTR direction overrides, no homoglyph substitution — normalize to NFKC and confusable-fold before validating
- No leading/trailing/repeated whitespace; not composed entirely of punctuation

These stop the *structural* attacks, which are the interesting ones, and they judge nothing.

**Dropped: the slur denylist.** It was always the weakest control here — denylists lose to the Scunthorpe problem, and this demographic genuinely contains members named Dick, Cummings, and Wang, whose real names it would reject at the door. Now that it would only ever guard verified members, the false-positive risk isn't worth the near-zero benefit. Two existing mechanisms already cover the case: the name cross-check (§6.3) flags a submitted name that doesn't resemble the FCC record, and moderators hold `Manage Nicknames` with a full audit trail.

**Deliberately not doing:** general profanity detection, sentiment analysis, or an LLM name-taste filter. A verified member who puts a slur in their name isn't a renaming problem — they're a banning problem.

---

## 8. Rules Acceptance

- Rules text authored separately and finalized before go-live (placeholder for now).
- Present rules (link to `#rules` or an embed) and require explicit **I Agree** before any role is granted.
- Record acceptance as `{user_id, rules_version, accepted_at}` in the audit log.
- **Versioning: yes.** `rules_version` is an integer in config. It costs one column now and is impossible to retrofit later once you have hundreds of members and no idea who agreed to what.
- `/rules bump` sets a new version and moves everyone below it into a re-acceptance state: they keep their roles but get a prompt on next activity. Do **not** mass-strip roles on a rules bump — that's a self-inflicted outage.
- If the user declines: no roles, stays `@unverified`, can restart anytime.

---

## 9. Role Assignment Logic

**States:** `@unverified` (on join) → `@pending-verification` | `@guest` | `@club-member` (+ `@club-officer`)

| Condition | Result |
|---|---|
| Callsign already claimed by **this** user | Re-grant prior roles, automatic (rejoin / re-verify) |
| Callsign in roster, member, **first claim** | **Queue** (`first_claim`). On approval: `@club-member` |
| Callsign in roster, member, first claim, **email code verified** (§6.5) | `@club-member`, automatic |
| Callsign in roster, officer flag | **Queue** *always*, even with an email code. On approval: `@club-member` + `@club-officer` |
| Valid callsign, not on roster | **Queue** (`roster_miss`). On denial: message user to contact an officer |
| Collision with another user's claim | **Queue.** No roles pending resolution (§6.4) |
| No callsign, rules accepted | `@guest`, automatic. **Nickname untouched** (§7.0) |
| Rules declined | No roles, regardless of callsign |

**Guest nickname rule (§7.0, §14.3):** the bot never renames a guest. The one exception is an **approved** `roster_miss` — a licensed visitor an officer has cleared — who may opt into the `Jim F - W1ABC` format. Unreviewed or denied → nickname untouched. The bot must never publish an unvalidated identity claim about a real licensee.

All role grants require: valid shape **and** no unresolved collision **and** rules accepted. `@unverified` is removed on any successful terminal outcome.

**Guest → member upgrade: this is the conversion G0a exists for.** A `@guest` re-runs the flow via the same persistent button once licensed. The bot must remove `@guest`, grant `@club-member`, and rewrite the nickname — idempotently, no duplicate roles, no mangled name.

Requirements specific to this path:
- **Make it discoverable.** A guest who passes their Technician exam should not have to wonder how to update their status. The `@guest` welcome message says how, and the `#welcome` button is permanent.
- **Celebrate it publicly.** The bot announces new licensees in a public channel (§6.6). This is the single best engagement moment the club gets — someone just joined the hobby, partly because of this server. Squandering it on a silent role swap is a waste. Make the announcement warm and make it obvious how to congratulate them.
- **Track it.** Time from `@guest` to `@club-member` is the club's conversion metric (§13.1).

This path will get more use than a security-first reading of this document suggests. A radio club exists partly to produce new hams.

---

## 10. Roster Source & Sync

### 10.1 Architecture decision — decouple first, choose second

**The bot must never call the roster source at request time.** It syncs to the local SQLite `roster` table on a schedule (hourly) and on demand (`/roster sync`), and serves every verification from that snapshot.

This matters more than the vendor choice:

- **Availability (G7):** a Sheets outage, an expired token, or a rate limit can't stop onboarding. Worst case, the snapshot is an hour stale.
- **Ownership (G6):** the club always holds a complete local copy. Ownership comes from *having the data*, not from hosting the editor.
- **Reversibility:** the source sits behind a `RosterSource` interface with one method — return a list of `{callsign, full_name, is_officer, notes}`. Swapping Sheets for Baserow later is one adapter, not a migration.

Because of this, the vendor choice is genuinely low-stakes. Decide it in five minutes and move on.

### 10.2 Options

| Option | Pro | Con |
|---|---|---|
| **Google Sheets** | Every officer already knows it; free; granular sharing; read-only service account is simple | Not self-hosted; a well-meaning officer can sort one column and shear the data |
| **Airtable** | Real field types, views, forms; a checkbox for `is_officer` is actually a checkbox | SaaS with a history of pricing changes; free-tier record caps; more lock-in than Sheets |
| **Grist** (self-host) | Spreadsheet-like, genuinely good, exports cleanly, docs are portable single files | You now run a server |
| **Baserow / NocoDB** (self-host) | Airtable-shaped, open source, API-first | You now run a server |
| **CSV in a private git repo** | Free, versioned, diffable, maximally owned, zero infra | Editing via GitHub's web UI is a hard sell to non-technical officers |
| **SQLite + officer slash-commands** | Zero extra infra, zero extra accounts, single source of truth | Bulk edits and annual roster reconciliation are miserable |

### 10.3 Recommendation

**Start with Google Sheets.** Take the self-host path only if a specific person actively wants to run it.

The self-hosting instinct is right about ownership and wrong about cost. A Baserow instance for ~100 rows means a VPS, TLS certs, backups, and version upgrades — and that burden lands permanently on whoever set it up. Volunteer infrastructure decays when its maintainer moves, changes jobs, or becomes a Silent Key. The failure mode isn't "vendor raises prices," it's "nobody has the server password anymore."

The snapshot-plus-export design already delivers what self-hosting was meant to protect. If someone does step up: **Grist** is the pick — self-hostable, spreadsheet-shaped for the officers, and its documents are portable files rather than a database you have to dump.

### 10.4 Sync requirements

- Parse by **header name**, never column index. Officers will insert a column.
- **Validate on load.** Malformed callsign, duplicate callsign, missing name → skip the row, log it, alert `#verification-review`. One bad row must not fail the sync.
- **Reject wholesale replacement on suspicious diffs.** If a sync would remove more than ~25% of the roster, refuse it, keep the prior snapshot, and alert. This is the "someone opened the sheet and hit undo" guard.
- Sync is **additive to roles, never subtractive**: removing someone from the roster raises an alert, it does not auto-strip their roles. Deprivileging is a human decision.
- **Nightly export** of `roster`, `claimed`, and `audit_log` to timestamped CSV, committed to a private repo or object storage. This is the actual ownership guarantee — write it in v1, not "later."
- `/roster status` surfaces last sync, row count, and last error so a stale sync is visible without reading logs.

---

## 11. Lookup Strategy (Decision Recorded)

- **v1:** US live lookup via `callook.info` / HamDB — free, keyless JSON wrappers over FCC public data. Worldwide members pass on Tier 1 regex + roster match only.
- **Why not FCC/ARRL direct:** FCC offers a web search UI or bulk database downloads, not a per-callsign JSON API. ARRL offers no lookup API (its developer program is Logbook of the World only). Tier 2 services are convenience wrappers over the same FCC data.

### 11.1 What the lookup actually returns — and what it doesn't

Verified against the callook.info API (July 2026): the response carries **callsign, licensee name, mailing address, license class, status, grant/expiry/last-action dates, FRN, previous callsign, trustee (for club calls), and a computed grid square.** <cite index="7-1">A real response shows exactly these fields — name, address lines, operator class, grid square, expiry, FRN, ULS URL, status, trustee, type</cite>. <cite index="2-1">Callook serves only US data and refreshes daily from FCC snapshots</cite>.

**There is no email address field.** <cite index="18-1">The FCC's own ULS public search likewise returns status, expiration, license class, and mailing address</cite> — not email. QRZ exposes an email but requires a paid subscription *and* the licensee's own opt-in.

**This is not a gap to work around. It's the correct state of affairs.**

Even a hypothetical FCC-sourced email would be useless here, and §6.5 explains why: that mechanism works *because* the address is **the club's own membership record** — it reads link 2 (§14.1.1). An FCC email would read link 1, which the FCC already established and which the bot has no business re-verifying (§14.1.2). Follow it through: a non-member who proves they control W1ABC's inbox has proven they are W1ABC. **They still are not a member of your club.** They still miss the roster and still land in `roster_miss`. The roster is the gate — verifying a stranger's callsign is a verification with no use, which is the identical reason license-class roles are rejected (§14.3).

**Note also what the lookup *does* return: a home mailing address.** That's why §6.7's postcard option was even conceivable, and it's a privacy consideration — the bot should hold that data in the `lookup_cache` only, use it for nothing but name matching, and let it expire with the TTL (§13).

### 11.2 Future options

- **Option A:** self-host the FCC ULS weekly database dump in local SQLite — full independence, no rate limits, survives ULS outages. This is the *one* self-hosting move with a clear payoff (§10.3), since it removes a runtime dependency rather than adding one. It does not change §11.1: more complete FCC data still only strengthens link 1, which isn't the weak link.
- **Option B:** QRZ.com or HamQTH for worldwide live lookup. QRZ needs a paid subscription login; HamQTH needs a free account.
- <cite index="6-1">`miaowware/callsignlookuptools` abstracts QRZ, Callook, HamQTH, and QRZCQ behind one Python interface with sync and async support</cite>, easing later expansion.

### 11.3 Outages are routine — this is why G7 exists

Not hypothetical. <cite index="13-1">ULS is scheduled down for maintenance from 6:00 PM Friday July 17, 2026 through 8:00 AM Monday July 20, 2026</cite>, and <cite index="2-1">callook itself goes offline briefly during its daily database refresh</cite>. A multi-day FCC outage would land squarely in launch week if the bot shipped now. Tier 2 **must** degrade silently to Tier 1 + roster (§5 step 6). Onboarding cannot notice.

---

## 12. Ham-Specific Edge Cases

Things that are obvious to a ham and invisible to a general-purpose bot design:

- **Vanity callsign changes.** Members change callsigns — KE4CAR upgrades and becomes W4XYZ. The roster is keyed by callsign, so this looks exactly like a `roster_miss` for a stranger. `/claim transfer <old> <new>` handles it; the audit log preserves both. Expect this a few times a year.
- **Club station callsigns.** If the club holds a station call under a trustee, it is not a person. Exclude it from the roster sync or it becomes claimable.
- **Expired licenses.** FCC data includes expiry. A member whose ticket lapsed is still a club member and probably a friend of 20 years. Recommendation: `lookup_miss` → queue, never auto-strip. Do not build license policing into an onboarding bot.
- **Multiple callsigns.** Some hams hold more than one. The `claimed` ledger is `callsign → user`, which permits it; the nickname shows one. Pick the roster-matching call for display.
- **Callsign as identity.** Worth stating plainly: a US callsign maps to a legal name and a mailing address in a public database. That's why the callsign is the identity anchor and the self-reported name is decoration.

---

## 13. Non-Functional Requirements

| Area | Requirement |
|---|---|
| **Persistence** | `claimed` ledger, `verifications`, and audit log survive restarts (SQLite). Non-negotiable — the dedup guard resets otherwise. |
| **Permissions/Intents** | `SERVER MEMBERS` privileged intent (for join events). Bot permissions: Manage Roles, Manage Nicknames. Bot role positioned above all managed roles. |
| **Rate limiting** | Throttle Tier 2 calls; cache lookup results (~24h TTL) to avoid hammering free community services. These are run as a courtesy; act accordingly. |
| **Failure handling** | External lookup timeout/outage must never block onboarding. Degrade to Tier 1 + roster (G7). Same for roster-source outages — serve the snapshot. |
| **Privacy** | Store the minimum needed. Display only the first initial of the last name. Document what's retained and for how long. Full FCC names used for matching are held in the lookup cache only and expire with it. |
| **Config over code** | Roster source, role IDs, channel IDs, rules version, review thresholds all in config/data files. |
| **Moderator tooling** | Review queue channel; the §6.4 command set; staleness alerts. |
| **Idempotency** | Re-verification, rejoins, and guest→member upgrades produce consistent state — no duplicate roles, no mangled or appended nicknames. Compute the target state and reconcile; never blind-append. |
| **Departures** | On member leave: keep the claim (it's still their callsign, they may rejoin), log the departure. Release only by officer command. |
| **Backups** | Nightly SQLite backup + CSV export (§10.4). Verify restore once before go-live, not after the first incident. |
| **Observability** | Log every state transition with reason codes. When a member says "the bot broke," the answer must be reconstructible without a debugger. |

### 13.1 Funnel reporting (required by G0)

If growth is the purpose, the officers need to know whether it's working — and if the bot doesn't tell them, nobody will measure it. The data is already in `verifications` and `audit_log`; this is a query and a monthly post, not a project.

`/stats` plus an automated monthly summary to the officer channel:

| Metric | Why |
|---|---|
| Guests joined this period | Top of funnel. Is the front door getting traffic? |
| **Guest → member conversions, and median time to convert** | **The G0a metric.** The number the club is actually trying to move. |
| Members verified / still unverified | Backfill completeness |
| Drop-off: `@unverified` who opened the modal and never finished | Onboarding friction, measured directly. A spike means the flow is broken or intimidating (§3). |
| Queue volume by reason code, median time-to-resolution | Whether §6.1 is a speed bump or a wall |
| `first_claim` resolved by email code vs officer review | Whether §6.5 is carrying the load it's meant to (§14.1.2) |

**Drop-off is the metric most likely to be ignored and most likely to matter.** People who bounce off the modal leave no trace and file no complaints — they're invisible precisely because they're gone. Instrument it from day one.

---

## 14. Security Model (Stated Plainly)

### 14.1 The trust chain — what this bot is actually for

Identity here rests on three links. Only one of them is the bot's problem:

| Link | Binding | Strength | Established by |
|---|---|---|---|
| 1 | Callsign ↔ legal person | **Strong, uniform** | The FCC. Legal name and address supplied under penalty, held in a public database. Done before the club ever met you — and done at *licensing*, not at first transmission. |
| 2 | Legal person ↔ club member | **Variable — see below** | The club |
| 3 | Club member ↔ Discord account | **Absent** | Nothing. This is new. |

**The bot exists to build link 3, and only link 3.**

### 14.1.1 Link 2 has two forms, and the club has both kinds of member

The v2 draft of this section described link 2 as uniformly strong — "a voice recognized on the net over years." **That was wrong, and wrong in the direction that matters.** It describes the membership the club already has, not the membership G0 is trying to recruit.

| Form | Consists of | Strength | Automatable? |
|---|---|---|---|
| **Social** | Meetings attended, a voice known on the air, decades of presence, someone who vouched | Very strong | **No.** An officer either recognizes you or doesn't. |
| **Records** | A dues payment, a name, an email address on file | Thinner, but real | **Yes.** |

A thirty-year member has the social form. A member licensed two years ago who has **never keyed a mic**, joined online, paid dues by card, and has met no officer in person has *only the records form* — and that is an entirely ordinary way to be a licensed ham today. Many people get licensed for emergency preparedness, for a class, or out of technical interest and never transmit. **Link 1 is unaffected by this**: the FCC binding comes from the license application, not from operating.

For that member, **link 2 literally is the dues record.**

### 14.1.2 Design consequences

- **Don't re-verify links 1 and 2.** Anything the bot could do there would be weaker than what already exists. An ID-check step would be *re-doing the FCC's work, worse*. This is why Tier 2 lookup is a convenience and a flag, never a gate (§11), and why the name cross-check is deliberately toothless (§6.3).
- **Read link 2 in whichever form the member actually has.** This is the correction that drives §6.5:
  - **Roster email code reads the records form.** It is not a weak proxy for social recognition — for a member who joined online and paid from an address, *"controls the email that paid the dues"* **is the club's own record of their membership**, read directly. It doesn't stand in for the real thing. It is the real thing.
  - **Officer recognition reads the social form.** Irreplaceable where it exists; simply unavailable for a member nobody has met.
- **Therefore email is primary and officer review is the fallback — not the reverse.** The reverse fails hardest for exactly the members G0 targets. A nineteen-year-old licensed last spring who paid dues online and has met no one would land in a queue where an officer squints and says *"...who?"* — and that is the growth demographic being served worst by the security control.
- **Never gate on operating.** See §6.7.

### 14.2 Threat table

What this system does and does not defend against, so nobody mistakes it for more than it is:

| Threat | Defense | Residual risk |
|---|---|---|
| Two people claim one callsign | `claimed` ledger + neutral collision review (§6.4) | Resolution depends on an officer recognizing the real member |
| Casual impersonation of a known member | `first_claim` officer review (§6.1); name cross-check | — |
| **Impersonator claims a real member's callsign before that member joins** | `first_claim` **never auto-approves** (§6.1); pre-seeded ledger (§15); public verification log (§6.6); optional roster email code (§6.5) | **Accepted, reduced.** Requires an officer to not recognize the imposture *and* the real member to never show up. Self-detecting when they do. |
| Determined impersonation (attacker reads the name off QRZ, ages the account) | Officer recognition; account-age signal; audit log; easy revocation | **Accepted.** Detectable after the fact, not preventable at the door |
| Unlicensed person claiming a real callsign | Tier 2 lookup + roster match | Foreign callsigns have no Tier 2 in v1 |
| Officer role escalation | Mandatory human approval, unconditionally | — |
| Offensive display names | **Scope collapsed by §7.0** — the bot only renames verified members. Structural format constraints + mod tooling | **Accepted.** Not solvable by a bot, and now barely worth trying: the exposed population is licensed, named, and dues-paying |
| **Impersonating a ham who is not a club member** (claim W1ABC, a real licensee with no club connection) | **Structurally unrewarding — see §14.3.** Roster is the gate, so a valid license grants nothing. Falls to `roster_miss` review; callsign displayed only on officer approval | Reputational harm to an uninvolved third party if approved carelessly. Takedown path in §14.4 |

### 14.3 Why third-party impersonation has no payoff — and how to keep it that way

Trace it: someone claims W1ABC, a real licensed ham in Vermont who has never heard of your club. Shape check passes. Callook confirms a valid license. **Roster lookup misses.** → `roster_miss` → queue → denied, or approved as a guest.

The impersonation bought nothing. A valid callsign gets you exactly as far as no callsign does — guest. In fact it gets you *less* than clicking "I don't have one," because now a human is reading your submission. There is no incentive, so there is no meaningful threat.

**This is an emergent property of "roster is the gate, not the license," and it is load-bearing.** It was not designed in; it fell out. Two ways to break it later — both are hereby closed:

1. **Never grant any role from an FCC lookup.** The instant a lookup result confers privilege, impersonating a stranger acquires a payoff — and the victim is structurally incapable of noticing, because they are not in the server and never will be. This is the decisive argument against license-class roles (§16).
2. **Never display an unverified callsign.** `Bob S - W1ABC` in the member list is a *published assertion about a real person's identity*. Since the bot no longer renames guests at all (§7.0), the default is already safe: an unreviewed or denied `roster_miss` leaves the nickname untouched. The only way a non-roster callsign reaches a nickname is an **officer-approved** `roster_miss` — a visiting ham a human has cleared.

**Guard against erosion in the queue UX.** The realistic failure is an officer seeing an embed headed *"✓ Valid license — Robert Smith, VT"* and concluding *seems legit, he's a real ham, let him in.* But the bot already answered that question, and it is the wrong question. The only question on a `roster_miss` is **"is this person a member of our club?"** Therefore: **a `roster_miss` embed must lead with `NOT ON ROSTER`**, and render the license lookup as secondary, muted context — never as a green check at the top. The UI should prime link 2, not link 1 (§14.1).

Note the asymmetry that makes this threat unlike the others: every other harm in this document lands on someone present who can complain. This one lands on an absent third party who will likely never know. That's precisely why the defense must be structural — nobody is coming to report it.

### 14.4 Third-party takedown path

If W1ABC ever does learn his callsign is in use in your server, he needs a door to knock on:

- Publish a contact address (officer email or the club's public address) in the server description and `#rules`.
- Handling: `/claim release <callsign>` → strip callsign from nickname → `/claim who` and the audit log establish who did it and when → moderate the account as the situation warrants.
- No verification burden on the complainant beyond the obvious. If someone bothers to write in claiming W1ABC and the roster doesn't list W1ABC, there is nothing to lose by releasing it.
- Log the complaint and resolution. One line, and it demonstrates the club took it seriously.

Worth stating plainly for the officers who'll enforce this: transmitting under a callsign that isn't yours is a serious matter with the FCC. Doing it in a Discord isn't illegal — but in ham culture your callsign *is* your name, and the club should treat misuse as the real breach of norms it is, not as a nickname dispute.

### 14.5 Honest summary

This raises the effort required to impersonate a **member** from *zero* to *fooling someone who knows them*, makes every attempt auditable, and makes every mistake cheaply reversible. Impersonating a **non-member** ham is structurally unrewarding and stays that way as long as §14.3's two rules hold. It is not authentication, and it does not try to be — links 1 and 2 (§14.1) already carry that weight, and carry it better than the bot could.

Club membership is a social fact. The roster plus a moderator's eyes are the real gate. The bot's job is to put the right facts in front of those eyes at the right moment — and, per §14.3, to avoid putting the *wrong* facts there.

**Assumed threat model:** the realistic adversary is a previously-removed member returning under a friend's callsign, or a troll with a grudge. Not a sophisticated or well-resourced attacker. Every "accepted" above is accepted *against that model*. If the club's threat model changes, §6.7 is where to look.

---

## 15. Rollout & Backfill

The bot handles joins. It does not handle the people already in the server — and **the backfill window is when impersonation risk is highest**, because every member's callsign is simultaneously unclaimed.

**1. Seed the ledger before opening verification.** The single highest-value step in the entire project. Every pre-seeded callsign is one that can never be stolen. Three sources, in order of yield:

- **Parse existing nicknames.** In a ham Discord a large share of members already carry their callsign in their display name. Extract, shape-validate, cross-check against the roster, have an officer review the list, seed the claims.
- **Officer knowledge.** Officers can identify a meaningful chunk of the server by hand. Let them.
- **Pre-launch public claim thread.** A pinned thread: *"post your callsign before the bot goes live."* This looks like it has the same trust problem, but it doesn't — it's **public**, in front of the whole club, with officers watching. That's the same crowd-detection property as §6.6, applied at exactly the moment risk is highest.

Anything left unseeded goes through `first_claim` review. That's the intended fallback, not a failure.

**2. Announce with a deadline.** Persistent verification button in `#welcome`, ~2–4 weeks.

**3. Elevated review during backfill.** Every callsign is unclaimed at once — this is the *only* moment the attack is easy. Watch the queue daily; two officers on it, not one. Volume is front-loaded and one-time, so scrutiny is cheapest exactly when it's most valuable. Expect the queue's worst latency here and staff for it (§6.8).

**4. Then enforce — but enforce *little*.** After the deadline, `@unverified` loses access to member channels. Don't kick anyone. Hams go on vacation, deploy, get sick, and disappear into contest season.

**5. Pilot first.** Run officers through it before anyone else. They'll find the modal typos, and they're the ones who'll be answering questions.

**6. Post-launch sweep.** Once the dust settles, report on: roster members with no claim (who's still missing?), and claimed accounts with zero messages ever (who verified and vanished?). The second list is where a successful impersonation would sit quietly.


### 15.1 What actually sits behind the member gate

G0 makes this a live question rather than a formality. **A public front door with nothing visible behind it converts nobody.** If a curious visitor accepts the rules and finds three empty channels, they leave and don't come back — and that failure is invisible, because they never spoke.

Default to **open**. The burden of proof is on locking a channel, not on opening one.

| Tier | Contents |
|---|---|
| **`@unverified`** (pre-modal) | `#welcome`, `#rules` only. Raid control. |
| **`@guest`** | **Most of the server.** `#general`, `#getting-licensed`, `#elmering`, help channels, technical chat, events. Everything a prospect needs to see to want in. |
| **`@club-member`** | The genuinely member-only short list: club business, dues, elections, repeater admin, member directory. |
| **`@club-officer`** | Officer coordination, `#verification-review`. |

Officers should have to *justify* each channel placed above `@guest`. The instinct to lock things is strong and mostly wrong here — a locked channel costs a real conversion and buys, usually, nothing.
---

## 16. Open Questions

**Resolved in this revision:**
- ~~Self-reported last name, auto-lookup, or hybrid?~~ → Hybrid: self-report, pre-fill from lookup when available (§7.1)
- ~~Failed US live lookup: warn-and-allow or hard block?~~ → Neither: queue for review (§6.1)
- ~~Guest role: yes or no?~~ → Yes (§9)
- ~~Rules versioning: re-acceptance on change?~~ → Yes, versioned; re-prompt without stripping roles (§8)
- ~~Who maintains the roster, and in what format?~~ → Officers, Google Sheets, synced to local SQLite (§10) — *pending name of owner, below*
- ~~License-class roles (Tech / General / Extra)?~~ → **No. Closed by §14.3.** Granting any role from an FCC lookup gives third-party callsign impersonation a payoff, against a victim structurally unable to notice. Not worth a cosmetic badge. Now a design rule, not a backlog item.
- ~~Build roster email verification, or start with the officer queue alone?~~ → **Build it, in v1, as the primary member path.** Closed by §14.1.1: officer recognition only reads link 2's *social* form, which the growth demographic doesn't have. Deferring it means the newest members are served worst by the security control.
- ~~Net check-in verification?~~ → **No. Rejected on mission grounds (§6.7).** It gates club access on operating, which excludes licensed hams who don't transmit — an ordinary group and exactly who G0 targets.
- ~~Should the bot normalize guest display names?~~ → **No. Guests keep their Discord name (§7.0).** G2 exists to map a person to a callsign; guests have no callsign, so the format buys nothing and costs an identity. The format becomes a verification signal instead.
- ~~Denylist contents — who writes the slur list?~~ → **Nobody. Denylist dropped (§7.3).** Downstream of §7.0: the bot now renames only verified members whose legal names are in a federal database and whose dues the club holds. False positives against real names ("Dick," "Cummings") outweigh a near-zero benefit. Character whitelist and length cap remain.
- ~~Can we look up emails for non-member hams?~~ → **No, and we don't want to (§11.1).** Callook and FCC ULS return mailing addresses, not emails; QRZ requires a paid plan plus licensee opt-in. Immaterial regardless: a non-member proving they own their callsign still isn't a member. The roster is the gate.

**Still open:**

1. **Who specifically owns the roster?** Not "the officers" — a named person, with a named backup. Every roster problem in §10.4 becomes their pager. If nobody wants it, that's real information about which option to pick.
2. **What's the newsletter bounce rate?** (§6.5) An address exists for every member; the open question is how many are *live*. This is measurable today from the last newsletter send, costs nothing, and determines whether §6.5 is a clean build or gated behind an email-backfill project. **Answer this first — it's the cheapest unblock on the list.**
3. **Which channels sit above `@guest`?** (§15.1) Needs an actual list, argued channel by channel, before launch. Default open; justify each lock.
4. **Where does verification happen** — dedicated `#welcome` channel, or Discord's native Onboarding as the entry point that routes to the bot? Native Onboarding is prettier and adds a moving part; the persistent-button design works either way.
5. **Retention.** How long does the audit log live? Indefinite is the easy answer and probably right at this scale, but §13 says "document what's retained" and that means answering this.
6. **Is `@pending-verification` visibly distinct?** A colored role tells everyone a member is in limbo. A silent one is kinder but makes stalled cases invisible. Lean silent + staleness alerts.
7. **Do licensed non-members get a distinct role, and may they display their callsign?** An approved `roster_miss` is a visiting ham from another club — a warm prospect under G0a. Distinguishing them from an unlicensed guest costs one role; letting them opt into the `Jim F - W1ABC` format is the one sanctioned exception to §7.0. Probably yes to both, but not before launch.
