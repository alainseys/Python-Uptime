#!/bin/bash
echo "Stop Containers, Pull Latest Changes, and Rebuild Containers"
docker compose down
echo "Pulling latest changes..."
git pull
echo "Rebuilding and starting containers... (no Daemon mode)"
docker compose up --build 
