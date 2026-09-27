import os
import time

from pyruns.update_coordination import UPDATE_LOCK_POLL_SECONDS, UpdateCoordinationError


def _acquire_native_guard(self, deadline: float) -> tuple[int | None, bool]:
    flags = os.O_CREAT | os.O_RDWR
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    fd = os.open(self.lock_guard_path, flags, 0o600)
    self._set_non_inheritable(fd)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        while True:
            acquired = self._try_native_lock(fd)
            if acquired is True:
                return fd, True
            if acquired is None:
                os.close(fd)
                return None, False
            if time.monotonic() >= deadline:
                raise UpdateCoordinationError(
                    "Timed out waiting for the shared Pyruns update lock."
                )
            time.sleep(UPDATE_LOCK_POLL_SECONDS)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
