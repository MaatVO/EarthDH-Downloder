# UI improvement plan

Source: seven design reviews run on the running UI (design-analysis, craft, accessibility, UX heuristics, Dieter Rams, cognitive load, general review), plus the earlier `.impeccable/critique` of the Jobs tab (24/40).

Written for: the maintainer of Weather Dataset Downloader, to work through in order.

## Scope and limits

- Reviewed: New job, Jobs (empty) and Keys at 1440px and 390px, and `gui/theme.py`.
- Not reviewed: populated states (variables table, plan, Summary numbers, job cards), the Climate Data Store source view, keyboard flow and screen-reader behaviour. The review ran with fake keys, so the dataset returned 401.
- Review step 0 below closes those gaps before the larger changes start.

## Ordering principle

Effort against reward. Phase 1 is cheap and fixes WCAG A/AA failures. Phase 2 fixes the unhappy path, which is the weakest part of an otherwise restrained UI. Later phases are larger or need a decision.

## Phase 0: close the review gaps (about 1 hour)

Goal: do not design fixes against states nobody has seen.

1. Run the app against the mock server in `tests/` (no API quota) so a dataset loads: capture variables table, plan, Summary, Launch enabled.
2. Seed the six job states used in `.impeccable/critique` and capture Jobs.
3. Capture the CDS source view (click the source toggle by role, not text).
4. Keyboard pass: Tab through New job from the top, note focus order against visual order, the Summary column position, `.edh-opt` reachability, and the welcome modal's focus trap.
5. Screen-reader spot check (NVDA or Orca) on the dataset error, the plan update and Launch.

Output: a short `docs/ui-review-evidence.md` with screenshots and findings. Later phases may change after this.

## Phase 1: accessibility quick wins (about half a day)

Each item is small and independently shippable.

| # | Change | Where | WCAG |
|---|---|---|---|
| 1.1 | Set `<html lang="en">` | `ui.run(...)` call in `app.py` (near `title=`); or `ui.add_head_html` script | 3.1.1 A |
| 1.2 | Card titles as headings: `role="heading" aria-level="2"` on `.edh-title` labels (Dataset, Variables and levels, Time, Area, Output, Summary, Keys sections, Jobs) | `app.py` where each card title label is created | 1.3.1 A |
| 1.3 | Live regions: `role="status"` on the dataset-info error area, Summary plan text, quota label and launch result | `ds_info`, summary container, `quota_label` in `app.py` | 4.1.3 AA |
| 1.4 | Accessible names for icon-only buttons: refresh dataset, delete key, per-job actions | `app.py` (`.props('aria-label="..."')`) | 4.1.2 A |
| 1.5 | Contrast: lighten accent text on selected surfaces. Failing pairs: accent on `#26406b` 3.24:1, muted on `#26406b` 4.01:1, accent on `#232b37` 4.46:1. Add `--edh-accent-text: #7fb0ff` and use it for text and links on `--edh-selected` and `--edh-surface-2`; keep `#4c8dff` for fills | `theme.py` `:root` and the `.edh-opt.selected`, `.edh-table tbody tr.selected`, `.edh-step` rules | 1.4.3 AA |
| 1.6 | Target size: Tour button and quota `open_in_new` link to at least 24px (aim 44px on `pointer: coarse`); Tour label from 10px to 12px | header block around `app.py:608`, `theme.py` | 2.5.8 (WCAG 2.2), 2.5.5 (AAA) |
| 1.7 | Focus rings for the rest of the controls: extend the `@layer overrides` `:focus-visible` rule to `.q-field`, `.q-toggle`, `.q-checkbox`, `.edh-opt`, links | `theme.py` | 2.4.7 AA |
| 1.8 | Global reduced motion: `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: .01ms !important; animation-duration: .01ms !important; } }` | `theme.py` | 2.3.3 (AAA); also craft R12 |

Verification: axe or Lighthouse on each tab, the contrast script from the review (re-run), and the Phase 0 keyboard pass.

## Phase 2: errors, status and honesty (about 1 day)

1. **Dataset error** (`app.py:1127`): replace `Could not load dataset: {e}` with a typed message.
   - 401 or 403: "Earth Data Hub rejected the key '{name}'." plus a **Test key** button that opens Keys with that key focused.
   - Network error: "Can't reach Earth Data Hub." plus **Retry** (reuse the refresh action).
   - Error colour (`--edh-err`) and an icon; the raw URL goes into an expandable "Details".
