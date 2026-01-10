#!/bin/bash
# Setup script untuk install dependencies (Python + Node.js)

set -e

echo "🔧 Setting up Metina Bot..."

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

# Install Node.js dependencies (Meteora SDK)
echo "📦 Installing Node.js dependencies (Meteora SDK)..."
if command -v npm &> /dev/null; then
    npm install
    echo "✅ Meteora SDK installed successfully"
else
    echo "⚠️  npm not found. Meteora SDK will not be available."
    echo "   Install Node.js and npm to enable Meteora SDK features."
fi

echo "✅ Setup complete!"

