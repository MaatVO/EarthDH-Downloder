---
name: EDH Downloader
description: A flat, dark instrument panel for planning and watching large Earth Data Hub downloads.
colors:
  signal-blue: "#4c8dff"
  on-signal: "#12161c"
  engaged-blue: "#26406b"
  deep-housing: "#12161c"
  panel-slate: "#1a2029"
  inset-slate: "#232b37"
  readout-white: "#e6e9ee"
  legend-grey: "#98a2b0"
  nominal-green: "#3ecf8e"
  caution-amber: "#f2b84b"
  fault-red: "#ef5f6b"
  chunk-magenta: "#ef2b89"
  chunk-violet: "#7b34db"
  shard-violet: "#c58cff"
typography:
  title:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "1.1rem"
    fontWeight: 500
    lineHeight: 1.5
  subtitle:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 500
    lineHeight: 1.5
  readout:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 500
    lineHeight: 1.3
  body:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  column-label:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: ".85rem"
    fontWeight: 500
  readout-label:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: ".8rem"
    fontWeight: 400
  caption:
    fontFamily: "Roboto, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "20px"
    letterSpacing: "0.03333em"
  mono:
    fontFamily: "Roboto Mono, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  button: "3px"
  field: "4px"
  panel: "6px"
  round: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.on-signal}"
    rounded: "{rounded.button}"
    padding: "4px 16px"
  button-primary-disabled:
    backgroundColor: "{colors.inset-slate}"
    textColor: "{colors.legend-grey}"
    rounded: "{rounded.button}"
  button-outline:
    textColor: "{colors.signal-blue}"
    rounded: "{rounded.button}"
    padding: "4px 16px"
  button-flat:
    textColor: "{colors.signal-blue}"
    rounded: "{rounded.button}"
  button-danger:
    textColor: "{colors.fault-red}"
    rounded: "{rounded.button}"
  segmented-toggle:
    backgroundColor: "{colors.inset-slate}"
    rounded: "{rounded.panel}"
    height: "36px"
  field-outlined:
    textColor: "{colors.readout-white}"
    typography: "{typography.body}"
    rounded: "{rounded.field}"
    padding: "0 12px"
    height: "40px"
  nav-tab:
    textColor: "{colors.legend-grey}"
    rounded: "{rounded.panel}"
    padding: "0 16px"
  nav-tab-active:
    backgroundColor: "{colors.inset-slate}"
    textColor: "{colors.readout-white}"
    rounded: "{rounded.panel}"
  card:
    backgroundColor: "{colors.panel-slate}"
    rounded: "{rounded.panel}"
    padding: "20px 24px"
  option-card:
    backgroundColor: "{colors.inset-slate}"
    rounded: "{rounded.panel}"
    padding: "14px 16px"
  option-card-selected:
    backgroundColor: "{colors.engaged-blue}"
    rounded: "{rounded.panel}"
    padding: "14px 16px"
  readout-tile:
    backgroundColor: "{colors.inset-slate}"
    rounded: "{rounded.panel}"
    padding: "10px 14px"
  chip:
    rounded: "{rounded.field}"
---

# Design System: EDH Downloader

## Overview

**Creative North Star: "The Instrument Panel"**

EDH Downloader looks like a quiet dark panel of labelled controls and readouts. Nothing on it is decoration: every element either sets a value or reports one. A researcher configures one consequential job here and later glances back to see how it is doing, so the surface stays calm, exact and unshowy, and trust comes from precision and legibility rather than from visual effect.

The panel is built from three tonal steps of slate, one lit colour and solid fills. Signal Blue marks where action or selection is. Green, amber and red appear only when they are reporting a state. Numbers are the most prominent content on screen wherever a decision or a check-in depends on them.

Density is moderate and form-like: a numbered sequence of setup panels beside a sticky summary of what the job will cost. Glows and decorative gradients are refused, as is consumer SaaS gloss.

**Key Characteristics:**
- Flat, solid surfaces separated by tone, with no shadows and no borders on panels.
- One accent colour; status colours carry state only.
- Small, consistent radii (3px to 6px) and matter-of-fact controls.
- Roboto throughout, with Roboto Mono for machine text.
- Dark only.

