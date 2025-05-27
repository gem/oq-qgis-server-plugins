import os
import stat
import random
import string
from qgis.core import QgsMessageLog


def gem_log(msg, log_level):
    QgsMessageLog.logMessage(msg, 'EWMS', log_level)


def alphanum_rndstr(length):
    return ''.join(random.choice(
        string.ascii_uppercase + string.ascii_lowercase + string.digits
    ) for _ in range(length))


def rmdir_recursive(path):
    try:
        # Check if path exists
        if not os.path.exists(path):
            return False

        # If path is a file return False
        if os.path.isfile(path):
            return False

        if os.path.isdir(path):
            items = os.listdir(path)

            for item in items:
                fullpath = os.path.join(path, item)

                if os.path.isdir(fullpath):
                    if not rmdir_recursive(fullpath):
                        return False
                else:
                    try:
                        # add writability to be able to remove file
                        os.chmod(fullpath, stat.S_IWRITE)
                        os.remove(fullpath)
                    except Exception as e:
                        print(f"Error erasing file '{fullpath}': {e}")
                        return False

            try:
                os.rmdir(path)
                return True
            except Exception as e:
                print(f"Error erasing folder '{path}': {e}")
                return False

        return False

    except Exception as e:
        print(f"Generic error: {e}")
        return False
