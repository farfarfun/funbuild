import os
import signal

import psutil


class BaseServer:
    def __init__(self, dir_path="~/.cache/servers/base"):
        self.dir_path = dir_path
        self.pid_path = f"{self.dir_path}/run.pid"

    def start(self, *args, **kwargs):
        pass

    def stop(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def _update(self, *args, **kwargs):
        self._cmd_stop(*args, **kwargs)
        self.update(*args, **kwargs)
        self._cmd_start(*args, **kwargs)

    def _cmd_restart(self, *args, **kwargs):
        self._cmd_stop(*args, **kwargs)
        self._cmd_start(*args, **kwargs)

    def _cmd_start(self, *args, **kwargs):
        self.__write_pid()
        self.start(*args, **kwargs)

    def _cmd_stop(self, *args, **kwargs):
        self.__kill_pid()
        self.stop(*args, **kwargs)

    def __write_pid(self):
        cache_dir = os.path.dirname(self.pid_path)
        if not os.path.exists(cache_dir):
            print(f"{cache_dir} not exists.make dir")
            os.makedirs(cache_dir)
        with open(self.pid_path, "w") as f:
            print(f"current pid={os.getpid()}")
            f.write(str(os.getpid()))

    def __read_pid(self, remove=False):
        pid = -1
        if os.path.exists(self.pid_path):
            with open(self.pid_path, "r") as f:
                pid = int(f.read())
            if remove:
                os.remove(self.pid_path)
        return pid

    def __kill_pid(self):
        pid = self.__read_pid(remove=True)
        if not psutil.pid_exists(pid):
            print(f"pid {pid} not exists")
            return
        p = psutil.Process(pid)
        print(pid, p.cwd(), p.name(), p.username(), p.cmdline())
        os.kill(pid, signal.SIGKILL)
