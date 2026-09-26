from units.convert import f_to_c


def report(city, fahrenheit):
    return f"{city}: {round(f_to_c(fahrenheit))}°C"
