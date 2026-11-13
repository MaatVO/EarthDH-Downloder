# UI review evidence

Written for: the maintainer, as the Phase 0 output of [ui-improvement-plan.md](ui-improvement-plan.md), plus what the phases changed.

Method: the real app, run against the fake Climate Data Store from `tests/fake_cds.py` in an isolated home, driven with Playwright (Chromium), scanned with axe-core 4.10.2 (WCAG 2.0/2.1/2.2 A and AA tags). Earth Data Hub cannot be faked (its host is fixed), so populated states are CDS ones; the Earth Data Hub view was checked with a rejected key.

## What was checked

| View | Width | axe serious/critical (after fixes) | Horizontal scroll |
|---|---|---|---|
| Welcome modal | 1440 | 0 | no |
| New job, Earth Data Hub, key rejected | 1440, 390 | 0 | no |
| New job, CDS, 3 variables, plan shown | 1440, 390 | 0 | no |
| Jobs, empty and with a running job card | 1440, 390 | 0 | no |
| Keys | 1440, 390 | 0 | no |

Colour contrast: axe reports no `color-contrast` failures in any of these views.

## Findings and what was done

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | Summary started at y=184 while neighbouring cards start at y=120, and it never stuck on scroll. Cause: Quasar makes `.q-tab-panels` (overflow hidden) and `.q-panel` (overflow auto) clipping containers, so `position: sticky; top: 80px` resolved inside the panel (104 + 80). | DOM walk, `[class*=q-panel]` computed overflow | Fixed in `theme.py`: panels `overflow: visible`. Summary is at y=120 and sticks at y=80 after scrolling. |
| 2 | At 390px the page scrolled sideways (scrollWidth 589). Cause: the variable name toggle in the dataset structure is as wide as the names. | overflow scan | Fixed: toggle wraps. scrollWidth 390 in every view. |
| 3 | At 390px the Summary and Launch sat at y=4869 of 5641. | page height | Decision: option A. A bottom bar shows the plan (requests, download size) and "Review and launch" jumps to the Summary; it hides while the Summary is on screen. Reordering was rejected because the steps are numbered 1 to 5. |
| 4 | Tooltip icons were mouse-only. | tab order | Icons are now focusable buttons; the tooltip opens on focus and closes on Esc and blur; the tooltip text is the accessible name. Nine help icons became one line of helper text (hours, UTC offset, directory and disk limits, memory, extra attributes, shard size). |
| 5 | Selection checkboxes in the variables table had no accessible name (9 nodes). | axe `aria-toggle-field-name` | Named "Select all variables" and "Select <variable>". |
| 6 | Progress rings and the page loading bar had no name. | axe `aria-progressbar-name` | Named; a small observer names Quasar's own loading bar. |
| 7 | Focusable icons carried Quasar's `aria-hidden` (the date-picker icon and my new tooltip icons). | axe `aria-hidden-focus` | `aria-hidden=false` on them. |
| 8 | The structure tree scrolls sideways but could not be reached by keyboard. | axe `scrollable-region-focusable` | `tabindex=0`, `role=region`, labelled. |
| 9 | Dataset thumbnails on job cards had `role=img` with an empty name. | axe `role-img-alt` on a running job | Decorative images are hidden from assistive technology. |
| 10 | Expansion headers (Advanced, Download settings) showed no focus ring. | tab order scan | Ring added. |
| 11 | Output folder was a free-text field only. | review | Folder button opens a picker of folders on the machine doing the work (this computer or the SSH server), with Home, Default and recent output folders. New service method `list_dirs`, tested. |
| 12 | The plan's lead figure was the same size as the other tiles. | squint test | Requests figure is now 34px (`--edh-fs-xl`). |

## Keyboard pass (1440px)

- Welcome modal: focus cycles Skip, Start the tour, dialog; it never reaches the page behind. Pass.
- New job, tab order follows the visual order: Dataset, Variables (search, header checkbox, row checkboxes, pagination), Time (dates, hours, step), Area (mode, bounds, presets, map controls, Center, Advanced, grid), Output, Summary. Summary is last in both DOM and the two-column layout. Pass.
- The map adds six tab stops (zoom, draw, edit, attribution). Acceptable.
- Inputs show the field border change as focus; buttons, tabs, checkboxes, tooltip icons and expansion headers show a 2px ring.
- Disabled Launch is skipped by Tab; its reason is read from the status line under it.

## Not done, and why

- Screen reader check (NVDA or Orca): not run here. The status regions, headings and names above are verified by axe only. Please do one pass on the dataset error, the plan update and Launch.
- Six seeded job states: the earlier `.impeccable/critique` already covered them; only a running card was re-scanned.
- Phase 5.2 (`.edh-opt` as a radio group): no element uses `.edh-opt` any more, so there is nothing to convert. Dead CSS remains in `theme.py`.
- Phase 3.3 duplicate Connections field: only one exists now (Summary, next to the sample hint), so nothing was removed.
- Touch targets: help icons and table checkboxes are 44px on touch. Dense text fields stay 40px and pagination arrows 24px; both meet WCAG 2.2 AA (24px) but not the 44px aim.
- Jobs tab help (Phase 5.4): no contextual help added. Status-coloured bars, plain-language failures and Resume cost were already in place.

## Screenshots

- [New job, desktop, plan shown](ui-evidence/new-job-desktop.png)
- [New job, mobile, plan bar](ui-evidence/new-job-mobile-bar.png)
- [New job, mobile, after "Review and launch"](ui-evidence/new-job-mobile-summary.png)
