#!/bin/bash

# =============================================================================
# StockMinded VPS Auto-Setup Script (Optimized for GCP e2-micro)
# =============================================================================
# This script prepares a clean Ubuntu server for independent 24/7 trading.
# It sets up swap, python, node, and pm2 for process management.
# =============================================================================

set -e # Exit on error

echo "🚀 Starting StockMinded VPS Setup..."

# 1. System Updates
sudo apt-get update
sudo apt-get upgrade -y

# 2. Install Essential Tools
sudo apt-get install -y git python3 python3-pip python3-venv curl build-essential libssl-dev

# 3. Enable 2GB Swap (Critical for 1GB RAM instances like e2-micro)
if [ ! -f /swapfile ]; then
    echo "💾 Creating 2GB Swap file..."
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "✅ Swap enabled."
else
    echo "ℹ️ Swap file already exists."
fi

# 4. Install Node.js & PM2 (Process Manager)
if ! command -v pm2 &> /dev/null; then
    echo "🟢 Installing Node.js and PM2..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
    sudo npm install -g pm2
    echo "✅ PM2 installed."
fi

# 5. Application Setup
APP_DIR="$HOME/StockMinded"

if [ ! -d "$APP_DIR" ]; then
    echo "📥 Repository not found at $APP_DIR."
    echo "Please clone your repository first using: git clone <your-repo-url> $HOME/StockMinded"
    echo "Then run this script again."
    exit 1
fi

cd "$APP_DIR"

# 6. Setup Python Virtual Environment
echo "🐍 Setting up Python Virtual Environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "✅ Dependencies installed."

# 7. Create Ecosystem File for PM2
cat <<EOF > ecosystem.config.js
module.exports = {
  apps: [
    {
      name: "stockminded",
      script: "python3",
      args: "dashboard/server.py",
      interpreter: "none",
      env: {
        PYTHONPATH: ".",
        FLASK_ENV: "production",
        PORT: "5000"
      },
      restart_delay: 5000,
      max_memory_restart: "800M"
    }
  ]
}
EOF

# 8. Final Instructions
echo ""
echo "================================================================="
echo "🎉 SETUP COMPLETE!"
echo "================================================================="
echo "Next Steps:"
echo "1. Configure your API keys in $APP_DIR/.env"
echo "2. Open Port 5000 in your GCP Firewall settings."
echo "3. Start the app with PM2:"
echo "   cd $APP_DIR && pm2 start ecosystem.config.js"
echo ""
echo "4. To make PM2 start automatically on server reboot:"
echo "   pm2 save"
echo "   pm2 startup"
echo "   (Then run the command PM2 gives you)"
echo "================================================================="
