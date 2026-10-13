#!/usr/bin/with-contenv bash
# Chromium слушает отладочный порт только на 127.0.0.1 внутри контейнера.
# socat открывает его для сети проекта (контейнер smm), наружу порт не публикуется.
exec socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222
