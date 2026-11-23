# Serves the fictional Northwind sites, transcripts and media from ai-disclosure-scan's
# examples/ at the same commit the app pins (keep SCANNER_REF in step with pyproject.toml).
FROM alpine:3.20 AS src
ARG SCANNER_REF=9e72975ffb9d52eaca59e54690672ba0995335e2
RUN apk add --no-cache git \
 && git clone --quiet https://github.com/pyroplast/ai-disclosure-scan /src \
 && git -C /src checkout --quiet "$SCANNER_REF"

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=src /src/examples /usr/share/nginx/html
