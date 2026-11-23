FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1
WORKDIR /sink
COPY sink.py ./
RUN useradd --uid 10001 sink && mkdir -p /data && chown sink /data
USER sink
EXPOSE 8080
CMD ["python", "sink.py"]
