#!/bin/sh
# Root administrator entrypoint: systemd loads private env without shell-sourcing it.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this administrator command as root.' >&2
    exit 1
fi
if [ "$#" -eq 0 ]; then
    echo 'Usage: quant-admin [--source] <hadan command> [arguments]' >&2
    exit 2
fi
if [ "$1" = '--source' ]; then
    shift
    exec systemd-run --quiet --wait --pipe --collect \
      --property=User=hadan --property=Group=hadan \
      --property=WorkingDirectory=/opt/quant-subscribe/current \
      --property=EnvironmentFile=/etc/quant-subscribe/hadan.env \
      --property=EnvironmentFile=/etc/quant-subscribe/source.env \
      --property=NoNewPrivileges=yes --property=UMask=0077 \
      /opt/quant-subscribe/current/.venv/bin/python -m hadan.cli "$@"
fi
exec systemd-run --quiet --wait --pipe --collect \
  --property=User=hadan --property=Group=hadan \
  --property=WorkingDirectory=/opt/quant-subscribe/current \
  --property=EnvironmentFile=/etc/quant-subscribe/hadan.env \
  --property=NoNewPrivileges=yes --property=UMask=0077 \
  /opt/quant-subscribe/current/.venv/bin/python -m hadan.cli "$@"
