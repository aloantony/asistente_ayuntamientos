FROM postgres:17-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends --yes python3-minimal

COPY backend/app/reference_layers/disaster_recovery.py \
    /opt/siur/disaster_recovery.py

USER 999:999
ENTRYPOINT ["python3", "/opt/siur/disaster_recovery.py"]
