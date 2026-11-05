"""Beijing calendar boundaries, independent of machine timezone."""
import re
from datetime import date,datetime,time,timedelta,timezone
from .common import require

ZONE=timezone(timedelta(hours=8),'Asia/Shanghai')


def report_period(period, anchor):
    require(period in ('day','week','month','year'),'统计周期须为日、周、月或年')
    try:
        require(not anchor or re.fullmatch(r'\d{4}-\d{2}-\d{2}',anchor),'请选择有效日期')
        day=date.fromisoformat(anchor) if anchor else datetime.now(ZONE).date()
        require(1900<=day.year<=9998,'请选择有效日期')
    except ValueError:
        require(False,'请选择有效日期')
    start=day
    if period=='week':start-=timedelta(days=day.weekday())
    elif period=='month':start=day.replace(day=1)
    elif period=='year':start=day.replace(month=1,day=1)
    if period=='year':end=start.replace(year=start.year+1)
    elif period=='month':end=date(start.year+start.month//12,start.month%12+1,1)
    else:end=start+timedelta(days=7 if period=='week' else 1)
    return datetime.combine(start,time(),ZONE),datetime.combine(end,time(),ZONE)
