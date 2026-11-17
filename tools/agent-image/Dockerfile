# Agent image for compaction-experiment runs (also the scoring container).
# Build context = the task skeleton only (no step messages, constraints, scoring items or hidden tests):
#   docker build -t cx-agent:2.1.326 --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#       -f tools/agent-image/Dockerfile tasks/seat-reservation/skeleton
# A throwaway stage builds the skeleton once so that the Gradle distribution and every dependency of the skeleton
# end up in GRADLE_USER_HOME; only that cache is copied into the final image (the skeleton itself is not).
# Works with the classic builder (no BuildKit features).
FROM eclipse-temurin:21-jdk-jammy AS jdk

FROM node:22-bookworm-slim AS base
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 ca-certificates curl bash procps util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.326 && claude --version

# Non-root user with the host uid/gid, so files written to the bind-mounted work folder stay readable on the host.
# The base image's `node` user (1000:1000) is renamed and renumbered.
ARG UID=1000
ARG GID=1000
RUN groupmod -g "${GID}" -n agent node \
    && usermod -u "${UID}" -g "${GID}" -l agent -d /home/agent -m node \
    && mkdir -p /home/agent/.claude /cx/hooks /cx/hookdata /work /opt/gradle-home \
    && chown -R agent:agent /home/agent /cx/hookdata /work /opt/gradle-home
LABEL cx.uid="${UID}" cx.gid="${GID}" cx.claude-code="2.1.326"

ENV HOME=/home/agent
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude
ENV GRADLE_USER_HOME=/opt/gradle-home
ENV DISABLE_AUTOUPDATER=1
USER agent
WORKDIR /work

# Warm-up stage: Gradle distribution + every resolvable configuration of the skeleton (compile, runtime, test,
# Kotlin compiler) without running tests (there is no database at build time).
FROM base AS warm
COPY --chown=agent:agent . /tmp/warm
RUN printf '%s\n' \
        'allprojects {' \
        '  tasks.register("cxResolveAll") {' \
        '    doLast { configurations.findAll { it.canBeResolved }.each { it.resolve() } }' \
        '  }' \
        '}' > /tmp/resolve-all.gradle \
    && cd /tmp/warm \
    && ./gradlew --no-daemon --console=plain -I /tmp/resolve-all.gradle build testClasses -x test cxResolveAll \
    && ./gradlew --stop

FROM base
COPY --from=warm --chown=agent:agent /opt/gradle-home /opt/gradle-home
