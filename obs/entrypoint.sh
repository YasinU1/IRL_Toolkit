#!/bin/bash
set -e

CONF="$HOME/.config/obs-studio"
mkdir -p "$CONF/basic/profiles/IRL" "$CONF/basic/scenes"

# Seed profile + service config on first run only (dashboard mutates these later
# via obs-websocket; never clobber them on restart).
if [ ! -f "$CONF/basic/profiles/IRL/basic.ini" ]; then
    cp /opt/obs-config-seed/profile-basic.ini "$CONF/basic/profiles/IRL/basic.ini"
    cp /opt/obs-config-seed/service.json      "$CONF/basic/profiles/IRL/service.json"
fi

# OBS 31 split its config: global.ini holds only the migration marker (without
# it, OBS shows a blocking "unable to migrate" dialog on startup); user prefs
# live in user.ini; obs-websocket reads plugin_config/obs-websocket/config.json.
# All rewritten every start so OBS_WS_PASSWORD from .env always wins.
printf '[General]\nPre31Migrated=true\n' > "$CONF/global.ini"
envsubst < /opt/obs-config-seed/global.ini.tmpl > "$CONF/user.ini"
mkdir -p "$CONF/plugin_config/obs-websocket"
cat > "$CONF/plugin_config/obs-websocket/config.json" <<EOF
{
    "alerts_enabled": false,
    "auth_required": true,
    "first_load": false,
    "server_enabled": true,
    "server_password": "${OBS_WS_PASSWORD}",
    "server_port": 4455
}
EOF

# Virtual display (no GPU on Ampere — llvmpipe software GL).
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &

# Dummy audio server so OBS's mixer has somewhere to live.
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit || true

# VNC on the virtual display, wrapped in noVNC for the browser.
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 &

# Give Xvfb a moment before OBS attaches to it.
sleep 2

exec obs --collection IRL --profile IRL \
     --disable-shutdown-check --disable-missing-files-check
