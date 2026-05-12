#!/bin/bash
# OpenRag Production Deployment Script

set -e

echo "=== OpenRag Production Deployment ==="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "Error: .env file not found!"
    echo "Please copy .env.example to .env and configure it."
    exit 1
fi

# Load environment variables
source .env

# Check required variables
if [ -z "$POSTGRES_PASSWORD" ] || [ -z "$SECRET_KEY" ]; then
    echo "Error: Required environment variables not set!"
    echo "Please configure POSTGRES_PASSWORD and SECRET_KEY in .env"
    exit 1
fi

echo "Step 1: Building Docker images..."
docker-compose -f docker-compose.prod.yml build

echo ""
echo "Step 2: Starting services..."
docker-compose -f docker-compose.prod.yml up -d

echo ""
echo "Step 3: Waiting for services to be healthy (60 seconds)..."
sleep 60

echo ""
echo "Step 4: Running database migrations..."
docker-compose -f docker-compose.prod.yml exec -T api alembic upgrade head || echo "Warning: Migration failed, database may already be initialized"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Services:"
echo "  - Frontend: http://localhost"
echo "  - API: http://localhost:8000"
echo "  - API Docs: http://localhost:8000/docs"
echo "  - Elasticsearch: http://localhost:9200"
echo "  - MinIO Console: http://localhost:9001"
echo ""
echo "Check service status:"
echo "  docker-compose -f docker-compose.prod.yml ps"
echo ""
echo "View logs:"
echo "  docker-compose -f docker-compose.prod.yml logs -f"
