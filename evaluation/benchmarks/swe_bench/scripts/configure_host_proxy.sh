#!/bin/bash
# Buildx can request registry credentials from the client process. Keep its
# proxy aligned with the Docker daemon even when the caller cleared proxy vars.
_docker_http_proxy=$(docker info --format '{{.HTTPProxy}}' 2>/dev/null) || _docker_http_proxy=
_docker_https_proxy=$(docker info --format '{{.HTTPSProxy}}' 2>/dev/null) || _docker_https_proxy=
_docker_no_proxy=$(docker info --format '{{.NoProxy}}' 2>/dev/null) || _docker_no_proxy=

if [[ "$_docker_http_proxy" =~ ^https?:// ]]; then
  export HTTP_PROXY="$_docker_http_proxy" http_proxy="$_docker_http_proxy"
fi
if [[ "$_docker_https_proxy" =~ ^https?:// ]]; then
  export HTTPS_PROXY="$_docker_https_proxy" https_proxy="$_docker_https_proxy"
fi
if [[ -n "$_docker_no_proxy" ]]; then
  export NO_PROXY="$_docker_no_proxy" no_proxy="$_docker_no_proxy"
fi
# BuildKit's default bridge cannot reach a proxy bound to the host loopback.
# Use host networking for build steps and forward the proxy variables already
# exported above. Keep an explicit caller setting authoritative.
if [[ -z "${RUNTIME_EXTRA_BUILD_ARGS:-}" && "$_docker_https_proxy" =~ ^https?://(127\.0\.0\.1|localhost):[0-9]+/?$ ]]; then
  export RUNTIME_EXTRA_BUILD_ARGS='["--network=host","--allow=network.host","--build-arg=HTTP_PROXY","--build-arg=HTTPS_PROXY"]'
fi
# A SOCKS URL here breaks HTTPX when its optional SOCKS support is absent.
if [[ "${ALL_PROXY:-}" == socks:* || "${ALL_PROXY:-}" == socks[45]* ]]; then
  unset ALL_PROXY
fi
if [[ "${all_proxy:-}" == socks:* || "${all_proxy:-}" == socks[45]* ]]; then
  unset all_proxy
fi
unset _docker_http_proxy _docker_https_proxy _docker_no_proxy
