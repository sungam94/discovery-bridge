#!/bin/sh
# Xvfb display and a PulseAudio null sink (Phase 3 spike), then the adapter service.
# After a crash Docker restarts this same container, so files from the previous run are still there.
rm -f /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie   # stale after a crash
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99                                          # else Xvfb refuses to start
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
i=0
while ! xdpyinfo -display :99 >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 150 ]; then echo "display :99 did not come up in 30 s" >&2; exit 1; fi   # Docker restarts us
  sleep 0.2
done
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=nullsink >/dev/null 2>&1 || true
pactl set-default-sink nullsink 2>/dev/null || true
exec /app/.venv/bin/python -m adapter.main
