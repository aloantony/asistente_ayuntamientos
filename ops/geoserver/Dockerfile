FROM maven:3.9.11-eclipse-temurin-17 AS gwc-health-builder

WORKDIR /build
COPY ops/geoserver-gwc-health/pom.xml ./pom.xml
RUN mvn --batch-mode --no-transfer-progress dependency:go-offline
COPY ops/geoserver-gwc-health/src ./src
RUN mvn --batch-mode --no-transfer-progress verify

FROM docker.osgeo.org/geoserver:3.0.0

COPY --from=gwc-health-builder \
  /build/target/siur-gwc-quota-health-3.0.0.jar \
  /opt/additional_libs/siur-gwc-quota-health-3.0.0.jar
COPY ops/geoserver/server.xml /opt/config_overrides/server.xml
