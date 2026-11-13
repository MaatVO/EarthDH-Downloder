"""Flat dark theme: one accent colour, solid surfaces, small radii, Roboto."""

from nicegui import ui

BG = "#12161c"
SURFACE = "#1a2029"
SURFACE_2 = "#232b37"
SELECTED = "#26406b"
ACCENT = "#4c8dff"
TEXT = "#e6e9ee"
MUTED = "#98a2b0"
OK = "#3ecf8e"
WARN = "#f2b84b"
ERR = "#ef5f6b"
PINK = ACCENT  # selection colour on the map

CSS = f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Roboto+Mono:wght@400&display=swap" rel="stylesheet">
<style>
:root {{
  --edh-bg: {BG}; --edh-surface: {SURFACE}; --edh-surface-2: {SURFACE_2}; --edh-selected: {SELECTED};
  --edh-text: {TEXT}; --edh-muted: {MUTED}; --edh-accent: {ACCENT};
  --edh-ok: {OK}; --edh-warn: {WARN}; --edh-err: {ERR}; --edh-r: 6px;
}}
html, body {{ background: var(--edh-bg) !important; color: var(--edh-text); font-family: 'Roboto', system-ui, sans-serif; }}
@media (min-width: 1280px) {{ .q-page-container {{ padding-top: 64px !important; }} }}  /* below that the header can wrap: Quasar measures it */
.nicegui-content {{ padding: 24px !important; }}

/* header */
.edh-header {{ background: var(--edh-surface) !important; box-shadow: none !important; }}
.edh-logo {{ display: block; flex: none; width: 40px; height: 40px; border-radius: 9px; user-select: none; }}
.edh-logo-sm {{ width: 24px; height: 24px; border-radius: 5px; }}
.edh-brand {{ font-weight: 500; font-size: 1.1rem; }}
.edh-tabs .q-tab {{ border-radius: var(--edh-r); margin: 0 2px; padding: 0 16px; min-height: 36px; color: var(--edh-muted); }}
.edh-tab-active {{ background: var(--edh-surface-2); color: var(--edh-text) !important; }}
.edh-panels, .edh-panels .q-tab-panel {{ background: transparent !important; }}

/* surfaces */
.edh-card {{ background: var(--edh-surface) !important; border: 0 !important; border-radius: var(--edh-r) !important;
  box-shadow: none !important; padding: 20px 24px !important; }}
.edh-struct, .edh-kpi, .edh-opt, .edh-rec {{ background: var(--edh-surface-2) !important; border: 0 !important;
  border-radius: var(--edh-r) !important; box-shadow: none !important; }}
