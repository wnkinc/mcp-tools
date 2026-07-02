# ubuntu/squid pinned by digest + Ubuntu's security updates. Canonical ships the
# image less often than the debs, so the pinned (latest) image can carry known-fixed
# CVEs -- the upgrade layer closes that gap at build time. Config/allowlist stay
# compose-mounted; this image is only the patched binary.
ARG BASE=ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029
FROM ${BASE}
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
