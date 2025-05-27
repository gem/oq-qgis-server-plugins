import os
from .gem_common import gem_log
from qgis.core import Qgis


def acquire_lock(lockfile):
    gem_log('acquire_lock: BEGIN: lockfile: %s' % lockfile, Qgis.Critical)
    try:
        with open(lockfile, 'x') as f:  # Atomic create
            f.write(str(os.getpid()))
            gem_log('acquire_lock: SUCCESS', Qgis.Critical)
        return True
    except FileExistsError:
        gem_log('acquire_lock: FAILED', Qgis.Critical)
        # Check if the process holding the lock is still alive
        try:
            with open(lockfile, 'r') as f:
                pid = int(f.read().strip())

            # Check if process exists (Unix-specific)
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return False  # Process exists, lock is valid
        except (ProcessLookupError, ValueError):
            # Process doesn't exist, remove stale lock
            os.unlink(lockfile)
            return acquire_lock(lockfile)  # Try again


def release_lock(lockfile):
    try:
        os.unlink(lockfile)
    except FileNotFoundError:
        pass

# # Usage
# lockfile = '/tmp/myapp.lock'
# if acquire_lock(lockfile):
#     try:
#         print("Lock acquired, doing work...")
#         time.sleep(5)  # Simulate work
#     finally:
#         release_lock(lockfile)
# else:
#     print("Could not acquire lock")
