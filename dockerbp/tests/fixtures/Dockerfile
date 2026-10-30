FROM alpine:3.23
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) test "$(uname -m)" = x86_64 ;; \
      arm64) test "$(uname -m)" = aarch64 ;; \
      *) exit 1 ;; \
    esac
COPY payload.txt /payload.txt
