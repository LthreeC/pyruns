"""
Time utilities — unified timestamp formatting for task naming and logs.
"""
import datetime

def get_now_str() -> str:
    """Return current time in unified format: YYYY-MM-DD_HH-MM-SS."""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def get_now_str_us() -> str:
    """Return current time with microseconds: YYYY-MM-DD_HH-MM-SS_mmmmmm."""
    # %f 直接生成 6 位微秒 (000000-999999)
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
