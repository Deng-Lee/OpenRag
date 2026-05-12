#!/bin/bash
# Stop OpenRag services

echo "Stopping OpenRag services..."
docker-compose -f docker-compose.prod.yml down

echo ""
echo "Services stopped."
echo ""
echo "To remove all data (WARNING: destructive):"
echo "  docker-compose -f docker-compose.prod.yml down -v"
