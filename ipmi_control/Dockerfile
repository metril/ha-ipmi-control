ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache ipmitool
RUN pip3 install --no-cache-dir fastapi uvicorn[standard] httpx

COPY app /app
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