2. **Header quota label** (`app.py:677`): append "· tracked locally", with a tooltip: "Earth Data Hub does not report usage; this counts requests from this app." This follows PRODUCT.md. Show an amber "not verified" state when the last key test failed.
3. **Launch disabled reason** (`app.py:1032`): wrap the button with a tooltip and a one-line reason under it, for example "Select variables first" or "Add an API key". Reasons come from the same checks that disable it.
4. **Single key warning:** show one message. Remove the header "no API key" label (`app.py:663`) and the inline card text; keep the banner (`app.py:696`) and make it the only place. Keep the header key picker.
5. **Version string:** hide `+local` build suffixes from the header subtitle outside dev mode.
6. **Variables table empty state:** replace "No data available" with a state that points to the cause: "Load a dataset to list its variables" or the same typed error.

Verification: add GUI tests where helpers exist (`tests/test_gui_structure_helpers.py` pattern); manual check with a wrong key, no network, and no key.

## Phase 3: hierarchy and simplification (about 1 to 2 days)

1. **Primary vs selected.** Selected toggles (source, Box/Global/Points, New store) currently use the same solid blue as Launch. Use `--edh-selected` fill plus an accent-text label and a 2px accent underline for selected state. Reserve solid `--edh-accent` for the one primary action per view.
2. **Demote Download job.json** (`app.py:1034`) to a text button so Launch is the only emphasised action, including when Launch is disabled.
3. **Move decisions out of the main path.**
   - Output longitudes (`app.py:797`) into the Area step's advanced row, default −180…180 (or inferred from the box).
   - Remove the duplicate Connections field in Summary (`app.py:978`) and keep it in Download settings (`app.py:990`).
4. **Fewer help icons.** Keep tooltips for genuinely non-obvious fields (Time step, Store mode, Format, Sample). Turn the rest into one line of helper text. Make tooltip icons focusable and keyboard-dismissible.
5. **Time inputs.** Label "from hour / to hour (incl.)" as "Start time / End time (UTC)" if the dataset is UTC, and attach "Today" to the Last day field rather than the hour row.
6. **Area presets row.** Fix the wrap that leaves "Trentino-Alto Adige" alone: put presets in one flex-wrap row with a consistent gap.
7. **Output folder.** When running locally, add a folder picker and recent folders next to Directory.

## Phase 4: craft and system (about 1 day)

1. **Type scale to five sizes:** 12 / 14 / 16 / 20 / 34, all in `rem`. Replace `.8rem`, `.82rem`, `.85rem`, `12.5px`, 13px and 17px uses. Add `--edh-fs-*` tokens.
2. **Spacing on 4px:** replace 10/14/20px paddings with 8/12/16/24 (`.edh-kpi`, `.edh-rec`, `.edh-opt`, `.edh-card`).
3. **Fix the Summary column offset** (starts at y=184 while neighbouring cards start at y=120) by aligning it to the grid top. Check whether the sticky offset or the `.q-page-container` padding causes it.
4. **Separate cards from the page:** surface vs surface-2 is 1.15:1. Add a 1px hairline `#2c3644` to `.edh-card` or raise surface-2 to about `#28313e`. Decide once: tonal layering plus hairline, no shadows.
5. **Z-index scale and containment.**

   ```css
   :root { --z-sticky: 10; --z-overlay: 100; --z-modal: 200; --z-chrome: 300; }
   .edh-card { isolation: isolate; }
   ```

   Replace `z-index:5` (`app.py:741`) and `6000` (`edh-remote-bar`) with these.
6. **Rename `.edh-grad-btn`** to `.edh-primary-btn`. It is flat; the name is misleading.
7. **Leaflet controls:** replace `#fff` and `#dfe4ee` with the token palette.

## Phase 5: larger structural work (needs a decision first)

1. **Mobile Summary.** On a 390px screen the plan and Launch sit at the end of about 3200px. Option A: sticky bottom bar with plan totals and Launch. Option B: reorder so Summary follows Area. Decide after the Phase 0 evidence.
2. **`.edh-opt` as a radio group.** Give the clickable option cards `role="radio"` inside `role="radiogroup"` with arrow-key support.
3. **Focal point on New job.** One element should lead at squint distance: a larger Summary/plan figure (requests, size) next to Launch is the natural candidate, since cost-before-spending is a headline promise.
4. **Jobs tab** items from the earlier critique that are still open: status-coloured progress bars, ETA emphasis, plain-language failure messages, Resume cost shown before it spends quota, contextual help.

## Acceptance checks

- axe: zero serious or critical issues on New job, Jobs, Keys.
- Contrast script: all text pairs at least 4.5:1 and large text at least 3:1.
- Keyboard: every control reachable and visibly focused; no trap in the welcome modal or the tour.
- Mobile 390px: no horizontal scroll, targets at least 44px on touch.
- Existing test suite passes; GUI helper tests added for the new error messages.

## Out of scope

- Visual identity redesign. PRODUCT.md says voice and identity are undecided; this plan keeps the current flat dark system.
- AI-specific reviews (none apply; the product has no AI features).
