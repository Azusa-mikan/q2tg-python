#!/usr/bin/env bash

version=""
commit_args=()

while (( $# > 0 )); do
    case "$1" in
        -v)
            if (( $# < 2 )); then
                echo "-v requires a version in x.x.x or x.x.x-suffix format" >&2
                exit 2
            fi
            if [[ -n "$version" ]]; then
                echo "-v may only be specified once" >&2
                exit 2
            fi
            version="$2"
            shift 2
            ;;
        *)
            commit_args+=("$1")
            shift
            ;;
    esac
done

if [[ -n "$version" && ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9][A-Za-z0-9_.-]*)?$ ]]; then
    echo "invalid version '$version': expected x.x.x or x.x.x-suffix" >&2
    exit 2
fi
if [[ -n "$version" && -n "$(git tag --list "$version")" ]]; then
    echo "version tag '$version' already exists" >&2
    exit 2
fi

git add . && git commit "${commit_args[@]}" || exit
if [[ -n "$version" ]]; then
    git tag "$version" && git push --atomic origin HEAD "$version"
else
    git push
fi
