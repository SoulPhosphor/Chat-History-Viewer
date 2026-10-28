# Working rules for Claude in this repo

These are hard rules. Follow them exactly.

## Repair plan

- Repair work follows [AUDIT.md](AUDIT.md). Read its "Session instructions
  (read first)" section before starting, and follow its rules and order.
- Codex reviews cost the user money. **Opening a pull request starts a Codex
  review by itself** (the repo's Codex setting). So:
  - **Never comment `@codex review` on a new pull request.** That starts a
    second, paid review of the same code. This overrides the "post
    `@codex review`" step in AUDIT.md rule 8.
  - Comment `@codex review` only to review **new commits** pushed to a pull
    request that is already open, and only when they are a substantial code
    change. Documentation and small fixes get no new review.
  - **How to know the review came:** after opening the pull request,
    subscribe to its activity. The Codex summary comment says Running, then
    Completed; its findings arrive as review comments. Wait for Completed,
    make the reasonable changes it asks for (not ones that go against what
    the user said), reply on each finding not followed, then merge.
  - If the review still says Running after 20 minutes, tell the user instead
    of asking for another one; they decide whether to retry or merge without
    it.
- **Merge the pull request yourself when the session is done**: once the
  Codex review (if any) is handled and the tests (CI) pass, merge it. Don't leave it
  waiting for the user (AUDIT.md rule 8).
- **End every session by pasting the next session's prompt in full in the
  chat reply** (AUDIT.md rule 6), not just pointing at AUDIT.md.

## Interaction

- **NEVER use pop-up / interactive multiple-choice questions** (the
  `AskUserQuestion` tool, chips, option cards, any modal picker). They are not
  acceptable here. If something genuinely needs clarifying, ask in **plain text**
  in your normal reply — but prefer to follow the instructions already given and
  just do the work.
- Do not re-ask things the user already answered. Read the whole message before
  acting.
- Keep replies short. Do the work; don't narrate options you won't take.

## Behavior

- Follow the user's instructions literally. Do not add, remove, or redesign
  features the user did not ask for. "Make it work" ≠ "redesign it."
- When told to change one thing, change that thing only.
- Use concise, direct language, plain english language.
- With UI elements consider that later color theming will happen and elements
  should be adjustable from anywhere.
- In practice that means: no hard-coded colours, sizes, or weights in a rule
  that a theme would want to change. Put the value in a CSS variable on
  `:root` and use `var(--x)` everywhere, so one override rethemes the lot.

## Design choices belong to the user

- **Ask before deciding** anything about what a feature does or how it
  behaves: layout, controls, how a screen flows, what is shown or hidden,
  defaults and automatic behavior, feature scope, which outside services or
  AI companies are used, and privacy, storage or logging behavior. User-facing
  wording describes approved behavior; it never decides it.
- **This covers plans too.** Never write a plan, placeholder, setting, test or
  "suggested default" that settles an undecided choice. If an instruction (a
  handoff prompt, AUDIT.md, an older note) asks for design work, check with
  the user first. Documents and existing code never outrank the user.
- **How to ask:** in plain text, one focused question at a time. Say what the
  user would see change, give a recommendation if there is a good reason for
  one, and say that nothing has been changed yet. Then stop and wait.
- **Talking is not approval.** A question, a "maybe" or a preference is not a
  go-ahead.
- Internal choices that nobody sees and that don't change behavior or data
  (names, helper functions, tests of approved behavior) don't need asking.
- **Usage costs the user money.** Ask before a broad exploratory pass, several
  agents, or a Codex review of anything but a substantial code change.

## Appearance follows the project's styles

- **Don't ask the user how things should look.** New or changed UI uses the
  project's existing styles (STYLE_GUIDE.md, `static/theme.css`). Ask about
  appearance only when no existing style fits. If the user asks for a
  different style, follow what they asked.
- **Before changing UI:** read STYLE_GUIDE.md's "Which existing styles to
  use", then look at the target screen and its current code yourself.
- Reuse the existing classes for buttons, dialogs, rows, headers, fields,
  menus, chips, section titles, and loading, empty, warning and error states.
  Don't make near-copies of a style or copy raw values because it's quicker.
- **Matching another screen:** go part by part (header, buttons, fields,
  rows, dialogs, spacing) and use the shared style for each part. Don't copy
  the other screen whole, and don't copy its raw values.
- If no shared style fits, stop and ask before inventing one. Changing a
  shared style that would alter other screens also needs the user's okay.
- A product requirement wins over reuse: never drop or move a control the
  user asked for because a shared style or layout makes it awkward.
- Only call something consistent or converted when every repeated part of it
  really uses the shared styles.

## Product notes (labels + bulk tool)

- **Conversations can have multiple labels** (not one).
- **Label names are optional.** A label with no name is a colour-only square.
  Enabling labels never forces the user to type names.
- **Sidebar placement.** One bare square (no name shown) sits inline before the
  title. As soon as there are two or more squares, or any square shows a name,
  they all move to their own row under the date/messages line. Bare squares wrap
  freely across that row; named ones are capped at three per line
  (Box Label Box Label Box Label, then wrap).
- **One element everywhere.** The chip beside the title, the chips under the
  date/messages line, and the chips in the thread header are the same
  `.label-chip`, sized from the same CSS variables.
- **Square + Label reminder.** While "Square + Label" is selected and any square
  still has no name, show the warning by the display dropdown. It clears when
  every square has a name and comes back if a name is later emptied. When no
  square has a name, put the cursor in the top name box.
- **Labels are never in the ⋮ menus.** Squares are the only place labels are
  set: click a square to cycle it, right-click any square for the full picker.
- **The right-click picker** lists every label with its colour AND its name,
  whatever the display mode is set to, and leaves out the square that was
  right-clicked (that one is already active). "Blank" clears them all.
- **Adding a label picks its colour.** After each add the colour box jumps to a
  fresh colour that is not in use and sits as far as possible from the ones
  that are, so a run of new labels lands in different colour families rather
  than five shades of one.
- **Reordering labels**: a grip on the row's right edge, no up/down arrows.
  Drag it, or click it to pick the row up and click where it should go, or
  focus it and use ↑ / ↓ / Home / End. Every move saves at once and is
  announced to screen readers.
- **Bulk tool Preview**: the Preview button stays. Preview **temporarily applies
  all the current filter criteria to the conversation list** so the user can
  review which conversations match — it is not a stats-only readout. The user is
  **never required** to press Preview before Apply; Apply works on its own.

## Style references

Read [STYLE_GUIDE.md](STYLE_GUIDE.md) before changing UI markup or styles.
`static/theme.css` is the central appearance-value source; component CSS uses
those variables. Keep decorative CSS out of HTML and JavaScript. Preserve
existing visual differences unless the user asks to change them; report them
in [STYLE_REVIEW.md](STYLE_REVIEW.md). Run `python scripts/check_styles.py` after
style changes. Standalone advanced-search mockups are design references and
must not be applied as an unrequested theme.
