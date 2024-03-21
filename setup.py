import sys

from funpypi import setup

install_requires = ["GitPython",'twine','build']

setup(
    name="funbuild",
    entry_points={
        "console_scripts": [
            "funbuild = funbuild.core:funbuild",
        ]
    },
    install_requires=install_requires,
)
