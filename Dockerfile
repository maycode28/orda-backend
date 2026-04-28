# syntax=docker/dockerfile:1

# --- Build stage ---
FROM gradle:8.10-jdk17 AS builder
WORKDIR /app

COPY build.gradle settings.gradle ./
COPY src ./src

RUN gradle bootJar -x test --no-daemon

# --- Runtime stage ---
FROM eclipse-temurin:17-jre

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app app
RUN mkdir -p /app/data/dem

COPY --from=builder --chown=app:app /app/build/libs/*.jar /app/app.jar
COPY --chown=app:app python/data/raw/dem/nasadem/korea_dem.tif /app/data/dem/korea_dem.tif
COPY --chown=app:app python/requirements-recommendation.txt /app/python/requirements-recommendation.txt
COPY --chown=app:app python/scripts/recommendation /app/python/scripts/recommendation

RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r /app/python/requirements-recommendation.txt

ENV JAVA_OPTS="-Xmx400m -Xms200m -XX:+UseSerialGC"
ENV SPRING_PROFILES_ACTIVE=prod
ENV DEM_FILE_PATH=/app/data/dem/korea_dem.tif
ENV RECOMMENDATION_PYTHON_EXECUTABLE=/app/venv/bin/python

USER app

EXPOSE 8080

ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/app.jar"]
