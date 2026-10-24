# Independent website review — initial assessment

Reviewed 2026-10-15. This assessment was formed without reading the other new reviewer reports. Review only; application source, production configuration and `solar.db` were not changed.

## Conclusion and material gaps

The real isolated NiceGUI website renders its four mounted tabs, five built-in charts, custom preview, chart exports and full CSV/XLSX downloads. Controlled energy, power, battery, array and economic outputs largely match independent arithmetic. Two Chrome contexts retained independent grouping selections; offline/reconnect returned to the correct persisted-data-stale warning. Confirmed presentation problems remain in telemetry totals, hour navigation and mobile custom-chart usability. Availability aggregation in downloads disagrees with the documented non-counter averaging rule and requires an explicit contract decision.

This is **partial system verification**, not a complete passing website suite. No representative real-data backup or scale benchmark was used. DST/leap/non-hour timezone, counter resets, one-sample fixtures, incomplete panel data, rapid request races, every custom metric/split combination and all failure/retry branches were not browser-verified. Physical hardware/provider behavior and real process restarts were deliberately replaced with fakes. Review completion does not imply these gaps passed.

## Baseline and reproducible evidence

- Git revision observed during checks: `3932b02f6de46bc74dc7392da0022726cf7af1d5`. Initial working tree had modified `documentation/index.md`, untracked review plan and `solar_model/tools/evaluate_load.py`; preserved.
- Windows, Python 3.11.8; NiceGUI 2.24.2; pandas 2.2.0; SQLAlchemy 2.0.52; Playwright 1.64.0; XlsxTurbo 1.11.0. Playwright used installed Chrome headlessly, desktop 1440×1000 and mobile 390×844, separate browser contexts.
- Evidence root: [temporary evidence directory](C:/Users/glees/AppData/Local/Temp/solar-browser-review-20261015). It contains launcher/fixture `server.py` (127.0.0.1:18091), empty database launcher `server_empty.py` (:18092), `workflows.py`, `remaining.py`, `extra_workflows.py`, `mobile.py`, `warning_navigation.py`, `independent_values.py`, CSV/XLSX files, JSON browser options/results and screenshots. Run Python scripts from repository root. Launch servers first; no external hardware/API connection is made. Existing fixture DB makes runs repeatable within the recorded review date; recreate it for a new date. `remaining.py` expects an initially absent `control-actions.txt` for its cancellation assertion.
- `workflows.py` completed chart/custom/CSV/Excel/validation checks, then stopped when a ten-second top error notification temporarily intercepted the System Info tab. `remaining.py` completed System Info, Control and offline/reconnect separately. The first mobile probe used an incorrect icon accessible-name selector; `mobile.py` uses the actual trigger class. Harness failures are distinguished from product findings below. JSON `workflow-results.json` was overwritten by the later continuation; individual downloads/options/text retain the earlier results.
- Browser console capture during the main chart/export run had no page errors. Server logs emitted Windows `ConnectionResetError [WinError 10054]` when contexts were closed; no associated failed UI operation was observed. Invalid download deliberately logged its validation exception. No full pytest result is claimed here.

## Fixture and independent expectations

The database has four physical-unit electrical readings at local 08:00/08:30/09:00/09:30 on the review day. Solar power is 2/3/4/5 kW; load 1/2/3/4; battery −0.5 each; import power 0.5; availability 8/9/10/11 kWh; SOC 50/55/60/65%. Every row contains interval solar 1, load 0.5, import 0.25, export 0.1 kWh; import cost 0.10, export revenue 0.02, net cost 0.08 and no-solar cost 0.20. Panel energy shares are 0.6/0.4; import labels are night/night/day/day. Nullable fields remain absent.

Thus site totals are solar **4**, load **2**, import **1**, export **0.4** kWh; cost **0.40**, revenue **0.08**, net **0.32**, comparator **0.80**. Each populated hour has half these totals. Sample means are solar **3.5**, load **2.5** kW, availability **9.5** kWh and SOC **57.5%**. The fake live source independently supplies solar 3 kW / 6 kWh today, battery −1.5 kW / net charge 1 kWh, available 8 kWh / 50%; these displayed correctly.

Two forecast collections at local midnight and 07:00 predict 08:00/09:00/10:00. First-vintage raw cumulative Wh per panel = 0/1000/2000; adjusted = 0/1100/2200. Second vintage doubles them. Analysis first-vintage differenced energy is therefore 2.2 kWh adjusted / 2.0 raw at 09:00 and 10:00 sitewide. Full-data exports intentionally retain stored-field semantics: first collection, average cumulative Wh **1100**, adjusted W **1100** for calendar buckets. These semantics were **not** forced to match chart differences. Recent latest estimate substitutes completed-hour actuals; at the early run it displayed 8.4 = 4 actual + 4.4 remaining forecast, then evolves with the current local hour.

