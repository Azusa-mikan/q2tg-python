#!/bin/sh
set -eu

if [ ! -d /app/data ] || [ -L /app/data ]; then
    printf '%s\n' '错误：/app/data 必须是普通目录' >&2
    exit 1
fi

# Compose 可能以 root:root 创建 bind mount 源目录。只修正挂载点本身，
# 不递归修改用户放入数据目录的其他内容。
chown q2tg:q2tg /app/data
chmod u+rwx /app/data

exec su-exec q2tg "$@"
