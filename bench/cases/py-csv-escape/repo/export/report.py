from export.csv_row import csv_row


def customer_line(customer):
    return csv_row([customer["name"], customer["city"], customer["total"]])