`independent_values.py` compares downloaded files and browser ECharts options without calling application aggregation functions: **126 comparisons, 121 pass, 5 availability-contract mismatches** (day/week/month/season/year). Numeric tolerance 1e−9 on unrounded export values; chart tolerance 0.05 for documented one-decimal rounding; UI table formats checked separately. It verifies all nonempty actual points in the controlled energy/power/battery families represented in that script, selected forecasts, every export timestep row count and totals, tariff ordering, cumulative last reading, and Excel local hour/numeric values. It does not claim every field in the broad export schema was independently verified. Forecast-only target rows account for minute/hour export counts 5/3; all calendar timesteps yield one row. Excel forecasts-off has 2 actual hourly rows and omits forecast columns.

## Complete mounted feature inventory and coverage

Interaction/value/edge statuses apply only to the named scenarios. P = Pass, F = Fail, Partial = exercised incompletely, NC = Not checked, NA = not applicable. Source inventory was reconciled with actual tabs and five browser canvas charts. `HourlyTab` is unmounted and excluded from mounted-feature claims.

| ID | Mounted feature / scenario | Interaction | Values | Edges / limits | Evidence |
|---|---|---|---|---|---|
| R1 | Recent Live/Today site solar/load/import/export/battery | P | P | Partial: fake device; no real decoding | `recent-verified.txt`, `recent-desktop.png` |
| R2 | East/West live allocation, battery availability/SOC | P | P for supplied live values | Partial: allocation algorithm upstream mocked | same |
| R3 | Latest / BoD / raw site forecast summaries | P | Partial: source formulas inspected, initial 8.4/4.4/4.0 | Partial: two complete vintages only | `server.py`, `recent-verified.txt` |
| R4 | Past 7 days rows, blanks, order, totals | P | P for actual/economics; forecast Partial | Partial: current day, prior empty dates | `recent-verified.txt` |
| R5 | Past 12 months, total/lifetime rows | P | P actual/economics | Partial: one populated month | same |
| R6 | Timer live updates / shared refresh | P live timer | P supplied live values | Partial: snapshot initial load; stale retained refresh not injected | server/logs, fixture source |
| A1 | Lazy Analysis activation, five charts | P | P series identities | P empty DB renders 5 charts | `charts-initial.json`, `empty-analysis.json` |
| A2 | Energy chart/table | P | P known actual and forecast points | Partial: missing periods zero-filled | `chart-0.csv`, `all-tables.txt` |
| A3 | Power chart/table | P | P all supplied non-null points | F misleading Total; initial title says hour although minute points | `chart-1.csv`, `charts-initial.json` |
| A4 | Battery chart/table, dual kWh/% axes | P | P all supplied non-null points | F summed level Total | `chart-2.csv`, `all-tables.txt` |
| A5 | Array actual/forecast chart/table | P | P 1.2/0.8 hourly actual; 1.1 forecast per panel | Partial: two configured arrays only | `chart-3.csv`, options |
| A6 | Money chart/table, costs/revenue/net/rates | P | P unrounded table/CSV | Partial: chart rounds €0.04 to 0.0 under documented one-decimal policy | `chart-4.csv`, options |
| A7 | Split tariff checkbox | NC | P initial split day/night cost totals | NC toggle preservation | source `charts.py` and initial options |
| A8 | As of / Last / Period / Apply | P | P one-day known range | P Last=0 rejected; other date validation Partial | `workflows.py` |
| A9 | Group hour/day/week/month/season/year | P every option applied | P hour and day spot checks | Partial: one populated day means no cross-bucket arithmetic | `charts-day.json`, workflow script |
| A10 | Sum/Average | P | Partial: one populated day; no unequal multi-day reference | Partial | `charts-average.json` |
| A11 | Previous/next period navigation | F for hours | F asymmetric dates | P reproduction for Last=1 hours; month/year edges NC | `extra-results.json`, `warning_navigation.py` |
| A12 | Legend selection retained after Apply | P | P browser selection false retained | Partial: one energy legend only | `extra_workflows.py` |
| A13 | Power zoom | P chart dispatch to 30–50% | P browser zoom state | Partial: API dispatch, drag gesture NC | same |
| A14 | Tooltip | P canvas hover | Partial: screenshot only; no full tooltip field comparison | Partial | `chart-tooltip.png` |
| A15 | Chart/table toggle all five | P | P selected table rows; F telemetry totals | Partial: missing inverter total displays `nan` | `all-tables.txt` |
| A16 | Clipboard | P energy TSV copied/read | P headers and known data observed | Partial other four paths share component | `workflows.py` |
| A17 | Chart CSV all five | P files downloaded/named | P selected fixture fields/totals | Partial full numeric schema | `chart-0.csv`…`chart-4.csv` |
| C1 | Custom dialog opening/Close, empty Refresh | P desktop | P empty chart then Solar preview | Partial input permutations | `custom-empty.txt`, `charts-custom.json` |
| C2 | Custom variables inventory | P Solar chosen | P Solar preview spot check | Partial: other metrics not all exercised | source `CUSTOM_CHART_METRICS`, custom text |
| C3 | Custom own as-of/count/period/group/Sum–Average/split | Partial: controls rendered | Partial inherited Average and hour | NC split combinations/invalid custom input | `custom-filled.txt` |
| C4 | Custom metric/split checkboxes and styles | Partial: rendered Solar legend | Partial | NC split style/filter preservation | `custom-filled.txt` |
| D1 | Full downloads all 7 timesteps, CSV forecasts on | P all downloaded | P known numeric totals/rows/labels | Partial: forecast-only rows included; no large range | `full-*.csv`, `independent-checks.json` |
| D2 | Excel forecasts off | P | P numeric solar, 2 rows, no forecast fields, local 08h | Partial Excel limits not reached | `full-hour-no-forecast.xlsx` |
| D3 | Start/end/timestep/format/forecast controls | P | P tested settings | Partial Cartesian combinations | scripts |
| D4 | Reversed dates, error form restoration, cancellation | P | P validation message | Partial retry after error not fully rerun; top toast intercepts tabs until dismissed/expiry | `workflows.py` |
| D5 | Download progress/success filenames | P | P filenames include chosen date range | NC interrupted transfer/server exception beyond validation | files/scripts |
| S1 | System Info variable/value/unit | P | P Fixture model | Partial missing fields/refresh/latest selection not independently stressed | `system.txt` |
| O1 | Restart confirm/cancel | P mocked | P cancel no call; confirm recorded one | Partial real supervised restart excluded | `control-actions.txt` |
| O2 | Recalculation confirm/success/progress/restart | P mocked | P selected arguments and success text | Partial running state transient | same; `workflow-results.json` |
| O3 | Recalculation failure | P mocked | P error/status; no success restart for failure | Partial subprocess OSError path NC | `fail-control` hook and result text |
| O4 | Control date validation/cancel recalculation | NC | NC | NC; source inspected only | `control.py` |
| W1 | Persisted-data stale banner + Reload | P banner | P fixture timestamps old | NC Reload click; correct stale priority observed | screenshots |
| W2 | Real browser offline/online/socket recovery | P offline and reconnection | P offline→persisted stale | Partial heartbeat/missing-live isolated DOM exercise separate | `workflow-results.json`, warning script |
| W3 | Fresh / missing-live / heartbeat / socket branches | P browser branches | P healthy blank, missing live, waiting heartbeat, socket disconnected, recovered blank | Partial DOM metadata used to supply fresh/missing timestamps; actual socket disconnect/reconnect exercised | `warning-navigation-results.json` |
| I1 | Two independent contexts, grouping isolation | P | P A day / B hour | Partial custom/download isolation and overlapping requests NC | `workflows.py` |
| L1 | Desktop 1440×1000 navigation/charts/tables | P | P visual axes and legends | Partial enormous expanded tables require scrolling | desktop screenshots |
| L2 | Mobile Recent/table overflow/navigation | Partial | P live values | Partial tabs overflow; table container clips/scrolls, 457px layout width vs 390 viewport | `recent-mobile.png`, `mobile-results.json` |
| L3 | Mobile Analysis drawer, apply day, dismiss | P | P day chart title | Partial mobile values same fixture, no separate all-point comparison | `mobile-drawer.png`, script |
| L4 | Mobile custom builder | F usable layout/Close with stale banner | Partial | F narrow metric control, chart collapsed, bottom Close obscured | `custom-mobile.png`, `mobile.py` |
| E1 | Entire empty DB Recent/Analysis | P | P empty charts, no browser errors | Partial fake live still present; metadata empty not separately asserted | empty artifacts |
| E2 | Other required fixture families | NC | NC | one-sample, resets, leap/DST/non-hour, partial panels, tariff changes inside an hour, DB/provider failure, big history | explicit residual gap |

