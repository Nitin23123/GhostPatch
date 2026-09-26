from settings.merge import merge

DEFAULTS = {
    "db": {"host": "localhost", "port": 5432, "options": {"timeout": 30, "ssl": False}},
    "debug": False,
}


def load(user_settings):
    return merge(DEFAULTS, user_settings)
