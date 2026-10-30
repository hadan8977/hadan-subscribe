#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p dist
tar -czf dist/hadan-subscribe-source.tar.gz --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='*.egg-info' src tests scripts deploy docs pyproject.toml uv.lock requirements-core.lock requirements-sources.lock README.md .env.example .gitignore AGENTS.md
sha256sum dist/hadan-subscribe-source.tar.gz > dist/hadan-subscribe-source.tar.gz.sha256
