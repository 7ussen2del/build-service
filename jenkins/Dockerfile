FROM jenkins/jenkins:lts

# Plugins the Jenkinsfile needs: Pipeline, credentials() binding, cleanWs().
RUN jenkins-plugin-cli --plugins workflow-aggregator credentials-binding ws-cleanup

USER root

# Docker CLI only -- the daemon comes from the host socket mounted by compose.
ARG DOCKER_CLI_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && docker --version
