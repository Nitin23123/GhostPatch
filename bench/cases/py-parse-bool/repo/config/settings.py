from config.env import parse_bool


def debug_enabled(env):
    return parse_bool(env.get("DEBUG", "false"))