## Findings and acceptance criteria

### B1 — Medium correctness: state and power levels are summed in Total rows (high confidence)

`charts.render_historical_charts` enables `show_total=True` for power/battery without supplying mean aggregations; `ChartDataDisplay._table_rows` defaults every numeric column to sum. In the real browser battery table, four levels 8/9/10/11 become **38.0 kWh**, and 50/55/60/65 become **230.00%**. Power Solar Total is **14.00 kW** rather than sample mean 3.5. These quantities are states/rates rather than additive energy. Unavailable inverter total also renders literal `nan` instead of the missing-value blanks used in regular cells.

Recommendation: remove totals for nonadditive chart tables, or define and label meaningful means (with a deliberate sampling-weighting policy); apply missing formatting to totals. Acceptance: this fixture never labels 38 kWh or 230% as a battery Total and never emits `nan` for all-missing telemetry. If means are chosen, availability 9.5 and SOC 57.5 within displayed precision; power 3.5 kW for these equally spaced observations. Regression cover missing columns and aggregation changes.

### B2 — Medium correctness: hour navigation loses the hour and is asymmetric (high confidence)

`AnalysisTab._render_controls.move_as_of` applies `timedelta(hours=direction * count)` to a **date**. With Last=1 and Period=hours, clicking `<` changes 2026-10-14 to 2026-10-13; `>` adds no day. Source/date arithmetic explains this: positive sub-day timedeltas are truncated while negative values floor to the preceding day. The date-only As of field cannot represent the intended one-hour movement.