## Colors

A restrained slate range lit by a single blue, with three status colours held in reserve.

### Primary
- **Signal Blue** (#4c8dff): The one lit indicator. Primary buttons, step numbers, selected segments, links, the header quota bar, and the selection rectangle on the map. Also coordinates and variables in the dataset structure tree. Source: `--edh-accent`.
- **Engaged Blue** (#26406b): Selection as a surface rather than a light. Hovered and selected option cards, selected table rows. Source: `--edh-selected`.
- **On Signal** (#12161c): Text on Signal Blue fills. Dark, because white on this blue is only 3.2:1.

### Neutral
- **Deep Housing** (#12161c): The page behind everything, and the well behind the job log and code snippets. Source: `--edh-bg`.
- **Panel Slate** (#1a2029): The header and every primary card. Source: `--edh-surface`.
- **Inset Slate** (#232b37): Anything set into a panel: readout tiles, option cards, expansions, segmented toggles, progress tracks, the active tab. Source: `--edh-surface-2`.
- **Readout White** (#e6e9ee): Primary text. Source: `--edh-text`.
- **Legend Grey** (#98a2b0): Captions, helper text, inactive tabs, table headers. Source: `--edh-muted`.

### Status
- **Nominal Green** (#3ecf8e): Running and completed jobs, other healthy states, and the connected dot. Source: `--edh-ok`.
- **Caution Amber** (#f2b84b): Warnings, quota that is close to its limit, and jobs held by quota. Source: `--edh-warn`.
- **Fault Red** (#ef5f6b): Errors, failed and interrupted jobs, destructive actions, and plans that exceed remaining quota. Source: `--edh-err`.

### Data Encoding
- **Chunk Magenta** (#ef2b89) to **Chunk Violet** (#7b34db): The gradient that marks the selected chunk inside dataset structure diagrams.
- **Shard Violet** (#c58cff): Shard boundaries inside the same diagrams.

### Named Rules

**The One Signal Rule.** There is one accent. Signal Blue means action or selection; a second hue is never introduced for emphasis.

**The State Only Rule.** Green, amber and red report the state of a job, a key or a plan. They are not decoration and are not used for variety.

**The Data Encoding Rule.** The magenta-to-violet gradient exists only inside dataset structure diagrams, where it marks the selected chunk. It never appears on chrome.

Implementation note: NiceGUI loads Quasar's `!important` rules in a CSS layer, and those beat unlayered `!important`, so a theme rule that must win over a Quasar utility such as `text-white` goes inside `@layer overrides`. Quasar's `accent` colour is mapped to Caution Amber and `secondary` to Signal Blue, so `color=accent` renders amber. The names `edh-grad-btn`, `edh-grad-bar` and `PINK` are legacy; all three now resolve to flat Signal Blue.

## Typography

**Body Font:** Roboto (with system-ui, sans-serif)
**Label/Mono Font:** Roboto Mono (with monospace)

**Character:** A plain, legible sans that stays out of the way, paired with its own monospace for anything a machine wrote. Hierarchy comes from weight and tone, within a narrow range of sizes.

### Hierarchy
- **Readout** (500, 1.3rem, 1.3): The numbers that matter: request counts, sizes, ETA, quota used.
- **Title** (500, 1.1rem, 1.5): Card titles and the product name in the header.
- **Subtitle** (500, 1rem, 1.5): Sub-sections inside a card, such as Dataset structure.
- **Body** (400, 14px, 1.5): Default text and field values.
- **Column Label** (500, .85rem): Table headers, in Legend Grey.
- **Readout Label** (400, .8rem): The name above a readout, in Legend Grey.
- **Caption** (400, 12px, 20px, slightly tracked): Helper text, quota status and job summaries.
- **Mono** (400, 12px for logs, 12.5px at 1.75 for the structure tree): Job IDs, output paths, chunk shapes, file paths, log lines.

### Named Rules

**The Sentence Case Rule.** Buttons and tabs are sentence case with no added tracking. Uppercase labels are not part of this system.

**The Machine Text Rule.** Paths, job IDs, chunk shapes, and logs are set in Roboto Mono. Human-written interface text never is.

## Layout

The New job screen is a numbered setup sequence beside a sticky Summary card (26rem wide) that holds the cost and the launch action. At 1440px, steps 1 and 2 stack in one column, steps 3 and 4 in a second, and step 5 spans both beneath them. Below 1024px the Summary drops to full width.

The header is a fixed three-zone bar (72px rendered): product name on the left, tabs in the centre, quota status and key selector on the right. Page content is padded 24px. Keys is centred at a maximum of 1024px and the connection page at 1100px; Jobs is a one-line count of jobs by state above a full-width stack of job cards, ordered with jobs needing attention first.

Spacing follows a 4px step (4, 8, 12, 16, 20, 24), with 12px the most common gap. Panels sit 20px apart. Cards pad 20px vertically and 24px horizontally.

Rows wrap at the 640, 768 and 1024px breakpoints and the page does not scroll sideways down to 320px. The header may wrap below 1280px, and content is offset by its measured height so nothing sits under it. Below 640px the header drops its subtitle and tab icons, and page and card padding tighten. On touch devices job actions are at least 44px.

## Elevation & Depth

This system is flat. Depth is conveyed by tone alone: Deep Housing, then Panel Slate, then Inset Slate, each set into the last. Selection raises nothing; it changes a surface to Engaged Blue. Dialogs are separated by a heavy black scrim (#000000b0), not by a shadow. Panels have no borders.

### Named Rules

**The Tonal Step Rule.** Every level of nesting is one step of slate. If something needs to stand apart, it moves up a tone; it does not gain a shadow, a glow or a border.

The missing-API-key banner is the one current exception: it uses a Tailwind amber tint and border, a 12px radius and a large shadow. It is drift from this system rather than a pattern to reuse.

## Shapes

Corners are small and square-ish. Panels, tabs, toggles, expansions and the map share a 6px radius (`--edh-r`). Fields, chips, and badges are slightly tighter at 4px. Buttons keep Quasar's 3px. Only icon-only buttons and the 8px status dot are fully round.

## Components

Solid and matter-of-fact: plain fills, small radii, no flourish. Controls look like what they do. Icons are Material Icons, filled.

### Buttons
- **Shape:** Nearly square corners (3px), sentence case, no shadow.
- **Primary:** Solid Signal Blue with On Signal text. Launch job is the only large one (`size=lg`). A failed job's Resume drops to Outline, because its note names what to fix first.
- **Hover:** Quasar's default state overlay.
- **Focus:** A 2px Signal Blue outline, offset 2px, on keyboard focus.
- **Disabled:** Primary drops to Inset Slate with Legend Grey text.
- **Outline:** Thin outline in Signal Blue or white, for the secondary action beside a primary (Measure on a sample, Download job.json, Structure on a completed job).
- **Flat:** Text only, for presets and light actions (Europe, Today). Cancel uses Fault Red.
- **Destructive confirm:** Solid Fault Red with dark text. The dialog names the job, and both buttons name their action (Keep running, Cancel job).
- **Icon:** Flat and round, with a tooltip.

### Segmented Toggle
- **Style:** Inset Slate track at 6px; the selected segment fills with Signal Blue.
- **Use:** Mutually exclusive modes: Global, Box, Points; New store, Extend existing Icechunk. Segments for these modes carry an icon before the label (`IconToggle`).

### Chips
- **Style:** Rectangular at 4px. Selected variables appear in a sticky Inset Slate strip.

### Cards / Containers
- **Corner Style:** 6px.
- **Background:** Panel Slate on the page; Inset Slate when nested.
- **Shadow Strategy:** None. See Elevation & Depth.
- **Border:** None.
- **Internal Padding:** 20px by 24px.
- **Step Header:** A Signal Blue step number, a 20px Legend Grey icon, then a Title. The number is the only colour in the header.

### Dataset Image
- **Style:** The Earth Data Hub catalogue thumbnail, a transparent 2:1 global map, set uncropped into an Inset Slate well at 6px. Widths are 64px in Collection and Dataset options, 88px on job cards and 176px beside the chosen dataset's name.
- **Fallback:** A Legend Grey globe icon when a dataset has no image or it cannot be loaded. Option rows keep an empty well.
- **Use:** Identifying a dataset, nowhere else. Its colours belong to the data shown, so they sit outside the One Signal and State Only rules; never sample them for chrome. The browser loads the images directly from public catalogue storage, which spends no API requests.

### Option Card
- **Style:** Inset Slate at 6px, the whole card clickable.
- **State:** Engaged Blue on hover and when selected; selected also gains a 2px Signal Blue outline. Used for chunk-layout comparison.

### Inputs / Fields
- **Style:** Outlined and dense, 40px high, 4px radius, floating label.
- **Focus:** Quasar's default: the outline turns Signal Blue.
- **Error / Disabled:** Quasar defaults mapped to Fault Red.

### Navigation
- **Style:** Tabs with an icon above the label, no indicator line. Inactive tabs are Legend Grey on the header; the active tab is an Inset Slate block with Readout White text, at 6px.
- **Connection shell:** A 36px bar with browser-style server tabs (28px high) and a green 8px dot.

### Readout Tile
- **Style:** Inset Slate at 6px, padded 10px by 14px: Readout Label, Readout value, then an optional caption.
- **Use:** Summary and job cards. The caption states whether a figure is measured or an estimate. Projected sizes are prefixed with ≈ in both cases.

### Quota Ring
- **Style:** An 84px circular gauge beside the request count in Summary. It shows the plan's share of remaining quota: green up to 50%, amber up to 100%, red beyond.

### Progress Bar
- **Style:** A flat fill on an Inset Slate track. On job cards it is 8px and takes the job's status colour, with Legend Grey for paused and cancelled jobs. The header quota bar is 6px in Signal Blue.

### Job Card
- **Style:** A Panel Slate card with the Dataset Image beside the job ID in Mono and a caption summary, then a headline at Readout size: an icon and the status in its status colour and, while running, the time left, with the percentage at the far end. The progress bar sits directly beneath the headline, followed by readout tiles in fixed slots (Units, Requests, Speed, Temporary, four columns or two below 768px) so a value lands in the same place on every card, then the actions: what this state calls for on the left, and the tools that are always available on the right with Log always last. A stopped job adds one Body sentence under the bar saying what happened, what is safe and what to do next, then a caption with the requests Resume will spend against the balance this app has counted. A running job that will hit the quota says when, and a waiting job shows a Resumes readout in place of Speed and Temporary files. Parallel connections sit behind a Connections menu. A completed job fills the last two slots with Output size and Finished, and shows a Roboto Mono code well that opens the output in Xarray.

### Map
- **Style:** Leaflet with tiles darkened by a CSS filter (invert, hue-rotate, reduced brightness and saturation), 6px radius, the selection rectangle in Signal Blue.

### Dataset Structure Tree
- **Style:** A monospace tree of 12.5px lines at 1.75, coordinates and variables in Signal Blue, details in Legend Grey, accompanied by isometric chunk diagrams that use the data encoding colours.

## Do's and Don'ts

### Do:
- **Do** use `--edh-*` variables and the constants in `theme.py` for every colour.
- **Do** express nesting by moving one step along Deep Housing, Panel Slate and Inset Slate.
- **Do** keep Signal Blue as the only accent, and reserve one solid primary button for the main action in a view.
- **Do** label figures as measured or estimated in the readout caption.
- **Do** set paths, job IDs and chunk shapes in Roboto Mono.
- **Do** use sentence case on buttons and tabs.

### Don't:
- **Don't** add glows or decorative gradients; the only gradient belongs to the selected chunk in dataset structure diagrams.
- **Don't** add consumer SaaS gloss, such as marketing-style polish, oversized heroes or friendly illustration, inside the tool.
- **Don't** add shadows or borders to panels.
- **Don't** use green, amber or red for anything other than state.
- **Don't** copy the missing-API-key banner's amber tint, 12px radius or shadow into new work.
