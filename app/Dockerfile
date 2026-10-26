# Dashboard: a static Vite build, served by the Python service below.
FROM node:26.12.1-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.13.17-alpine
WORKDIR /app
COPY fleetpulse_server ./fleetpulse_server
COPY --from=ui /ui/dist ./ui
RUN adduser -D -u 1000 pulse && mkdir /data && chown pulse /data
USER pulse
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
# Traefik only routes healthy containers: check every 2 s while starting so a redeploy is back within seconds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["python", "-m", "fleetpulse_server.main"]
