FROM python:3.13-alpine

RUN addgroup -g 1000 media && adduser -D -u 1000 -G media xtream
WORKDIR /app
COPY --chown=xtream:media xtream_strm.py /app/xtream_strm.py
USER xtream
ENTRYPOINT ["python3", "/app/xtream_strm.py"]