.edh-struct {{ padding: 16px; }}
.edh-kpi {{ padding: 10px 14px; }}
.edh-rec {{ padding: 10px 14px; }}
/* job card: what a running job is doing; the request table's states */
.edh-act {{ background: var(--edh-surface-2); border-radius: var(--edh-r); padding: 8px 12px; }}
.edh-req-active {{ color: var(--edh-accent); }}
.edh-req-ok {{ color: var(--edh-ok); }}
.edh-req-wait {{ color: var(--edh-warn); }}
.edh-req-err {{ color: var(--edh-err); }}
.edh-req-idle {{ color: var(--edh-muted); }}
/* section 5: input format -> staging -> output */
.edh-flow-step {{ flex: 0 1 auto; min-width: 10rem; }}
.edh-flow-value {{ font-size: 1rem; font-weight: 500; line-height: 1.4; }}
.edh-flow-note {{ max-width: 24rem; padding-left: 4px; }}
@media (max-width: 767px) {{ .edh-flow-step {{ flex: 1 1 100%; }} .edh-flow-note {{ max-width: none; padding-left: 0; }} }}
.edh-opt {{ padding: 14px 16px !important; cursor: pointer; gap: 6px !important; }}
.edh-opt:hover {{ background: var(--edh-selected) !important; }}
.edh-opt.selected {{ background: var(--edh-selected) !important; outline: 2px solid var(--edh-accent); }}
.edh-chunkshape {{ background: var(--edh-surface); border-radius: 4px; padding: 2px 6px; }}
.edh-summary {{ background: var(--edh-surface) !important; }}
.q-dialog .edh-card {{ background: var(--edh-surface) !important; }}
.q-dialog__backdrop {{ background: #000000b0 !important; }}

/* typography */
.edh-title {{ font-size: 1.1rem; font-weight: 500; }}
.edh-subtitle {{ font-size: 1rem; font-weight: 500; }}
.edh-muted {{ color: var(--edh-muted) !important; }}
.edh-mono {{ font-family: 'Roboto Mono', monospace; }}
.edh-step {{ min-width: 1.4rem; color: var(--edh-accent); font-size: 1.1rem; font-weight: 500; }}
.edh-sep {{ display: none; }}
.edh-kpi-label {{ font-size: .8rem; color: var(--edh-muted); }}
.edh-kpi-value {{ font-size: 1.3rem; font-weight: 500; line-height: 1.3; }}
.edh-tip {{ color: var(--edh-muted); }}
.edh-tip:hover {{ color: var(--edh-text); }}
.edh-tooltip {{ max-width: 340px; font-size: .82rem !important; line-height: 1.35; background: var(--edh-surface-2) !important; color: var(--edh-text); }}
a {{ color: var(--edh-accent); }}

/* controls: square-ish, solid */
.q-btn {{ letter-spacing: 0; }}
/* NiceGUI loads Quasar's !important rules in a layer, and those beat unlayered !important. An earlier layer beats them. */
@layer overrides {{
  .q-btn:focus-visible, .q-tab:focus-visible {{ outline: 2px solid var(--edh-accent) !important; outline-offset: 2px; }}
  .q-btn.bg-primary, .edh-grad-btn, .edh-grad-btn.bg-primary {{ color: var(--edh-bg) !important; }}
  .edh-grad-btn.disabled, .edh-grad-btn[disabled] {{ background: var(--edh-surface-2) !important; color: var(--edh-muted) !important; }}
}}
.q-btn--rounded, .edh-toggle, .edh-toggle .q-btn {{ border-radius: var(--edh-r) !important; }}
.edh-toggle {{ border: 0; background: var(--edh-surface-2); }}
.edh-grad-btn, .edh-grad-btn.bg-primary {{ background: var(--edh-accent) !important; }}
.edh-grad-bar .q-linear-progress__model {{ background: var(--edh-bar, var(--edh-accent)) !important; }}
.edh-bar-positive {{ --edh-bar: var(--edh-ok); }}
.edh-bar-warning {{ --edh-bar: var(--edh-warn); }}
.edh-bar-negative {{ --edh-bar: var(--edh-err); }}
.edh-bar-grey-6 {{ --edh-bar: var(--edh-muted); }}
.edh-job-tiles {{ display: grid; grid-template-columns: repeat(4, 11rem); gap: 12px; }}
.edh-kpi-value, .edh-tabular {{ font-variant-numeric: tabular-nums; }}
@media (max-width: 767px) {{ .edh-job-tiles {{ width: 100%; grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
.edh-grad-bar .q-linear-progress__track {{ background: var(--edh-surface-2) !important; opacity: 1 !important; }}
.q-chip {{ border-radius: 4px; }}
.edh-badge {{ font-size: 12px; padding: 2px 6px; border-radius: 4px; }}
.edh-sel-box {{ background: var(--edh-surface-2); border-radius: var(--edh-r); }}

/* catalogue images: transparent 2:1 global maps, set into a tonal well and never cropped */
.edh-thumb {{ flex: none; display: flex; align-items: center; justify-content: center; aspect-ratio: 2 / 1;
  background: var(--edh-surface-2); border-radius: var(--edh-r); overflow: hidden; color: var(--edh-muted); }}
.edh-thumb img {{ width: 100%; height: 100%; object-fit: contain; }}
.edh-thumb .q-img {{ width: 100%; height: 100%; }}
.edh-thumb .q-img__content > div {{ background: transparent; padding: 0; }}
.edh-thumb-sm {{ width: 64px; }}
.edh-thumb-md {{ width: 88px; }}
.edh-thumb-lg {{ width: 176px; }}
.edh-option-text {{ min-width: 0; }}
.edh-option-text .q-item__label {{ overflow-wrap: anywhere; }}

/* tables, expansions, map, log */
.edh-table, .edh-table .q-table__top, .edh-table .q-table__bottom, .edh-table thead tr {{ background: transparent !important; }}
.edh-table thead th {{ color: var(--edh-muted); font-weight: 500; font-size: .85rem; }}
.edh-table tbody tr.selected {{ background: var(--edh-selected) !important; }}
.edh-expansion {{ border-radius: var(--edh-r); overflow: hidden; background: var(--edh-surface-2); }}
.edh-map {{ border-radius: var(--edh-r); overflow: hidden; }}
.leaflet-container {{ background: var(--edh-bg) !important; }}
.edh-map .leaflet-tile-pane {{ filter: invert(1) hue-rotate(200deg) brightness(.78) contrast(.92) saturate(.6); }}
.leaflet-bar a, .leaflet-draw-toolbar a {{ background-color: var(--edh-surface-2) !important; color: #fff !important; border-color: var(--edh-surface) !important; }}
.leaflet-draw-toolbar a {{ background-color: #dfe4ee !important; }}
.leaflet-control-attribution {{ background: var(--edh-surface) !important; color: var(--edh-muted) !important; }}
.edh-map-btn {{ background: var(--edh-surface) !important; color: #fff !important; }}
.edh-log {{ background: var(--edh-bg) !important; border-radius: var(--edh-r); font-family: 'Roboto Mono', monospace; font-size: 12px; }}
.edh-code {{ display: flex; align-items: flex-start; gap: 8px; background: var(--edh-bg); border-radius: var(--edh-r);
  padding: 10px 12px; font-family: 'Roboto Mono', monospace; font-size: 12px; line-height: 1.6; }}
.edh-code-text {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
.edh-quota-link {{ color: var(--edh-muted); display: inline-flex; }}

/* dataset structure tree */
.edh-tree {{ font-family: 'Roboto Mono', monospace; font-size: 12.5px; line-height: 1.75; overflow-x: auto; }}
.edh-tree .st-row, .edh-tree .st-root {{ white-space: nowrap; display: flex; align-items: flex-start; gap: 8px; }}
.edh-tree .st-root {{ font-weight: 500; font-size: 14px; margin-bottom: 2px; }}
.edh-tree .st-br {{ color: var(--edh-muted); white-space: pre; }}
.edh-tree .st-ic {{ font-size: 16px; color: var(--edh-muted); }}
.edh-tree .st-coord, .edh-tree .st-var {{ color: var(--edh-accent); }}
.edh-tree .st-attr, .edh-tree .st-dims {{ color: var(--edh-text); }}
.edh-tree .st-group, .edh-tree .st-more, .edh-tree .st-sub {{ color: var(--edh-muted); }}
.edh-tree .st-group {{ font-size: 12px; }}
.edh-tree .st-sub {{ font-size: 12px; line-height: 1.5; }}
.edh-tree .st-sub b {{ color: var(--edh-text); font-weight: 500; }}
.edh-tree .st-type {{ color: var(--edh-muted); font-size: 12px; }}
.edh-tree .st-detail {{ white-space: normal; line-height: 1.55; padding: 0 0 4px 24px; }}
.edh-tree .st-chip {{ display: inline; color: var(--edh-muted); margin-right: 8px; font-size: 12px; }}
.edh-tree .st-codecs {{ margin-top: 6px; }}
.st-legend {{ display: flex; gap: 14px; align-items: center; font-size: 12px; color: var(--edh-muted); }}
.st-sw {{ display: inline-block; width: 14px; height: 10px; margin-right: -8px; }}
.st-sw-chunk {{ background: linear-gradient(90deg, #ef2b89, #7b34db); }}
.st-sw-grid {{ border-top: 1px solid var(--edh-muted); height: 1px; }}
.st-sw-shard {{ border-top: 2px solid #c58cff; height: 1px; }}

/* connect shell */
.edh-remote-bar {{ position: fixed; top: 0; left: 0; right: 0; height: 36px; z-index: 6000; display: flex;
  align-items: center; gap: 10px; padding: 0 12px; background: var(--edh-surface); font-size: 13px; }}
.edh-remote-frame {{ position: fixed; top: 36px; left: 0; width: 100vw; height: calc(100vh - 36px); border: 0; }}
.edh-pane {{ position: fixed; top: 36px; left: 0; right: 0; bottom: 0; overflow: auto; }}
.edh-ctabs {{ min-width: 0; overflow-x: auto; scrollbar-width: none; }}
.edh-ctab {{ display: flex; align-items: center; gap: 6px; height: 28px; padding: 0 6px 0 10px; border-radius: var(--edh-r);
  color: var(--edh-muted); cursor: pointer; max-width: 220px; white-space: nowrap; user-select: none; }}
.edh-ctab:hover {{ background: var(--edh-surface-2); color: var(--edh-text); }}
.edh-ctab-active {{ background: var(--edh-surface-2); color: var(--edh-text); }}
.edh-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--edh-ok); display: inline-block; }}

/* job actions */
.edh-job-actions .q-btn .q-icon {{ font-size: 18px; }}
.edh-menu {{ background: var(--edh-surface-2) !important; box-shadow: none !important; }}
@media (pointer: coarse) {{ .edh-job-actions .q-btn {{ min-height: 44px; min-width: 44px; }} }}

/* browser surfaces */
::selection {{ background: var(--edh-selected); color: var(--edh-text); }}
html {{ scrollbar-color: var(--edh-surface-2) var(--edh-bg); }}

/* phones */
@media (max-width: 639px) {{
  .nicegui-content {{ padding: 12px !important; }}
  .edh-card {{ padding: 16px !important; }}
  .edh-tabs .q-tab__icon {{ display: none; }}
  .edh-tabs .q-tab {{ min-height: 44px; padding: 0 12px; }}
  .q-field__native {{ font-size: 16px; }}
}}
</style>
"""


def apply_theme() -> None:
    ui.add_head_html(CSS)
    ui.dark_mode(True)
    ui.colors(primary=ACCENT, secondary=ACCENT, accent=WARN, dark=SURFACE, dark_page=BG,
              positive=OK, negative=ERR, warning=WARN, info=ACCENT)
    for el in (ui.input, ui.number, ui.select, ui.textarea):
        el.default_props("outlined dense")
    ui.button.default_props("no-caps")
