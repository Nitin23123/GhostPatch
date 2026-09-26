from datetime import date

from calendar_utils.days import next_business_day


def delivery_date(order_day: date, business_days: int = 2) -> date:
    day = order_day
    for _ in range(business_days):
        day = next_business_day(day)
    return day
