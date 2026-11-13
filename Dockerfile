FROM python:3.13.15-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
LABEL org.opencontainers.image.title="sg-card-advisor" \
      org.opencontainers.image.description="Singapore credit card catalogue, MCC rules, sign-up offers and wallet advisor" \
      org.opencontainers.image.source="https://github.com/quinneydavid/sg-card-advisor" \
      org.opencontainers.image.licenses="MIT"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# /keys (local key file) and /inbox (statement drop folder) belong to the app user, so named volumes mounted there do too.
RUN mkdir -p /keys /inbox && chown 1000:1000 /keys /inbox
COPY cards /app/cards
COPY LICENSE /app/LICENSE
WORKDIR /app
USER 1000:1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080 8081
ENTRYPOINT ["python3", "-m", "cards"]
CMD ["web"]
