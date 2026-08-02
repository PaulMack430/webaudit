#!/bin/bash
echo "Stopping WebAudit server..."
pkill -f "python3 webaudit.py" 2>/dev/null && echo "Stopped." || echo "Server wasn't running."
