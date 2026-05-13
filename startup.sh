#!/bin/bash

cd /home/ubuntu/chatbot/ || exit 1

tmux kill-session -t api 2>/dev/null

tmux new-session -d -s api 'venv/bin/uvicorn main:app --reload --port 8000 --host 172.17.0.1'
