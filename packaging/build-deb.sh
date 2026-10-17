#!/usr/bin/env bash
# 把一个已经构建好的静态二进制打成 .deb。
#
#   build-deb.sh <二进制> <版本号> <amd64|arm64> [输出目录]
#
# 只接受 musl 静态二进制 —— control 里不写库依赖，拿 gnu 动态二进制打出来的包在
# glibc 更老的机器上装得上、跑不起来。
set -euo pipefail

BIN="${1:-}"
VERSION="${2:-}"
ARCH="${3:-}"
OUTPUT_DIR="${4:-dist}"

die() {
  echo "错误: $*" >&2
  exit 1
}

[[ -f "$BIN" ]] || die "给的二进制不存在: ${BIN:-<空>}"
VERSION="${VERSION#v}"
[[ -n "$VERSION" && "$VERSION" =~ ^[0-9][0-9A-Za-z.+:~-]*$ ]] || die "版本号无效: ${VERSION:-<空>}"
case "$ARCH" in
  amd64 | arm64) ;;
  *) die "架构只认 amd64 / arm64，给的是: ${ARCH:-<空>}" ;;
esac
command -v dpkg-deb >/dev/null 2>&1 || die "缺少 dpkg-deb"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
STAGE="${WORK}/root"

install -D -m 0755 "$BIN" "${STAGE}/usr/bin/funbuild"
install -D -m 0644 "${ROOT}/LICENSE" "${STAGE}/usr/share/doc/funbuild/copyright"

INSTALLED_SIZE="$(du -sk "${STAGE}/usr" | awk '{print $1}')"
mkdir -p "${STAGE}/DEBIAN"
# 没有库依赖：二进制是 musl 静态链接的。
#
# 但 git 是 Depends 而不是 Recommends：funbuild 的每一条命令都要先 `git
# rev-parse --show-toplevel` 找仓库根，没有 git 它连启动探测都过不去。
#
# uv 不写进依赖：Debian 仓库里没有这个包（它自己走 PyPI / 独立二进制分发）。
# 发 Python 项目的人得自己装，见 Description。
cat >"${STAGE}/DEBIAN/control" <<EOF
Package: funbuild
Version: ${VERSION}
Section: devel
Priority: optional
Architecture: ${ARCH}
Maintainer: farfarfun <farfarfun@qq.com>
Installed-Size: ${INSTALLED_SIZE}
Depends: git
Homepage: https://github.com/farfarfun/funbuild
Description: One-command release pipeline for Python, frontend, Flutter and Rust repos
 funbuild detects the build strategy from the repository layout, then chains
 version bump, manifest sync, build, install check, publish, commit and tag.
 Statically linked; no library dependencies.
 .
 Releasing Python projects additionally needs uv on PATH (not packaged for
 Debian; install it from https://github.com/astral-sh/uv). Frontend, Flutter
 and Rust projects need their own toolchains.
EOF

PACKAGE="${OUTPUT_DIR}/funbuild_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "${STAGE}" "${PACKAGE}" >/dev/null
echo "$PACKAGE"
