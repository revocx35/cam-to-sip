FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAM2SIP_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cam2sip ./cam2sip

RUN useradd --system --uid 1000 --home /app cam2sip \
    && mkdir -p /data && chown cam2sip:cam2sip /data
USER cam2sip
VOLUME /data

LABEL org.opencontainers.image.source="https://github.com/revocx35/cam-to-sip" \
      org.opencontainers.image.description="Bridge IP camera two-way audio to SIP phones" \
      org.opencontainers.image.licenses="MIT"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('CAM2SIP_WEB_PORT', '8090'), timeout=4)"

CMD ["python", "-m", "cam2sip"]
