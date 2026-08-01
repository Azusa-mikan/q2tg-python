#!/bin/sh
set -eu

if [ ! -d /app/data ] || [ -L /app/data ]; then
    printf '%s\n' '错误：/app/data 必须是普通目录' >&2
    exit 1
fi

# Compose 可能以 root:root 创建 bind mount 源目录。已有 ACL 能让容器用户写入时
# 保留宿主机所有者，避免破坏本地运行；否则只修正挂载点本身。
if ! gosu q2tg test -w /app/data; then
    chown q2tg:q2tg /app/data
fi
chmod u+rwx /app/data

exec gosu q2tg "$@"
