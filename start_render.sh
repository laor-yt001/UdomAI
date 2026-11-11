#!/bin/bash

# Load .env file if it exists (useful for local testing)
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Setup wireproxy config if WG_PRIVATE_KEY is present
if [ -n "$WG_PRIVATE_KEY" ] && [ -n "$WG_PEER_PUBLIC_KEY" ] && [ -n "$WG_ENDPOINT" ]; then
    echo "WireGuard config found, setting up wireproxy..."
    cat > wireproxy.conf << EOF
[Interface]
Address = ${WG_ADDRESS:-172.16.0.2/32}
PrivateKey = $WG_PRIVATE_KEY
MTU = 1280
DNS = 1.1.1.1

[Peer]
PublicKey = $WG_PEER_PUBLIC_KEY
Endpoint = $WG_ENDPOINT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 15

[http]
BindAddress = 127.0.0.1:1080
EOF
    
    # Start wireproxy in background
    ./wireproxy -c wireproxy.conf &
    
    # Wait for wireproxy to start
    sleep 2
    export USE_VPN_PROXY=true
else
    echo "No WireGuard config found in environment variables. Skipping wireproxy."
fi

# Start the python bot
python main.py
