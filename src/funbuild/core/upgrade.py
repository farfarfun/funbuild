import os.path


def __version_upgrade(version, step=128):
    if version is None:
        version = "0.0.1"

    version1 = [int(i) for i in version.split(".")]
    version2 = version1[0] * step * step + version1[1] * step + version1[2] + 1

    version1[2] = version2 % step
    version1[1] = int(version2 / step) % step
    version1[0] = int(version2 / step / step)

    return "{}.{}.{}".format(*version1)


def method1():
    version_path = "./script/__version__.md"

    def read():
        return open(version_path, "r").read()

    def write(version):
        with open(version_path, "w") as f:
            f.write(version)

    return os.path.exists(version_path), read, write


def method2():
    toml_path = "./pyproject.toml"

    def read():
        import toml

        a = toml.load(toml_path)
        return a["tool"]["poetry"]["version"]

    def write(version):
        import toml

        a = toml.load(toml_path)
        a["tool"]["poetry"]["version"] = version
        with open(toml_path, "w") as f:
            toml.dump(a, f)

    return os.path.exists(toml_path), read, write


method_list = [method2, method1]


def version_read():
    for method in method_list:
        exists, read, write = method()
        if exists:
            return read()
    print("not support")


def version_upgrade(args=None, step=64, **kwargs):
    for method in method_list:
        exists, read, write = method()
        if exists:
            version1 = read()
            version2 = __version_upgrade(version1, step=step)
            write(version2)
            return version2
    print("not support")
