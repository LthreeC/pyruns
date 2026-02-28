from .log_utils import get_logger  # noqa: F401
from .time_utils import get_now_str, get_now_str_us  # noqa: F401


def client_connected() -> bool:
    """Return True if the current NiceGUI client context is still alive.

    Use this at the top of ``@ui.refreshable`` functions and event
    callbacks to bail out early when the browser tab has already closed,
    avoiding the ``Client has been deleted`` warning.
    """
    try:
        from nicegui import ui
        client = ui.context.client
        # NiceGUI marks deleted clients by removing them from Client.instances
        from nicegui import Client
        return client.id in Client.instances
    except Exception:
        return False
