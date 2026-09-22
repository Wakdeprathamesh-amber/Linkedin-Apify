# --workers 1 is load-bearing: jobs.py keeps the background job registry in
# process memory. Do not scale to multiple workers without moving that state.
web: gunicorn app:app --workers 1 --threads 8 --worker-class gthread --timeout 600 --bind 0.0.0.0:$PORT
