ARG version=37.74.1
FROM registry.access.redhat.com/ubi9/ubi-minimal@sha256:bc552efb4966aaa44b02532be3168ac1ff18e2af299d0fe89502a1d9fabafbc5

LABEL name="renovate"
LABEL org.opencontainers.image.source="https://github.com/renovatebot/renovate" \
  org.opencontainers.image.url="https://renovatebot.com"

RUN microdnf -y module enable nodejs:20 && \
    microdnf -y install nodejs npm docker shadow-utils git python make g++ wget xz && \
    groupadd -r renovate -g 1001 && \
    useradd -u 1001 -r -g root -G renovate -m -d /home/renovate -s /sbin/nologin -c "Renovate user" renovate && \
    chmod 0770 /home/renovate

ENV PATH="$PATH:/home/renovate/node_modules/.bin"
WORKDIR /home/renovate
USER 1001
RUN ulimit unlimited && npm install renovate@$RENOVATE_VERSION re2

ENV RENOVATE_X_IGNORE_NODE_WARN=true

ENTRYPOINT ["renovate"]

RUN set -ex; \
  renovate --version; \
  renovate-config-validator; \
  node -e "new require('re2')('.*').exec('test')"; \
  true

LABEL org.opencontainers.image.version="${RENOVATE_VERSION}"
