ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache ipmitool
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app /app
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
