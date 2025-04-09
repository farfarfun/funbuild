import subprocess
import sys
from typing import List


def run_shell(command: str, printf=True) -> str:
    """
    执行shell命令
    """
    if printf:
        try:
            cmd = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stderr=sys.stderr,
                close_fds=True,
                stdout=sys.stdout,
                universal_newlines=True,
                shell=True,
                bufsize=1,
            )
            cmd.communicate()
            return cmd.returncode
        except Exception as e:
            return f"run shell error:{e}"
    else:
        try:
            outputs = subprocess.Popen(command, stdout=subprocess.PIPE, shell=True).communicate()
            outputs = [i.decode("utf-8")[:-1] for i in outputs if i is not None]
            outputs = [i for i in outputs if len(i.strip()) > 0]
            return "".join(outputs)
        except Exception as e:
            return f"run shell error:{e}"


def run_shell_list(command_list: List[str], printf=True) -> str:
    """
    批量执行shell命令
    """
    # for shell in shell_list:
    #    run_shell(shell, printf=printf)
    command = " && ".join(command_list)
    return run_shell(command, printf=printf)
