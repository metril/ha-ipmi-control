ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache ipmitool
RUN pip3 install --no-cache-dir fastapi uvicorn[standard]

COPY app /app
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
