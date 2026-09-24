web: sh -c 'gunicorn -b 0.0.0.0:${PORT:-8080} -w 2 --timeout 240 --graceful-timeout 30 server:app'
