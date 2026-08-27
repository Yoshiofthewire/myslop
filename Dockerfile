FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 handoff && mkdir -p /data && chown handoff /data
USER handoff

ENV HANDOFF_DB=/data/handoff.db HANDOFF_BIND=0.0.0.0 HANDOFF_PORT=8080
VOLUME /data
EXPOSE 8080

ENTRYPOINT ["handoff"]
CMD ["serve", "--allow-any-interface"]
