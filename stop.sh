#!/usr/bin/env bash
cd "$(dirname "$0")"
( cd monitoring && docker compose down )
kind delete cluster --name capstone
echo "Stopped: monitoring down + kind cluster deleted."
