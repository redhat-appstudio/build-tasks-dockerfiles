FROM registry.access.redhat.com/ubi8:8.8-1032

ARG syft_version="1.5.0"

RUN dnf install -y https://github.com/anchore/syft/releases/download/v"${syft_version}"/syft_"${syft_version}"_linux_amd64.rpm && dnf clean all
