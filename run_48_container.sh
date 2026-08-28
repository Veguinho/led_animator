#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container_image="led-animator:local"
container_memory_limit="${LED_ANIMATOR_MEMORY_LIMIT:-1g}"
container_cpu_limit="${LED_ANIMATOR_CPU_LIMIT:-2}"

if [[ $# -eq 0 ]]; then
    echo "Usage: ./run_48_container.sh VIDEO [animator options]" >&2
    echo "Example: ./run_48_container.sh video_clips/movie.mp4 -o output/movie_48" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "error: Docker was not found on PATH" >&2
    exit 1
fi

if ! docker image inspect "$container_image" >/dev/null 2>&1; then
    docker build --tag "$container_image" "$project_dir"
fi
docker run --rm --init \
    --memory "$container_memory_limit" \
    --memory-swap "$container_memory_limit" \
    --cpus "$container_cpu_limit" \
    --pids-limit 256 \
    --network none \
    --user "$(id -u):$(id -g)" \
    --volume "$project_dir:/workspace" \
    --workdir /workspace \
    --entrypoint python3 \
    "$container_image" /workspace/led_animator_48.py "$@"
