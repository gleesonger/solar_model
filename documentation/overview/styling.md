# Styling

## Where styles live

| Location | Ownership |
| --- | --- |
| [layout.py](../../solar_model/dashboard/layout.py), `render_dashboard_styles` | Shared CSS, tab/control positioning, mobile table rules |
| Tab/component Python files | NiceGUI utility classes, Quasar props, local inline sizing |
| [charts.py](../../solar_model/dashboard/components/charts.py) | ECharts colours, series, axes, legends, zoom, forecast patterns |
| [state.py](../../solar_model/dashboard/state.py), `PANEL_COLORS` | Array colour sequence |
| [chart_display.py](../../solar_model/dashboard/components/chart_display.py), [tables.py](../../solar_model/dashboard/components/tables.py) | Chart headers/actions, table presentation, total-row class |
| [warnings.py](../../solar_model/dashboard/components/warnings.py) | Inline persistent-banner appearance; shared notification settings |
| [live_updates.css](../../solar_model/dashboard/assets/live_updates.css) | Hidden-by-default three-dot indicator, repeating pulse cycles, three-second expiry and reduced-motion variant |
| [live_updates.js](../../solar_model/dashboard/assets/live_updates.js) | Three-dot indicator wall-clock expiry and lifecycle/health handling |

There is no standalone theme stylesheet or central typography/token system. Current appearance combines shared CSS and local classes.

[create_solar_icon.py](../../solar_model/dashboard/assets/create_solar_icon.py) writes `solar_power.svg` beside the generator script, regardless of the current working directory.

Regenerate it from the repository root with `python dashboard/assets/create_solar_icon.py`. The asset must be valid SVG without Git conflict markers. On 2026-10-22, committed conflict markers caused NiceGUI to emit `/favicon.ico` instead of an embedded SVG and return HTTP 500 for the icon; regenerating the asset restored valid XML, an embedded favicon URL and browser image decoding.

## Existing layout conventions

- Page uses a centred `max-w-7xl` container, full-width sections, `p-2 md:p-4` spacing, white background, and a sticky topbar (`z-index: 30`).
- Section titles typically use `text-lg font-semibold`; Recent subsection titles use `text-base`; hints use `text-sm text-gray-600`.
- Optional chart series selection uses one compact flat text button labelled **Default**, **None** or **All** in the existing bordered toolbar, alongside Split tariff and copy/download/table controls; the toolbar wraps within the header on narrow screens.
- Charts usually use `w-full h-96`. The desktop custom builder uses a 34rem chart and matching scrollable legend, with a fixed-width legend column.
- Tables use `dense flat bordered`, right-aligned numeric columns, and fit-content width where appropriate. Global table headers are blue `#0070C0` with white text.
- Final summary rows use `.summary-total-table`, grey `#f1f3f5`, bold. Only mark tables whose last row really is a total.

## Colours and chart meaning

| Series | Colour |
| --- | --- |
| Solar | `#F9C74F` |
| Load | `#0072B2` |
| Analysis House Load / original counter Load | `#0072B2` / `#6C757D` |
| Battery | `#56B4E9` |
| Grid import | `#D1495B` |
| Grid export | `#009E73` |
| Inverter / net cost | `#6C757D` |
| No-solar cost comparator | `#7B2CBF` |

- House Load (Forecast) shares Load blue `#0072B2` and uses the same diagonal decal as Solar (Forecast); it is initially deselected and appears only in Energy by hour. Energy charts reserve 125px above the plot, keeping the mobile wrapped legend clear of the axis.
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

## Plan presentation

Plan controls wrap on narrow screens; summary metrics share one bordered grid of inline label/value pairs, using one, two or three columns by viewport width. The full DataFrame table scrolls horizontally, shows all rows without pagination (clock hours in Compact, original periods in Advanced), and displays local start/end times as HH:MM. The mobile tab grid uses a shrinking middle column so NiceGUI tab scrolling accommodates Plan alongside the existing four tabs.

The plan chart shares the existing Solar amber, House Load blue and Battery light blue. Actual lines are solid, scenario forecasts dashed; both segments share a legend name. Tooltip labels explain the distinction. Power and battery energy use separate labelled axes; timestamp formatting uses the configured timezone. A spinner replaces Refresh while calculating, and controls restore on failure; any previous successful report remains visible with an explicit failure status.

Plan scenario changes retain chart zoom and legend selections; rendering is immediate without line-drawing animation.

Plan Grid Import uses red and Grid Export uses green on the power axis, with solid actuals and dashed forecasts. Extra top spacing keeps the wrapped five-item mobile legend clear of axes; date/time tick labels use two lines to reduce horizontal overlap.

The Plan chart sets `alignTicks=True` on the battery y-axis so its tick/gridline positions align with the power axis while retaining independent kWh and kW scales.

All dashboard table headers stay below the dashboard topbar during page scrolling and leave the viewport at the end of their own table. Shared `assets/table_headers.js` translates the real header within the table bounds on scroll/resize, preserving column widths, sorting and horizontal scrolling without a second header or an inner vertical scrollbar. Updates use animation frames; child-list observation also handles newly displayed tables and refreshed rows. Styling applies to all Quasar table headers.

Plan detail defaults to Compact with an Advanced toggle beside Download Excel. Short core labels, wrapping headers and scoped 12px table text with 4px/3px cell padding reduce column widths; numeric cells remain unwrapped and horizontal scrolling remains available. Start/end SOC columns sit beside their kWh counterparts in Advanced; Compact shows ending kWh and SOC. Adjustment guidance lives in its tooltip, and the next action has its own line and scenario selection shares the average-cost/range row rather than a separate card.

Plan numeric cells use tabular digits with fixed trailing decimal places per column: two for kWh/kW/cost, four for tariff rates and one for SOC, including Total values. Compact detail aggregates by local clock hour; Advanced retains source timesteps.

Plan headers use short labels while retaining units; sorting icons/actions are disabled. Scenario-average cost/range shares a wrapping row with two compact labelled Solar/Load button groups. Each shows Low (0.5x), Base and High (1.5x); exactly one button is selected in each group. Controls use standard dense, unelevated NiceGUI toggles and wrap within the mobile viewport.

After a successful plan run, Refresh uses its normal disabled/grey state until the adjustment changes; no separate changed-adjustment text occupies space. Failed attempts leave Refresh enabled for retry.

Solar/Load scenario controls use individual bordered fieldsets with centred legends above the buttons. Unselected buttons use Quasar grey-2 with dark text; the selected button retains the primary colour and white text. The groups have a 24px gap (twice the former 12px) and wrap on narrow screens. Border/legend rules are scoped to `.plan-scenario-group`.

Advanced Plan detail adds `.plan-detail-advanced`: 13px text, shared 4px/3px cell padding matching Compact, headers at least 110px wide (70px for Start/End) and up to 220px before wrapping. Compact retains its tighter existing styling; both remain horizontally scrollable.

Advanced Plan detail keeps full descriptive column names alongside its wider columns. Compact alone uses the shortened headers; underlying field keys and numeric formatting are unchanged.

Plan table t is elapsed row duration in hours and shows two decimals. Score headers remain Cost Score, Protection Score and Total Score; their cells show hourly rates to two decimals; the Total row is time-weighted, rather than summing hourly rates. This presentation does not change other interval totals or Excel score contributions.
