import argparse
import os

from funutil import getLogger

from funbuild.shell import run_shell

logger = getLogger("funbuild")


class ServerManage:
    def __init__(self):
        self.manage_conf_path = "/fundata/funbuild/supervisord.conf"
        self.conf_dir = "/fundata/funbuild/conf"

    def init(self):
        run_shell(f"mkdir -p {self.conf_dir}")
        run_shell(f"cp -f {os.path.abspath(os.path.dirname(__file__))}/supervisord.conf {self.manage_conf_path}")

    def init2(self):
        run_shell(f"mkdir -p {self.conf_dir}")
        run_shell(f"echo_supervisord_conf > {self.manage_conf_path}")
        append_data = f"""
[include]
files = {self.conf_dir}/*.ini
        """
        data = open(self.manage_conf_path, "r").read()  # noqa: UP015
        data += append_data
        with open(self.manage_conf_path, "w") as f:
            f.write(data)

    def start(self):
        cmd = f"supervisord -c {self.manage_conf_path}"
        logger.info(cmd)
        run_shell(cmd)

    def add_job(self, server_name, directory, command, user="bingtao", stdout_logfile=None):
        default_logfile = f"/fundata/logs/funbuild/{server_name}.log"
        config = f"""[program:{server_name}]
directory = {directory}
command = {command}
autostart = true
autorestart = true
user = {user}
stdout_logfile = {stdout_logfile or default_logfile}
        """
        with open(f"{self.conf_dir}/{server_name}.ini", "w") as f:
            f.write(config)


class BaseServer:
    def __init__(self, server_name="base_server", current_path=None, *args, **kwargs):
        self.server_name = server_name
        self.current_path = current_path or os.path.abspath(os.path.dirname(__file__))
        self.manage = ServerManage()
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument("cmd", default="unknown", help="init, stop, start, restart")

    def init(self):
        pass

    def status(self):
        run_shell(f"supervisorctl -c {self.manage.manage_conf_path} status")

    def stop(self):
        run_shell(f"supervisorctl -c {self.manage.manage_conf_path} stop {self.server_name}")

    def start(self):
        run_shell(f"supervisorctl -c {self.manage.manage_conf_path} start {self.server_name}")

    def restart(self):
        run_shell(f"supervisorctl -c {self.manage.manage_conf_path} restart {self.server_name}")

    def run(self, cmd):
        logger.info(cmd)
        run_shell(cmd)

    def parse_and_run(self):
        values, unknown = self.parser.parse_known_args()
        if values.cmd == "init":
            self.init()
        elif values.cmd == "stop":
            self.stop()
        elif values.cmd == "start":
            self.start()
        elif values.cmd == "restart":
            self.restart()
        else:
            self.parser.print_help()
