FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/runs && chown -R pwuser:pwuser /app

USER pwuser
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/readyz', timeout=3)"

# One meridian_service web process is intentional for this submission build
# because live run and intervention state is process-local. The production
# architecture moves that state to durable storage and executes browsers in
# a separate worker tier.
#
# mock_app runs alongside it on 127.0.0.1:5000 so the public demo is fully
# self-contained. Readiness checks include this process and remove the service
# from traffic if it is unavailable.
CMD ["sh", "-c", "gunicorn --workers=1 --threads=4 --worker-class=gthread --timeout=60 --bind=127.0.0.1:5000 mock_app.app:app & exec gunicorn --workers=1 --threads=8 --worker-class=gthread --timeout=180 --bind=0.0.0.0:${PORT:-10000} meridian_service.app:app"]