Recommendation: decide the hour-navigation contract, then represent an actual datetime or disable/relabel those arrows for hour selections. Acceptance: sequential previous/next navigation for a one-hour range is reversible and neither jumps 24h for a 1h step nor silently does nothing. Test 1/12/24/25 hours and local-day transitions. `extra-results.json` and `warning-navigation-results.json` record actual dates.

### B3 — Medium usability: mobile custom chart controls/chart collapse and stale banner blocks bottom Close (high confidence)

At 390×844, open Analysis drawer → Custom chart. The fixed `w-52 shrink-0` legend leaves the preview only a narrow strip; the Variables selector collapses to an unusably narrow field. With the fixture’s legitimately stale-data banner, the dialog’s bottom Close cannot be clicked: Playwright reports the fixed `dashboard-warning-banner` intercepting pointer events after scrolling. The top close icon/Escape remain alternatives, so this is bounded rather than a complete page lockout. `custom-mobile.png` visibly captures the squeezed chart/selector and banner overlap.

Recommendation: stack or collapse the legend below the chart on phones; ensure selection control minimum usable width and dialog actions remain reachable above persistent warnings. Acceptance: at 390×844 and 360×800, add Solar and Load, refresh, toggle each legend and close via the bottom action with stale warning shown, without horizontal page scrolling or obscured controls. Inspect labels, legends and chart width in the final rendering.

### B4 — Contract inconsistency: battery availability exported as last rather than documented average (high confidence observation; intended policy unresolved)

`data.export_aggregation` treats every `*_kwh` field as a cumulative counter, including `plant_battery_available_discharge_kwh`, which is a state level. Browser-downloaded day/week/month/season/year files contain **11**, while the documented rule “other numeric fields average” gives **9.5** for these readings; hour files similarly contain 9 and 11 instead of 8.5 and 10.5. This is distinct from intentional cumulative forecast stored-field semantics.

Recommendation: explicitly classify counter versus state fields and decide/document the availability contract. If the intended export is a period-ending state, keep last and document that exception; if it follows averaging, use the numeric mean. Acceptance: an independent fixture check passes the selected, explicitly documented policy in CSV and Excel for hour/day intervals, and cumulative solar/grid meters still use last. Do not silently force chart and export state semantics to agree.

## Other observations and follow-up

- Initial power and battery chart titles say “by hour” even though the default one-day data contains 1,440 minute categories. After Apply, power title changes to “by minute”; battery title stays “by hour”. Values are minute points. This is a reproducible labeling inconsistency, lower impact than B1.
- One-decimal chart rounding makes €0.04 hourly export revenue appear as 0.0 and €0.16 net cost as 0.2 although table/CSV preserve cents. This matches the documented current `chart_values` rule; choose a money-specific precision if exact monetary tooltips are required.
- Current-day Recent Solar Forecast changed during elapsed review time and does not match the Analysis vintage by construction. Do not classify these as equal-contract comparisons without specifying current-hour substitution and vintage/cutoff.
- Strong next coverage: controlled DST repeat/skip and non-hour-zone fixtures, within-hour mixed tariffs, one-sample and reset data, latest-valid metadata histories, custom metric/split matrix, real-data backup scale, repeated/overlapping downloads and selected-field browser session isolation.

