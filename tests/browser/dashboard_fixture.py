import sys, os
from pathlib import Path
ROOT=Path.cwd(); sys.path.insert(0,str(ROOT))
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from solar_model.storage.schema import Base, SigenStorModbusSample as M, ForecastSolarSample as F, SigenStorDevice as D
from solar_model.storage.migrations import open_database
from solar_model.config import load_config
TMP=Path(__file__).parent
cfg=load_config(ROOT/'config.yaml')
cfg=replace(cfg,database=replace(cfg.database,path=str(TMP/'fixture.db')),dashboard=replace(cfg.dashboard,host='127.0.0.1',port=18091),actuals=replace(cfg.actuals,data_retrival_schedule=replace(cfg.actuals.data_retrival_schedule,live_update_interval_seconds=1,full_updated_interval_seconds=300)))
if not Path(cfg.database.path).exists():
 e=create_engine('sqlite:///'+cfg.database.path); Base.metadata.create_all(e)
 with Session(e) as s:
  day=datetime.now(ZoneInfo(cfg.timezone)).replace(hour=0,minute=0,second=0,microsecond=0)
  for i in range(4):
   stamp=(day+timedelta(hours=8,minutes=30*i)).astimezone(timezone.utc).isoformat()
   s.add(M(collected_at_utc=stamp,plant_pv_power_kw=2+i,plant_load_power_kw=1+i,plant_grid_power_kw=0.5,plant_battery_power_kw=-0.5,plant_battery_available_discharge_kwh=8+i,plant_battery_soc_percent=50+5*i,plant_pv_daily_kwh=1+i,plant_pv_daily_kwh_period=1,plant_pv_total_kwh=101+i,plant_pv_total_kwh_period=1,plant_load_daily_kwh=0.5*(1+i),plant_load_daily_kwh_period=0.5,plant_load_total_kwh=50+i*0.5,plant_load_total_kwh_period=0.5,plant_grid_import_total_kwh=10+0.25*i,plant_grid_import_total_kwh_period=0.25,plant_grid_export_total_kwh=20+0.1*i,plant_grid_export_total_kwh_period=0.1,plant_battery_charge_total_kwh_period=0.2,plant_battery_discharge_total_kwh_period=0.1,inverter_battery_charge_daily_kwh_period=0.2,inverter_battery_discharge_daily_kwh_period=0.1,inverter_battery_charge_daily_kwh=0.2*(1+i),inverter_battery_discharge_daily_kwh=0.1*(1+i),inverter_pv1_power_kw=(2+i)*0.6,inverter_pv2_power_kw=(2+i)*0.4,inverter_pv1_energy_kwh_period=0.6,inverter_pv2_energy_kwh_period=0.4,import_rate=0.4,export_rate=0.2,import_tariff_band='night' if i<2 else 'day',grid_import_cost_period=0.1,grid_export_revenue_period=0.02,net_cost_period=0.08,no_solar_battery_import_cost_period=0.2))
  for vintage,factor in [(0,1),(7,2)]:
   for h in [8,9,10]:
    vals={}
    for p in [1,2]:
     for kind,mult in [('raw',1),('adj',1.1)]:
      vals.update({f'panel_{p}_{kind}_watts':1000*factor*mult,f'panel_{p}_{kind}_watt_hours':(h-8)*1000*factor*mult,f'panel_{p}_{kind}_watt_hours_day':5000*factor*mult})
    s.add(F(collected_at_utc=(day+timedelta(hours=vintage)).isoformat(),forecast_time=(day+timedelta(hours=h)).isoformat(),**vals))
  s.add(D(collected_at_utc=day.isoformat(),collected_at_local=day.isoformat(),variable='model',variable_name='Device model',value='Fixture model',unit=''))
  s.commit()
 db=open_database(cfg.database.path); db.rebuild_hourly_rollup(day.date(),day.date(),ZoneInfo(cfg.timezone)); db.close()
import solar_model.dashboard.app as main_dashboard
from solar_model.storage.queries.types import LivePowerData
class FakeLive:
 def __init__(self,*a): pass
 def stop(self): pass
 def refresh_browser_activity(self): pass
 def snapshot(self):
  now=datetime.now(timezone.utc)
  return LivePowerData(now.isoformat(),now.astimezone(ZoneInfo(cfg.timezone)).isoformat(),{'solar':3.,'load':1.,'battery':-1.5,'grid_import':0.,'grid_export':0.5,'battery_available_energy_kwh':8.,'battery_soc_percent':50.,'today_solar':6.,'today_load':4.,'today_battery_charge':2.,'today_battery_discharge':1.,'today_grid_import':0.5,'today_grid_export':2.,'pv1':1.8,'pv2':1.2,'today_pv1':3.6,'today_pv2':2.4})
main_dashboard.load_config=lambda:cfg
main_dashboard.LivePowerCollector=FakeLive
import solar_model.dashboard.tabs.control as ctl
os.environ['SOLAR_MODEL_RESTART_FILE']=str(TMP/'mock-restart')
def record(kind,*args):
 with (TMP/'control-actions.txt').open('a') as f: f.write(kind+' '+repr(args)+'\n')
def recalc(*args):
 record('recalculate',*args)
 if (TMP/'fail-control').exists(): raise RuntimeError('fixture recalculation failure')
 return 'Fixture recalculated successfully'
ctl.run_recalculation=recalc
ctl.restart_application=lambda:record('restart')
from nicegui import app
@app.post('/__review_shutdown')
def review_shutdown():
 app.shutdown()
 return {'shutdown':'requested'}
if __name__=='__main__': main_dashboard.main()

