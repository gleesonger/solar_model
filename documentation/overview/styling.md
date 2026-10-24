# Styling

## Where styles live

| Location | Ownership |
| --- | --- |
| [layout.py](../../dashboard/layout.py), `render_dashboard_styles` | Shared CSS, tab/control positioning, mobile table rules |
| Tab/component Python files | NiceGUI utility classes, Quasar props, local inline sizing |
| [charts.py](../../dashboard/charts.py) | ECharts colours, series, axes, legends, zoom, forecast patterns |
| [models.py](../../dashboard/models.py), `PANEL_COLORS` | Array colour sequence |
| [chart_display.py](../../dashboard/chart_display.py), [tables.py](../../dashboard/tables.py) | Chart headers/actions, table presentation, total-row class |
| [warnings.py](../../dashboard/warnings.py) | Inline persistent-banner appearance; shared notification settings |
| [live_updates.css](../../dashboard/assets/live_updates.css) | Hidden-by-default three-dot indicator, repeating pulse cycles, three-second expiry and reduced-motion variant |
| [live_updates.js](../../dashboard/assets/live_updates.js) | Three-dot indicator wall-clock expiry and lifecycle/health handling |

There is no standalone theme stylesheet or central typography/token system. Current appearance combines shared CSS and local classes.

[create_solar_icon.py](../../dashboard/assets/create_solar_icon.py) writes `solar_power.svg` beside the generator script, regardless of the current working directory.

Regenerate it from the repository root with `python dashboard/assets/create_solar_icon.py`. The asset must be valid SVG without Git conflict markers. On 2026-10-22, committed conflict markers caused NiceGUI to emit `/favicon.ico` instead of an embedded SVG and return HTTP 500 for the icon; regenerating the asset restored valid XML, an embedded favicon URL and browser image decoding.

## Existing layout conventions

- Page uses a centred `max-w-7xl` container, full-width sections, `p-2 md:p-4` spacing, white background, and a sticky topbar (`z-index: 30`).
- Section titles typically use `text-lg font-semibold`; Recent subsection titles use `text-base`; hints use `text-sm text-gray-600`.
- Charts usually use `w-full h-96`. The desktop custom builder uses a 34rem chart and matching scrollable legend, with a fixed-width legend column.
- Tables use `dense flat bordered`, right-aligned numeric columns, and fit-content width where appropriate. Global table headers are blue `#0070C0` with white text.
- Final summary rows use `.summary-total-table`, grey `#f1f3f5`, bold. Only mark tables whose last row really is a total.

## Colours and chart meaning

| Series | Colour |
| --- | --- |
| Solar | `#F9C74F` |
| Load | `#0072B2` |
| Battery | `#56B4E9` |
| Grid import | `#D1495B` |
| Grid export | `#009E73` |
| Inverter / net cost | `#6C757D` |
| No-solar cost comparator | `#7B2CBF` |

- Load (Forecast) shares Load blue `#0072B2` and uses the same diagonal decal as Solar (Forecast); it is initially deselected and appears only in Energy by hour. Energy charts reserve 125px above the plot, keeping the mobile wrapped legend clear of the axis.
- Array palette cycles green, blue, yellow, purple. Actual/forecast array pairs share colour; forecasts add a decal pattern.
- Standard energy and cost series use bars; rates, telemetry, battery levels, and all custom-chart series use lines. Units appear on axes; different units may require separate axes.
- Custom charts assign colours to the larger of variable/split dimensions (variables win ties); the other dimension uses dashes/markers.
- Style order: solid, short dash, dotted, filled circle/square/triangle, cross, empty circle/square/triangle, long dash, then automatic styles.
- Compute styles from the full selection before filtering so toggling legend entries preserves identity.

## Responsive and status states

- At widths ≤767px, desktop Analysis controls hide and a drawer/trigger appears; drawer width is min(22rem, 90vw).
- The mobile custom builder uses two control columns, full-width Variables, an 18rem full-width chart, and a stacked legend capped at 12rem. Shared `.custom-chart-*` rules live in `layout.py`.
- Live/Today table switches to 0.7rem text, tight padding, wrapping headers, non-wrapping values, and full width. Solar-orientation subrows become muted `#64748b`.
- Loading uses spinners/progress labels; new live readings restart three slate-grey dots (5.6px diameter, at least 70% opacity, pulsing between 80% and 115% scale) with a cyclic pulse and three-second expiry whose final CSS state is hidden. Reduced-motion mode keeps static dots until expiry. The persistent Last reading timestamp sits below Live / Today with `text-xs text-slate-400` (12px, light grey). Do not infer freshness from animation alone.
- Persistent stale/connection banner uses dark red `#991b1b`, white text, `z-index:1999` below modal dialogs, and safe-area bottom padding.

## Guidance for future changes

- Reuse existing series colours, component classes, and warning helpers. Change shared rules at their owning source.
- Keep units, forecast patterns, and legend styles aligned with meaning; preserve missing-value blanks and user selections.
- Check mobile table width, drawer access, chart legends, notification/banner overlap, and loading/stale states.
- A central theme remains a possible improvement.
