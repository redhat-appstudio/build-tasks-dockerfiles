FROM registry.access.redhat.com/ubi9/ubi:9.4-1123.1719560047

ARG BSI_VERSION=0.2.0
ARG bsi_source=https://github.com/containers/BuildSourceImage/archive/refs/tags/v${BSI_VERSION}.tar.gz
ARG patch0=0001-cleanup-directory-with-all-rpms-which-isn-t-used-any.patch
ARG patch1=0001-Increase-counter-as-numeric-rather-than-string.patch
ARG patch2=0001-Use-extra-src-archive-checksum-in-filename.patch
ARG patch3=0001-Set-mediaType-on-image-manifest.patch

# hadolint ignore=DL3041
RUN dnf update -y && dnf install -y python3.11 git jq skopeo file tar && dnf clean all

WORKDIR /opt/BuildSourceImage
COPY $patch0 $patch1 $patch2 $patch3 ./
RUN curl -s -O -L $bsi_source && \
    tar --extract -f v${BSI_VERSION}.tar.gz -z --strip-components=1 BuildSourceImage-${BSI_VERSION}/BuildSourceImage.sh && \
    git apply --allow-empty BuildSourceImage.sh $patch0 $patch1 $patch2 $patch3 && \
    rm -r $patch0 $patch1 $patch2 $patch3 && \
    mv BuildSourceImage.sh bsi

WORKDIR /opt/source_build/
COPY app/source_build.py app/requirements.txt ./
RUN python3.11 -m venv appenv && \
    ./appenv/bin/python3 -m pip install --no-cache-dir -r ./requirements.txt
