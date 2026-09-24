"""Read-only self-consumption simulation for SigEnergy planning inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import cast

import numpy as np
from scipy.optimize import linprog
from config import Config
from economics import tariff_rule, tariff_rules_for_timestamp

type Variable = tuple[str, int]

@dataclass
class DeviceInfo:
    inverter_model: str
    inverter_serial_number: str
    inverter_firmware: str
    inverter_rated_power_kw: float
    inverter_max_apparent_power_kva: float
    inverter_max_active_power_kw: float
    inverter_max_absorption_power_kw: float
    inverter_battery_charge_power_kw: float
    inverter_battery_discharge_power_kw: float
    rated_grid_voltage_v: float
    rated_grid_frequency_hz: float
    battery_pack_count: float
    pv_string_count: float
    mppt_count: float
    system_battery_capacity_kwh: float
    battery_charge_cut_off_soc_percent: float
    battery_discharge_cut_off_soc_percent: float
    battery_state_of_health_percent: float
    maximum_pv_input_power_kw: float
    charge_efficiency: float
    discharge_efficiency: float

@dataclass
class DeviceState:
    battery_kwh: float


@dataclass(frozen=True)
class InputParameters:
    num_projection_hours: int
    projection_resolution_mins: int


@dataclass(init=False)
class ForecastFigures:
    starts_at: datetime
    ends_at: datetime
    solar_kwh: float
    load_kwh: float

    @property
    def duration_hours(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 3600


@dataclass(init=False)
class TimePeriodState:
    starts_at: datetime
    ends_at: datetime
    solar_kwh: float
    load_kwh: float
    minimum_battery_kwh: float

    battery_start_kwh: float

    solar_to_battery_kwh: float
    solar_to_grid_kwh: float
    solar_to_load_kwh: float
    solar_to_inverter_kwh: float
    solar_clipped_kwh: float

    battery_to_load_kwh: float
    battery_to_grid_kwh: float
    battery_to_inverter_kwh: float
    solar_to_battery_loss_kwh: float
    grid_to_battery_loss_kwh: float
    battery_to_ac_loss_kwh: float

    inverter_dc_to_ac_kwh: float
    inverter_ac_to_dc_kwh: float

    grid_import_kwh: float
    grid_export_kwh: float

    battery_end_kwh: float

    tariff_name: str
    import_rate: float
    export_rate: float
    import_cost: float
    export_revenue: float
    net_cost: float


def simulate_energy_plan(
    device_info: DeviceInfo,
    device_state: DeviceState,
    input_parameters: InputParameters,
    forecast_figures: ForecastFigures,
    config: Config,
) -> list[TimePeriodState]:

    num_time_periods: int = int(input_parameters.num_projection_hours * 60) // input_parameters.projection_resolution_mins

    if num_time_periods <= 0:
        raise ValueError("num_projection_hours must be greater than zero")

    state = [ TimePeriodState() for t in range(num_time_periods) ]
    theta_hrs = input_parameters.projection_resolution_mins / 60

    # Assign load & forecast figures to each time period
    for t,tp in enumerate(state):
        tp.starts_at = forecast_figures.starts_at + timedelta(minutes=t * input_parameters.projection_resolution_mins)
        tp.ends_at = tp.starts_at + timedelta(minutes=input_parameters.projection_resolution_mins)
        tp.solar_kwh = forecast_figures.solar_kwh * theta_hrs
        tp.load_kwh = forecast_figures.load_kwh * theta_hrs

    # Assign tariff rates to each time period
    for t,tp in enumerate(state):
        import_rules, export_rules = tariff_rules_for_timestamp(config.tariffs, tp.starts_at)
        import_rule = tariff_rule(import_rules, tp.starts_at)
        tp.import_rate = import_rule.rate
        tp.tariff_name = cast(str, import_rule.name)
        tp.export_rate = tariff_rule(export_rules, tp.starts_at).rate

        tp.minimum_battery_kwh = tariff_rule(config.energy_plan.minimum_battery_kwh, tp.starts_at).rate



    # convert the problem into a linear programming problem, where we minimize the cost of energy over the time periods, subject to constraints on battery capacity, charge/discharge rates, load requirements, required minimum battery charge to meet load
    if device_info.charge_efficiency <= 0 or device_info.discharge_efficiency <= 0:
        raise ValueError("battery efficiencies must be greater than zero")

    # Build the model using named variables and constraints.  ``linprog`` uses
    # a single vector internally, but the conversion is intentionally deferred
    # until every energy equation has been declared below.
    variables: list[Variable] = []
    variable_index: dict[Variable, int] = {}
    variable_bounds: dict[Variable, tuple[float | None, float | None]] = {}
    objective: dict[Variable, float] = {}
    equalities: list[tuple[dict[Variable, float], float]] = []
    inequalities: list[tuple[dict[Variable, float], float]] = []

    def add_variable(name: str,period: int,lower: float | None = None,upper: float | None = None) -> Variable:
        variable = (name, period)
        variable_index[variable] = len(variables)
        variables.append(variable)
        variable_bounds[variable] = (lower, upper)
        return variable

    # The objective is to minimise the net cost (inflow_cost-outflow_revenue) of energy over the time periods.
    # Each variable is multiplied by its coefficient and summed to form the objective function.
    # The coefficient is import / export rate (eg 0.3c/kWh)
    # The lin program will find the values of each variable that minimise the objective function, subject to the constraints defined below.
    def add_objective(variable: Variable, coefficient: float):
        objective[variable] = objective.get(variable, 0) + coefficient

    # Add an equality constraint of the form sum(coefficient × variable) = value.
    def add_equality(value: float, *terms: tuple[Variable, float]):
        equalities.append((dict(terms), value))

    # Add an inequality constraint of the form sum(coefficient × variable) ≤ value.
    def add_inequality(value: float, *terms: tuple[Variable, float]):
        inequalities.append((dict(terms), value))

    # Convert named constraints into the matrix form A x = b or A x ≤ b expected by linprog.
    def compile_rows(rows: list[tuple[dict[Variable, float], float]]) -> tuple[np.ndarray, np.ndarray]:
        coefficients = np.zeros((len(rows), len(variables)))
        values = np.zeros(len(rows))
        for row, (terms, value) in enumerate(rows):
            values[row] = value
            for variable, coefficient in terms.items():
                coefficients[row, variable_index[variable]] = coefficient
        return coefficients, values

    capacity_kwh = device_info.system_battery_capacity_kwh * device_info.battery_state_of_health_percent / 100
    max_charge_kwh = device_info.inverter_battery_charge_power_kw * theta_hrs   # the maximum amount of energy that can be charged into the battery in this time period
    max_discharge_kwh = device_info.inverter_battery_discharge_power_kw * theta_hrs

    if capacity_kwh <= 0 or max_charge_kwh < 0 or max_discharge_kwh < 0:
        raise ValueError("battery capacity and charge/discharge limits must be non-negative")

    flows: dict[str, list[Variable]] = {
        name: []
        for name in (
            "solar_to_load",
            "solar_to_battery",
            "solar_to_grid",
            "solar_to_battery_loss",
            "solar_clipped",
            "battery_to_load",
            "battery_to_grid",
            "battery_to_ac_loss",
            "grid_to_load",
            "grid_to_battery",
            "grid_to_battery_loss",
        )
    }
    battery_level_terms: list[tuple[Variable, float]] = []

    # Add each flow for each period; each flow-period pair is a separate LP variable.
    for period, tp in enumerate(state):
        for name in flows:
            flows[name].append(add_variable(name, period, lower=0))

    # For each period t, choose energy flows that satisfy:
    #
    #   solar_kwh[t] = solar_to_load[t] + solar_to_battery[t]
    #       + solar_to_battery_loss[t] + solar_to_grid[t] + solar_clipped[t]
    #
    #   load_kwh[t] = solar_to_load[t] + battery_to_load[t] + grid_to_load[t]
    #
    # Charging flows represent energy stored in the battery; charging losses
    # are separate energy portions supplied alongside those flows:
    #
    #   solar_to_battery_loss[t] = (1 / charge_efficiency - 1) * solar_to_battery[t]
    #   grid_to_battery_loss[t]  = (1 / charge_efficiency - 1) * grid_to_battery[t]
    #
    # Discharge loss is fixed by the discharge efficiency:
    #
    #   battery_to_ac_loss[t] = (1 / discharge_efficiency - 1) * (battery_to_load[t] + battery_to_grid[t])
    #
    # The battery level is derived from its initial value plus cumulative net
    # battery flows, and must remain within its reserve and usable capacity:
    #
    #   minimum_battery_kwh[t] <= battery_end_kwh[t] <= capacity_kwh
    #   battery_end_kwh[t] = battery_start_kwh
    #         + solar_to_battery[t] + grid_to_battery[t]
    #         - ( battery_to_load[t] + battery_to_grid[t] + battery_to_ac_loss[t] )
    #
    # Finally, charging, discharging, and inverter output are capped. The
    # objective then minimises total import cost minus export revenue across
    # every period; solar_clipped[t] explicitly accounts for curtailed solar.
    for period, tp in enumerate(state):
        solar_to_load = flows["solar_to_load"][period]
        solar_to_battery = flows["solar_to_battery"][period]
        solar_to_grid = flows["solar_to_grid"][period]
        solar_clipped = flows["solar_clipped"][period]
        battery_to_load = flows["battery_to_load"][period]
        battery_to_grid = flows["battery_to_grid"][period]
        grid_to_load = flows["grid_to_load"][period]
        grid_to_battery = flows["grid_to_battery"][period]
        solar_to_battery_loss = flows["solar_to_battery_loss"][period]
        grid_to_battery_loss = flows["grid_to_battery_loss"][period]
        battery_to_ac_loss = flows["battery_to_ac_loss"][period]

        # Every unit of solar generation has an explicit destination.
        add_equality(
            tp.solar_kwh,
            (solar_to_load, 1),
            (solar_to_battery, 1),
            (solar_to_battery_loss, 1),
            (solar_to_grid, 1),
            (solar_clipped, 1),
        )
        add_equality(
            tp.load_kwh,
            (solar_to_load, 1),
            (battery_to_load, 1),
            (grid_to_load, 1),
        )

        # Explicitly account for energy lost during battery charging and discharging.
        add_equality(
            0,
            (solar_to_battery_loss, 1),
            (solar_to_battery, -(1 / device_info.charge_efficiency - 1)),
        )
        add_equality(
            0,
            (grid_to_battery_loss, 1),
            (grid_to_battery, -(1 / device_info.charge_efficiency - 1)),
        )
        add_equality(
            0,
            (battery_to_ac_loss, 1),
            (battery_to_load, -(1 / device_info.discharge_efficiency - 1)),
            (battery_to_grid, -(1 / device_info.discharge_efficiency - 1)),
        )

        # Battery energy is derived from cumulative flows; constrain the derived
        # level to remain between the configured reserve and usable capacity.
        battery_level_terms.extend(
            (variable,coefficient)
            for variable, coefficient in (
                (solar_to_battery, 1),
                (grid_to_battery, 1),
                (battery_to_load, -1),
                (battery_to_grid, -1),
                (battery_to_ac_loss, -1),
            )
        )
        add_inequality(
            capacity_kwh - device_state.battery_kwh,
            *[(variable, -coefficient) for variable, coefficient in battery_level_terms],
        )
        add_inequality(
            device_state.battery_kwh - tp.minimum_battery_kwh,
            *battery_level_terms,
        )

        add_inequality(
            max_charge_kwh,
            (solar_to_battery, 1),
            (solar_to_battery_loss, 1),
            (grid_to_battery, 1),
            (grid_to_battery_loss, 1),
        )
        add_inequality(
            max_discharge_kwh,
            (battery_to_load, 1),
            (battery_to_grid, 1),
        )

        # inverter limit (solar_clipping)
        add_inequality(
            device_info.inverter_max_active_power_kw * theta_hrs,
            (solar_to_load, 1),
            (solar_to_grid, 1),
            (battery_to_load, 1),
            (battery_to_grid, 1),
        )

        add_objective(grid_to_load, tp.import_rate)
        add_objective(grid_to_battery, tp.import_rate)
        add_objective(grid_to_battery_loss, tp.import_rate)
        add_objective(solar_to_grid, -tp.export_rate)
        add_objective(battery_to_grid, -tp.export_rate)

    objective_vector = np.zeros(len(variables))
    for variable, coefficient in objective.items():
        objective_vector[variable_index[variable]] = coefficient

    equality_matrix, equality_values = compile_rows(equalities)
    inequality_matrix, inequality_values = compile_rows(inequalities)

    result = linprog(
        objective_vector,
        A_ub=inequality_matrix,
        b_ub=inequality_values,
        A_eq=equality_matrix,
        b_eq=equality_values,
        bounds=[variable_bounds[variable] for variable in variables],
        method="highs",
    )

    if not result.success:
        raise ValueError(f"unable to find a feasible energy plan: {result.message}")

    def value(name: str, period: int) -> float:
        return result.x[variable_index[flows[name][period]]]

    for period, tp in enumerate(state):
        tp.battery_start_kwh = (
            device_state.battery_kwh
            if period == 0
            else state[period - 1].battery_end_kwh
        )
        tp.solar_to_load_kwh = value("solar_to_load", period)
        tp.solar_to_battery_kwh = value("solar_to_battery", period)
        tp.solar_to_battery_loss_kwh = value("solar_to_battery_loss", period)
        tp.grid_to_battery_loss_kwh = value("grid_to_battery_loss", period)
        tp.solar_to_grid_kwh = value("solar_to_grid", period)
        tp.solar_to_inverter_kwh = (
            tp.solar_to_load_kwh
            + tp.solar_to_battery_kwh
            + tp.solar_to_battery_loss_kwh
            + tp.solar_to_grid_kwh
        )
        tp.solar_clipped_kwh = value("solar_clipped", period)
        tp.battery_to_load_kwh = value("battery_to_load", period)
        tp.battery_to_grid_kwh = value("battery_to_grid", period)
        tp.battery_to_inverter_kwh = (
            tp.battery_to_load_kwh + tp.battery_to_grid_kwh
        )
        tp.battery_to_ac_loss_kwh = value("battery_to_ac_loss", period)
        tp.inverter_dc_to_ac_kwh = (
            tp.solar_to_load_kwh
            + tp.solar_to_grid_kwh
            + tp.battery_to_load_kwh
            + tp.battery_to_grid_kwh
        )
        tp.inverter_ac_to_dc_kwh = (
            value("grid_to_battery", period) + tp.grid_to_battery_loss_kwh
        )
        tp.grid_import_kwh = (
            value("grid_to_load", period)
            + value("grid_to_battery", period)
            + tp.grid_to_battery_loss_kwh
        )
        tp.grid_export_kwh = (
            tp.solar_to_grid_kwh + tp.battery_to_grid_kwh
        )
        tp.battery_end_kwh = (
            tp.battery_start_kwh
            + tp.solar_to_battery_kwh
            + value("grid_to_battery", period)
            - tp.battery_to_load_kwh
            - tp.battery_to_grid_kwh
            - tp.battery_to_ac_loss_kwh
        )
        tp.import_cost = tp.grid_import_kwh * tp.import_rate
        tp.export_revenue = tp.grid_export_kwh * tp.export_rate
        tp.net_cost = tp.import_cost - tp.export_revenue

    return state
